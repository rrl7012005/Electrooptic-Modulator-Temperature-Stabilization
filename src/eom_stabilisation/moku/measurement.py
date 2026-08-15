"""Compile waveform-aware measurement windows and reduce Oscilloscope frames."""

from __future__ import annotations

import math
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .models import (
    CompiledWaveform,
    MeasurementAlignmentDiagnostics,
    MeasurementPlan,
    MeasurementResult,
    MeasurementWindow,
    OscilloscopeTimebase,
)
from .waveform_compiler import duration_seconds


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class FrameMeasurementError(ValueError):
    """Base error for a scientifically rejected, structurally received frame."""

    failure_kind = "frame_measurement_failure"

    def __init__(
        self,
        message: str,
        *,
        diagnostics: MeasurementAlignmentDiagnostics | None = None,
    ) -> None:
        self.diagnostics = diagnostics
        super().__init__(message)


class InvalidReferenceTraceError(FrameMeasurementError):
    """ChannelB is absent, malformed, or inconsistent with the compiled trigger."""

    failure_kind = "invalid_reference_trace"


class OpticalAlignmentError(FrameMeasurementError):
    """ChannelA optical timing could not be established confidently."""

    failure_kind = "optical_alignment_failure"


class UnusableVoltageSamplesError(FrameMeasurementError):
    """Selected role regions contain too few finite ChannelA samples."""

    failure_kind = "unusable_voltage_samples"


class FrameGeometryError(ValueError):
    """The returned time axis cannot satisfy the compiled measurement plan."""


def _finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be numeric") from error
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _duration_keys(stem: str) -> set[str]:
    return {f"{stem}_{suffix}" for suffix in ("s", "ms", "us", "ns")}


def _window_name(value: Any, label: str) -> str:
    if value is None:
        raise ValueError(f"{label} is required")
    name = str(value).strip()
    if not name:
        raise ValueError(f"{label} must not be empty")
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError(
            f"{label} must contain only letters, numbers, underscores, and hyphens"
        )
    return name


def _explicit_windows(
    raw_windows: Iterable[Mapping[str, Any]],
    *,
    period_s: float,
) -> tuple[MeasurementWindow, ...]:
    windows: list[MeasurementWindow] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_windows, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"measurement window {index} must be a mapping")
        allowed = (
            {"name", "role"}
            | _duration_keys("start")
            | _duration_keys("end")
            | _duration_keys("duration")
            | _duration_keys("exclude_start")
            | _duration_keys("exclude_end")
        )
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"measurement window {index} has unknown fields: {sorted(unknown)}"
            )
        name = _window_name(raw.get("name", f"window_{index}"), "window name")
        if name in names:
            raise ValueError(f"duplicate measurement window name {name!r}")
        names.add(name)
        role = _window_name(raw.get("role"), f"measurement window {name} role")
        start = float(duration_seconds(raw, "start", allow_zero=True))
        has_end = bool(set(raw).intersection(_duration_keys("end")))
        has_duration = bool(set(raw).intersection(_duration_keys("duration")))
        if has_end == has_duration:
            raise ValueError(
                f"measurement window {name} requires exactly one of end or duration"
            )
        end = (
            float(duration_seconds(raw, "end"))
            if has_end
            else start + float(duration_seconds(raw, "duration"))
        )
        exclude_start = float(
            duration_seconds(
                raw,
                "exclude_start",
                required=False,
                default=0.0,
                allow_zero=True,
            )
        )
        exclude_end = float(
            duration_seconds(
                raw,
                "exclude_end",
                required=False,
                default=0.0,
                allow_zero=True,
            )
        )
        start += exclude_start
        end -= exclude_end
        if start < 0 or end > period_s:
            raise ValueError(
                f"measurement window {name} must remain within 0 to {period_s:g} s"
            )
        if end <= start:
            raise ValueError(f"measurement window {name} is empty after edge exclusion")
        windows.append(MeasurementWindow(name, role, start, end))

    ordered = sorted(windows, key=lambda window: (window.start_s, window.end_s))
    for previous, current in zip(ordered, ordered[1:]):
        if current.start_s < previous.end_s:
            raise ValueError(
                f"measurement windows {previous.name!r} and {current.name!r} overlap"
            )
    return tuple(windows)


