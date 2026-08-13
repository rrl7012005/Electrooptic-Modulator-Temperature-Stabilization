"""Configuration-driven, SDK-free Moku:Go waveform compiler.

All voltages in this module are requested volts at the Moku connector.  The
compiler reports digital LUT timing only; analogue bandwidth, cabling, and the
EOM load can change the physical waveform.
"""

from __future__ import annotations

from dataclasses import dataclass
import csv
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .models import (
    CountRecoveryMode,
    CompiledRun,
    CompiledSegment,
    CompiledWaveform,
    CompiledWaveformAction,
    CompiledWaveformProgram,
    DurationEndPolicy,
    RunMode,
    WaveformType,
)
from .waveform_registry import get_registered_waveform


MOKU_GO_OUTPUT_MIN_V = -5.0
MOKU_GO_OUTPUT_MAX_V = 5.0
MOKU_GO_AWG_MIN_AMPLITUDE_VPP = 4e-3
MOKU_GO_AWG_MIN_FREQUENCY_HZ = 1e-3
MOKU_GO_AWG_MAX_FREQUENCY_HZ = 10e6
MOKU_GO_MAX_BURST_CYCLES = 1_000_000


@dataclass(frozen=True)
class AwgMemoryMode:
    """One currently documented Moku:Go AWG sample-rate/memory mode."""

    api_name: str
    sample_rate_hz: float
    max_points: int


# Liquid Instruments API reference, checked 2026-08-11.  Do not add the old
# 15.625Ms label: it is not an accepted current Moku:Go API sample-rate name.
MOKU_GO_AWG_MEMORY_MODES = (
    AwgMemoryMode("125Ms", 125e6, 16_384),
    AwgMemoryMode("62.5Ms", 62.5e6, 32_768),
    AwgMemoryMode("31.25Ms", 31.25e6, 65_536),
)

_DURATION_UNITS = {
    "s": 1.0,
    "ms": 1e-3,
    "us": 1e-6,
    "ns": 1e-9,
}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class _RequestedSegment:
    kind: str
    name: str
    level_v: float
    duration_s: float
    edge_time_s: float
    measurement_role: str | None


