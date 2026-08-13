"""Typed, SDK-free models for compiled Moku waveform programs.

The models in this module describe requested connector voltages and achieved
digital timing.  They deliberately make no claim about the voltage or edge
shape delivered to the EOM after cabling, termination, or analogue bandwidth.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np


def _json_compatible(value: Any) -> Any:
    """Convert compiler metadata to stable JSON-compatible primitives."""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


class WaveformType(str, Enum):
    """Supported waveform geometries."""

    SQUARE = "square"
    SEGMENTS = "segments"
    PULSE_TRAIN = "pulse_train"
    STAIRCASE = "staircase"
    CUSTOM_PYTHON = "custom_python"
    CSV_LUT = "csv_lut"


class RunMode(str, Enum):
    """Supported waveform action lifetimes."""

    COUNT = "count"
    DURATION = "duration"
    UNTIL_EXPERIMENT_END = "until_experiment_end"
    UNTIL_TEMPERATURE_STAGE_END = "until_temperature_stage_end"
    FILL_TEMPERATURE_STAGE = "fill_temperature_stage"
    FOREVER = "forever"
    CONTINUOUS = "continuous"


class DurationEndPolicy(str, Enum):
    """How a requested duration relates to complete waveform cycles."""

    REJECT_PARTIAL_CYCLE = "reject_partial_cycle"
    ROUND_DOWN = "round_down"
    ROUND_UP = "round_up"
    TRUNCATE = "truncate"


class WaveformContinuity(str, Enum):
    """Knowledge of phase continuity across a runtime boundary."""

    CONTINUOUS = "continuous"
    RESTARTED_FROM_PHASE_ZERO = "restarted_from_phase_zero"
    UNCONFIRMED = "unconfirmed"


class OutputState(str, Enum):
    """Last confirmed physical-output state."""

    DISABLED = "disabled"
    ENABLED = "enabled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CompiledSegment:
    """Requested and quantized geometry for one waveform segment."""

    kind: str
    name: str
    level_v: float
    requested_start_s: float
    requested_end_s: float
    achieved_start_s: float
    achieved_end_s: float
    start_index: int
    end_index: int
    requested_edge_time_s: float | None = None
    achieved_edge_time_s: float | None = None
    edge_point_count: int = 0
    measurement_role: str | None = None

    @property
    def requested_duration_s(self) -> float:
        return self.requested_end_s - self.requested_start_s

    @property
    def achieved_duration_s(self) -> float:
        return self.achieved_end_s - self.achieved_start_s

    def summary_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible timing report."""

        return {
            "kind": self.kind,
            "name": self.name,
            "level_v": self.level_v,
            "requested_start_s": self.requested_start_s,
            "requested_end_s": self.requested_end_s,
            "requested_duration_s": self.requested_duration_s,
            "achieved_start_s": self.achieved_start_s,
            "achieved_end_s": self.achieved_end_s,
            "achieved_duration_s": self.achieved_duration_s,
            "start_index": self.start_index,
            "end_index": self.end_index,
            "requested_edge_time_s": self.requested_edge_time_s,
            "achieved_edge_time_s": self.achieved_edge_time_s,
            "edge_point_count": self.edge_point_count,
            "measurement_role": self.measurement_role,
        }