def _segment_windows(waveform: CompiledWaveform) -> tuple[MeasurementWindow, ...]:
    windows: list[MeasurementWindow] = []
    for segment in waveform.segments:
        if segment.measurement_role is None:
            continue
        edge = segment.achieved_edge_time_s or 0.0
        start = segment.achieved_start_s
        end = segment.achieved_end_s
        if segment.kind in {"pulse", "level"}:
            start += edge
        if segment.kind == "pulse":
            end -= edge
        if end <= start:
            raise ValueError(
                f"segment {segment.name!r} has no measurement plateau after "
                "excluding achieved edge intervals"
            )
        windows.append(
            MeasurementWindow(
                name=segment.name,
                role=segment.measurement_role,
                start_s=start,
                end_s=end,
                source_segment=segment.name,
            )
        )
    return tuple(windows)


def compile_measurement_plan(
    waveform: CompiledWaveform,
    explicit_windows: Sequence[Mapping[str, Any]] | None = None,
    *,
    raw_only: bool = False,
    trigger_phase_s: float | None = 0.0,
    alignment_required: bool = False,
    trigger_level_v: float | None = None,
    trigger_edge: str | None = None,
    reference_edge_tolerance_s: float | None = None,
    maximum_optical_delay_s: float = 0.0,
    minimum_valid_points_per_role: int = 1,
    minimum_optical_edge_snr: float = 3.0,
    include_square_low_before: bool = False,
    trigger_candidates: Sequence[tuple[float, str]] | None = None,
    optical_delay_mode: str = "per_frame",
    fixed_optical_delay_s: float | None = None,
    optical_settling_guard_s: float = 0.0,
) -> MeasurementPlan:
    """Tie explicit or segment-derived windows to achieved waveform timing.

    Edge exclusions use achieved, not merely requested, edge times.  A waveform
    without named roles must provide explicit windows or opt into ``raw_only``;
    the function never invents minimum/high-level semantics.
    """

    configured_windows = waveform.requested_parameters.get("measurement_windows")
    if explicit_windows is not None and configured_windows is not None:
        raise ValueError("measurement windows were defined twice")
    if explicit_windows is None and configured_windows is not None:
        if not isinstance(configured_windows, Sequence) or isinstance(
            configured_windows, (str, bytes)
        ):
            raise ValueError("measurement_windows must be a list")
        explicit_windows = configured_windows
    if raw_only and explicit_windows:
        raise ValueError("raw_only cannot be combined with measurement windows")

    if raw_only:
        windows: tuple[MeasurementWindow, ...] = ()
    elif explicit_windows is not None:
        windows = _explicit_windows(
            explicit_windows,
            period_s=waveform.achieved_period_s,
        )
    else:
        windows = _segment_windows(waveform)
    if not math.isfinite(optical_settling_guard_s) or optical_settling_guard_s < 0:
        raise ValueError("optical_settling_guard_s must be finite and non-negative")
    guarded_windows: list[MeasurementWindow] = []
    for window in windows:
        guarded_start = window.start_s + optical_settling_guard_s
        if guarded_start >= window.end_s:
            raise ValueError(
                f"measurement window {window.name!r} has no plateau after "
                "applying optical_settling_guard_s"
            )
        guarded_windows.append(
            MeasurementWindow(
                name=window.name,
                role=window.role,
                start_s=guarded_start,
                end_s=window.end_s,
                source_segment=window.source_segment,
                phase_start_s=guarded_start,
                phase_end_s=window.end_s,
            )
        )
    windows = tuple(guarded_windows)
    if not raw_only and not windows:
        raise ValueError(
            f"waveform {waveform.name!r} has no meaningful measurement roles; "
            "provide explicit measurement_windows or select raw_only"
        )
    normalized_candidates = tuple(trigger_candidates or ())
    if not normalized_candidates and trigger_phase_s is not None:
        normalized_candidates = (
            (float(trigger_phase_s), str(trigger_edge or "Rising")),
        )
    if trigger_phase_s is not None:
        trigger_phase = _finite(trigger_phase_s, "trigger_phase_s")
        if not 0 <= trigger_phase < waveform.achieved_period_s:
            raise ValueError(
                "trigger_phase_s must lie within the achieved waveform period"
            )
        shifted: list[MeasurementWindow] = []
        for window in windows:
            phase_start = window.start_s
            phase_end = window.end_s
            for candidate_phase, _direction in normalized_candidates:
                if phase_start < candidate_phase < phase_end:
                    tolerance = waveform.point_interval_s * 1.01
                    if candidate_phase - phase_start <= tolerance:
                        phase_start = candidate_phase
                    elif phase_end - candidate_phase <= tolerance:
                        phase_end = candidate_phase
                    else:
                        raise ValueError(
                            f"measurement window {window.name!r} crosses a "
                            "possible trigger phase; exclude the trigger edge or "
                            "split the window"
                        )
            start = phase_start - trigger_phase
            end = phase_end - trigger_phase
            if start < 0 < end:
                raise ValueError(
                    f"measurement window {window.name!r} crosses the configured "
                    "trigger phase; exclude the trigger edge or split the window"
                )
            shifted.append(
                MeasurementWindow(
                    name=window.name,
                    role=window.role,
                    start_s=start,
                    end_s=end,
                    source_segment=window.source_segment,
                    phase_start_s=phase_start,
                    phase_end_s=phase_end,
                )
            )
        windows = tuple(shifted)
    elif windows:
        raise ValueError(
            "A reduced measurement plan requires a deterministic trigger phase"
        )
    if include_square_low_before and waveform.waveform_type.value == "square":
        before: list[MeasurementWindow] = []
        for window in windows:
            if window.source_segment == "low":
                before.append(
                    MeasurementWindow(
                        name=f"{window.name}_before",
                        role=window.role,
                        start_s=window.start_s - waveform.achieved_period_s,
                        end_s=window.end_s - waveform.achieved_period_s,
                        source_segment=window.source_segment,
                        phase_start_s=window.phase_start_s,
                        phase_end_s=window.phase_end_s,
                    )
                )
        windows = tuple(before) + windows
    if alignment_required:
        if trigger_level_v is None or reference_edge_tolerance_s is None:
            raise ValueError(
                "reference-aligned reduction requires trigger level and tolerance"
            )
        if trigger_edge not in {"Rising", "Falling", "Both"}:
            raise ValueError(
                "reference-aligned reduction requires a valid trigger edge"
            )
    expected_reference_edges: list[tuple[float, str]] = []
    reference_edges_by_phase: list[tuple[float, str]] = []
    if alignment_required and trigger_phase_s is not None:
        values = waveform.connector_voltage_v
        following = np.roll(values, -1)
        assert trigger_level_v is not None
        indices = np.flatnonzero(
            ((values < trigger_level_v) & (following >= trigger_level_v))
            | ((values > trigger_level_v) & (following <= trigger_level_v))
        )
        for index_value in indices:
            index = int(index_value)
            fraction = (trigger_level_v - float(values[index])) / (
                float(following[index]) - float(values[index])
            )
            phase = (
                (index + fraction) * waveform.point_interval_s
            ) % waveform.achieved_period_s
            offset = (phase - trigger_phase_s) % waveform.achieved_period_s
            if math.isclose(
                offset,
                waveform.achieved_period_s,
                abs_tol=waveform.point_interval_s * 1.01,
            ):
                offset = 0.0
            direction = (
                "Rising"
                if float(following[index]) > float(values[index])
                else "Falling"
            )
            expected_reference_edges.append((float(offset), direction))
            reference_edges_by_phase.append((float(phase), direction))
    if optical_delay_mode not in {"per_frame", "fixed"}:
        raise ValueError("optical_delay_mode must be per_frame or fixed")
    if optical_delay_mode == "fixed":
        if fixed_optical_delay_s is None or not math.isfinite(fixed_optical_delay_s):
            raise ValueError("fixed optical delay mode requires a finite delay")
        if not 0 <= fixed_optical_delay_s <= maximum_optical_delay_s:
            raise ValueError(
                "fixed optical delay must be non-negative and no larger than "
                "maximum_optical_delay_s"
            )
    elif fixed_optical_delay_s is not None:
        raise ValueError("fixed_optical_delay_s requires fixed optical delay mode")
    return MeasurementPlan(
        waveform_name=waveform.name,
        waveform_timing_sha256=waveform.timing_sha256,
        period_s=waveform.achieved_period_s,
        windows=windows,
        trigger_phase_s=trigger_phase_s,
        raw_only=raw_only,
        alignment_required=alignment_required,
        trigger_level_v=trigger_level_v,
        trigger_edge=trigger_edge,
        reference_edge_tolerance_s=reference_edge_tolerance_s,
        maximum_optical_delay_s=float(maximum_optical_delay_s),
        minimum_valid_points_per_role=int(minimum_valid_points_per_role),
        minimum_optical_edge_snr=float(minimum_optical_edge_snr),
        expected_reference_edges=tuple(expected_reference_edges),
        trigger_candidates=normalized_candidates,
        reference_edges_by_phase=tuple(reference_edges_by_phase),
        optical_delay_mode=optical_delay_mode,
        fixed_optical_delay_s=fixed_optical_delay_s,
        optical_settling_guard_s=float(optical_settling_guard_s),
    )


