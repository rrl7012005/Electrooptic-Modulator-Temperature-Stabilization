"""Validate and compile Moku:Go pulse programs without opening hardware.

Custom sequences are converted into one uniformly sampled AWG lookup table.
The limits in this module deliberately use the conservative Moku:Go table
published by Liquid Instruments: 8,192 points at 125 MSa/s through 65,536
points at 15.625 MSa/s.  This avoids relying on API coercion or skipped LUT
points.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from typing import Any, Iterable, Mapping, Sequence

import numpy as np


MOKU_GO_OUTPUT_MIN_V = -5.0
MOKU_GO_OUTPUT_MAX_V = 5.0
MOKU_GO_TRADITIONAL_MIN_AMPLITUDE_VPP = 2e-3
MOKU_GO_AWG_MIN_AMPLITUDE_VPP = 4e-3
MOKU_GO_TRADITIONAL_MIN_FREQUENCY_HZ = 1e-3
MOKU_GO_TRADITIONAL_MAX_FREQUENCY_HZ = 20e6
MOKU_GO_AWG_MIN_FREQUENCY_HZ = 1e-3
MOKU_GO_AWG_MAX_FREQUENCY_HZ = 10e6
MOKU_GO_MIN_PULSE_EDGE_S = 16e-9
MOKU_GO_MAX_BURST_CYCLES = 1_000_000


@dataclass(frozen=True)
class AwgMemoryMode:
    """One conservative Moku:Go AWG sample-rate/memory combination."""

    api_name: str
    sample_rate_hz: float
    max_points: int


MOKU_GO_AWG_MEMORY_MODES = (
    AwgMemoryMode("125Ms", 125e6, 8_192),
    AwgMemoryMode("62.5Ms", 62.5e6, 16_384),
    AwgMemoryMode("31.25Ms", 31.25e6, 32_768),
    AwgMemoryMode("15.625Ms", 15.625e6, 65_536),
)

SAFE_SEQUENCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class PulseSegment:
    """One pulse or low-level gap in a custom sequence."""

    kind: str
    duration_s: float
    edge_time_s: float | None = None


@dataclass(frozen=True)
class TraditionalPulse:
    """Configuration for the built-in repeating Pulse waveform."""

    low_level_v: float
    high_level_v: float
    frequency_hz: float
    duty_cycle_percent: float
    edge_time_s: float

    @property
    def amplitude_vpp(self) -> float:
        return self.high_level_v - self.low_level_v

    @property
    def offset_v(self) -> float:
        return (self.high_level_v + self.low_level_v) / 2.0

    @property
    def period_s(self) -> float:
        return 1.0 / self.frequency_hz

    @property
    def pulse_width_s(self) -> float:
        return self.period_s * self.duty_cycle_percent / 100.0


@dataclass(frozen=True)
class CustomSequence:
    """A sequence repeated by the AWG either finitely or continuously."""

    name: str
    segments: tuple[PulseSegment, ...]
    repeat_count: int | None

    @property
    def period_s(self) -> float:
        return sum(segment.duration_s for segment in self.segments)


@dataclass(frozen=True)
class QuantizedSegment:
    """Requested and achieved timing for one compiled segment."""

    kind: str
    requested_duration_s: float
    actual_duration_s: float
    point_count: int
    requested_edge_time_s: float | None
    actual_edge_time_s: float | None
    edge_point_count: int | None


@dataclass(frozen=True)
class CompiledCustomSequence:
    """A hardware-bounded Moku:Go AWG lookup table and timing report."""

    sequence: CustomSequence
    sample_rate_name: str
    sample_rate_hz: float
    frequency_hz: float
    point_interval_s: float
    lut_data: np.ndarray
    quantized_segments: tuple[QuantizedSegment, ...]
    maximum_timing_error_s: float

    @property
    def burst_duration_s(self) -> float | None:
        if self.sequence.repeat_count is None:
            return None
        return self.sequence.period_s * self.sequence.repeat_count

    def summary_dict(self) -> dict[str, Any]:
        """Return JSON-serializable requested and achieved settings."""
        return {
            "name": self.sequence.name,
            "repeat_count": self.sequence.repeat_count,
            "requested_period_s": self.sequence.period_s,
            "frequency_hz": self.frequency_hz,
            "sample_rate_name": self.sample_rate_name,
            "sample_rate_hz": self.sample_rate_hz,
            "point_count": len(self.lut_data),
            "point_interval_s": self.point_interval_s,
            "maximum_timing_error_s": self.maximum_timing_error_s,
            "burst_duration_s": self.burst_duration_s,
            "segments": [asdict(segment) for segment in self.quantized_segments],
        }


def _finite_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be numeric") from error
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def validate_output_levels(
    low_level_v: Any,
    high_level_v: Any,
    *,
    minimum_amplitude_vpp: float,
) -> tuple[float, float]:
    """Validate voltage levels against Moku:Go's ±5 V output range."""
    low = _finite_number(low_level_v, "low_level_v")
    high = _finite_number(high_level_v, "high_level_v")
    if not MOKU_GO_OUTPUT_MIN_V <= low <= MOKU_GO_OUTPUT_MAX_V:
        raise ValueError(
            f"low_level_v must be within {MOKU_GO_OUTPUT_MIN_V:g} to "
            f"{MOKU_GO_OUTPUT_MAX_V:g} V for Moku:Go"
        )
    if not MOKU_GO_OUTPUT_MIN_V <= high <= MOKU_GO_OUTPUT_MAX_V:
        raise ValueError(
            f"high_level_v must be within {MOKU_GO_OUTPUT_MIN_V:g} to "
            f"{MOKU_GO_OUTPUT_MAX_V:g} V for Moku:Go"
        )
    if high <= low:
        raise ValueError("high_level_v must be above low_level_v")
    amplitude = high - low
    if amplitude < minimum_amplitude_vpp:
        raise ValueError(
            f"pulse amplitude must be at least {minimum_amplitude_vpp:g} Vpp"
        )
    if amplitude > 10.0:
        raise ValueError("pulse amplitude cannot exceed 10 Vpp on Moku:Go")
    return low, high