@dataclass(frozen=True)
class CompiledWaveform:
    """One hardware-bounded lookup table and its provenance."""

    name: str
    waveform_type: WaveformType
    requested_period_s: float
    achieved_period_s: float
    requested_frequency_hz: float
    achieved_frequency_hz: float
    sample_rate_name: str
    sample_rate_hz: float
    point_interval_s: float
    connector_voltage_v: np.ndarray
    normalized_lut: np.ndarray
    amplitude_vpp: float
    offset_v: float
    segments: tuple[CompiledSegment, ...] = ()
    requested_parameters: Mapping[str, Any] = field(default_factory=dict)
    source_path: Path | None = None
    source_sha256: str | None = None
    warnings: tuple[str, ...] = ()
    lut_sha256: str = field(init=False)
    timing_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        voltage = np.asarray(self.connector_voltage_v, dtype=float).copy()
        normalized = np.asarray(self.normalized_lut, dtype=float).copy()
        if voltage.ndim != 1 or normalized.ndim != 1 or len(voltage) != len(normalized):
            raise ValueError("compiled LUT arrays must be equal-length one-dimensional arrays")
        if len(voltage) < 2:
            raise ValueError("compiled LUT must contain at least two points")
        if not np.all(np.isfinite(voltage)) or not np.all(np.isfinite(normalized)):
            raise ValueError("compiled LUT arrays must be finite")
        if np.any(normalized < -1.0) or np.any(normalized > 1.0):
            raise ValueError("normalized LUT values must be within [-1, 1]")
        voltage.setflags(write=False)
        normalized.setflags(write=False)
        object.__setattr__(self, "connector_voltage_v", voltage)
        object.__setattr__(self, "normalized_lut", normalized)
        # Keep this as a plain dictionary: compiled waveforms cross a spawned
        # process boundary and ``mappingproxy`` is not pickle-compatible.
        object.__setattr__(
            self,
            "requested_parameters",
            copy.deepcopy(dict(self.requested_parameters)),
        )

        lut_digest = hashlib.sha256()
        # This digest identifies the exact dimensionless table sent to
        # ``generate_waveform``.  Connector scaling is recorded separately in
        # amplitude_vpp and offset_v and included in the timing provenance.
        lut_digest.update(np.asarray(normalized, dtype="<f8").tobytes(order="C"))
        object.__setattr__(self, "lut_sha256", lut_digest.hexdigest())

        timing_payload = {
            "name": self.name,
            "type": self.waveform_type.value,
            "period_s": self.achieved_period_s,
            "frequency_hz": self.achieved_frequency_hz,
            "sample_rate_name": self.sample_rate_name,
            "sample_rate_hz": self.sample_rate_hz,
            "point_interval_s": self.point_interval_s,
            "amplitude_vpp": self.amplitude_vpp,
            "offset_v": self.offset_v,
            "segments": [segment.summary_dict() for segment in self.segments],
            "lut_sha256": self.lut_sha256,
        }
        encoded = json.dumps(
            timing_payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        object.__setattr__(self, "timing_sha256", hashlib.sha256(encoded).hexdigest())

    @property
    def point_count(self) -> int:
        return len(self.normalized_lut)

    def summary_dict(self, *, include_lut: bool = False) -> dict[str, Any]:
        """Return requested/achieved settings suitable for JSON or YAML."""

        summary: dict[str, Any] = {
            "name": self.name,
            "type": self.waveform_type.value,
            "requested_period_s": self.requested_period_s,
            "achieved_period_s": self.achieved_period_s,
            "requested_frequency_hz": self.requested_frequency_hz,
            "achieved_frequency_hz": self.achieved_frequency_hz,
            "sample_rate_name": self.sample_rate_name,
            "sample_rate_hz": self.sample_rate_hz,
            "point_count": self.point_count,
            "point_interval_s": self.point_interval_s,
            "amplitude_vpp": self.amplitude_vpp,
            "offset_v": self.offset_v,
            "lut_sha256": self.lut_sha256,
            "timing_sha256": self.timing_sha256,
            "segments": [segment.summary_dict() for segment in self.segments],
            "source_path": None if self.source_path is None else str(self.source_path),
            "source_sha256": self.source_sha256,
            "warnings": list(self.warnings),
            "requested_parameters": _json_compatible(self.requested_parameters),
        }
        if include_lut:
            summary["connector_voltage_v"] = self.connector_voltage_v.tolist()
            summary["normalized_lut"] = self.normalized_lut.tolist()
        return summary


@dataclass(frozen=True)
class CompiledRun:
    """Validated runtime policy for one waveform action."""

    mode: RunMode
    repeat_count: int | None
    requested_duration_s: float | None
    achieved_duration_s: float | None
    end_policy: DurationEndPolicy | None
    exact_hardware_burst: bool
    temperature_stage: str | None = None

    def summary_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "repeat_count": self.repeat_count,
            "requested_duration_s": self.requested_duration_s,
            "achieved_duration_s": self.achieved_duration_s,
            "end_policy": None if self.end_policy is None else self.end_policy.value,
            "exact_hardware_burst": self.exact_hardware_burst,
            "temperature_stage": self.temperature_stage,
        }


@dataclass(frozen=True)
class CompiledWaveformAction:
    """One named schedule entry referencing a compiled waveform."""

    name: str
    waveform_name: str
    run: CompiledRun
    start: Mapping[str, Any] = field(default_factory=lambda: {"mode": "immediately"})


@dataclass(frozen=True)
class CompiledWaveformProgram:
    """Complete set of compiled waveforms and ordered actions."""

    waveforms: Mapping[str, CompiledWaveform]
    actions: tuple[CompiledWaveformAction, ...]
    program_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        # Programs are configuration records; a plain mapping also keeps them
        # usable with multiprocessing spawn on Windows.
        frozen_waveforms = dict(self.waveforms)
        object.__setattr__(self, "waveforms", frozen_waveforms)
        payload = {
            "waveforms": {
                name: waveform.timing_sha256
                for name, waveform in sorted(frozen_waveforms.items())
            },
            "actions": [
                {
                    "name": action.name,
                    "waveform": action.waveform_name,
                    "run": action.run.summary_dict(),
                    "start": dict(action.start),
                }
                for action in self.actions
            ],
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        object.__setattr__(self, "program_sha256", hashlib.sha256(encoded).hexdigest())


@dataclass(frozen=True)
class MeasurementWindow:
    """One interval relative to the compiled measurement-plan time origin."""

    name: str
    role: str
    start_s: float
    end_s: float
    source_segment: str | None = None


@dataclass(frozen=True)
class MeasurementPlan:
    """Named response windows for one achieved waveform timing identifier."""

    waveform_name: str
    waveform_timing_sha256: str
    period_s: float
    windows: tuple[MeasurementWindow, ...]
    trigger_phase_s: float | None = 0.0
    raw_only: bool = False
    measurement_plan_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        payload = {
            "waveform_name": self.waveform_name,
            "waveform_timing_sha256": self.waveform_timing_sha256,
            "period_s": self.period_s,
            "trigger_phase_s": self.trigger_phase_s,
            "raw_only": self.raw_only,
            "windows": [
                {
                    "name": window.name,
                    "role": window.role,
                    "start_s": window.start_s,
                    "end_s": window.end_s,
                    "source_segment": window.source_segment,
                }
                for window in self.windows
            ],
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        object.__setattr__(
            self,
            "measurement_plan_sha256",
            hashlib.sha256(encoded).hexdigest(),
        )


@dataclass(frozen=True)
class MeasurementResult:
    """Reduced values and contributing point counts for one accepted frame."""

    waveform_name: str
    waveform_timing_sha256: str
    measurement_plan_sha256: str
    values_by_role: Mapping[str, float]
    point_counts_by_role: Mapping[str, int]