def finite_number(value: Any, label: str) -> float:
    """Return a finite float with a field-specific validation error."""

    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be numeric") from error
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _whole_number(value: Any, label: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number")
    try:
        integer = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a whole number") from error
    if integer != value or integer < minimum:
        raise ValueError(f"{label} must be a whole number at least {minimum}")
    return integer


def _safe_name(value: Any, label: str) -> str:
    name = str(value).strip()
    if not name or not _SAFE_NAME.fullmatch(name):
        raise ValueError(
            f"{label} must start with a letter or number and contain only "
            "letters, numbers, underscores, and hyphens"
        )
    return name


def _duration_keys(stem: str) -> set[str]:
    return {f"{stem}_{suffix}" for suffix in _DURATION_UNITS}


def duration_seconds(
    values: Mapping[str, Any],
    stem: str,
    *,
    required: bool = True,
    default: float | None = None,
    allow_zero: bool = False,
) -> float | None:
    """Read exactly one unit-bearing duration field and normalize to seconds."""

    present = [key for key in _duration_keys(stem) if key in values]
    if len(present) > 1:
        raise ValueError(
            f"{stem} has conflicting duration representations: {sorted(present)}"
        )
    if not present:
        if required:
            expected = ", ".join(sorted(_duration_keys(stem)))
            raise ValueError(f"exactly one {stem} field is required ({expected})")
        return default
    key = present[0]
    suffix = key.rsplit("_", 1)[1]
    result = finite_number(values[key], key) * _DURATION_UNITS[suffix]
    if result < 0 or (result == 0 and not allow_zero):
        comparison = "non-negative" if allow_zero else "above zero"
        raise ValueError(f"{key} must be {comparison}")
    return result


def _connector_voltage(value: Any, label: str) -> float:
    voltage = finite_number(value, label)
    if not MOKU_GO_OUTPUT_MIN_V <= voltage <= MOKU_GO_OUTPUT_MAX_V:
        raise ValueError(
            f"{label} must be within {MOKU_GO_OUTPUT_MIN_V:g} to "
            f"{MOKU_GO_OUTPUT_MAX_V:g} requested connector volts"
        )
    return voltage


def _role(value: Any, label: str) -> str | None:
    if value is None:
        return None
    role = str(value).strip()
    if not role:
        raise ValueError(f"{label} must not be empty")
    if not _SAFE_NAME.fullmatch(role):
        raise ValueError(f"{label} contains characters unsafe for output fields")
    return role


def _reject_unknown(values: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"{label} has unknown fields: {sorted(unknown)}")


def _select_point_count(period_s: float) -> tuple[AwgMemoryMode, int]:
    frequency_hz = 1.0 / period_s
    candidates: list[tuple[int, float, AwgMemoryMode]] = []
    for mode in MOKU_GO_AWG_MEMORY_MODES:
        throughput = mode.sample_rate_hz * period_s
        nearest = round(throughput)
        # Decimal SI aliases and reciprocal frequency forms should compile to
        # identical LUTs.  Remove only binary floating-point noise near an
        # integer; genuinely non-integral throughput is still rounded down.
        if math.isclose(throughput, nearest, rel_tol=1e-12, abs_tol=1e-12):
            throughput_points = int(nearest)
        else:
            throughput_points = math.floor(throughput)
        point_count = min(mode.max_points, throughput_points)
        if point_count >= 2:
            candidates.append((point_count, mode.sample_rate_hz, mode))
    if not candidates:
        raise ValueError("waveform period is too short for a two-point Moku:Go LUT")
    point_count, _, mode = max(candidates)
    return mode, point_count


def select_awg_memory_mode(period_s: float) -> tuple[AwgMemoryMode, int]:
    """Return the finest current Moku:Go memory mode and safe LUT length."""

    period = _validate_period(period_s, "waveform period")
    return _select_point_count(period)


def _select_mode_for_fixed_points(
    period_s: float,
    point_count: int,
) -> AwgMemoryMode:
    frequency_hz = 1.0 / period_s
    for mode in MOKU_GO_AWG_MEMORY_MODES:
        if (
            point_count <= mode.max_points
            and point_count * frequency_hz <= mode.sample_rate_hz
        ):
            return mode
    raise ValueError(
        f"{point_count:,} LUT points at {frequency_hz:g} Hz exceed the "
        "documented Moku:Go memory/sample-rate combinations"
    )


def _round_half_up_nonnegative(value: float) -> int:
    """Round a non-negative sample count deterministically at half points."""

    tolerance = 1e-12 * max(1.0, abs(value))
    return math.floor(value + 0.5 + tolerance)


def quantize_lut_point_count(duration_s: float, point_interval_s: float) -> int:
    """Quantize a non-negative duration with the shared half-up policy."""

    duration = finite_number(duration_s, "duration_s")
    interval = finite_number(point_interval_s, "point_interval_s")
    if duration < 0:
        raise ValueError("duration_s must be non-negative")
    if interval <= 0:
        raise ValueError("point_interval_s must be above zero")
    return _round_half_up_nonnegative(duration / interval)


def _validate_period(period_s: float, label: str) -> float:
    period = finite_number(period_s, label)
    if period <= 0:
        raise ValueError(f"{label} must be above zero")
    frequency = 1.0 / period
    if not MOKU_GO_AWG_MIN_FREQUENCY_HZ <= frequency <= MOKU_GO_AWG_MAX_FREQUENCY_HZ:
        raise ValueError(
            f"waveform frequency {frequency:g} Hz is outside the documented "
            "Moku:Go AWG range 1e-3 to 10e6 Hz"
        )
    return period


def _normalise_connector_lut(
    connector_voltage_v: np.ndarray,
) -> tuple[np.ndarray, float, float]:
    voltage = np.asarray(connector_voltage_v, dtype=float)
    if voltage.ndim != 1 or len(voltage) < 2 or not np.all(np.isfinite(voltage)):
        raise ValueError("waveform function/LUT must return at least two finite points")
    minimum = float(np.min(voltage))
    maximum = float(np.max(voltage))
    _connector_voltage(minimum, "minimum waveform voltage")
    _connector_voltage(maximum, "maximum waveform voltage")
    amplitude = maximum - minimum
    if amplitude < MOKU_GO_AWG_MIN_AMPLITUDE_VPP:
        raise ValueError(
            "waveform connector-voltage span must be at least 0.004 Vpp for "
            "the Moku:Go Arbitrary Waveform Generator"
        )
    offset = (maximum + minimum) / 2.0
    normalized = (voltage - offset) * (2.0 / amplitude)
    normalized = np.clip(normalized, -1.0, 1.0)
    return normalized, amplitude, offset


def _compile_segments(
    name: str,
    waveform_type: WaveformType,
    segments: Sequence[_RequestedSegment],
    *,
    low_level_v: float,
    requested_parameters: Mapping[str, Any],
) -> CompiledWaveform:
    if not segments:
        raise ValueError(f"{name}: waveform requires at least one segment")
    period = _validate_period(
        sum(segment.duration_s for segment in segments),
        f"{name} period",
    )
    mode, point_count = _select_point_count(period)
    point_interval = period / point_count

    cumulative_requested = np.cumsum([segment.duration_s for segment in segments])
    boundaries = np.rint(cumulative_requested / period * point_count).astype(int)
    boundaries[-1] = point_count
    starts = np.concatenate(([0], boundaries[:-1]))
    counts = boundaries - starts
    if np.any(counts <= 0):
        index = int(np.flatnonzero(counts <= 0)[0])
        raise ValueError(
            f"{name}: segment {index + 1} ({segments[index].name}) is shorter "
            f"than the achieved LUT interval {point_interval:.12g} s"
        )

    voltage = np.full(point_count, low_level_v, dtype=float)
    compiled_segments: list[CompiledSegment] = []
    requested_start = 0.0
    previous_terminal_v = low_level_v

    for raw, start_value, count_value in zip(segments, starts, counts):
        start = int(start_value)
        count = int(count_value)
        end = start + count
        edge_count = 0
        achieved_edge: float | None = None
        if raw.edge_time_s > 0:
            edge_count = quantize_lut_point_count(
                raw.edge_time_s,
                point_interval,
            )
            if edge_count < 1:
                raise ValueError(
                    f"{name}: {raw.name} edge_time is shorter than one "
                    f"achievable LUT interval ({point_interval:.12g} s)"
                )
            achieved_edge = edge_count * point_interval

        if raw.kind == "gap":
            voltage[start:end] = low_level_v
            previous_terminal_v = low_level_v
        elif raw.kind == "pulse":
            if 2 * edge_count >= count:
                raise ValueError(
                    f"{name}: {raw.name} does not have a high-level LUT point "
                    "after quantizing both edges"
                )
            if edge_count:
                rise = np.linspace(low_level_v, raw.level_v, edge_count + 1)[:-1]
                plateau = np.full(count - 2 * edge_count, raw.level_v)
                fall = np.linspace(raw.level_v, low_level_v, edge_count + 1)[1:]
                voltage[start:end] = np.concatenate((rise, plateau, fall))
            else:
                voltage[start:end] = raw.level_v
            previous_terminal_v = low_level_v
        elif raw.kind == "level":
            if edge_count >= count:
                raise ValueError(
                    f"{name}: {raw.name} has no dwell point after edge quantization"
                )
            if edge_count:
                voltage[start : start + edge_count] = np.linspace(
                    previous_terminal_v,
                    raw.level_v,
                    edge_count + 1,
                )[1:]
            voltage[start + edge_count : end] = raw.level_v
            previous_terminal_v = raw.level_v
        else:  # pragma: no cover - protected by parsers
            raise AssertionError(f"unsupported internal segment kind {raw.kind}")

        requested_end = requested_start + raw.duration_s
        compiled_segments.append(
            CompiledSegment(
                kind=raw.kind,
                name=raw.name,
                level_v=raw.level_v,
                requested_start_s=requested_start,
                requested_end_s=requested_end,
                achieved_start_s=start * point_interval,
                achieved_end_s=end * point_interval,
                start_index=start,
                end_index=end,
                requested_edge_time_s=(
                    raw.edge_time_s if raw.edge_time_s > 0 else None
                ),
                achieved_edge_time_s=achieved_edge,
                edge_point_count=edge_count,
                measurement_role=raw.measurement_role,
            )
        )
        requested_start = requested_end

    normalized, amplitude, offset = _normalise_connector_lut(voltage)
    warnings: list[str] = []
    if segments[-1].kind != "gap":
        warnings.append(
            "No trailing gap was supplied; the next repetition begins "
            "immediately after the final segment."
        )
    return CompiledWaveform(
        name=name,
        waveform_type=waveform_type,
        requested_period_s=period,
        achieved_period_s=period,
        requested_frequency_hz=1.0 / period,
        achieved_frequency_hz=1.0 / period,
        sample_rate_name=mode.api_name,
        sample_rate_hz=mode.sample_rate_hz,
        point_interval_s=point_interval,
        connector_voltage_v=voltage,
        normalized_lut=normalized,
        amplitude_vpp=amplitude,
        offset_v=offset,
        segments=tuple(compiled_segments),
        requested_parameters=requested_parameters,
        warnings=tuple(warnings),
    )


def _measurement_roles(values: Mapping[str, Any]) -> tuple[str | None, str | None]:
    raw = values.get("measurement_roles", {})
    if not isinstance(raw, Mapping):
        raise ValueError("measurement_roles must be a mapping")
    _reject_unknown(raw, {"low", "high"}, "measurement_roles")
    return (
        _role(raw.get("low", "minimum"), "measurement_roles.low"),
        _role(raw.get("high", "high_level"), "measurement_roles.high"),
    )


def _compile_square(name: str, values: Mapping[str, Any]) -> CompiledWaveform:
    allowed = {
        "type", "name", "low_level_v", "high_level_v", "frequency_hz",
        "duty_cycle_percent", "measurement_roles", "measurement_windows",
    } | _duration_keys("period") | _duration_keys("pulse_duration") | _duration_keys(
        "gap_duration"
    ) | _duration_keys("edge_time")
    _reject_unknown(values, allowed, name)
    low = _connector_voltage(values.get("low_level_v"), f"{name}.low_level_v")
    high = _connector_voltage(values.get("high_level_v"), f"{name}.high_level_v")
    if high <= low:
        raise ValueError(f"{name}: high_level_v must be above low_level_v")

    has_frequency = "frequency_hz" in values or "duty_cycle_percent" in values
    has_period = bool(set(values).intersection(_duration_keys("period")))
    has_gap = bool(set(values).intersection(_duration_keys("gap_duration")))
    form_count = sum((has_frequency, has_period, has_gap))
    if form_count != 1:
        raise ValueError(
            f"{name}: choose exactly one square timing form: frequency+duty, "
            "period+pulse duration, or pulse duration+gap duration"
        )

    if has_frequency:
        if "frequency_hz" not in values or "duty_cycle_percent" not in values:
            raise ValueError(f"{name}: frequency_hz and duty_cycle_percent are both required")
        if set(values).intersection(_duration_keys("pulse_duration")):
            raise ValueError(f"{name}: pulse_duration conflicts with frequency+duty")
        frequency = finite_number(values["frequency_hz"], f"{name}.frequency_hz")
        if frequency <= 0:
            raise ValueError(f"{name}.frequency_hz must be above zero")
        period = 1.0 / frequency
        duty = finite_number(
            values["duty_cycle_percent"], f"{name}.duty_cycle_percent"
        )
        if not 0.0 < duty < 100.0:
            raise ValueError(f"{name}.duty_cycle_percent must be above 0 and below 100")
        pulse_duration = period * duty / 100.0
        gap_duration = period - pulse_duration
    elif has_period:
        if "frequency_hz" in values or "duty_cycle_percent" in values or has_gap:
            raise ValueError(f"{name}: conflicting square timing fields")
        period = float(duration_seconds(values, "period"))
        pulse_duration = float(duration_seconds(values, "pulse_duration"))
        if pulse_duration >= period:
            raise ValueError(f"{name}: pulse duration must be shorter than period")
        gap_duration = period - pulse_duration
    else:
        if "frequency_hz" in values or "duty_cycle_percent" in values or has_period:
            raise ValueError(f"{name}: conflicting square timing fields")
        pulse_duration = float(duration_seconds(values, "pulse_duration"))
        gap_duration = float(duration_seconds(values, "gap_duration"))
        period = pulse_duration + gap_duration

    _validate_period(period, f"{name}.period")
    edge = float(
        duration_seconds(
            values,
            "edge_time",
            required=False,
            default=0.0,
            allow_zero=True,
        )
    )
    if 2.0 * edge >= pulse_duration:
        raise ValueError(f"{name}: pulse duration must exceed twice edge_time")
    low_role, high_role = _measurement_roles(values)
    segments = (
        _RequestedSegment("pulse", "high", high, pulse_duration, edge, high_role),
        _RequestedSegment("gap", "low", low, gap_duration, 0.0, low_role),
    )
    return _compile_segments(
        name,
        WaveformType.SQUARE,
        segments,
        low_level_v=low,
        requested_parameters=dict(values),
    )


def _parse_segment_list(
    name: str,
    values: Mapping[str, Any],
) -> tuple[float, tuple[_RequestedSegment, ...]]:
    allowed = {
        "type", "name", "low_level_v", "segments", "measurement_windows"
    }
    _reject_unknown(values, allowed, name)
    low = _connector_voltage(values.get("low_level_v"), f"{name}.low_level_v")
    raw_segments = values.get("segments")
    if not isinstance(raw_segments, Sequence) or isinstance(raw_segments, (str, bytes)):
        raise ValueError(f"{name}.segments must be a list")
    if not raw_segments:
        raise ValueError(f"{name}.segments must not be empty")
    parsed: list[_RequestedSegment] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_segments, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"{name}.segments[{index}] must be a mapping")
        allowed_segment = {
            "type", "name", "level_v", "measurement_role"
        } | _duration_keys("duration") | _duration_keys("edge_time")
        _reject_unknown(raw, allowed_segment, f"{name}.segments[{index}]")
        kind = str(raw.get("type", "")).strip().lower()
        if kind not in {"pulse", "gap", "level"}:
            raise ValueError(
                f"{name}.segments[{index}].type must be pulse, gap, or level"
            )
        segment_name = _safe_name(raw.get("name", f"segment_{index}"), "segment name")
        if segment_name in names:
            raise ValueError(f"{name}: duplicate segment name {segment_name!r}")
        names.add(segment_name)
        duration = float(duration_seconds(raw, "duration"))
        role = _role(raw.get("measurement_role"), "measurement_role")
        if kind == "gap":
            if "level_v" in raw or set(raw).intersection(_duration_keys("edge_time")):
                raise ValueError(f"{name}: gap {segment_name} cannot set level_v or edge_time")
            level = low
            edge = 0.0
        else:
            if "level_v" not in raw:
                raise ValueError(f"{name}: {kind} {segment_name} requires level_v")
            level = _connector_voltage(raw["level_v"], f"{name}.{segment_name}.level_v")
            edge = float(
                duration_seconds(
                    raw,
                    "edge_time",
                    required=False,
                    default=0.0,
                    allow_zero=True,
                )
            )
            if kind == "pulse" and 2.0 * edge >= duration:
                raise ValueError(
                    f"{name}: pulse {segment_name} duration must exceed twice edge_time"
                )
            if kind == "level" and edge >= duration:
                raise ValueError(
                    f"{name}: level {segment_name} duration must exceed edge_time"
                )
        parsed.append(_RequestedSegment(kind, segment_name, level, duration, edge, role))
    return low, tuple(parsed)