def _diagnostics(
    *,
    edge: float | None = None,
    delay: float | None = None,
    quality: float | None = None,
    selected: Mapping[str, int] | None = None,
    finite: Mapping[str, int] | None = None,
    rejected: Mapping[str, int] | None = None,
    accepted: bool = False,
    reason: str | None = None,
) -> MeasurementAlignmentDiagnostics:
    return MeasurementAlignmentDiagnostics(
        channel_b_edge_time_s=edge,
        optical_delay_s=delay,
        alignment_quality=quality,
        selected_points_by_role=MappingProxyType(dict(selected or {})),
        finite_points_by_role=MappingProxyType(dict(finite or {})),
        rejected_points_by_role=MappingProxyType(dict(rejected or {})),
        accepted=accepted,
        rejection_reason=reason,
    )


def _crossing_times(
    time_axis: np.ndarray,
    values: np.ndarray,
    level: float,
    edge: str,
) -> np.ndarray:
    if edge == "Rising":
        indices = np.flatnonzero((values[:-1] < level) & (values[1:] >= level))
    elif edge == "Falling":
        indices = np.flatnonzero((values[:-1] > level) & (values[1:] <= level))
    else:
        indices = np.flatnonzero(
            ((values[:-1] < level) & (values[1:] >= level))
            | ((values[:-1] > level) & (values[1:] <= level))
        )
    result: list[float] = []
    for index in indices:
        start = float(values[index])
        finish = float(values[index + 1])
        if finish == start:
            continue
        fraction = (level - start) / (finish - start)
        result.append(
            float(
                time_axis[index] + fraction * (time_axis[index + 1] - time_axis[index])
            )
        )
    return np.asarray(result, dtype=float)