def validate_traditional_pulse(config: TraditionalPulse) -> TraditionalPulse:
    """Reject a traditional pulse that exceeds Moku:Go API limits."""
    low_level, high_level = validate_output_levels(
        config.low_level_v,
        config.high_level_v,
        minimum_amplitude_vpp=MOKU_GO_TRADITIONAL_MIN_AMPLITUDE_VPP,
    )
    frequency = _finite_number(config.frequency_hz, "frequency_hz")
    if not (
        MOKU_GO_TRADITIONAL_MIN_FREQUENCY_HZ
        <= frequency
        <= MOKU_GO_TRADITIONAL_MAX_FREQUENCY_HZ
    ):
        raise ValueError(
            "traditional frequency_hz must be between 1e-3 and 20e6 Hz "
            "for Moku:Go"
        )
    duty = _finite_number(config.duty_cycle_percent, "duty_cycle_percent")
    if not 0.0 < duty < 100.0:
        raise ValueError("duty_cycle_percent must be above 0 and below 100")
    edge = _finite_number(config.edge_time_s, "edge_time_s")
    if edge < MOKU_GO_MIN_PULSE_EDGE_S:
        raise ValueError("edge_time_s must be at least 16 ns on Moku:Go")
    validated = TraditionalPulse(
        low_level_v=low_level,
        high_level_v=high_level,
        frequency_hz=frequency,
        duty_cycle_percent=duty,
        edge_time_s=edge,
    )
    if edge > validated.pulse_width_s:
        raise ValueError("edge_time_s cannot exceed the calculated pulse width")
    return validated