def _compile_explicit_segments(name: str, values: Mapping[str, Any]) -> CompiledWaveform:
    low, segments = _parse_segment_list(name, values)
    return _compile_segments(
        name,
        WaveformType.SEGMENTS,
        segments,
        low_level_v=low,
        requested_parameters=dict(values),
    )


def _compile_pulse_train(name: str, values: Mapping[str, Any]) -> CompiledWaveform:
    allowed = {
        "type", "name", "pulse_count", "low_level_v", "high_level_v",
        "measurement_roles", "measurement_windows",
    } | _duration_keys("pulse_duration") | _duration_keys(
        "inter_pulse_gap"
    ) | _duration_keys("final_gap") | _duration_keys("edge_time")
    _reject_unknown(values, allowed, name)
    count = _whole_number(values.get("pulse_count"), f"{name}.pulse_count")
    low = _connector_voltage(values.get("low_level_v"), f"{name}.low_level_v")
    high = _connector_voltage(values.get("high_level_v"), f"{name}.high_level_v")
    if high <= low:
        raise ValueError(f"{name}: high_level_v must be above low_level_v")
    pulse_duration = float(duration_seconds(values, "pulse_duration"))
    inter_gap = float(duration_seconds(values, "inter_pulse_gap", allow_zero=True))
    final_gap = float(
        duration_seconds(
            values,
            "final_gap",
            required=False,
            default=0.0,
            allow_zero=True,
        )
    )
    edge = float(
        duration_seconds(
            values,
            "edge_time",
            required=False,
            default=0.0,
            allow_zero=True,
        )
    )
    if 2.0 * edge >= pulse_duration:
        raise ValueError(f"{name}: pulse duration must exceed twice edge_time")
    low_role, high_role = _measurement_roles(values)
    segments: list[_RequestedSegment] = []
    for index in range(1, count + 1):
        segments.append(
            _RequestedSegment(
                "pulse", f"pulse_{index}", high, pulse_duration, edge, high_role
            )
        )
        if index < count and inter_gap > 0:
            segments.append(
                _RequestedSegment(
                    "gap", f"inter_pulse_gap_{index}", low, inter_gap, 0.0, low_role
                )
            )
    if final_gap > 0:
        segments.append(
            _RequestedSegment("gap", "final_gap", low, final_gap, 0.0, low_role)
        )
    return _compile_segments(
        name,
        WaveformType.PULSE_TRAIN,
        segments,
        low_level_v=low,
        requested_parameters=dict(values),
    )