def _reference_edge_time(
    time_axis: np.ndarray,
    reference: np.ndarray,
    plan: MeasurementPlan,
) -> tuple[float, float]:
    assert plan.trigger_level_v is not None
    assert plan.trigger_edge is not None
    assert plan.reference_edge_tolerance_s is not None
    level = plan.trigger_level_v
    equal_indices = np.flatnonzero(reference == level)
    if equal_indices.size > 1 and np.any(np.diff(equal_indices) == 1):
        raise InvalidReferenceTraceError(
            "ChannelB contains a sampled plateau exactly at the trigger level",
            diagnostics=_diagnostics(reason="channel_b_plateau_at_trigger_level"),
        )

    observed: list[tuple[float, str]] = []
    for direction in ("Rising", "Falling"):
        observed.extend(
            (float(item), direction)
            for item in _crossing_times(time_axis, reference, level, direction)
        )
    eligible_directions = (
        {"Rising", "Falling"} if plan.trigger_edge == "Both" else {plan.trigger_edge}
    )
    near_trigger = [
        item
        for item in observed
        if item[1] in eligible_directions
        and abs(item[0]) <= plan.reference_edge_tolerance_s
    ]
    if not near_trigger:
        raise InvalidReferenceTraceError(
            "ChannelB has no configured threshold crossing near t=0",
            diagnostics=_diagnostics(reason="channel_b_crossing_not_near_trigger"),
        )
    tolerance = plan.reference_edge_tolerance_s
    spacing = float(np.median(np.diff(time_axis)))
    interior_start = float(time_axis[0] + spacing)
    interior_end = float(time_axis[-1] - spacing)
    observed_interior = [
        item for item in observed if interior_start <= item[0] <= interior_end
    ]
    reference_edges = plan.reference_edges_by_phase
    if not reference_edges and plan.trigger_phase_s is not None:
        reference_edges = tuple(
            (
                (plan.trigger_phase_s + offset) % plan.period_s,
                direction,
            )
            for offset, direction in plan.expected_reference_edges
        )
    trigger_candidates = plan.trigger_candidates
    if not trigger_candidates and plan.trigger_phase_s is not None:
        trigger_candidates = ((plan.trigger_phase_s, plan.trigger_edge),)

    matches: list[tuple[float, float]] = []
    for candidate_phase, candidate_direction in trigger_candidates:
        candidate_anchors = [
            time_value
            for time_value, direction in near_trigger
            if direction == candidate_direction
        ]
        for edge_time in candidate_anchors:
            expected_interior: list[tuple[float, str]] = []
            for phase, direction in reference_edges:
                offset = (phase - candidate_phase) % plan.period_s
                if math.isclose(
                    offset,
                    plan.period_s,
                    abs_tol=tolerance,
                ):
                    offset = 0.0
                first_cycle = (
                    math.floor((interior_start - edge_time - offset) / plan.period_s)
                    - 1
                )
                last_cycle = (
                    math.ceil((interior_end - edge_time - offset) / plan.period_s) + 1
                )
                for cycle in range(first_cycle, last_cycle + 1):
                    expected_time = edge_time + offset + cycle * plan.period_s
                    if interior_start <= expected_time <= interior_end:
                        expected_interior.append((expected_time, direction))
            expected_matches = all(
                any(
                    observed_direction == direction
                    and abs(observed_time - expected_time) <= tolerance
                    for observed_time, observed_direction in observed_interior
                )
                for expected_time, direction in expected_interior
            )
            observed_matches = all(
                any(
                    expected_direction == direction
                    and abs(observed_time - expected_time) <= tolerance
                    for expected_time, expected_direction in expected_interior
                )
                for observed_time, direction in observed_interior
            )
            if expected_matches and observed_matches:
                matches.append((float(edge_time), float(candidate_phase)))

    unique_matches: list[tuple[float, float]] = []
    for match in matches:
        if not any(
            abs(match[0] - known[0]) <= tolerance
            and abs(match[1] - known[1]) <= tolerance
            for known in unique_matches
        ):
            unique_matches.append(match)
    if len(unique_matches) != 1:
        reason = (
            "channel_b_trigger_phase_ambiguous"
            if len(unique_matches) > 1
            else "channel_b_lut_shape_inconsistent"
        )
        raise InvalidReferenceTraceError(
            "ChannelB does not identify exactly one compiled trigger edge and LUT phase",
            diagnostics=_diagnostics(reason=reason),
        )
    return unique_matches[0]


