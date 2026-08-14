"""Compose strict master, run, temperature, and pulse YAML files."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Mapping

from eom_stabilisation.tec.schedule import (
    TemperatureSchedule,
    parse_temperature_schedule,
)

from .errors import ConfigurationError, ReferenceCycleError
from .models import (
    CompletionMode,
    CompletionPolicy,
    ExperimentSpec,
    LinienSettings,
    LoadedExperiment,
    MeasurementSettings,
    MonitoringSettings,
    MokuSettings,
    PulseSchedule,
    RawCaptureSettings,
    RecoverySettings,
    RunSettings,
    TemperatureControllerSettings,
)
from .yaml_loader import (
    duration_field_names,
    ensure_mapping,
    ensure_sequence,
    load_yaml_mapping,
    parse_duration_seconds,
    reject_unknown_fields,
    require_fields,
    resolve_referenced_path,
    strict_positive_int,
    strict_bool,
    strict_float,
    strict_string,
)


SUPPORTED_COMPONENTS = frozenset({"lock", "moku", "temp-control", "temp-log"})
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
START_MODES = frozenset(
    {
        "immediately",
        "elapsed_experiment_time",
        "after_previous_waveform_action",
        "temperature_stage_started",
        "temperature_became_stable",
        "temperature_stage_completed",
        "named_event",
    }
)
TEMPERATURE_START_MODES = frozenset(
    {
        "temperature_stage_started",
        "temperature_became_stable",
        "temperature_stage_completed",
    }
)
RUN_MODES = frozenset(
    {
        "count",
        "duration",
        "until_experiment_end",
        "until_temperature_stage_end",
        "fill_temperature_stage",
        "forever",
        "continuous",
        "fill_experiment",
    }
)
TEMPERATURE_RUN_MODES = frozenset(
    {"until_temperature_stage_end", "fill_temperature_stage"}
)
END_POLICIES = frozenset({"reject_partial_cycle", "round_down", "round_up", "truncate"})


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_completion_policy(value: Any) -> CompletionPolicy:
    if isinstance(value, str):
        mode_text = strict_string(value, "experiment.end_when")
        try:
            mode = CompletionMode(mode_text)
        except ValueError as error:
            raise ConfigurationError(
                f"Unknown experiment end_when policy {mode_text!r}."
            ) from error
        if mode is CompletionMode.FIXED_DURATION:
            raise ConfigurationError(
                "fixed_duration end_when must be a mapping containing one "
                "explicit-unit duration."
            )
        return CompletionPolicy(mode=mode)

    mapping = ensure_mapping(value, "experiment.end_when")
    allowed = {"mode", *duration_field_names("duration")}
    reject_unknown_fields(mapping, allowed, "experiment.end_when")
    require_fields(mapping, {"mode"}, "experiment.end_when")
    mode_text = strict_string(mapping["mode"], "experiment.end_when.mode")
    if mode_text != CompletionMode.FIXED_DURATION.value:
        raise ConfigurationError(
            "Mapping-form experiment.end_when is reserved for fixed_duration."
        )
    duration_s = parse_duration_seconds(mapping, "duration", "experiment.end_when")
    assert duration_s is not None
    return CompletionPolicy(
        mode=CompletionMode.FIXED_DURATION,
        duration_s=duration_s,
    )


def _parse_master(path: Path, document: Mapping[str, Any]) -> ExperimentSpec:
    context = f"experiment file {path}"
    allowed = {
        "name",
        "components",
        "run_settings_file",
        "temperature_schedule_file",
        "pulse_schedule_file",
        "end_when",
    }
    reject_unknown_fields(document, allowed, context)
    require_fields(document, {"name", "components", "end_when"}, context)
    name = strict_string(document["name"], "experiment.name")
    if not SAFE_NAME.fullmatch(name):
        raise ConfigurationError(
            "experiment.name must start with an alphanumeric character and use "
            "only letters, numbers, underscore, dot, or hyphen."
        )

    components_raw = ensure_sequence(document["components"], "experiment.components")
    if not components_raw:
        raise ConfigurationError("experiment.components must not be empty.")
    components = tuple(
        strict_string(value, f"experiment.components[{index}]")
        for index, value in enumerate(components_raw)
    )
    if len(set(components)) != len(components):
        raise ConfigurationError("experiment.components contains duplicate entries.")
    unknown_components = set(components) - SUPPORTED_COMPONENTS
    if unknown_components:
        raise ConfigurationError(
            "Unknown experiment component(s): " + ", ".join(sorted(unknown_components))
        )
    if "temp-control" in components and "temp-log" in components:
        raise ConfigurationError(
            "temp-control and temp-log cannot both own the TEC connection."
        )

    references = {
        "run_settings": resolve_referenced_path(
            path,
            document.get("run_settings_file"),
            "experiment.run_settings_file",
        ),
        "temperature_schedule": resolve_referenced_path(
            path,
            document.get("temperature_schedule_file"),
            "experiment.temperature_schedule_file",
        ),
        "pulse_schedule": resolve_referenced_path(
            path,
            document.get("pulse_schedule_file"),
            "experiment.pulse_schedule_file",
        ),
    }
    non_null = [reference for reference in references.values() if reference is not None]
    if path in non_null:
        raise ReferenceCycleError(f"Experiment file {path} references itself.")
    if len(set(non_null)) != len(non_null):
        raise ConfigurationError(
            "The same referenced file cannot own more than one configuration role."
        )

    return ExperimentSpec(
        name=name,
        components=components,
        run_settings_path=references["run_settings"],
        temperature_schedule_path=references["temperature_schedule"],
        pulse_schedule_path=references["pulse_schedule"],
        completion_policy=_parse_completion_policy(document["end_when"]),
        source_path=path,
    )


def _optional_nullable_duration(
    mapping: Mapping[str, Any], prefix: str, context: str, default: float | None
) -> float | None:
    present = duration_field_names(prefix) & set(mapping)
    if len(present) == 1 and mapping[next(iter(present))] is None:
        return None
    return parse_duration_seconds(
        mapping,
        prefix,
        context,
        required=False,
        default=default,
    )


def _parse_run_settings(document: Mapping[str, Any], path: Path) -> RunSettings:
    context = f"run settings {path}"
    reject_unknown_fields(
        document,
        {
            "timezone",
            "recovery",
            "monitoring",
            "measurement",
            "temperature",
            "moku",
            "linien",
        },
        context,
    )
    timezone = strict_string(
        document.get("timezone", "Europe/London"), "run_settings.timezone"
    )
    if timezone != "Europe/London":
        raise ConfigurationError(
            "run_settings.timezone must be Europe/London for this apparatus."
        )

    recovery_raw = document.get("recovery", {})
    recovery_mapping = ensure_mapping(recovery_raw, "run_settings.recovery")
    allowed_recovery = {
        "temperature_hold_during_moku_outage",
        "waveform_duration_during_outage",
        "continuous_waveform",
        "finite_burst_interrupted",
        *duration_field_names("maximum_moku_outage"),
    }
    reject_unknown_fields(recovery_mapping, allowed_recovery, "run_settings.recovery")
    defaults = RecoverySettings()
    temperature_timing = strict_string(
        recovery_mapping.get(
            "temperature_hold_during_moku_outage",
            defaults.temperature_hold_during_moku_outage,
        ),
        "recovery.temperature_hold_during_moku_outage",
    )
    waveform_timing = strict_string(
        recovery_mapping.get(
            "waveform_duration_during_outage",
            defaults.waveform_duration_during_outage,
        ),
        "recovery.waveform_duration_during_outage",
    )
    for value, field in (
        (temperature_timing, "temperature_hold_during_moku_outage"),
        (waveform_timing, "waveform_duration_during_outage"),
    ):
        if value not in {"pause_timer", "continue_timer"}:
            raise ConfigurationError(
                f"recovery.{field} must be pause_timer or continue_timer."
            )
    continuous = strict_string(
        recovery_mapping.get("continuous_waveform", defaults.continuous_waveform),
        "recovery.continuous_waveform",
    )
    if continuous not in {"restart_from_phase_zero", "abort"}:
        raise ConfigurationError(
            "recovery.continuous_waveform must be restart_from_phase_zero or abort."
        )
    finite = strict_string(
        recovery_mapping.get(
            "finite_burst_interrupted", defaults.finite_burst_interrupted
        ),
        "recovery.finite_burst_interrupted",
    )
    if finite != "abort":
        raise ConfigurationError(
            "Only finite_burst_interrupted: abort is currently safe and supported."
        )
    maximum_outage_s = _optional_nullable_duration(
        recovery_mapping,
        "maximum_moku_outage",
        "run_settings.recovery",
        defaults.maximum_moku_outage_s,
    )
    monitoring = _parse_monitoring_settings(document.get("monitoring"))
    measurement = _parse_measurement_settings(document.get("measurement"))
    temperature = _parse_temperature_controller_settings(document.get("temperature"))
    moku = _parse_moku_settings(document.get("moku"))
    linien = _parse_linien_settings(document.get("linien"))
    return RunSettings(
        timezone=timezone,
        recovery=RecoverySettings(
            temperature_hold_during_moku_outage=temperature_timing,
            waveform_duration_during_outage=waveform_timing,
            continuous_waveform=continuous,
            finite_burst_interrupted=finite,
            maximum_moku_outage_s=maximum_outage_s,
        ),
        monitoring=monitoring,
        measurement=measurement,
        temperature=temperature,
        moku=moku,
        linien=linien,
    )


def _parse_monitoring_settings(value: Any) -> MonitoringSettings:
    """Parse v2-native equivalents of the v1 live monitoring controls."""

    defaults = MonitoringSettings()
    if value is None:
        return defaults
    context = "run_settings.monitoring"
    mapping = ensure_mapping(value, context)
    allowed = {
        "final_plots",
        *duration_field_names("plot_interval"),
        *duration_field_names("console_interval"),
    }
    reject_unknown_fields(mapping, allowed, context)
    plot_interval_s = _optional_nullable_duration(
        mapping,
        "plot_interval",
        context,
        defaults.plot_interval_s,
    )
    console_interval_s = parse_duration_seconds(
        mapping,
        "console_interval",
        context,
        required=False,
        default=defaults.console_interval_s,
    )
    assert console_interval_s is not None
    if plot_interval_s is not None and plot_interval_s <= 0:
        raise ConfigurationError(
            "run_settings.monitoring.plot_interval must be above zero or null."
        )
    if console_interval_s <= 0:
        raise ConfigurationError(
            "run_settings.monitoring.console_interval must be above zero."
        )
    return MonitoringSettings(
        plot_interval_s=plot_interval_s,
        final_plots=strict_bool(
            mapping.get("final_plots", defaults.final_plots),
            f"{context}.final_plots",
        ),
        console_interval_s=console_interval_s,
        configured=True,
    )


def _parse_measurement_settings(value: Any) -> MeasurementSettings:
    """Parse analysis settings, retaining documented historical defaults."""

    defaults = MeasurementSettings()
    if value is None:
        return defaults
    context = "run_settings.measurement"
    mapping = ensure_mapping(value, context)
    allowed = {
        "dark_offset_v",
        "minimum_high_level_v",
        "maximum_minimum_v",
        "minimum_sample_count",
        "maximum_optical_delay_s",
        "reference_edge_tolerance_s",
        "minimum_valid_points_per_role",
        "minimum_optical_edge_snr",
        "optical_delay_mode",
        "fixed_optical_delay_s",
        "optical_settling_guard_s",
        "maximum_consecutive_invalid_optical_samples",
        "maximum_invalid_optical_duration_s",
    }
    reject_unknown_fields(mapping, allowed, context)

    dark_offset_v = strict_float(
        mapping.get("dark_offset_v", defaults.dark_offset_v),
        f"{context}.dark_offset_v",
    )

    def optional_finite(name: str, default: float | None) -> float | None:
        if name not in mapping:
            return default
        raw_value = mapping[name]
        if raw_value is None:
            return None
        return strict_float(raw_value, f"{context}.{name}")

    return MeasurementSettings(
        dark_offset_v=dark_offset_v,
        minimum_high_level_v=optional_finite(
            "minimum_high_level_v", defaults.minimum_high_level_v
        ),
        maximum_minimum_v=optional_finite(
            "maximum_minimum_v", defaults.maximum_minimum_v
        ),
        minimum_sample_count=strict_positive_int(
            mapping.get("minimum_sample_count", defaults.minimum_sample_count),
            f"{context}.minimum_sample_count",
        ),
        maximum_optical_delay_s=strict_float(
            mapping.get("maximum_optical_delay_s", defaults.maximum_optical_delay_s),
            f"{context}.maximum_optical_delay_s",
        ),
        reference_edge_tolerance_s=strict_float(
            mapping.get(
                "reference_edge_tolerance_s", defaults.reference_edge_tolerance_s
            ),
            f"{context}.reference_edge_tolerance_s",
        ),
        minimum_valid_points_per_role=strict_positive_int(
            mapping.get(
                "minimum_valid_points_per_role",
                defaults.minimum_valid_points_per_role,
            ),
            f"{context}.minimum_valid_points_per_role",
        ),
        minimum_optical_edge_snr=strict_float(
            mapping.get("minimum_optical_edge_snr", defaults.minimum_optical_edge_snr),
            f"{context}.minimum_optical_edge_snr",
        ),
        optical_delay_mode=strict_string(
            mapping.get("optical_delay_mode", defaults.optical_delay_mode),
            f"{context}.optical_delay_mode",
        ).lower(),
        fixed_optical_delay_s=optional_finite(
            "fixed_optical_delay_s", defaults.fixed_optical_delay_s
        ),
        optical_settling_guard_s=strict_float(
            mapping.get("optical_settling_guard_s", defaults.optical_settling_guard_s),
            f"{context}.optical_settling_guard_s",
        ),
        maximum_consecutive_invalid_optical_samples=(
            strict_positive_int(
                mapping["maximum_consecutive_invalid_optical_samples"],
                f"{context}.maximum_consecutive_invalid_optical_samples",
            )
            if mapping.get("maximum_consecutive_invalid_optical_samples") is not None
            else None
        ),
        maximum_invalid_optical_duration_s=optional_finite(
            "maximum_invalid_optical_duration_s",
            defaults.maximum_invalid_optical_duration_s,
        ),
    )


def _parse_linien_settings(value: Any) -> LinienSettings | None:
    if value is None:
        return None
    context = "run_settings.linien"
    mapping = ensure_mapping(value, context)
    reject_unknown_fields(mapping, {"host"}, context)
    require_fields(mapping, {"host"}, context)
    return LinienSettings(host=strict_string(mapping["host"], f"{context}.host"))


def _parse_temperature_controller_settings(
    value: Any,
) -> TemperatureControllerSettings | None:
    if value is None:
        return None
    context = "run_settings.temperature"
    mapping = ensure_mapping(value, context)
    required = {"serial_port", "channel", "min_target_c", "max_target_c"}
    sensor_bound_fields = {
        "object_temperature_min_c",
        "object_temperature_max_c",
        "sink_temperature_min_c",
        "sink_temperature_max_c",
    }
    allowed = (
        required
        | sensor_bound_fields
        | {
            "safe_target_c",
            "sampling_interval_s",
        }
    )
    reject_unknown_fields(mapping, allowed, context)
    require_fields(mapping, required, context)
    serial_port = strict_string(mapping["serial_port"], f"{context}.serial_port")
    channel = strict_positive_int(mapping["channel"], f"{context}.channel")
    if channel != 1:
        raise ConfigurationError(
            "The verified TEC-1091 adapter currently supports only channel 1."
        )
    minimum = strict_float(mapping["min_target_c"], f"{context}.min_target_c")
    maximum = strict_float(mapping["max_target_c"], f"{context}.max_target_c")
    if minimum >= maximum:
        raise ConfigurationError(
            "run_settings.temperature.min_target_c must be below max_target_c."
        )
    safe_target_c = None
    if mapping.get("safe_target_c") is not None:
        safe_target_c = strict_float(
            mapping["safe_target_c"], f"{context}.safe_target_c"
        )
        if not minimum <= safe_target_c <= maximum:
            raise ConfigurationError(
                "run_settings.temperature.safe_target_c must lie within the "
                "configured target limits."
            )
    sampling_interval_s = strict_float(
        mapping.get("sampling_interval_s", 1.0),
        f"{context}.sampling_interval_s",
    )
    if sampling_interval_s <= 0:
        raise ConfigurationError(
            "run_settings.temperature.sampling_interval_s must be above zero."
        )

    sensor_bounds = {
        field: (
            None
            if mapping.get(field) is None
            else strict_float(mapping[field], f"{context}.{field}")
        )
        for field in sensor_bound_fields
    }
    configured_bound_count = sum(value is not None for value in sensor_bounds.values())
    if configured_bound_count not in {0, len(sensor_bound_fields)}:
        raise ConfigurationError(
            "run_settings.temperature sensor plausibility bounds must either all "
            "be finite values or all be null/omitted."
        )
    if configured_bound_count:
        if (
            sensor_bounds["object_temperature_min_c"]
            >= sensor_bounds["object_temperature_max_c"]
        ):
            raise ConfigurationError(
                "run_settings.temperature.object_temperature_min_c must be below "
                "object_temperature_max_c."
            )
        if (
            sensor_bounds["sink_temperature_min_c"]
            >= sensor_bounds["sink_temperature_max_c"]
        ):
            raise ConfigurationError(
                "run_settings.temperature.sink_temperature_min_c must be below "
                "sink_temperature_max_c."
            )
    return TemperatureControllerSettings(
        serial_port=serial_port,
        channel=channel,
        min_target_c=minimum,
        max_target_c=maximum,
        safe_target_c=safe_target_c,
        sampling_interval_s=sampling_interval_s,
        **sensor_bounds,
    )


def _parse_moku_settings(value: Any) -> MokuSettings | None:
    if value is None:
        return None
    context = "run_settings.moku"
    mapping = ensure_mapping(value, context)
    required = {
        "address",
        "force_connect",
        "platform_id",
        "awg_slot",
        "oscilloscope_slot",
        "output_channel",
        "input_channel",
        "frontend_impedance",
        "frontend_coupling",
        "frontend_attenuation",
        "trigger_source",
        "trigger_level_v",
        "trigger_edge",
        "trigger_mode",
        "trigger_type",
        "timebase_max_length",
        "sample_period_s",
        "frames_per_sample",
    }
    allowed = required | {
        "fallback_address",
        "timebase_mode",
        "timebase_start_s",
        "timebase_end_s",
        "automatic_timebase_max_duration_s",
        "raw_capture",
    }
    reject_unknown_fields(mapping, allowed, context)
    require_fields(mapping, required, context)

    fallback_address = None
    if mapping.get("fallback_address") is not None:
        fallback_address = strict_string(
            mapping["fallback_address"], f"{context}.fallback_address"
        )
    platform_id = strict_positive_int(mapping["platform_id"], f"{context}.platform_id")
    if platform_id != 2:
        raise ConfigurationError(
            "The verified shared Moku:Go runtime requires platform_id 2."
        )
    awg_slot = strict_positive_int(mapping["awg_slot"], f"{context}.awg_slot")
    oscilloscope_slot = strict_positive_int(
        mapping["oscilloscope_slot"], f"{context}.oscilloscope_slot"
    )
    if (awg_slot, oscilloscope_slot) != (1, 2):
        raise ConfigurationError(
            "The verified Moku:Go layout requires awg_slot 1 and "
            "oscilloscope_slot 2."
        )
    frontend_attenuation = strict_string(
        mapping["frontend_attenuation"], f"{context}.frontend_attenuation"
    )
    if frontend_attenuation not in {"0dB", "14dB"}:
        raise ConfigurationError(
            "run_settings.moku.frontend_attenuation must be 0dB or 14dB."
        )
    timebase_mode = strict_string(
        mapping.get("timebase_mode", "manual"), f"{context}.timebase_mode"
    ).lower()
    if timebase_mode not in {"manual", "automatic"}:
        raise ConfigurationError(
            "run_settings.moku.timebase_mode must be manual or automatic."
        )
    has_timebase_start = "timebase_start_s" in mapping
    has_timebase_end = "timebase_end_s" in mapping
    if timebase_mode == "manual" and not (has_timebase_start and has_timebase_end):
        raise ConfigurationError(
            "manual Moku timebase mode requires timebase_start_s and " "timebase_end_s."
        )
    if has_timebase_start != has_timebase_end:
        raise ConfigurationError(
            "timebase_start_s and timebase_end_s must be supplied together."
        )
    timebase_start_s = (
        strict_float(mapping["timebase_start_s"], f"{context}.timebase_start_s")
        if has_timebase_start
        else None
    )
    timebase_end_s = (
        strict_float(mapping["timebase_end_s"], f"{context}.timebase_end_s")
        if has_timebase_end
        else None
    )
    if timebase_start_s is not None and (
        timebase_start_s >= timebase_end_s or timebase_end_s <= 0
    ):
        raise ConfigurationError(
            "run_settings.moku.timebase_start_s must be below timebase_end_s, "
            "and timebase_end_s must be above zero."
        )
    automatic_timebase_max_duration_s = None
    if "automatic_timebase_max_duration_s" in mapping:
        automatic_timebase_max_duration_s = strict_float(
            mapping["automatic_timebase_max_duration_s"],
            f"{context}.automatic_timebase_max_duration_s",
        )
        if automatic_timebase_max_duration_s <= 0:
            raise ConfigurationError(
                "automatic_timebase_max_duration_s must be above zero."
            )
    raw_capture = _parse_raw_capture_settings(mapping.get("raw_capture"))
    sample_period_s = strict_float(
        mapping["sample_period_s"], f"{context}.sample_period_s"
    )
    if sample_period_s <= 0:
        raise ConfigurationError(
            "run_settings.moku.sample_period_s must be above zero."
        )
    output_channel = strict_positive_int(
        mapping["output_channel"], f"{context}.output_channel"
    )
    input_channel = strict_positive_int(
        mapping["input_channel"], f"{context}.input_channel"
    )
    if (input_channel, output_channel) != (1, 2):
        raise ConfigurationError(
            "The verified Moku:Go routing requires input_channel 1 and "
            "output_channel 2."
        )
    frontend_impedance = strict_string(
        mapping["frontend_impedance"], f"{context}.frontend_impedance"
    )
    if frontend_impedance != "1MOhm":
        raise ConfigurationError("The verified Moku:Go frontend_impedance is 1MOhm.")
    frontend_coupling = strict_string(
        mapping["frontend_coupling"], f"{context}.frontend_coupling"
    )
    if frontend_coupling not in {"AC", "DC"}:
        raise ConfigurationError(
            "run_settings.moku.frontend_coupling must be AC or DC."
        )
    trigger_level_v = strict_float(
        mapping["trigger_level_v"], f"{context}.trigger_level_v"
    )
    if not -5.0 <= trigger_level_v <= 5.0:
        raise ConfigurationError(
            "run_settings.moku.trigger_level_v must be between -5 and 5 V."
        )
    trigger_source = strict_string(
        mapping["trigger_source"], f"{context}.trigger_source"
    )
    if trigger_source not in {"ChannelA", "ChannelB"}:
        raise ConfigurationError(
            "run_settings.moku.trigger_source must be ChannelA or ChannelB "
            "inside the verified MIM Oscilloscope slot."
        )
    trigger_edge = strict_string(mapping["trigger_edge"], f"{context}.trigger_edge")
    if trigger_edge not in {"Rising", "Falling", "Both"}:
        raise ConfigurationError(
            "run_settings.moku.trigger_edge must be Rising, Falling, or Both."
        )
    trigger_mode = strict_string(mapping["trigger_mode"], f"{context}.trigger_mode")
    if trigger_mode not in {"Normal", "Auto"}:
        raise ConfigurationError(
            "run_settings.moku.trigger_mode must be Normal or Auto."
        )
    trigger_type = strict_string(mapping["trigger_type"], f"{context}.trigger_type")
    if trigger_type != "Edge":
        raise ConfigurationError(
            "Only the verified MIM Oscilloscope trigger_type Edge is supported."
        )
    timebase_max_length = strict_positive_int(
        mapping["timebase_max_length"], f"{context}.timebase_max_length"
    )
    allowed_timebase_lengths = {
        128,
        256,
        512,
        1_024,
        2_048,
        4_096,
        8_192,
        16_384,
    }
    if timebase_max_length not in allowed_timebase_lengths:
        raise ConfigurationError(
            "run_settings.moku.timebase_max_length must be one of 128, 256, "
            "512, 1024, 2048, 4096, 8192, or 16384."
        )
    address = strict_string(mapping["address"], f"{context}.address")
    if fallback_address == address:
        raise ConfigurationError(
            "run_settings.moku.fallback_address must differ from address."
        )
    return MokuSettings(
        address=address,
        fallback_address=fallback_address,
        force_connect=strict_bool(mapping["force_connect"], f"{context}.force_connect"),
        platform_id=platform_id,
        awg_slot=awg_slot,
        oscilloscope_slot=oscilloscope_slot,
        output_channel=output_channel,
        input_channel=input_channel,
        frontend_impedance=frontend_impedance,
        frontend_coupling=frontend_coupling,
        frontend_attenuation=frontend_attenuation,
        trigger_source=trigger_source,
        trigger_level_v=trigger_level_v,
        trigger_edge=trigger_edge,
        trigger_mode=trigger_mode,
        trigger_type=trigger_type,
        timebase_mode=timebase_mode,
        timebase_start_s=timebase_start_s,
        timebase_end_s=timebase_end_s,
        timebase_max_length=timebase_max_length,
        automatic_timebase_max_duration_s=automatic_timebase_max_duration_s,
        sample_period_s=sample_period_s,
        frames_per_sample=strict_positive_int(
            mapping["frames_per_sample"], f"{context}.frames_per_sample"
        ),
        raw_capture=raw_capture,
    )


def _parse_raw_capture_settings(value: Any) -> RawCaptureSettings:
    """Parse a bounded raw-trace policy without enabling storage implicitly."""

    defaults = RawCaptureSettings()
    if value is None:
        return defaults
    context = "run_settings.moku.raw_capture"
    mapping = ensure_mapping(value, context)
    duration_keys = (
        duration_field_names("interval")
        | duration_field_names("trigger_window_pre")
        | duration_field_names("trigger_window_post")
    )
    allowed = {
        "reduced_mode",
        "every_n_accepted_samples",
        "first_after_action",
        "first_after_session",
        "save_rejected",
        "maximum_rejected_frames_per_sample",
        "raw_only_window",
        *duration_keys,
    }
    reject_unknown_fields(mapping, allowed, context)

    def optional_duration(stem: str) -> float | None:
        if not (duration_field_names(stem) & set(mapping)):
            return None
        return parse_duration_seconds(mapping, stem, context)

    return RawCaptureSettings(
        reduced_mode=strict_string(
            mapping.get("reduced_mode", defaults.reduced_mode),
            f"{context}.reduced_mode",
        ).lower(),
        interval_s=optional_duration("interval"),
        every_n_accepted_samples=(
            strict_positive_int(
                mapping["every_n_accepted_samples"],
                f"{context}.every_n_accepted_samples",
            )
            if mapping.get("every_n_accepted_samples") is not None
            else None
        ),
        first_after_action=strict_bool(
            mapping.get("first_after_action", defaults.first_after_action),
            f"{context}.first_after_action",
        ),
        first_after_session=strict_bool(
            mapping.get("first_after_session", defaults.first_after_session),
            f"{context}.first_after_session",
        ),
        save_rejected=strict_bool(
            mapping.get("save_rejected", defaults.save_rejected),
            f"{context}.save_rejected",
        ),
        maximum_rejected_frames_per_sample=strict_positive_int(
            mapping.get(
                "maximum_rejected_frames_per_sample",
                defaults.maximum_rejected_frames_per_sample,
            ),
            f"{context}.maximum_rejected_frames_per_sample",
        ),
        raw_only_window=strict_string(
            mapping.get("raw_only_window", defaults.raw_only_window),
            f"{context}.raw_only_window",
        ).lower(),
        trigger_window_pre_s=optional_duration("trigger_window_pre"),
        trigger_window_post_s=optional_duration("trigger_window_post"),
    )


def _validate_start(
    value: Any,
    *,
    action_index: int,
    temperature_schedule: TemperatureSchedule | None,
) -> str:
    context = f"moku_schedule[{action_index}].start"
    if isinstance(value, str):
        mode = strict_string(value, context)
        mapping: Mapping[str, Any] = {"mode": mode}
    else:
        mapping = ensure_mapping(value, context)
        allowed = {
            "mode",
            "temperature_stage",
            "event",
            *duration_field_names("elapsed"),
        }
        reject_unknown_fields(mapping, allowed, context)
        require_fields(mapping, {"mode"}, context)
        mode = strict_string(mapping["mode"], f"{context}.mode")
    if mode not in START_MODES:
        raise ConfigurationError(f"Unknown waveform start mode {mode!r} in {context}.")

    elapsed_fields = duration_field_names("elapsed") & set(mapping)
    temperature_stage = mapping.get("temperature_stage")
    event = mapping.get("event")
    if mode == "elapsed_experiment_time":
        parse_duration_seconds(mapping, "elapsed", context, allow_zero=True)
        if temperature_stage is not None or event is not None:
            raise ConfigurationError(f"{context} has fields unrelated to {mode}.")
    elif mode in TEMPERATURE_START_MODES:
        if elapsed_fields or event is not None:
            raise ConfigurationError(f"{context} has fields unrelated to {mode}.")
        stage_name = strict_string(temperature_stage, f"{context}.temperature_stage")
        _validate_temperature_stage_reference(stage_name, temperature_schedule, context)
    elif mode == "named_event":
        if elapsed_fields or temperature_stage is not None:
            raise ConfigurationError(f"{context} has fields unrelated to {mode}.")
        strict_string(event, f"{context}.event")
    else:
        if elapsed_fields or temperature_stage is not None or event is not None:
            raise ConfigurationError(f"{context} has fields unrelated to {mode}.")
        if mode == "after_previous_waveform_action" and action_index == 0:
            raise ConfigurationError(
                "The first waveform action cannot start after a previous action."
            )
    return mode


def _validate_temperature_stage_reference(
    stage_name: str,
    schedule: TemperatureSchedule | None,
    context: str,
) -> None:
    if schedule is None:
        raise ConfigurationError(
            f"{context} links to temperature stage {stage_name!r}, but no active "
            "temperature schedule is configured."
        )
    if stage_name not in schedule.stage_names:
        raise ConfigurationError(
            f"{context} references unknown temperature stage {stage_name!r}."
        )


def _validate_run(
    value: Any,
    *,
    action_index: int,
    action_count: int,
    temperature_schedule: TemperatureSchedule | None,
) -> str:
    context = f"moku_schedule[{action_index}].run"
    mapping = ensure_mapping(value, context)
    allowed = {
        "mode",
        "count",
        "end_policy",
        "temperature_stage",
        "recovery",
        *duration_field_names("duration"),
    }
    reject_unknown_fields(mapping, allowed, context)
    require_fields(mapping, {"mode"}, context)
    mode = strict_string(mapping["mode"], f"{context}.mode")
    if mode not in RUN_MODES:
        raise ConfigurationError(f"Unknown waveform run mode {mode!r} in {context}.")

    duration_fields = duration_field_names("duration") & set(mapping)
    has_count = "count" in mapping
    end_policy = mapping.get("end_policy")
    temperature_stage = mapping.get("temperature_stage")
    if mode == "count":
        strict_positive_int(mapping.get("count"), f"{context}.count")
        if duration_fields or end_policy is not None or temperature_stage is not None:
            raise ConfigurationError(f"{context} has fields unrelated to count mode.")
        recovery_raw = mapping.get("recovery", {"mode": "strict"})
        recovery_mapping = ensure_mapping(recovery_raw, f"{context}.recovery")
        reject_unknown_fields(
            recovery_mapping,
            {"mode", "maximum_uncertain_fraction"},
            f"{context}.recovery",
        )
        recovery_mode = strict_string(
            recovery_mapping.get("mode", "strict"),
            f"{context}.recovery.mode",
        )
        if recovery_mode not in {"strict", "bounded_uncertainty"}:
            raise ConfigurationError(
                f"{context}.recovery.mode must be strict or bounded_uncertainty."
            )
        if recovery_mode == "bounded_uncertainty":
            fraction = strict_float(
                recovery_mapping.get("maximum_uncertain_fraction"),
                f"{context}.recovery.maximum_uncertain_fraction",
            )
            if not 0 < fraction < 1:
                raise ConfigurationError(
                    f"{context}.recovery.maximum_uncertain_fraction must be "
                    "above zero and below one."
                )
            count = strict_positive_int(mapping.get("count"), f"{context}.count")
            if math.floor(count * fraction / 2.0) < 1:
                raise ConfigurationError(
                    f"{context} count/tolerance is too small: bounded uncertainty "
                    "requires floor(count * maximum_uncertain_fraction / 2) "
                    "to be at least one cycle."
                )
        elif "maximum_uncertain_fraction" in recovery_mapping:
            raise ConfigurationError(
                f"{context}.recovery.maximum_uncertain_fraction is only valid "
                "for bounded_uncertainty mode."
            )
    elif mode == "duration":
        parse_duration_seconds(mapping, "duration", context)
        if has_count or temperature_stage is not None or "recovery" in mapping:
            raise ConfigurationError(
                f"{context} has fields unrelated to duration mode."
            )
        if end_policy is not None:
            policy = strict_string(end_policy, f"{context}.end_policy")
            if policy not in END_POLICIES:
                raise ConfigurationError(
                    f"Unknown duration end_policy {policy!r} in {context}."
                )
    elif mode in TEMPERATURE_RUN_MODES:
        if (
            duration_fields
            or has_count
            or end_policy is not None
            or "recovery" in mapping
        ):
            raise ConfigurationError(f"{context} has fields unrelated to {mode}.")
        stage_name = strict_string(temperature_stage, f"{context}.temperature_stage")
        _validate_temperature_stage_reference(stage_name, temperature_schedule, context)
    else:
        if (
            duration_fields
            or has_count
            or end_policy is not None
            or temperature_stage is not None
            or "recovery" in mapping
        ):
            raise ConfigurationError(f"{context} has fields unrelated to {mode}.")
    if mode == "forever" and action_index != action_count - 1:
        raise ConfigurationError("A forever waveform action must be the last action.")
    return mode


def _parse_pulse_schedule(
    document: Mapping[str, Any],
    path: Path,
    *,
    temperature_schedule: TemperatureSchedule | None,
) -> PulseSchedule:
    context = f"pulse schedule {path}"
    reject_unknown_fields(document, {"waveforms", "moku_schedule"}, context)
    require_fields(document, {"waveforms", "moku_schedule"}, context)
    waveforms_raw = ensure_mapping(document["waveforms"], f"{context}.waveforms")
    if not waveforms_raw:
        raise ConfigurationError(f"{context}.waveforms must not be empty.")
    waveforms: dict[str, Mapping[str, Any]] = {}
    for raw_name, raw_body in waveforms_raw.items():
        name = strict_string(raw_name, f"{context} waveform name")
        if not SAFE_NAME.fullmatch(name):
            raise ConfigurationError(f"Invalid waveform name {name!r}.")
        body = ensure_mapping(raw_body, f"waveform {name!r}")
        if not body:
            raise ConfigurationError(f"waveform {name!r} must not be empty.")
        waveforms[name] = deepcopy(dict(body))

    actions_raw = ensure_sequence(document["moku_schedule"], f"{context}.moku_schedule")
    if not actions_raw:
        raise ConfigurationError(f"{context}.moku_schedule must not be empty.")
    actions: list[Mapping[str, Any]] = []
    action_names: set[str] = set()
    for index, raw_action in enumerate(actions_raw):
        action_context = f"moku_schedule[{index}]"
        action = ensure_mapping(raw_action, action_context)
        reject_unknown_fields(
            action, {"name", "waveform", "start", "run"}, action_context
        )
        require_fields(action, {"waveform", "run"}, action_context)
        waveform_name = strict_string(action["waveform"], f"{action_context}.waveform")
        if waveform_name not in waveforms:
            raise ConfigurationError(
                f"{action_context} references unknown waveform {waveform_name!r}."
            )
        if "name" in action:
            action_name = strict_string(action["name"], f"{action_context}.name")
            if not SAFE_NAME.fullmatch(action_name):
                raise ConfigurationError(
                    f"Invalid waveform action name {action_name!r}."
                )
            if action_name in action_names:
                raise ConfigurationError(
                    f"Duplicate waveform action name {action_name!r}."
                )
            action_names.add(action_name)
        start_value = action.get("start", "immediately")
        _validate_start(
            start_value,
            action_index=index,
            temperature_schedule=temperature_schedule,
        )
        _validate_run(
            action["run"],
            action_index=index,
            action_count=len(actions_raw),
            temperature_schedule=temperature_schedule,
        )
        actions.append(deepcopy(dict(action)))
    return PulseSchedule(
        waveforms=waveforms,
        actions=tuple(actions),
        source_path=path,
    )


def _validate_composed_experiment(
    spec: ExperimentSpec,
    run_settings: RunSettings,
    temperature_schedule: TemperatureSchedule | None,
    pulse_schedule: PulseSchedule | None,
) -> None:
    components = set(spec.components)
    if temperature_schedule is not None and "temp-control" not in components:
        raise ConfigurationError(
            "temperature_schedule_file requires the temp-control component."
        )
    if "temp-control" in components and temperature_schedule is None:
        raise ConfigurationError(
            "The temp-control component requires temperature_schedule_file."
        )
    if pulse_schedule is not None and "moku" not in components:
        raise ConfigurationError("pulse_schedule_file requires the moku component.")
    if {"temp-control", "temp-log"} & components and run_settings.temperature is None:
        raise ConfigurationError(
            "TEC components require explicit run_settings.temperature connection "
            "and target-limit settings."
        )
    if "moku" in components and run_settings.moku is None:
        raise ConfigurationError(
            "The moku component requires explicit run_settings.moku settings."
        )
    if temperature_schedule is not None:
        assert run_settings.temperature is not None
        minimum = run_settings.temperature.min_target_c
        maximum = run_settings.temperature.max_target_c
        for stage in temperature_schedule.stages:
            if stage.target_c is not None and not minimum <= stage.target_c <= maximum:
                raise ConfigurationError(
                    f"Temperature stage {stage.name!r} target {stage.target_c:g} C "
                    f"is outside configured limits {minimum:g} to {maximum:g} C."
                )
        if (
            temperature_schedule.completion_behavior == "return_to_safe_target"
            and run_settings.temperature.safe_target_c is None
        ):
            raise ConfigurationError(
                "temperature completion_behavior return_to_safe_target requires "
                "run_settings.temperature.safe_target_c."
            )

    mode = spec.completion_policy.mode
    if mode is CompletionMode.MOKU_SCHEDULE_COMPLETE and "moku" not in components:
        raise ConfigurationError("moku_schedule_complete requires the moku component.")
    if (
        mode is CompletionMode.TEMPERATURE_SCHEDULE_COMPLETE
        and "temp-control" not in components
    ):
        raise ConfigurationError(
            "temperature_schedule_complete requires the temp-control component."
        )
    if mode is CompletionMode.ALL_SCHEDULES_COMPLETE and not (
        {"moku", "temp-control"} & components
    ):
        raise ConfigurationError(
            "all_schedules_complete requires moku or temp-control; use "
            "operator_ctrl_c for passive-only runs."
        )
    if pulse_schedule is not None and mode in {
        CompletionMode.ALL_SCHEDULES_COMPLETE,
        CompletionMode.MOKU_SCHEDULE_COMPLETE,
    }:
        final_run = ensure_mapping(
            pulse_schedule.actions[-1]["run"],
            "final moku_schedule action run",
        )
        final_mode = str(final_run["mode"])
        if final_mode in {
            "until_experiment_end",
            "fill_experiment",
            "continuous",
            "forever",
        }:
            raise ConfigurationError(
                f"end_when {mode.value} is circular with final waveform run "
                f"mode {final_mode}; use fixed_duration, "
                "temperature_schedule_complete, or operator_ctrl_c."
            )


def load_experiment(path: Path) -> LoadedExperiment:
    """Load, compose, hash, and validate an experiment before hardware access."""

    master_path = Path(path).expanduser().resolve()
    master_document = load_yaml_mapping(master_path)
    spec = _parse_master(master_path, master_document)

    sources: dict[str, Path] = {"experiment": master_path}
    source_hashes: dict[str, str] = {"experiment": _sha256_file(master_path)}

    if spec.run_settings_path is None:
        run_settings = RunSettings()
    else:
        run_document = load_yaml_mapping(spec.run_settings_path)
        run_settings = _parse_run_settings(run_document, spec.run_settings_path)
        sources["run_settings"] = spec.run_settings_path
        source_hashes["run_settings"] = _sha256_file(spec.run_settings_path)

    temperature_schedule = None
    if spec.temperature_schedule_path is not None:
        temperature_document = load_yaml_mapping(spec.temperature_schedule_path)
        temperature_schedule = parse_temperature_schedule(
            temperature_document,
            source_path=spec.temperature_schedule_path,
        )
        sources["temperature_schedule"] = spec.temperature_schedule_path
        source_hashes["temperature_schedule"] = _sha256_file(
            spec.temperature_schedule_path
        )

    pulse_schedule = None
    if spec.pulse_schedule_path is not None:
        pulse_document = load_yaml_mapping(spec.pulse_schedule_path)
        pulse_schedule = _parse_pulse_schedule(
            pulse_document,
            spec.pulse_schedule_path,
            temperature_schedule=temperature_schedule,
        )
        sources["pulse_schedule"] = spec.pulse_schedule_path
        source_hashes["pulse_schedule"] = _sha256_file(spec.pulse_schedule_path)

    _validate_composed_experiment(
        spec,
        run_settings,
        temperature_schedule,
        pulse_schedule,
    )
    return LoadedExperiment(
        name=spec.name,
        components=spec.components,
        run_settings=run_settings,
        temperature_schedule=temperature_schedule,
        pulse_schedule=pulse_schedule,
        completion_policy=spec.completion_policy,
        sources=sources,
        source_hashes=source_hashes,
    )