def _generated_levels(values: Mapping[str, Any], name: str) -> list[float]:
    has_list = "levels_v" in values
    generated_keys = {"start_v", "finish_v", "step_v"}
    has_generated = bool(set(values).intersection(generated_keys))
    if has_list == has_generated:
        raise ValueError(
            f"{name}: provide either levels_v or start_v/finish_v/step_v, not both"
        )
    if has_list:
        raw_levels = values["levels_v"]
        if not isinstance(raw_levels, Sequence) or isinstance(raw_levels, (str, bytes)):
            raise ValueError(f"{name}.levels_v must be a list")
        if len(raw_levels) < 2:
            raise ValueError(f"{name}.levels_v must contain at least two levels")
        return [
            _connector_voltage(value, f"{name}.levels_v[{index}]")
            for index, value in enumerate(raw_levels)
        ]

    missing = generated_keys - set(values)
    if missing:
        raise ValueError(f"{name}: missing staircase fields {sorted(missing)}")
    start = _connector_voltage(values["start_v"], f"{name}.start_v")
    finish = _connector_voltage(values["finish_v"], f"{name}.finish_v")
    step = finite_number(values["step_v"], f"{name}.step_v")
    if step == 0:
        raise ValueError(f"{name}.step_v must not be zero")
    if (finish - start) * step <= 0:
        raise ValueError(f"{name}.step_v sign must move from start_v toward finish_v")
    step_count_float = (finish - start) / step
    step_count = round(step_count_float)
    if step_count < 1 or not math.isclose(
        step_count_float, step_count, rel_tol=1e-10, abs_tol=1e-10
    ):
        raise ValueError(f"{name}: finish_v must be reached by a whole number of steps")
    return [start + index * step for index in range(step_count + 1)]


