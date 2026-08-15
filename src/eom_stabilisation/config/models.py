"""Typed effective configuration models."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from eom_stabilisation.tec.schedule import TemperatureSchedule


class CompletionMode(str, Enum):
    """Master experiment completion policies."""

    FIXED_DURATION = "fixed_duration"
    ALL_SCHEDULES_COMPLETE = "all_schedules_complete"
    MOKU_SCHEDULE_COMPLETE = "moku_schedule_complete"
    TEMPERATURE_SCHEDULE_COMPLETE = "temperature_schedule_complete"
    OPERATOR_CTRL_C = "operator_ctrl_c"


@dataclass(frozen=True)
class CompletionPolicy:
    """Validated master completion policy."""

    mode: CompletionMode
    duration_s: float | None = None

    def to_dict(self) -> str | dict[str, Any]:
        if self.mode is CompletionMode.FIXED_DURATION:
            return {"mode": self.mode.value, "duration_s": self.duration_s}
        return self.mode.value


@dataclass(frozen=True)
class RecoverySettings:
    """Explicit timer and waveform behaviour during Moku recovery."""

    temperature_hold_during_moku_outage: str = "pause_timer"
    waveform_duration_during_outage: str = "pause_timer"
    continuous_waveform: str = "restart_from_phase_zero"
    finite_burst_interrupted: str = "abort"
    maximum_moku_outage_s: float | None = 1800.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "temperature_hold_during_moku_outage": (
                self.temperature_hold_during_moku_outage
            ),
            "waveform_duration_during_outage": self.waveform_duration_during_outage,
            "continuous_waveform": self.continuous_waveform,
            "finite_burst_interrupted": self.finite_burst_interrupted,
            "maximum_moku_outage_s": self.maximum_moku_outage_s,
        }


@dataclass(frozen=True)
class MonitoringSettings:
    """Operator-visible progress and automatic plot generation."""

    plot_interval_s: float | None = 600.0
    final_plots: bool = True
    console_interval_s: float = 60.0
    configured: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("plot_interval_s", self.plot_interval_s),
            ("console_interval_s", self.console_interval_s),
        ):
            if value is None and name == "plot_interval_s":
                continue
            if (
                isinstance(value, bool)
                or value is None
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
        if not isinstance(self.final_plots, bool):
            raise ValueError("final_plots must be boolean")
        if not isinstance(self.configured, bool):
            raise ValueError("configured must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "plot_interval_s": self.plot_interval_s,
            "final_plots": self.final_plots,
            "console_interval_s": self.console_interval_s,
        }


@dataclass(frozen=True)
class TemperatureControllerSettings:
    """Safety limits and connection selection for scheduled TEC control."""

    serial_port: str
    channel: int
    min_target_c: float
    max_target_c: float
    safe_target_c: float | None = None
    sampling_interval_s: float = 1.0
    object_temperature_min_c: float | None = None
    object_temperature_max_c: float | None = None
    sink_temperature_min_c: float | None = None
    sink_temperature_max_c: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.serial_port, str) or not self.serial_port.strip():
            raise ValueError("serial_port must be a non-empty string")
        if (
            isinstance(self.channel, bool)
            or not isinstance(self.channel, int)
            or self.channel < 1
        ):
            raise ValueError("channel must be a positive integer")
        numeric_values = (
            ("min_target_c", self.min_target_c),
            ("max_target_c", self.max_target_c),
            ("sampling_interval_s", self.sampling_interval_s),
        )
        for name, value in numeric_values:
            try:
                finite = not isinstance(value, bool) and math.isfinite(value)
            except TypeError:
                finite = False
            if not finite:
                raise ValueError(f"{name} must be finite")
        if self.min_target_c >= self.max_target_c:
            raise ValueError("min_target_c must be below max_target_c")
        if self.sampling_interval_s <= 0:
            raise ValueError("sampling_interval_s must be above zero")
        if self.safe_target_c is not None and (
            isinstance(self.safe_target_c, bool)
            or not math.isfinite(self.safe_target_c)
            or not self.min_target_c <= self.safe_target_c <= self.max_target_c
        ):
            raise ValueError("safe_target_c must be finite and within target limits")

        bounds = (
            self.object_temperature_min_c,
            self.object_temperature_max_c,
            self.sink_temperature_min_c,
            self.sink_temperature_max_c,
        )
        configured = sum(value is not None for value in bounds)
        if configured not in {0, len(bounds)}:
            raise ValueError("sensor plausibility bounds must be complete or absent")
        if configured:
            try:
                finite_bounds = all(
                    not isinstance(value, bool) and math.isfinite(value)
                    for value in bounds
                )
            except TypeError:
                finite_bounds = False
            if not finite_bounds:
                raise ValueError("sensor plausibility bounds must be finite")
            if self.object_temperature_min_c >= self.object_temperature_max_c:
                raise ValueError(
                    "object_temperature_min_c must be below " "object_temperature_max_c"
                )
            if self.sink_temperature_min_c >= self.sink_temperature_max_c:
                raise ValueError(
                    "sink_temperature_min_c must be below sink_temperature_max_c"
                )

    @property
    def has_sensor_bounds(self) -> bool:
        """Whether a complete apparatus-verified plausibility envelope exists."""

        return all(
            value is not None
            for value in (
                self.object_temperature_min_c,
                self.object_temperature_max_c,
                self.sink_temperature_min_c,
                self.sink_temperature_max_c,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "serial_port": self.serial_port,
            "channel": self.channel,
            "min_target_c": self.min_target_c,
            "max_target_c": self.max_target_c,
            "safe_target_c": self.safe_target_c,
            "sampling_interval_s": self.sampling_interval_s,
            "object_temperature_min_c": self.object_temperature_min_c,
            "object_temperature_max_c": self.object_temperature_max_c,
            "sink_temperature_min_c": self.sink_temperature_min_c,
            "sink_temperature_max_c": self.sink_temperature_max_c,
        }


@dataclass(frozen=True)
class LinienSettings:
    """Explicit connection selection for the retained Linien logger."""

    host: str

    def to_dict(self) -> dict[str, Any]:
        return {"host": self.host}


@dataclass(frozen=True)
class RawCaptureSettings:
    """Bounded raw-trace policy for reduced and raw-only Moku acquisition."""

    reduced_mode: str = "none"
    interval_s: float | None = None
    every_n_accepted_samples: int | None = None
    first_after_action: bool = True
    first_after_session: bool = True
    save_rejected: bool = False
    maximum_rejected_frames_per_sample: int = 1
    raw_only_window: str = "full_period"
    trigger_window_pre_s: float | None = None
    trigger_window_post_s: float | None = None

    def __post_init__(self) -> None:
        if self.reduced_mode not in {"none", "all", "periodic"}:
            raise ValueError("raw_capture.reduced_mode must be none, all, or periodic")
        selectors = sum(
            value is not None
            for value in (self.interval_s, self.every_n_accepted_samples)
        )
        if self.reduced_mode == "periodic" and selectors != 1:
            raise ValueError(
                "periodic raw capture requires exactly one of interval_s or "
                "every_n_accepted_samples"
            )
        if self.reduced_mode != "periodic" and selectors:
            raise ValueError(
                "raw capture interval/count selectors are valid only for periodic mode"
            )
        if self.interval_s is not None and (
            not math.isfinite(self.interval_s) or self.interval_s <= 0
        ):
            raise ValueError("raw_capture.interval_s must be finite and positive")
        if self.every_n_accepted_samples is not None and (
            isinstance(self.every_n_accepted_samples, bool)
            or not isinstance(self.every_n_accepted_samples, int)
            or self.every_n_accepted_samples < 1
        ):
            raise ValueError(
                "raw_capture.every_n_accepted_samples must be a positive integer"
            )
        if (
            isinstance(self.maximum_rejected_frames_per_sample, bool)
            or not isinstance(self.maximum_rejected_frames_per_sample, int)
            or self.maximum_rejected_frames_per_sample < 1
        ):
            raise ValueError(
                "raw_capture.maximum_rejected_frames_per_sample must be positive"
            )
        if self.raw_only_window not in {"full_period", "trigger_window"}:
            raise ValueError(
                "raw_capture.raw_only_window must be full_period or trigger_window"
            )
        trigger_values = (self.trigger_window_pre_s, self.trigger_window_post_s)
        if self.raw_only_window == "trigger_window":
            if any(value is None for value in trigger_values):
                raise ValueError(
                    "trigger_window raw capture requires pre-trigger and post-trigger durations"
                )
            if any(
                value is None or not math.isfinite(value) or value <= 0
                for value in trigger_values
            ):
                raise ValueError(
                    "raw trigger-window durations must be finite and positive"
                )
        elif any(value is not None for value in trigger_values):
            raise ValueError(
                "raw trigger-window durations require raw_only_window: trigger_window"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reduced_mode": self.reduced_mode,
            "interval_s": self.interval_s,
            "every_n_accepted_samples": self.every_n_accepted_samples,
            "first_after_action": self.first_after_action,
            "first_after_session": self.first_after_session,
            "save_rejected": self.save_rejected,
            "maximum_rejected_frames_per_sample": (
                self.maximum_rejected_frames_per_sample
            ),
            "raw_only_window": self.raw_only_window,
            "trigger_window_pre_s": self.trigger_window_pre_s,
            "trigger_window_post_s": self.trigger_window_post_s,
        }


@dataclass(frozen=True)
class MokuSettings:
    """Explicit Moku connection, routing, acquisition, and trigger settings."""

    address: str
    fallback_address: str | None
    force_connect: bool
    platform_id: int | None
    awg_slot: int | None
    oscilloscope_slot: int | None
    output_channel: int
    input_channel: int
    frontend_impedance: str
    frontend_coupling: str
    frontend_attenuation: str
    trigger_source: str
    trigger_level_v: float
    trigger_edge: str
    trigger_mode: str
    trigger_type: str
    timebase_mode: str
    timebase_start_s: float | None
    timebase_end_s: float | None
    timebase_max_length: int
    automatic_timebase_max_duration_s: float | None
    sample_period_s: float
    frames_per_sample: int
    raw_capture: RawCaptureSettings = field(default_factory=RawCaptureSettings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "fallback_address": self.fallback_address,
            "force_connect": self.force_connect,
            "platform_id": self.platform_id,
            "awg_slot": self.awg_slot,
            "oscilloscope_slot": self.oscilloscope_slot,
            "output_channel": self.output_channel,
            "input_channel": self.input_channel,
            "frontend_impedance": self.frontend_impedance,
            "frontend_coupling": self.frontend_coupling,
            "frontend_attenuation": self.frontend_attenuation,
            "trigger_source": self.trigger_source,
            "trigger_level_v": self.trigger_level_v,
            "trigger_edge": self.trigger_edge,
            "trigger_mode": self.trigger_mode,
            "trigger_type": self.trigger_type,
            "timebase_mode": self.timebase_mode,
            "timebase_start_s": self.timebase_start_s,
            "timebase_end_s": self.timebase_end_s,
            "timebase_max_length": self.timebase_max_length,
            "automatic_timebase_max_duration_s": (
                self.automatic_timebase_max_duration_s
            ),
            "sample_period_s": self.sample_period_s,
            "frames_per_sample": self.frames_per_sample,
            "raw_capture": self.raw_capture.to_dict(),
        }


@dataclass(frozen=True)
class MeasurementSettings:
    """Configured detector correction and optional analysis filters.

    The defaults reproduce the historical analysis.  ``None`` disables only
    the corresponding optional plausibility threshold; metric-domain checks
    remain mandatory in the analysis layer.
    """

    dark_offset_v: float = 0.0
    minimum_high_level_v: float | None = 0.6
    maximum_minimum_v: float | None = 0.6
    minimum_sample_count: int = 10
    maximum_optical_delay_s: float = 0.0
    reference_edge_tolerance_s: float = 1e-6
    minimum_valid_points_per_role: int = 10
    minimum_optical_edge_snr: float = 3.0
    optical_delay_mode: str = "per_frame"
    fixed_optical_delay_s: float | None = None
    optical_settling_guard_s: float = 0.0
    maximum_consecutive_invalid_optical_samples: int | None = None
    maximum_invalid_optical_duration_s: float | None = None

    def __post_init__(self) -> None:
        values = (
            ("dark_offset_v", self.dark_offset_v, False),
            ("minimum_high_level_v", self.minimum_high_level_v, True),
            ("maximum_minimum_v", self.maximum_minimum_v, True),
        )
        for name, value, nullable in values:
            if value is None and nullable:
                continue
            if isinstance(value, bool) or value is None:
                raise ValueError(f"{name} must be finite")
            try:
                finite = math.isfinite(value)
            except TypeError as error:
                raise ValueError(f"{name} must be finite") from error
            if not finite:
                raise ValueError(f"{name} must be finite")
        if (
            isinstance(self.minimum_sample_count, bool)
            or not isinstance(self.minimum_sample_count, int)
            or self.minimum_sample_count < 1
        ):
            raise ValueError("minimum_sample_count must be a positive integer")
        for name, value, allow_zero in (
            ("maximum_optical_delay_s", self.maximum_optical_delay_s, True),
            ("reference_edge_tolerance_s", self.reference_edge_tolerance_s, False),
            ("minimum_optical_edge_snr", self.minimum_optical_edge_snr, False),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or (not allow_zero and value == 0)
            ):
                qualifier = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be finite and {qualifier}")
        if (
            isinstance(self.minimum_valid_points_per_role, bool)
            or not isinstance(self.minimum_valid_points_per_role, int)
            or self.minimum_valid_points_per_role < 1
        ):
            raise ValueError("minimum_valid_points_per_role must be a positive integer")
        if self.optical_delay_mode not in {"per_frame", "fixed"}:
            raise ValueError("optical_delay_mode must be per_frame or fixed")
        if self.fixed_optical_delay_s is not None and (
            isinstance(self.fixed_optical_delay_s, bool)
            or not math.isfinite(self.fixed_optical_delay_s)
            or self.fixed_optical_delay_s < 0
        ):
            raise ValueError("fixed_optical_delay_s must be finite and non-negative")
        if self.optical_delay_mode == "fixed" and self.fixed_optical_delay_s is None:
            raise ValueError("fixed optical delay mode requires fixed_optical_delay_s")
        if (
            self.optical_delay_mode != "fixed"
            and self.fixed_optical_delay_s is not None
        ):
            raise ValueError("fixed_optical_delay_s requires optical_delay_mode: fixed")
        if (
            self.fixed_optical_delay_s is not None
            and self.fixed_optical_delay_s > self.maximum_optical_delay_s
        ):
            raise ValueError(
                "fixed_optical_delay_s must not exceed maximum_optical_delay_s"
            )
        if (
            isinstance(self.optical_settling_guard_s, bool)
            or not math.isfinite(self.optical_settling_guard_s)
            or self.optical_settling_guard_s < 0
        ):
            raise ValueError("optical_settling_guard_s must be finite and non-negative")
        if self.maximum_consecutive_invalid_optical_samples is not None and (
            isinstance(self.maximum_consecutive_invalid_optical_samples, bool)
            or not isinstance(self.maximum_consecutive_invalid_optical_samples, int)
            or self.maximum_consecutive_invalid_optical_samples < 1
        ):
            raise ValueError(
                "maximum_consecutive_invalid_optical_samples must be positive"
            )
        if self.maximum_invalid_optical_duration_s is not None and (
            isinstance(self.maximum_invalid_optical_duration_s, bool)
            or not math.isfinite(self.maximum_invalid_optical_duration_s)
            or self.maximum_invalid_optical_duration_s <= 0
        ):
            raise ValueError(
                "maximum_invalid_optical_duration_s must be finite and positive"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dark_offset_v": self.dark_offset_v,
            "minimum_high_level_v": self.minimum_high_level_v,
            "maximum_minimum_v": self.maximum_minimum_v,
            "minimum_sample_count": self.minimum_sample_count,
            "maximum_optical_delay_s": self.maximum_optical_delay_s,
            "reference_edge_tolerance_s": self.reference_edge_tolerance_s,
            "minimum_valid_points_per_role": self.minimum_valid_points_per_role,
            "minimum_optical_edge_snr": self.minimum_optical_edge_snr,
            "optical_delay_mode": self.optical_delay_mode,
            "fixed_optical_delay_s": self.fixed_optical_delay_s,
            "optical_settling_guard_s": self.optical_settling_guard_s,
            "maximum_consecutive_invalid_optical_samples": (
                self.maximum_consecutive_invalid_optical_samples
            ),
            "maximum_invalid_optical_duration_s": (
                self.maximum_invalid_optical_duration_s
            ),
        }


@dataclass(frozen=True)
class RunSettings:
    """Cross-component runtime settings owned by run_settings.yaml."""

    timezone: str = "Europe/London"
    recovery: RecoverySettings = field(default_factory=RecoverySettings)
    monitoring: MonitoringSettings = field(default_factory=MonitoringSettings)
    measurement: MeasurementSettings = field(default_factory=MeasurementSettings)
    temperature: TemperatureControllerSettings | None = None
    moku: MokuSettings | None = None
    linien: LinienSettings | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "timezone": self.timezone,
            "recovery": self.recovery.to_dict(),
            "measurement": self.measurement.to_dict(),
            "temperature": (
                None if self.temperature is None else self.temperature.to_dict()
            ),
            "moku": None if self.moku is None else self.moku.to_dict(),
            "linien": None if self.linien is None else self.linien.to_dict(),
        }
        # Preserve hashes of configured-v2 snapshots created before monitoring
        # was added. Non-default monitoring remains part of the immutable plan.
        if self.monitoring.configured or self.monitoring != MonitoringSettings():
            result["monitoring"] = self.monitoring.to_dict()
        return result


@dataclass(frozen=True)
class PulseSchedule:
    """Strict pulse-file envelope with compiler-owned waveform bodies."""

    waveforms: Mapping[str, Mapping[str, Any]]
    actions: tuple[Mapping[str, Any], ...]
    source_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "waveforms": deepcopy(dict(self.waveforms)),
            "moku_schedule": [deepcopy(dict(action)) for action in self.actions],
        }


@dataclass(frozen=True)
class ExperimentSpec:
    """Master file after reference paths and completion policy are parsed."""

    name: str
    components: tuple[str, ...]
    run_settings_path: Path | None
    temperature_schedule_path: Path | None
    pulse_schedule_path: Path | None
    completion_policy: CompletionPolicy
    source_path: Path


@dataclass(frozen=True)
class LoadedExperiment:
    """Fully composed, validated experiment with provenance."""

    name: str
    components: tuple[str, ...]
    run_settings: RunSettings
    temperature_schedule: TemperatureSchedule | None
    pulse_schedule: PulseSchedule | None
    completion_policy: CompletionPolicy
    sources: Mapping[str, Path]
    source_hashes: Mapping[str, str]

    def effective_dict(self) -> dict[str, Any]:
        """Return a fully expanded, YAML/JSON-serialisable configuration."""

        return {
            "name": self.name,
            "components": list(self.components),
            "run_settings": self.run_settings.to_dict(),
            "temperature_schedule": (
                None
                if self.temperature_schedule is None
                else self.temperature_schedule.to_dict()
            ),
            "pulse_schedule": (
                None if self.pulse_schedule is None else self.pulse_schedule.to_dict()
            ),
            "end_when": self.completion_policy.to_dict(),
        }

    @property
    def configuration_hash(self) -> str:
        """SHA-256 of the canonical expanded experiment."""

        canonical = json.dumps(
            self.effective_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()
