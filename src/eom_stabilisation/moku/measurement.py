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
        allowed = {"name", "role"} | _duration_keys("start") | _duration_keys(
            "end"
        ) | _duration_keys("duration") | _duration_keys(
            "exclude_start"
        ) | _duration_keys("exclude_end")
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
    if not raw_only and not windows:
        raise ValueError(
            f"waveform {waveform.name!r} has no meaningful measurement roles; "
            "provide explicit measurement_windows or select raw_only"
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
            if phase_start < trigger_phase < phase_end:
                tolerance = waveform.point_interval_s * 1.01
                if trigger_phase - phase_start <= tolerance:
                    phase_start = trigger_phase
                elif phase_end - trigger_phase <= tolerance:
                    phase_end = trigger_phase
                else:
                    raise ValueError(
                        f"measurement window {window.name!r} crosses the "
                        "configured trigger phase; exclude the trigger edge or "
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
                    )
                )
        windows = tuple(before) + windows
    if alignment_required:
        if trigger_level_v is None or reference_edge_tolerance_s is None:
            raise ValueError(
                "reference-aligned reduction requires trigger level and tolerance"
            )
        if trigger_edge not in {"Rising", "Falling", "Both"}:
            raise ValueError("reference-aligned reduction requires a valid trigger edge")
    expected_reference_edges: list[tuple[float, str]] = []
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
            float(time_axis[index] + fraction * (time_axis[index + 1] - time_axis[index]))
        )
    return np.asarray(result, dtype=float)


def _reference_edge_time(
    time_axis: np.ndarray,
    reference: np.ndarray,
    plan: MeasurementPlan,
) -> float:
    assert plan.trigger_level_v is not None
    assert plan.trigger_edge is not None
    assert plan.reference_edge_tolerance_s is not None
    crossings = _crossing_times(
        time_axis, reference, plan.trigger_level_v, plan.trigger_edge
    )
    if crossings.size == 0:
        raise InvalidReferenceTraceError(
            "ChannelB has no crossing in the configured direction at the "
            "configured threshold",
            diagnostics=_diagnostics(reason="missing_configured_channel_b_crossing"),
        )
    edge_time = float(crossings[np.argmin(np.abs(crossings))])
    tolerance = plan.reference_edge_tolerance_s
    if abs(edge_time) > tolerance:
        raise InvalidReferenceTraceError(
            f"nearest ChannelB crossing is {edge_time:g} s from t=0, beyond "
            f"the configured {tolerance:g} s tolerance",
            diagnostics=_diagnostics(
                edge=edge_time, reason="channel_b_crossing_not_near_trigger"
            ),
        )
    # A unique trigger edge may recur once per period in a long manual frame.
    # Every observed recurrence must retain the compiled period and phase.
    for crossing in crossings:
        cycles = round((float(crossing) - edge_time) / plan.period_s)
        expected = edge_time + cycles * plan.period_s
        if abs(float(crossing) - expected) > tolerance:
            raise InvalidReferenceTraceError(
                "ChannelB crossing timing is inconsistent with the compiled LUT period",
                diagnostics=_diagnostics(
                    edge=edge_time, reason="channel_b_timing_inconsistent"
                ),
            )
    if plan.expected_reference_edges:
        observed: list[tuple[float, str]] = []
        for direction in ("Rising", "Falling"):
            observed.extend(
                (float(item), direction)
                for item in _crossing_times(
                    time_axis, reference, plan.trigger_level_v, direction
                )
            )
        spacing = float(np.median(np.diff(time_axis)))
        expected_in_frame: list[tuple[float, str]] = []
        for offset, direction in plan.expected_reference_edges:
            first_cycle = math.floor(
                (float(time_axis[0]) - edge_time - offset) / plan.period_s
            ) - 1
            last_cycle = math.ceil(
                (float(time_axis[-1]) - edge_time - offset) / plan.period_s
            ) + 1
            for cycle in range(first_cycle, last_cycle + 1):
                expected_time = edge_time + offset + cycle * plan.period_s
                if (
                    time_axis[0] + spacing
                    <= expected_time
                    <= time_axis[-1] - spacing
                ):
                    expected_in_frame.append((expected_time, direction))
        for expected_time, direction in expected_in_frame:
            if not any(
                observed_direction == direction
                and abs(observed_time - expected_time) <= tolerance
                for observed_time, observed_direction in observed
            ):
                raise InvalidReferenceTraceError(
                    "ChannelB transitions are inconsistent with the compiled LUT",
                    diagnostics=_diagnostics(
                        edge=edge_time, reason="channel_b_lut_shape_inconsistent"
                    ),
                )
        for observed_time, direction in observed:
            if not any(
                expected_direction == direction
                and abs(observed_time - expected_time) <= tolerance
                for expected_time, expected_direction in expected_in_frame
            ):
                raise InvalidReferenceTraceError(
                    "ChannelB contains an unexpected threshold transition",
                    diagnostics=_diagnostics(
                        edge=edge_time, reason="unexpected_channel_b_transition"
                    ),
                )
    return edge_time


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
    candidate_magnitudes = np.abs(differences[candidates])
    peak_position = int(np.argmax(candidate_magnitudes))
    peak = float(candidate_magnitudes[peak_position])
    onset_positions = np.flatnonzero(candidate_magnitudes >= peak * 0.5)
    peak_index = int(candidates[int(onset_positions[0])])
    noise = float(
        1.4826
        * np.median(
            np.abs(finite_differences - np.median(finite_differences))
        )
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


def measure_frame(
    frame: Mapping[str, Sequence[Any]],
    plan: MeasurementPlan,
) -> MeasurementResult:
    """Average all finite frame points selected by each named measurement role."""

    try:
        time_axis = np.asarray(frame["time"], dtype=float)
        voltage_source = (
            frame["photodiode_v"]
            if "photodiode_v" in frame
            else frame["ch1"]
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
        raise ValueError("frame time and ch1 must be equal-length one-dimensional arrays")
    if not np.all(np.isfinite(time_axis)):
        raise ValueError("frame contains non-finite time values")
    if not np.all(np.diff(time_axis) > 0):
        raise ValueError("frame time values must be strictly increasing")

    reference_edge = 0.0
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
        reference_edge = _reference_edge_time(time_axis, reference, plan)
        optical_delay, alignment_quality = _optical_delay(
            time_axis, voltage, reference_edge, plan
        )

    values_by_role: dict[str, list[np.ndarray]] = {}
    selected_counts: dict[str, int] = {}
    finite_counts: dict[str, int] = {}
    rejected_counts: dict[str, int] = {}
    for window in plan.windows:
        aligned_start = window.start_s + reference_edge + optical_delay
        aligned_end = window.end_s + reference_edge + optical_delay
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
        if selected_count == 0:
            raise OpticalAlignmentError(
                f"aligned frame contains no points for role {role!r}",
                diagnostics=diagnostic,
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