def _compile_staircase(name: str, values: Mapping[str, Any]) -> CompiledWaveform:
    allowed = {
        "type", "name", "levels_v", "start_v", "finish_v", "step_v",
        "direction", "repeat_endpoints", "low_level_v", "measurement_windows",
    } | _duration_keys("dwell") | _duration_keys("final_gap") | _duration_keys(
        "edge_time"
    )
    _reject_unknown(values, allowed, name)
    levels = _generated_levels(values, name)
    direction = str(values.get("direction", "up")).strip().lower()
    if direction not in {"up", "down", "up_and_down"}:
        raise ValueError(f"{name}.direction must be up, down, or up_and_down")
    if direction == "down":
        levels = list(reversed(levels))
    elif direction == "up_and_down":
        repeat_endpoints = values.get("repeat_endpoints", False)
        if not isinstance(repeat_endpoints, bool):
            raise ValueError(f"{name}.repeat_endpoints must be true or false")
        reverse = list(reversed(levels)) if repeat_endpoints else list(reversed(levels[1:-1]))
        levels = levels + reverse
    elif "repeat_endpoints" in values and not isinstance(values["repeat_endpoints"], bool):
        raise ValueError(f"{name}.repeat_endpoints must be true or false")

    dwell = float(duration_seconds(values, "dwell"))
    edge = float(
        duration_seconds(
            values,
            "edge_time",
            required=False,
            default=0.0,
            allow_zero=True,
        )
    )
    if edge >= dwell:
        raise ValueError(f"{name}: dwell must exceed edge_time")
    low = _connector_voltage(values.get("low_level_v", 0.0), f"{name}.low_level_v")
    segments = [
        _RequestedSegment(
            "level",
            f"level_{index}",
            level,
            dwell,
            edge,
            f"level_{index}",
        )
        for index, level in enumerate(levels)
    ]
    final_gap = float(
        duration_seconds(
            values,
            "final_gap",
            required=False,
            default=0.0,
            allow_zero=True,
        )
    )
    if final_gap > 0:
        segments.append(
            _RequestedSegment("gap", "final_gap", low, final_gap, 0.0, None)
        )
    return _compile_segments(
        name,
        WaveformType.STAIRCASE,
        segments,
        low_level_v=low,
        requested_parameters=dict(values),
    )