def _optical_delay(
    time_axis: np.ndarray,
    voltage: np.ndarray,
    edge_time: float,
    plan: MeasurementPlan,
) -> tuple[float, float]:
    midpoints = (time_axis[:-1] + time_axis[1:]) / 2.0
    spacing = float(np.median(np.diff(time_axis)))
    search_end = edge_time + max(plan.maximum_optical_delay_s, spacing * 0.51)
    candidates = np.flatnonzero(
        (midpoints >= edge_time - spacing * 0.51)
        & (midpoints <= search_end + spacing * 0.51)
        & np.isfinite(voltage[:-1])
        & np.isfinite(voltage[1:])
    )
    if candidates.size == 0:
        raise OpticalAlignmentError(
            "ChannelA has no finite adjacent samples in the optical-delay search range",
            diagnostics=_diagnostics(
                edge=edge_time, reason="no_finite_optical_alignment_samples"
            ),
        )
    differences = np.diff(voltage)
    finite_differences = differences[np.isfinite(differences)]
    # Score a sustained level change, not one adjacent-sample impulse. This
    # suppresses isolated spikes and makes the selected timing less sensitive
    # to the first ringing lobe. Fixed-delay mode bypasses this estimator.
    support = 3
    sustained_scores: list[float] = []
    for index_value in candidates:
        index = int(index_value)
        before = voltage[max(0, index - support + 1) : index + 1]
        after = voltage[index + 1 : min(len(voltage), index + 1 + support)]
        if (
            len(before) < 2
            or len(after) < 2
            or not np.all(np.isfinite(before))
            or not np.all(np.isfinite(after))
        ):
            sustained_scores.append(0.0)
        else:
            sustained_scores.append(
                abs(float(np.median(after)) - float(np.median(before)))
            )
    score_array = np.asarray(sustained_scores, dtype=float)
    best_score = float(np.max(score_array))
    sustained_candidates = np.flatnonzero(score_array >= best_score * (1.0 - 1e-12))
    peak_position = int(
        sustained_candidates[
            int(np.argmax(np.abs(differences[candidates[sustained_candidates]])))
        ]
    )
    peak = float(score_array[peak_position])
    peak_index = int(candidates[peak_position])
    noise = float(
        1.4826 * np.median(np.abs(finite_differences - np.median(finite_differences)))
    )
    scale_floor = max(float(np.nanmax(np.abs(voltage))) * 1e-12, 1e-15)
    quality = peak / max(noise, scale_floor)
    if peak <= scale_floor or quality < plan.minimum_optical_edge_snr:
        raise OpticalAlignmentError(
            "ChannelA optical response edge was not detected confidently",
            diagnostics=_diagnostics(
                edge=edge_time,
                quality=quality,
                reason="optical_edge_confidence_below_threshold",
            ),
        )
    delay = max(0.0, float(midpoints[peak_index] - edge_time))
    if delay > plan.maximum_optical_delay_s + spacing * 0.51:
        raise OpticalAlignmentError(
            "ChannelA optical response exceeds maximum_optical_delay_s",
            diagnostics=_diagnostics(
                edge=edge_time,
                delay=delay,
                quality=quality,
                reason="optical_delay_above_configured_maximum",
            ),
        )
    return delay, quality


