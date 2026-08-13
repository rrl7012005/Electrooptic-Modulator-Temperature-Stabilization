"""Pure temperature-schedule parsing, expansion, and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from eom_stabilisation.config.errors import ConfigurationError
from eom_stabilisation.config.yaml_loader import (
    duration_field_names,
    ensure_mapping,
    ensure_sequence,
    parse_duration_seconds,
    reject_unknown_fields,
    require_fields,
    strict_bool,
    strict_float,
    strict_positive_int,
    strict_string,
)


DEFAULT_SAMPLE_INTERVAL_S = 5.0
DEFAULT_STABILITY_TIMEOUT_S = 1800.0
SCHEDULE_COMPLETION_BEHAVIORS = frozenset(
    {
        "disable_output",
        "hold_current_target",
        "return_to_safe_target",
        "revert_to_stored_target",
    }
)
STAGE_COMPLETION_BEHAVIORS = frozenset({"advance", "stop_schedule"})


@dataclass(frozen=True)
class StabilitySpec:
    """Software qualification required before a stage hold begins."""

    required: bool = True
    tolerance_c: float | None = None
    stable_duration_s: float = 0.0
    timeout_s: float = DEFAULT_STABILITY_TIMEOUT_S

    def __post_init__(self) -> None:
        if self.tolerance_c is not None and (
            not math.isfinite(self.tolerance_c) or self.tolerance_c <= 0
        ):
            raise ConfigurationError("stability tolerance_c must be above zero.")
        if not math.isfinite(self.stable_duration_s) or self.stable_duration_s < 0:
            raise ConfigurationError(
                "stability stable_duration_s must be zero or greater."
            )
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ConfigurationError("stability timeout_s must be above zero.")
        if self.required and self.stable_duration_s > self.timeout_s:
            raise ConfigurationError(
                "stability stable_duration_s cannot exceed timeout_s."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "tolerance_c": self.tolerance_c,
            "stable_duration_s": self.stable_duration_s,
            "timeout_s": self.timeout_s,
        }


@dataclass(frozen=True)
class TemperatureStage:
    """One expanded target or explicit output-off interval."""

    name: str
    target_c: float | None
    hold_duration_s: float
    stability: StabilitySpec = field(default_factory=StabilitySpec)
    sampling_interval_s: float = DEFAULT_SAMPLE_INTERVAL_S
    notes: str | None = None
    role: str = "temperature"
    completion_behavior: str = "advance"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ConfigurationError("Every temperature stage requires a name.")
        if self.target_c is not None and not math.isfinite(self.target_c):
            raise ConfigurationError(
                f"Temperature stage {self.name!r} target_c must be finite."
            )
        if not math.isfinite(self.hold_duration_s) or self.hold_duration_s <= 0:
            raise ConfigurationError(
                f"Temperature stage {self.name!r} hold duration must be above zero."
            )
        if (
            not math.isfinite(self.sampling_interval_s)
            or self.sampling_interval_s <= 0
        ):
            raise ConfigurationError(
                f"Temperature stage {self.name!r} sampling interval must be "
                "above zero."
            )
        if self.target_c is None and self.stability.required:
            raise ConfigurationError(
                f"Output-off stage {self.name!r} cannot require temperature "
                "stability."
            )
        if self.completion_behavior not in STAGE_COMPLETION_BEHAVIORS:
            raise ConfigurationError(
                f"Unknown completion_behavior {self.completion_behavior!r} in "
                f"temperature stage {self.name!r}."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target_c": self.target_c,
            "hold_duration_s": self.hold_duration_s,
            "stability": self.stability.to_dict(),
            "sampling_interval_s": self.sampling_interval_s,
            "notes": self.notes,
            "role": self.role,
            "completion_behavior": self.completion_behavior,
        }


@dataclass(frozen=True)
class TemperatureSchedule:
    """Fully expanded immutable temperature schedule."""

    name: str
    stages: tuple[TemperatureStage, ...]
    completion_behavior: str
    source_type: str = "explicit"
    generator: Mapping[str, Any] | None = None
    source_path: Path | None = None

    def __post_init__(self) -> None:
        validate_temperature_schedule(self)

    @property
    def stage_names(self) -> tuple[str, ...]:
        return tuple(stage.name for stage in self.stages)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "type": "explicit",
            "source_type": self.source_type,
            "completion_behavior": self.completion_behavior,
            "stages": [stage.to_dict() for stage in self.stages],
        }
        if self.generator is not None:
            result["generator"] = dict(self.generator)
        return result


def validate_temperature_schedule(schedule: TemperatureSchedule) -> TemperatureSchedule:
    """Validate a fully expanded schedule and return it unchanged."""

    if not isinstance(schedule.name, str) or not schedule.name.strip():
        raise ConfigurationError("Temperature schedule name must not be empty.")
    if not schedule.stages:
        raise ConfigurationError("Temperature schedule must contain at least one stage.")
    names = [stage.name for stage in schedule.stages]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigurationError(
            "Temperature stage names must be unique; duplicate(s): "
            + ", ".join(duplicates)
        )
    if schedule.completion_behavior not in SCHEDULE_COMPLETION_BEHAVIORS:
        raise ConfigurationError(
            f"Unknown temperature completion_behavior "
            f"{schedule.completion_behavior!r}."
        )
    return schedule


def _stability_fields() -> set[str]:
    return {
        "required",
        "tolerance_c",
        *duration_field_names("stable_duration"),
        *duration_field_names("timeout"),
    }


def _parse_stability(
    value: Any,
    context: str,
    *,
    base: StabilitySpec | None = None,
) -> StabilitySpec:
    base = base or StabilitySpec()
    if value is None:
        return base
    mapping = ensure_mapping(value, context)
    reject_unknown_fields(mapping, _stability_fields(), context)
    required = (
        strict_bool(mapping["required"], f"{context}.required")
        if "required" in mapping
        else base.required
    )
    tolerance_c = base.tolerance_c
    if "tolerance_c" in mapping:
        if mapping["tolerance_c"] is None:
            tolerance_c = None
        else:
            tolerance_c = strict_float(
                mapping["tolerance_c"], f"{context}.tolerance_c"
            )
            if tolerance_c <= 0:
                raise ConfigurationError(f"{context}.tolerance_c must be above zero.")
    stable_duration_s = parse_duration_seconds(
        mapping,
        "stable_duration",
        context,
        required=False,
        default=base.stable_duration_s,
        allow_zero=True,
    )
    timeout_s = parse_duration_seconds(
        mapping,
        "timeout",
        context,
        required=False,
        default=base.timeout_s,
    )
    assert stable_duration_s is not None and timeout_s is not None
    return StabilitySpec(
        required=required,
        tolerance_c=tolerance_c,
        stable_duration_s=stable_duration_s,
        timeout_s=timeout_s,
    )


def _optional_duration(
    mapping: Mapping[str, Any],
    prefix: str,
    context: str,
) -> float | None:
    present = duration_field_names(prefix) & set(mapping)
    if len(present) == 1 and mapping[next(iter(present))] is None:
        return None
    return parse_duration_seconds(
        mapping,
        prefix,
        context,
        required=False,
        default=None,
    )


def _temperature_token(value: float) -> str:
    text = f"{value:.9g}".replace("-", "minus_").replace(".", "p")
    return text.replace("+", "")


def _make_generated_stages(
    entries: Sequence[tuple[float, float, str]],
    *,
    stability: StabilitySpec,
    sampling_interval_s: float,
) -> tuple[TemperatureStage, ...]:
    stages = []
    for index, (target_c, hold_s, role) in enumerate(entries, start=1):
        stages.append(
            TemperatureStage(
                name=(
                    f"stage_{index:03d}_{role}_{_temperature_token(target_c)}_c"
                ),
                target_c=target_c,
                hold_duration_s=hold_s,
                stability=stability,
                sampling_interval_s=sampling_interval_s,
                role=role,
            )
        )
    return tuple(stages)


def _inclusive_values(start: float, finish: float, maximum_step: float) -> list[float]:
    if maximum_step <= 0 or not math.isfinite(maximum_step):
        raise ConfigurationError("Temperature step/increment must be above zero.")
    if math.isclose(start, finish, rel_tol=0.0, abs_tol=1e-12):
        return [start]
    direction = 1.0 if finish > start else -1.0
    values = [start]
    candidate = start + direction * maximum_step
    if direction > 0:
        while candidate < finish - 1e-12:
            values.append(candidate)
            candidate += maximum_step
    else:
        while candidate > finish + 1e-12:
            values.append(candidate)
            candidate -= maximum_step
    values.append(finish)
    return values


def _repeat_path(
    path: Sequence[float],
    *,
    reverse: bool,
    cycles: int,
) -> list[float]:
    if not path:
        return []
    cycle = list(path)
    if reverse and len(path) > 1:
        cycle.extend(path[-2::-1])
    result: list[float] = []
    for _ in range(cycles):
        addition = list(cycle)
        if result and addition and math.isclose(
            result[-1], addition[0], rel_tol=0.0, abs_tol=1e-12
        ):
            addition = addition[1:]
        result.extend(addition)
    return result


def generate_temperature_sweep(
    *,
    start_c: float,
    finish_c: float,
    measurement_interval_c: float,
    transition_increment_c: float,
    transition_hold_s: float,
    measurement_hold_s: float,
    reverse: bool,
    cycles: int,
    stability: StabilitySpec | None = None,
    sampling_interval_s: float = DEFAULT_SAMPLE_INTERVAL_S,
) -> tuple[TemperatureStage, ...]:
    """Expand a gradual measurement sweep into deterministic named stages."""

    for value, label in (
        (start_c, "start_c"),
        (finish_c, "finish_c"),
        (measurement_interval_c, "measurement_interval_c"),
        (transition_increment_c, "transition_increment_c"),
        (transition_hold_s, "transition_hold_s"),
        (measurement_hold_s, "measurement_hold_s"),
        (sampling_interval_s, "sampling_interval_s"),
    ):
        if not math.isfinite(value):
            raise ConfigurationError(f"{label} must be finite.")
    if math.isclose(start_c, finish_c, rel_tol=0.0, abs_tol=1e-12):
        raise ConfigurationError("Sweep start_c and finish_c must be different.")
    if measurement_interval_c <= 0 or transition_increment_c <= 0:
        raise ConfigurationError("Sweep intervals/increments must be above zero.")
    if transition_hold_s <= 0 or measurement_hold_s <= 0:
        raise ConfigurationError("Sweep hold durations must be above zero.")
    if sampling_interval_s <= 0:
        raise ConfigurationError("Sweep sampling interval must be above zero.")
    if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles <= 0:
        raise ConfigurationError("Sweep cycles must be an integer above zero.")

    measurement_targets = _inclusive_values(
        start_c, finish_c, measurement_interval_c
    )
    target_path = _repeat_path(
        measurement_targets,
        reverse=reverse,
        cycles=cycles,
    )
    entries: list[tuple[float, float, str]] = [
        (target_path[0], measurement_hold_s, "measurement")
    ]
    for previous, target in zip(target_path, target_path[1:]):
        intermediate = _inclusive_values(previous, target, transition_increment_c)
        entries.extend(
            (value, transition_hold_s, "transition")
            for value in intermediate[1:-1]
        )
        entries.append((target, measurement_hold_s, "measurement"))
    return _make_generated_stages(
        entries,
        stability=stability or StabilitySpec(),
        sampling_interval_s=sampling_interval_s,
    )


def _parse_common(document: Mapping[str, Any], source: Path) -> tuple[
    str,
    str,
    StabilitySpec,
    float,
    str,
]:
    require_fields(
        document,
        {"name", "type", "completion_behavior"},
        f"temperature schedule {source}",
    )
    name = strict_string(document["name"], "temperature schedule name")
    schedule_type = strict_string(document["type"], "temperature schedule type").lower()
    stability = _parse_stability(
        document.get("stability"), "temperature schedule stability"
    )
    sampling_interval_s = parse_duration_seconds(
        document,
        "sampling_interval",
        "temperature schedule",
        required=False,
        default=DEFAULT_SAMPLE_INTERVAL_S,
    )
    assert sampling_interval_s is not None
    completion = strict_string(
        document["completion_behavior"],
        "temperature schedule completion_behavior",
    )
    if completion not in SCHEDULE_COMPLETION_BEHAVIORS:
        raise ConfigurationError(
            f"Unknown temperature completion_behavior {completion!r}."
        )
    return name, schedule_type, stability, sampling_interval_s, completion


def _common_fields() -> set[str]:
    return {
        "name",
        "type",
        "stability",
        "completion_behavior",
        *duration_field_names("sampling_interval"),
    }


def _parse_explicit_stages(
    document: Mapping[str, Any],
    *,
    default_stability: StabilitySpec,
    default_sampling_interval_s: float,
) -> tuple[TemperatureStage, ...]:
    stages_raw = ensure_sequence(document["stages"], "temperature stages")
    if not stages_raw:
        raise ConfigurationError("temperature stages must not be empty.")
    stages = []
    allowed = {
        "name",
        "target_c",
        "stability",
        "notes",
        "completion_behavior",
        *duration_field_names("hold_duration"),
        *duration_field_names("sampling_interval"),
    }
    for index, raw in enumerate(stages_raw, start=1):
        context = f"temperature stage {index}"
        mapping = ensure_mapping(raw, context)
        reject_unknown_fields(mapping, allowed, context)
        require_fields(mapping, {"target_c"}, context)
        name = strict_string(mapping.get("name", f"stage_{index:03d}"), f"{context}.name")
        target_c = (
            None
            if mapping["target_c"] is None
            else strict_float(mapping["target_c"], f"{context}.target_c")
        )
        hold_s = parse_duration_seconds(mapping, "hold_duration", context)
        assert hold_s is not None
        sampling_s = parse_duration_seconds(
            mapping,
            "sampling_interval",
            context,
            required=False,
            default=default_sampling_interval_s,
        )
        assert sampling_s is not None
        stability = _parse_stability(
            mapping.get("stability"), f"{context}.stability", base=default_stability
        )
        if target_c is None:
            stability = StabilitySpec(
                required=False,
                tolerance_c=None,
                stable_duration_s=0.0,
                timeout_s=stability.timeout_s,
            )
        notes = None
        if mapping.get("notes") is not None:
            notes = strict_string(mapping["notes"], f"{context}.notes", allow_empty=True)
        stage_completion = strict_string(
            mapping.get("completion_behavior", "advance"),
            f"{context}.completion_behavior",
        )
        stages.append(
            TemperatureStage(
                name=name,
                target_c=target_c,
                hold_duration_s=hold_s,
                stability=stability,
                sampling_interval_s=sampling_s,
                notes=notes,
                role="output_off" if target_c is None else "temperature",
                completion_behavior=stage_completion,
            )
        )
    return tuple(stages)


def parse_temperature_schedule(
    document: Mapping[str, Any],
    *,
    source_path: str | Path | None = None,
) -> TemperatureSchedule:
    """Parse and fully expand one strict temperature schedule mapping."""

    source = Path(source_path).resolve() if source_path is not None else Path("<memory>")
    name, schedule_type, stability, sampling_s, completion = _parse_common(
        document, source
    )
    common = _common_fields()
    generator: dict[str, Any] | None = None

    if schedule_type == "explicit":
        allowed = common | {"stages"}
        reject_unknown_fields(document, allowed, f"temperature schedule {source}")
        require_fields(document, {"stages"}, f"temperature schedule {source}")
        stages = _parse_explicit_stages(
            document,
            default_stability=stability,
            default_sampling_interval_s=sampling_s,
        )

    elif schedule_type == "sweep":
        duration_prefixes = (
            "initial_tec_off_hold",
            "transition_hold",
            "measurement_hold",
        )
        allowed = common | {
            "start_c",
            "finish_c",
            "measurement_interval_c",
            "transition_increment_c",
            "reverse",
            "cycles",
        }
        for prefix in duration_prefixes:
            allowed |= set(duration_field_names(prefix))
        reject_unknown_fields(document, allowed, f"temperature schedule {source}")
        require_fields(
            document,
            {
                "start_c",
                "finish_c",
                "measurement_interval_c",
                "transition_increment_c",
            },
            f"temperature schedule {source}",
        )
        transition_hold_s = parse_duration_seconds(
            document, "transition_hold", "temperature sweep"
        )
        measurement_hold_s = parse_duration_seconds(
            document, "measurement_hold", "temperature sweep"
        )
        initial_off_s = _optional_duration(
            document, "initial_tec_off_hold", "temperature sweep"
        )
        reverse = strict_bool(document.get("reverse", False), "temperature sweep.reverse")
        cycles = strict_positive_int(document.get("cycles", 1), "temperature sweep.cycles")
        start_c = strict_float(document["start_c"], "temperature sweep.start_c")
        finish_c = strict_float(document["finish_c"], "temperature sweep.finish_c")
        measurement_interval_c = strict_float(
            document["measurement_interval_c"],
            "temperature sweep.measurement_interval_c",
        )
        transition_increment_c = strict_float(
            document["transition_increment_c"],
            "temperature sweep.transition_increment_c",
        )
        assert transition_hold_s is not None and measurement_hold_s is not None
        generated = list(
            generate_temperature_sweep(
                start_c=start_c,
                finish_c=finish_c,
                measurement_interval_c=measurement_interval_c,
                transition_increment_c=transition_increment_c,
                transition_hold_s=transition_hold_s,
                measurement_hold_s=measurement_hold_s,
                reverse=reverse,
                cycles=cycles,
                stability=stability,
                sampling_interval_s=sampling_s,
            )
        )
        if initial_off_s is not None:
            generated.insert(
                0,
                TemperatureStage(
                    name="initial_tec_off",
                    target_c=None,
                    hold_duration_s=initial_off_s,
                    stability=StabilitySpec(
                        required=False,
                        timeout_s=stability.timeout_s,
                    ),
                    sampling_interval_s=sampling_s,
                    role="output_off",
                ),
            )
        stages = tuple(generated)
        generator = {
            "type": "sweep",
            "start_c": start_c,
            "finish_c": finish_c,
            "measurement_interval_c": measurement_interval_c,
            "transition_increment_c": transition_increment_c,
            "transition_hold_s": transition_hold_s,
            "measurement_hold_s": measurement_hold_s,
            "initial_tec_off_hold_s": initial_off_s,
            "reverse": reverse,
            "cycles": cycles,
        }

    elif schedule_type in {"targets", "range"}:
        allowed = common | {"reverse", "cycles", *duration_field_names("hold_duration")}
        if schedule_type == "targets":
            allowed |= {"targets_c"}
            required = {"targets_c"}
        else:
            allowed |= {"start_c", "finish_c", "step_c"}
            required = {"start_c", "finish_c", "step_c"}
        reject_unknown_fields(document, allowed, f"temperature schedule {source}")
        require_fields(document, required, f"temperature schedule {source}")
        hold_s = parse_duration_seconds(document, "hold_duration", "temperature schedule")
        assert hold_s is not None
        reverse = strict_bool(document.get("reverse", False), "temperature schedule.reverse")
        cycles = strict_positive_int(document.get("cycles", 1), "temperature schedule.cycles")
        if schedule_type == "targets":
            raw_targets = ensure_sequence(document["targets_c"], "temperature targets_c")
            if not raw_targets:
                raise ConfigurationError("temperature targets_c must not be empty.")
            base_targets = [
                strict_float(value, f"temperature targets_c[{index}]")
                for index, value in enumerate(raw_targets)
            ]
        else:
            start_c = strict_float(document["start_c"], "temperature range.start_c")
            finish_c = strict_float(document["finish_c"], "temperature range.finish_c")
            step_c = strict_float(document["step_c"], "temperature range.step_c")
            if math.isclose(start_c, finish_c, rel_tol=0.0, abs_tol=1e-12):
                raise ConfigurationError("Temperature range endpoints must differ.")
            base_targets = _inclusive_values(start_c, finish_c, step_c)
        target_path = _repeat_path(base_targets, reverse=reverse, cycles=cycles)
        entries = [(target, hold_s, "measurement") for target in target_path]
        stages = _make_generated_stages(
            entries,
            stability=stability,
            sampling_interval_s=sampling_s,
        )
        generator = {
            "type": schedule_type,
            "targets_c": base_targets,
            "hold_duration_s": hold_s,
            "reverse": reverse,
            "cycles": cycles,
        }
    else:
        raise ConfigurationError(
            "temperature schedule type must be explicit, sweep, targets, or range."
        )

    return TemperatureSchedule(
        name=name,
        stages=stages,
        completion_behavior=completion,
        source_type=schedule_type,
        generator=generator,
        source_path=None if source_path is None else Path(source_path).resolve(),
    )


def expand_temperature_schedule(
    document: Mapping[str, Any],
    *,
    source_path: str | Path | None = None,
) -> TemperatureSchedule:
    """Alias emphasizing that generated definitions return explicit stages."""

    return parse_temperature_schedule(document, source_path=source_path)