def _period_from_frequency_or_duration(name: str, values: Mapping[str, Any]) -> float:
    has_frequency = "frequency_hz" in values
    has_period = bool(set(values).intersection(_duration_keys("period")))
    if has_frequency == has_period:
        raise ValueError(f"{name}: provide exactly one of frequency_hz or period duration")
    if has_frequency:
        frequency = finite_number(values["frequency_hz"], f"{name}.frequency_hz")
        if frequency <= 0:
            raise ValueError(f"{name}.frequency_hz must be above zero")
        return _validate_period(1.0 / frequency, f"{name}.period")
    return _validate_period(float(duration_seconds(values, "period")), f"{name}.period")


def _compile_custom_python(name: str, values: Mapping[str, Any]) -> CompiledWaveform:
    allowed = {
        "type", "name", "function", "parameters", "frequency_hz", "point_count",
        "measurement_windows",
    } | _duration_keys("period")
    _reject_unknown(values, allowed, name)
    function_name = str(values.get("function", "")).strip()
    if not function_name:
        raise ValueError(f"{name}.function is required")
    registration = get_registered_waveform(function_name)
    parameters = registration.parameter_validator(values.get("parameters", {}))
    period = _period_from_frequency_or_duration(name, values)
    if "point_count" in values:
        point_count = _whole_number(values["point_count"], f"{name}.point_count", minimum=2)
        mode = _select_mode_for_fixed_points(period, point_count)
    else:
        mode, point_count = _select_point_count(period)
    phase = np.arange(point_count, dtype=float) / point_count
    try:
        output = registration.function(phase.copy(), parameters)
        voltage = np.asarray(output, dtype=float)
    except Exception as error:
        raise ValueError(
            f"registered waveform {function_name!r} failed during dry compilation: {error}"
        ) from error
    if voltage.shape != phase.shape:
        raise ValueError(
            f"registered waveform {function_name!r} must return shape {phase.shape}, "
            f"not {voltage.shape}"
        )
    normalized, amplitude, offset = _normalise_connector_lut(voltage)
    point_interval = period / point_count
    return CompiledWaveform(
        name=name,
        waveform_type=WaveformType.CUSTOM_PYTHON,
        requested_period_s=period,
        achieved_period_s=period,
        requested_frequency_hz=1.0 / period,
        achieved_frequency_hz=1.0 / period,
        sample_rate_name=mode.api_name,
        sample_rate_hz=mode.sample_rate_hz,
        point_interval_s=point_interval,
        connector_voltage_v=voltage,
        normalized_lut=normalized,
        amplitude_vpp=amplitude,
        offset_v=offset,
        requested_parameters=dict(values),
    )


def _compile_csv_lut(
    name: str,
    values: Mapping[str, Any],
    *,
    base_path: Path | None,
) -> CompiledWaveform:
    allowed = {"type", "name", "path", "measurement_windows"}
    _reject_unknown(values, allowed, name)
    raw_path = values.get("path")
    if not isinstance(raw_path, (str, Path)) or not str(raw_path).strip():
        raise ValueError(f"{name}.path is required")
    source_path = Path(raw_path).expanduser()
    if not source_path.is_absolute():
        source_path = (base_path or Path.cwd()) / source_path
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"CSV LUT does not exist: {source_path}")

    times: list[float] = []
    voltages: list[float] = []
    with source_path.open(newline="", encoding="utf-8-sig") as source_file:
        reader = csv.DictReader(source_file)
        missing = {"time_s", "voltage_v"} - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{name}: CSV LUT is missing columns {sorted(missing)}")
        for row_index, row in enumerate(reader, start=2):
            times.append(finite_number(row.get("time_s"), f"{source_path}:{row_index} time_s"))
            voltages.append(
                _connector_voltage(
                    row.get("voltage_v"),
                    f"{source_path}:{row_index} voltage_v",
                )
            )
    if len(times) < 2:
        raise ValueError(f"{name}: CSV LUT must contain at least two data rows")
    time_array = np.asarray(times, dtype=float)
    spacing = np.diff(time_array)
    if not np.all(spacing > 0):
        raise ValueError(f"{name}: CSV time_s must be strictly increasing")
    interval = float(np.median(spacing))
    tolerance = max(1e-15, abs(interval) * 1e-9)
    if not np.allclose(spacing, interval, rtol=1e-7, atol=tolerance):
        raise ValueError(
            f"{name}: CSV LUT time_s spacing must be uniform for deterministic AWG timing"
        )
    period = _validate_period(interval * len(times), f"{name} inferred period")
    mode = _select_mode_for_fixed_points(period, len(times))
    voltage = np.asarray(voltages, dtype=float)
    normalized, amplitude, offset = _normalise_connector_lut(voltage)
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    return CompiledWaveform(
        name=name,
        waveform_type=WaveformType.CSV_LUT,
        requested_period_s=period,
        achieved_period_s=period,
        requested_frequency_hz=1.0 / period,
        achieved_frequency_hz=1.0 / period,
        sample_rate_name=mode.api_name,
        sample_rate_hz=mode.sample_rate_hz,
        point_interval_s=interval,
        connector_voltage_v=voltage,
        normalized_lut=normalized,
        amplitude_vpp=amplitude,
        offset_v=offset,
        requested_parameters=dict(values),
        source_path=source_path,
        source_sha256=source_sha256,
    )