def parse_custom_sequence(
    raw_sequence: Mapping[str, Any],
    *,
    default_edge_time_s: float,
) -> CustomSequence:
    """Parse one strict, human-editable custom sequence mapping."""
    allowed_sequence_keys = {"name", "segments", "repeat_count"}
    unknown = set(raw_sequence) - allowed_sequence_keys
    if unknown:
        raise ValueError(f"unknown custom-sequence fields: {sorted(unknown)}")

    name = str(raw_sequence.get("name", "")).strip()
    if not name:
        raise ValueError("each custom sequence requires a non-empty name")
    if not SAFE_SEQUENCE_NAME.fullmatch(name):
        raise ValueError(
            f"{name}: sequence names may contain only letters, numbers, "
            "underscores, and hyphens, and must start with a letter or number"
        )

    raw_segments = raw_sequence.get("segments")
    if not isinstance(raw_segments, Sequence) or isinstance(
        raw_segments, (str, bytes)
    ):
        raise ValueError(f"{name}: segments must be a list")
    if not raw_segments:
        raise ValueError(f"{name}: at least one segment is required")

    parsed_segments = []
    for index, raw_segment in enumerate(raw_segments, start=1):
        if not isinstance(raw_segment, Mapping):
            raise ValueError(f"{name}: segment {index} must be a mapping")
        allowed_segment_keys = {"type", "duration_s", "edge_time_s"}
        unknown = set(raw_segment) - allowed_segment_keys
        if unknown:
            raise ValueError(
                f"{name}: segment {index} has unknown fields {sorted(unknown)}"
            )

        kind = str(raw_segment.get("type", "")).strip().lower()
        if kind not in {"pulse", "gap"}:
            raise ValueError(
                f"{name}: segment {index} type must be 'pulse' or 'gap'"
            )
        duration = _finite_number(
            raw_segment.get("duration_s"),
            f"{name} segment {index} duration_s",
        )
        if duration <= 0:
            raise ValueError(f"{name}: segment {index} duration_s must be above 0")

        if kind == "gap":
            if "edge_time_s" in raw_segment:
                raise ValueError(
                    f"{name}: gap segment {index} cannot have edge_time_s"
                )
            edge_time = None
        else:
            edge_time = _finite_number(
                raw_segment.get("edge_time_s", default_edge_time_s),
                f"{name} segment {index} edge_time_s",
            )
            if edge_time < MOKU_GO_MIN_PULSE_EDGE_S:
                raise ValueError(
                    f"{name}: pulse {index} edge_time_s must be at least 16 ns"
                )
            if 2.0 * edge_time >= duration:
                raise ValueError(
                    f"{name}: pulse {index} duration must exceed twice its "
                    "edge time so the pulse reaches its high level"
                )

        parsed_segments.append(PulseSegment(kind, duration, edge_time))

    if not any(segment.kind == "pulse" for segment in parsed_segments):
        raise ValueError(f"{name}: a custom sequence must contain at least one pulse")

    repeat_raw = raw_sequence.get("repeat_count")
    if repeat_raw is None:
        repeat_count = None
    elif isinstance(repeat_raw, bool):
        raise ValueError(f"{name}: repeat_count must be an integer or None")
    else:
        try:
            repeat_count = int(repeat_raw)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"{name}: repeat_count must be an integer or None"
            ) from error
        if repeat_count != repeat_raw:
            raise ValueError(f"{name}: repeat_count must be a whole number")
        if not 1 <= repeat_count <= MOKU_GO_MAX_BURST_CYCLES:
            raise ValueError(
                f"{name}: repeat_count must be between 1 and "
                f"{MOKU_GO_MAX_BURST_CYCLES:,}, or None for continuous"
            )

    sequence = CustomSequence(name, tuple(parsed_segments), repeat_count)
    frequency = 1.0 / sequence.period_s
    if not (
        MOKU_GO_AWG_MIN_FREQUENCY_HZ
        <= frequency
        <= MOKU_GO_AWG_MAX_FREQUENCY_HZ
    ):
        raise ValueError(
            f"{name}: total sequence duration gives {frequency:g} Hz, outside "
            "the Moku:Go AWG range of 1e-3 to 10e6 Hz"
        )
    return sequence


def _select_point_count(period_s: float) -> tuple[AwgMemoryMode, int]:
    """Select the finest LUT that will not skip points at its cycle rate."""
    frequency_hz = 1.0 / period_s
    candidates = []
    for mode in MOKU_GO_AWG_MEMORY_MODES:
        throughput_limited_points = math.floor(mode.sample_rate_hz / frequency_hz)
        point_count = min(mode.max_points, throughput_limited_points)
        if point_count >= 2:
            candidates.append((point_count, mode.sample_rate_hz, mode))
    if not candidates:
        raise ValueError("sequence is too short for a two-point Moku:Go AWG LUT")
    point_count, _, mode = max(candidates)
    return mode, point_count