def _validate_frame_geometry(
    time_axis: np.ndarray,
    plan: MeasurementPlan,
    timebase: OscilloscopeTimebase | None,
) -> None:
    """Fail once, rather than retry forever, when the SDK frame cannot fit the plan."""

    differences = np.diff(time_axis)
    spacing = float(np.median(differences))
    if not np.allclose(
        differences,
        spacing,
        rtol=1e-3,
        atol=max(1e-15, abs(spacing) * 1e-6),
    ):
        raise FrameGeometryError("Oscilloscope frame time spacing is not uniform")
    if timebase is not None:
        if len(time_axis) > timebase.max_length:
            raise FrameGeometryError(
                "Oscilloscope returned more points than the configured maximum"
            )
        boundary_tolerance = spacing * 2.0
        if (
            time_axis[0] > timebase.start_s + boundary_tolerance
            or time_axis[-1] < timebase.end_s - boundary_tolerance
        ):
            raise FrameGeometryError(
                "Oscilloscope returned a shorter time interval than the compiled "
                "action-specific timebase"
            )
    if plan.alignment_required and not plan.raw_only:
        before = int(np.count_nonzero(time_axis < 0.0))
        after = int(np.count_nonzero(time_axis > 0.0))
        if before < 2 or after < 2:
            raise FrameGeometryError(
                "Reduced frame must contain at least two samples before and after t=0"
            )