def compile_waveform(
    name: str,
    definition: Mapping[str, Any],
    *,
    base_path: str | Path | None = None,
) -> CompiledWaveform:
    """Compile one strict waveform definition without importing the Moku SDK."""

    safe_name = _safe_name(name, "waveform name")
    if not isinstance(definition, Mapping):
        raise ValueError(f"waveform {safe_name!r} must be a mapping")
    if "name" in definition and str(definition["name"]).strip() != safe_name:
        raise ValueError(f"waveform {safe_name!r} contains a conflicting name field")
    type_text = str(definition.get("type", "")).strip().lower()
    try:
        waveform_type = WaveformType(type_text)
    except ValueError as error:
        supported = ", ".join(item.value for item in WaveformType)
        raise ValueError(f"{safe_name}: unsupported waveform type {type_text!r}; use {supported}") from error
    base = None if base_path is None else Path(base_path).expanduser().resolve()
    compilers = {
        WaveformType.SQUARE: _compile_square,
        WaveformType.SEGMENTS: _compile_explicit_segments,
        WaveformType.PULSE_TRAIN: _compile_pulse_train,
        WaveformType.STAIRCASE: _compile_staircase,
        WaveformType.CUSTOM_PYTHON: _compile_custom_python,
    }
    if waveform_type is WaveformType.CSV_LUT:
        return _compile_csv_lut(safe_name, definition, base_path=base)
    return compilers[waveform_type](safe_name, definition)


def compile_waveforms(
    definitions: Mapping[str, Mapping[str, Any]],
    *,
    base_path: str | Path | None = None,
) -> dict[str, CompiledWaveform]:
    """Compile a named waveform mapping, rejecting an empty program."""

    if not isinstance(definitions, Mapping) or not definitions:
        raise ValueError("waveforms must be a non-empty mapping")
    return {
        str(name): compile_waveform(str(name), definition, base_path=base_path)
        for name, definition in definitions.items()
    }


def parse_run_spec(definition: Mapping[str, Any], period_s: float) -> CompiledRun:
    """Validate one waveform action run policy and resolve exact cycle counts."""

    if not isinstance(definition, Mapping):
        raise ValueError("run must be a mapping")
    raw_mode = str(definition.get("mode", "")).strip().lower()
    if raw_mode == "fill_experiment":
        raw_mode = RunMode.UNTIL_EXPERIMENT_END.value
    try:
        mode = RunMode(raw_mode)
    except ValueError as error:
        raise ValueError(f"unknown waveform run mode {raw_mode!r}") from error
    period = _validate_period(period_s, "run waveform period")
    base_allowed = {"mode"}

    if mode is RunMode.COUNT:
        _reject_unknown(definition, base_allowed | {"count", "recovery"}, "run")
        repeat_count = _whole_number(definition.get("count"), "run.count")
        recovery_raw = definition.get("recovery", {"mode": "strict"})
        if not isinstance(recovery_raw, Mapping):
            raise ValueError("run.recovery must be a mapping")
        _reject_unknown(
            recovery_raw,
            {"mode", "maximum_uncertain_fraction"},
            "run.recovery",
        )
        try:
            recovery_mode = CountRecoveryMode(
                str(recovery_raw.get("mode", "strict")).strip().lower()
            )
        except ValueError as error:
            raise ValueError(
                "run.recovery.mode must be strict or bounded_uncertainty"
            ) from error
        fraction: float | None = None
        maximum_ambiguous_cycles = 0
        initial_chunk_size: int | None = None
        if recovery_mode is CountRecoveryMode.STRICT:
            if "maximum_uncertain_fraction" in recovery_raw:
                raise ValueError(
                    "maximum_uncertain_fraction is only valid for "
                    "bounded_uncertainty count recovery"
                )
            if repeat_count > MOKU_GO_MAX_BURST_CYCLES:
                raise ValueError(
                    "strict run.count exceeds the Moku:Go NCycle limit of 1,000,000"
                )
        else:
            fraction = finite_number(
                recovery_raw.get("maximum_uncertain_fraction"),
                "run.recovery.maximum_uncertain_fraction",
            )
            if not 0.0 < fraction < 1.0:
                raise ValueError(
                    "maximum_uncertain_fraction must be above zero and below one"
                )
            maximum_ambiguous_cycles = math.floor(repeat_count * fraction)
            initial_chunk_size = min(
                MOKU_GO_MAX_BURST_CYCLES,
                math.floor(repeat_count * fraction / 2.0),
            )
            if initial_chunk_size < 1:
                raise ValueError(
                    "count/tolerance is too small for a one-cycle bounded "
                    "ambiguity; increase count or maximum_uncertain_fraction"
                )
        return CompiledRun(
            mode=mode,
            repeat_count=repeat_count,
            requested_duration_s=None,
            achieved_duration_s=repeat_count * period,
            end_policy=None,
            exact_hardware_burst=(recovery_mode is CountRecoveryMode.STRICT),
            count_recovery_mode=recovery_mode,
            maximum_uncertain_fraction=fraction,
            maximum_ambiguous_cycles=maximum_ambiguous_cycles,
            initial_chunk_size=initial_chunk_size,
        )

    if mode is RunMode.DURATION:
        allowed = base_allowed | {"end_policy"} | _duration_keys("duration")
        _reject_unknown(definition, allowed, "run")
        duration = float(duration_seconds(definition, "duration"))
        policy_text = str(
            definition.get("end_policy", DurationEndPolicy.REJECT_PARTIAL_CYCLE.value)
        ).strip().lower()
        try:
            policy = DurationEndPolicy(policy_text)
        except ValueError as error:
            raise ValueError(f"unknown duration end_policy {policy_text!r}") from error
        cycle_ratio = duration / period
        nearest = round(cycle_ratio)
        integral = math.isclose(cycle_ratio, nearest, rel_tol=1e-10, abs_tol=1e-10)
        if integral:
            repeat_count = nearest
        elif policy is DurationEndPolicy.REJECT_PARTIAL_CYCLE:
            repeat_count = None
        elif policy is DurationEndPolicy.ROUND_DOWN:
            repeat_count = math.floor(cycle_ratio)
        elif policy is DurationEndPolicy.ROUND_UP:
            repeat_count = math.ceil(cycle_ratio)
        else:
            repeat_count = math.floor(cycle_ratio)
        return CompiledRun(
            mode=mode,
            # Retained as a compatibility/provenance cycle equivalent only.
            # Duration output is continuous and is never configured as NCycle.
            repeat_count=repeat_count,
            requested_duration_s=duration,
            achieved_duration_s=duration,
            end_policy=policy,
            exact_hardware_burst=False,
        )

    if mode in {RunMode.UNTIL_TEMPERATURE_STAGE_END, RunMode.FILL_TEMPERATURE_STAGE}:
        _reject_unknown(definition, base_allowed | {"temperature_stage"}, "run")
        stage = str(definition.get("temperature_stage", "")).strip()
        if not stage:
            raise ValueError(f"run mode {mode.value} requires temperature_stage")
        return CompiledRun(mode, None, None, None, None, False, stage)

    _reject_unknown(definition, base_allowed, "run")
    return CompiledRun(mode, None, None, None, None, False)