def compile_custom_sequence(sequence: CustomSequence) -> CompiledCustomSequence:
    """Compile one custom sequence within conservative Moku:Go limits."""
    mode, point_count = _select_point_count(sequence.period_s)
    point_interval_s = sequence.period_s / point_count
    frequency_hz = 1.0 / sequence.period_s

    cumulative = np.cumsum([segment.duration_s for segment in sequence.segments])
    boundaries = np.rint(cumulative / sequence.period_s * point_count).astype(int)
    boundaries[-1] = point_count
    starts = np.concatenate(([0], boundaries[:-1]))
    counts = boundaries - starts
    if np.any(counts <= 0):
        bad_index = int(np.flatnonzero(counts <= 0)[0]) + 1
        raise ValueError(
            f"{sequence.name}: segment {bad_index} is shorter than the finest "
            f"achievable LUT interval ({point_interval_s:.3g} s)"
        )

    lut = np.full(point_count, -1.0, dtype=float)
    quantized_segments = []
    maximum_error = 0.0

    for index, (segment, start, count) in enumerate(
        zip(sequence.segments, starts, counts), start=1
    ):
        actual_duration = int(count) * point_interval_s
        maximum_error = max(
            maximum_error,
            abs(actual_duration - segment.duration_s),
        )
        actual_edge = None
        edge_count = None

        if segment.kind == "pulse":
            requested_edge = float(segment.edge_time_s)
            edge_count = int(round(requested_edge / point_interval_s))
            if edge_count < 2:
                raise ValueError(
                    f"{sequence.name}: pulse segment {index} edge time needs "
                    "fewer than two LUT points at the finest safe Moku:Go "
                    f"resolution ({point_interval_s:.3g} s per point)"
                )
            if 2 * edge_count >= count:
                raise ValueError(
                    f"{sequence.name}: pulse segment {index} cannot contain "
                    "both requested edges and a high-level point at the finest "
                    "safe Moku:Go resolution"
                )
            actual_edge = edge_count * point_interval_s
            maximum_error = max(maximum_error, abs(actual_edge - requested_edge))

            rise = np.linspace(-1.0, 1.0, edge_count + 1)[:-1]
            plateau_count = int(count) - 2 * edge_count
            plateau = np.ones(plateau_count)
            fall = np.linspace(1.0, -1.0, edge_count + 1)[1:]
            lut[int(start) : int(start + count)] = np.concatenate(
                (rise, plateau, fall)
            )

        quantized_segments.append(
            QuantizedSegment(
                kind=segment.kind,
                requested_duration_s=segment.duration_s,
                actual_duration_s=actual_duration,
                point_count=int(count),
                requested_edge_time_s=segment.edge_time_s,
                actual_edge_time_s=actual_edge,
                edge_point_count=edge_count,
            )
        )

    if len(lut) > mode.max_points:
        raise AssertionError("compiled LUT exceeds selected Moku:Go memory mode")
    if len(lut) * frequency_hz > mode.sample_rate_hz:
        raise AssertionError("compiled LUT would skip points on Moku:Go")

    return CompiledCustomSequence(
        sequence=sequence,
        sample_rate_name=mode.api_name,
        sample_rate_hz=mode.sample_rate_hz,
        frequency_hz=frequency_hz,
        point_interval_s=point_interval_s,
        lut_data=lut,
        quantized_segments=tuple(quantized_segments),
        maximum_timing_error_s=maximum_error,
    )


def compile_custom_program(
    raw_sequences: Iterable[Mapping[str, Any]],
    *,
    low_level_v: Any,
    high_level_v: Any,
    default_edge_time_s: Any,
) -> tuple[CompiledCustomSequence, ...]:
    """Validate voltage, sequence order, and compile an entire program."""
    validate_output_levels(
        low_level_v,
        high_level_v,
        minimum_amplitude_vpp=MOKU_GO_AWG_MIN_AMPLITUDE_VPP,
    )
    default_edge = _finite_number(default_edge_time_s, "default_edge_time_s")
    if default_edge < MOKU_GO_MIN_PULSE_EDGE_S:
        raise ValueError("default_edge_time_s must be at least 16 ns")

    sequences = tuple(
        parse_custom_sequence(raw, default_edge_time_s=default_edge)
        for raw in raw_sequences
    )
    if not sequences:
        raise ValueError("CUSTOM_SEQUENCES must contain at least one sequence")

    continuous_indices = [
        index
        for index, sequence in enumerate(sequences)
        if sequence.repeat_count is None
    ]
    if continuous_indices and continuous_indices[-1] != len(sequences) - 1:
        raise ValueError(
            "a continuously repeating custom sequence must be the final program entry"
        )
    if len(continuous_indices) > 1:
        raise ValueError("only one continuously repeating sequence is reachable")

    return tuple(compile_custom_sequence(sequence) for sequence in sequences)