def _aligned_window_bounds(
    window: MeasurementWindow,
    plan: MeasurementPlan,
    *,
    reference_edge_s: float,
    trigger_phase_s: float,
    optical_delay_s: float,
    frame_start_s: float,
    frame_end_s: float,
) -> tuple[float, float]:
    """Map one phase window to the uniquely identified trigger edge in this frame."""

    if len(plan.trigger_candidates) <= 1:
        return (
            window.start_s + reference_edge_s + optical_delay_s,
            window.end_s + reference_edge_s + optical_delay_s,
        )
    assert window.phase_start_s is not None and window.phase_end_s is not None
    width = window.phase_end_s - window.phase_start_s
    base = (window.phase_start_s - trigger_phase_s) % plan.period_s
    candidates = [
        (
            base + cycle * plan.period_s + reference_edge_s + optical_delay_s,
            base + cycle * plan.period_s + width + reference_edge_s + optical_delay_s,
        )
        for cycle in range(-2, 3)
    ]
    contained = [
        item
        for item in candidates
        if item[0] >= frame_start_s and item[1] <= frame_end_s
    ]
    if contained:
        return min(contained, key=lambda item: abs((item[0] + item[1]) / 2.0))
    return max(
        candidates,
        key=lambda item: max(
            0.0,
            min(item[1], frame_end_s) - max(item[0], frame_start_s),
        ),
    )