def _parse_start_spec(raw: Any) -> Mapping[str, Any]:
    if raw is None:
        return {"mode": "immediately"}
    if isinstance(raw, str):
        raw = {"mode": raw}
    if not isinstance(raw, Mapping):
        raise ValueError("waveform action start must be a mapping or mode string")
    mode = str(raw.get("mode", "")).strip().lower()
    aliases = {
        "immediate": "immediately",
        "elapsed": "elapsed_experiment_time",
        "after_previous": "after_previous_waveform_action",
        "temperature_stable": "temperature_became_stable",
    }
    mode = aliases.get(mode, mode)
    allowed_modes = {
        "immediately", "elapsed_experiment_time",
        "after_previous_waveform_action", "temperature_stage_started",
        "temperature_became_stable", "temperature_stage_completed", "named_event",
    }
    if mode not in allowed_modes:
        raise ValueError(f"unknown waveform start mode {mode!r}")
    allowed = {"mode"}
    result = dict(raw)
    result["mode"] = mode
    if mode == "elapsed_experiment_time":
        allowed |= _duration_keys("elapsed")
        result["elapsed_s"] = duration_seconds(raw, "elapsed")
        for key in _duration_keys("elapsed") - {"elapsed_s"}:
            result.pop(key, None)
    elif mode.startswith("temperature_"):
        allowed.add("temperature_stage")
        if not str(raw.get("temperature_stage", "")).strip():
            raise ValueError(f"start mode {mode} requires temperature_stage")
    elif mode == "named_event":
        allowed.add("event")
        if not str(raw.get("event", "")).strip():
            raise ValueError("start mode named_event requires event")
    _reject_unknown(raw, allowed, "waveform action start")
    return result


def compile_waveform_program(
    waveforms: Mapping[str, Mapping[str, Any]],
    actions: Iterable[Mapping[str, Any]],
    *,
    base_path: str | Path | None = None,
) -> CompiledWaveformProgram:
    """Compile all LUTs and validate ordered action references."""

    compiled = compile_waveforms(waveforms, base_path=base_path)
    action_items = tuple(actions)
    if not action_items:
        raise ValueError("waveform actions must not be empty")
    parsed: list[CompiledWaveformAction] = []
    names: set[str] = set()
    for index, raw in enumerate(action_items, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"waveform action {index} must be a mapping")
        _reject_unknown(raw, {"name", "waveform", "start", "run"}, f"action {index}")
        waveform_name = str(raw.get("waveform", "")).strip()
        if waveform_name not in compiled:
            raise ValueError(f"action {index} references unknown waveform {waveform_name!r}")
        action_name = _safe_name(raw.get("name", f"action_{index}"), "action name")
        if action_name in names:
            raise ValueError(f"duplicate waveform action name {action_name!r}")
        names.add(action_name)
        run = parse_run_spec(raw.get("run", {}), compiled[waveform_name].achieved_period_s)
        parsed.append(
            CompiledWaveformAction(
                action_name,
                waveform_name,
                run,
                _parse_start_spec(raw.get("start")),
            )
        )
    forever_indices = [
        index for index, action in enumerate(parsed) if action.run.mode is RunMode.FOREVER
    ]
    if forever_indices and forever_indices != [len(parsed) - 1]:
        raise ValueError("an action using forever must be the final reachable action")
    return CompiledWaveformProgram(compiled, tuple(parsed))