def measure_frame(
    frame: Mapping[str, Sequence[Any]],
    plan: MeasurementPlan,
    timebase: OscilloscopeTimebase | None = None,
) -> MeasurementResult:
    """Average all finite frame points selected by each named measurement role."""

    try:
        time_axis = np.asarray(frame["time"], dtype=float)
        voltage_source = (
            frame["photodiode_v"] if "photodiode_v" in frame else frame["ch1"]
        )
        voltage = np.asarray(voltage_source, dtype=float)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "frame must contain numeric time and photodiode_v/ch1 arrays"
        ) from error
    if (
        time_axis.ndim != 1
        or voltage.ndim != 1
        or len(time_axis) != len(voltage)
        or len(time_axis) < 2
    ):
        raise ValueError(
            "frame time and ch1 must be equal-length one-dimensional arrays"
        )
    if not np.all(np.isfinite(time_axis)):
        raise ValueError("frame contains non-finite time values")
    if not np.all(np.diff(time_axis) > 0):
        raise ValueError("frame time values must be strictly increasing")
    _validate_frame_geometry(time_axis, plan, timebase)

    reference_edge = 0.0
    trigger_phase = plan.trigger_phase_s or 0.0
    optical_delay = 0.0
    alignment_quality: float | None = None
    if plan.alignment_required and not plan.raw_only:
        try:
            reference_source = (
                frame["waveform_reference_v"]
                if "waveform_reference_v" in frame
                else frame["ch2"]
            )
            reference = np.asarray(reference_source, dtype=float)
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidReferenceTraceError(
                "reduced frame requires numeric waveform_reference_v/ChannelB",
                diagnostics=_diagnostics(reason="missing_or_non_numeric_channel_b"),
            ) from error
        if reference.ndim != 1 or reference.shape != time_axis.shape:
            raise InvalidReferenceTraceError(
                "ChannelB must be a one-dimensional array matching frame time",
                diagnostics=_diagnostics(reason="malformed_channel_b_shape"),
            )
        if not np.all(np.isfinite(reference)):
            raise InvalidReferenceTraceError(
                "ChannelB contains non-finite values",
                diagnostics=_diagnostics(reason="non_finite_channel_b"),
            )
        reference_edge, trigger_phase = _reference_edge_time(time_axis, reference, plan)
        if plan.optical_delay_mode == "fixed":
            assert plan.fixed_optical_delay_s is not None
            optical_delay = plan.fixed_optical_delay_s
        else:
            optical_delay, alignment_quality = _optical_delay(
                time_axis, voltage, reference_edge, plan
            )

    values_by_role: dict[str, list[np.ndarray]] = {}
    selected_counts: dict[str, int] = {}
    finite_counts: dict[str, int] = {}
    rejected_counts: dict[str, int] = {}
    for window in plan.windows:
        aligned_start, aligned_end = _aligned_window_bounds(
            window,
            plan,
            reference_edge_s=reference_edge,
            trigger_phase_s=trigger_phase,
            optical_delay_s=optical_delay,
            frame_start_s=float(time_axis[0]),
            frame_end_s=float(time_axis[-1]),
        )
        mask = (time_axis >= aligned_start) & (time_axis < aligned_end)
        selected = voltage[mask]
        if selected.size == 0:
            continue
        finite_selected = selected[np.isfinite(selected)]
        selected_counts[window.role] = selected_counts.get(window.role, 0) + int(
            selected.size
        )
        finite_counts[window.role] = finite_counts.get(window.role, 0) + int(
            finite_selected.size
        )
        rejected_counts[window.role] = rejected_counts.get(window.role, 0) + int(
            selected.size - finite_selected.size
        )
        if finite_selected.size:
            values_by_role.setdefault(window.role, []).append(finite_selected)

    means: dict[str, float] = {}
    counts: dict[str, int] = {}
    planned_roles = {window.role for window in plan.windows}
    for role in planned_roles:
        selected_count = selected_counts.get(role, 0)
        finite_count = finite_counts.get(role, 0)
        diagnostic = _diagnostics(
            edge=reference_edge,
            delay=optical_delay,
            quality=alignment_quality,
            selected=selected_counts,
            finite=finite_counts,
            rejected=rejected_counts,
            reason=(
                "aligned_role_outside_frame"
                if selected_count == 0
                else "too_few_finite_role_samples"
            ),
        )
        if selected_count < plan.minimum_valid_points_per_role:
            if selected_count == 0:
                raise FrameGeometryError(
                    f"aligned frame contains no points for role {role!r}"
                )
            raise FrameGeometryError(
                f"actual frame geometry provides only {selected_count} selected "
                f"points for role {role!r}; the compiled minimum is "
                f"{plan.minimum_valid_points_per_role}"
            )
        if finite_count < plan.minimum_valid_points_per_role:
            raise UnusableVoltageSamplesError(
                f"role {role!r} has {finite_count} finite selected points; "
                f"at least {plan.minimum_valid_points_per_role} are required",
                diagnostics=diagnostic,
            )
        arrays = values_by_role[role]
        combined = np.concatenate(arrays)
        means[role] = float(np.mean(combined))
        counts[role] = int(combined.size)
    return MeasurementResult(
        waveform_name=plan.waveform_name,
        waveform_timing_sha256=plan.waveform_timing_sha256,
        measurement_plan_sha256=plan.measurement_plan_sha256,
        values_by_role=MappingProxyType(means),
        point_counts_by_role=MappingProxyType(counts),
        alignment=_diagnostics(
            edge=reference_edge,
            delay=optical_delay,
            quality=alignment_quality,
            selected=selected_counts,
            finite=finite_counts,
            rejected=rejected_counts,
            accepted=True,
        ),
    )
