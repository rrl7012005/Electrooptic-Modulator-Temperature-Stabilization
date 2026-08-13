"""Typed atomic runtime checkpoints and strict resume validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from eom_stabilisation.config.errors import (
    ConfigurationError,
    ResumeConfigurationMismatch,
)
from eom_stabilisation.run_store import atomic_write_json


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
OUTPUT_STATES = frozenset({"enabled", "disabled", "unknown"})
PHASE_CONTINUITY_STATES = frozenset(
    {"continuous", "reset_to_phase_zero", "unknown", "not_applicable"}
)
TEMPERATURE_PHASES = frozenset(
    {"idle", "waiting_for_stability", "holding", "complete", "failed"}
)
MOKU_PHASES = frozenset(
    {"idle", "waiting", "running", "complete", "indeterminate", "failed"}
)
REPEAT_MODES = frozenset(
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


def _finite_nonnegative(value: float, context: str) -> float:
    if isinstance(value, bool):
        raise ConfigurationError(f"{context} must be a finite number.")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ConfigurationError(f"{context} must be finite and zero or greater.")
    return result


def _optional_finite(value: float | None, context: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigurationError(f"{context} must be a finite number or null.")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigurationError(f"{context} must be finite or null.")
    return result


def _validate_sha256(value: str, context: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ConfigurationError(f"{context} must be a lowercase SHA-256 digest.")
    return value


def _validate_hash_mapping(value: Mapping[str, str], context: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{context} must be a mapping.")
    result = {}
    for key, digest in value.items():
        if not isinstance(key, str) or not key:
            raise ConfigurationError(f"{context} keys must be non-empty strings.")
        result[key] = _validate_sha256(digest, f"{context}.{key}")
    return result


def _validate_utc_timestamp(value: str | None, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{context} must be an aware ISO timestamp or null.")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ConfigurationError(f"{context} is not a valid ISO timestamp.") from error
    if parsed.tzinfo is None:
        raise ConfigurationError(f"{context} must be timezone-aware.")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _strict_mapping(
    value: Any,
    *,
    allowed: set[str],
    required: set[str],
    context: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{context} must be a mapping.")
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise ConfigurationError(
            f"Unknown checkpoint field(s) in {context}: {', '.join(sorted(unknown))}."
        )
    if missing:
        raise ConfigurationError(
            f"Missing checkpoint field(s) in {context}: {', '.join(sorted(missing))}."
        )
    return value


@dataclass(frozen=True)
class TemperatureCheckpoint:
    """Independent resumable temperature schedule state."""

    stage_index: int | None
    stage_name: str | None
    phase: str
    requested_target_c: float | None
    completed_hold_s: float
    stable_elapsed_s: float
    phase_elapsed_s: float
    schedule_elapsed_s: float
    safe_resume_boundary: bool
    last_confirmed_output_state: str

    def __post_init__(self) -> None:
        if self.stage_index is not None and (
            isinstance(self.stage_index, bool)
            or not isinstance(self.stage_index, int)
            or self.stage_index < 0
        ):
            raise ConfigurationError("temperature.stage_index must be nonnegative.")
        if self.stage_name is not None and (
            not isinstance(self.stage_name, str) or not self.stage_name
        ):
            raise ConfigurationError("temperature.stage_name must be non-empty.")
        if not isinstance(self.phase, str) or not self.phase:
            raise ConfigurationError("temperature.phase must be non-empty.")
        if self.phase not in TEMPERATURE_PHASES:
            raise ConfigurationError("temperature.phase is not recognised.")
        _optional_finite(self.requested_target_c, "temperature.requested_target_c")
        _finite_nonnegative(self.completed_hold_s, "temperature.completed_hold_s")
        _finite_nonnegative(self.stable_elapsed_s, "temperature.stable_elapsed_s")
        phase_elapsed_s = _finite_nonnegative(
            self.phase_elapsed_s, "temperature.phase_elapsed_s"
        )
        schedule_elapsed_s = _finite_nonnegative(
            self.schedule_elapsed_s, "temperature.schedule_elapsed_s"
        )
        if phase_elapsed_s > schedule_elapsed_s + 1e-9:
            raise ConfigurationError(
                "temperature.phase_elapsed_s cannot exceed schedule_elapsed_s."
            )
        if not isinstance(self.safe_resume_boundary, bool):
            raise ConfigurationError("temperature.safe_resume_boundary must be boolean.")
        if self.phase == "failed" and self.safe_resume_boundary:
            raise ConfigurationError(
                "A failed temperature phase cannot be a safe resume boundary."
            )
        if self.last_confirmed_output_state not in OUTPUT_STATES:
            raise ConfigurationError(
                "temperature.last_confirmed_output_state must be enabled, "
                "disabled, or unknown."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_index": self.stage_index,
            "stage_name": self.stage_name,
            "phase": self.phase,
            "requested_target_c": self.requested_target_c,
            "completed_hold_s": self.completed_hold_s,
            "stable_elapsed_s": self.stable_elapsed_s,
            "phase_elapsed_s": self.phase_elapsed_s,
            "schedule_elapsed_s": self.schedule_elapsed_s,
            "safe_resume_boundary": self.safe_resume_boundary,
            "last_confirmed_output_state": self.last_confirmed_output_state,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "TemperatureCheckpoint":
        fields = {
            "stage_index",
            "stage_name",
            "phase",
            "requested_target_c",
            "completed_hold_s",
            "stable_elapsed_s",
            "phase_elapsed_s",
            "schedule_elapsed_s",
            "safe_resume_boundary",
            "last_confirmed_output_state",
        }
        mapping = _strict_mapping(
            value, allowed=fields, required=fields, context="checkpoint.temperature"
        )
        return cls(**{key: mapping[key] for key in fields})


@dataclass(frozen=True)
class MokuCheckpoint:
    """Independent waveform/acquisition state with finite-burst ambiguity."""

    action_index: int | None
    action_name: str | None
    waveform_name: str | None
    waveform_run_id: str | None
    waveform_session_id: int
    phase: str
    repeat_mode: str | None
    requested_duration_s: float | None
    completed_duration_s: float
    requested_count: int | None
    completed_count: int
    safe_resume_boundary: bool
    action_indeterminate: bool
    last_valid_sample_timestamp_utc: str | None
    last_confirmed_output_state: str
    phase_continuity: str
    delivered_lower_bound: int | None = None
    delivered_upper_bound: int | None = None
    cumulative_ambiguous_cycles: int = 0
    uncertainty_budget_exhausted: bool = False
    count_recovery_mode: str | None = None

    def __post_init__(self) -> None:
        if self.action_index is not None and (
            isinstance(self.action_index, bool)
            or not isinstance(self.action_index, int)
            or self.action_index < 0
        ):
            raise ConfigurationError("moku.action_index must be nonnegative.")
        for value, field in (
            (self.action_name, "action_name"),
            (self.waveform_name, "waveform_name"),
            (self.waveform_run_id, "waveform_run_id"),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise ConfigurationError(f"moku.{field} must be non-empty or null.")
        if (
            isinstance(self.waveform_session_id, bool)
            or not isinstance(self.waveform_session_id, int)
            or self.waveform_session_id < 0
        ):
            raise ConfigurationError("moku.waveform_session_id must be nonnegative.")
        if self.phase not in MOKU_PHASES:
            raise ConfigurationError("moku.phase is not recognised.")
        if self.repeat_mode is not None and self.repeat_mode not in REPEAT_MODES:
            raise ConfigurationError("moku.repeat_mode is not recognised.")
        requested_duration = _optional_finite(
            self.requested_duration_s, "moku.requested_duration_s"
        )
        _finite_nonnegative(self.completed_duration_s, "moku.completed_duration_s")
        if requested_duration is not None and requested_duration < 0:
            raise ConfigurationError("moku requested duration must be nonnegative.")
        # A compiled round-up duration can legitimately run past the requested
        # wall time by one partial-cycle remainder.  The achieved duration is
        # validated against the compiled program during state-machine restore.
        if self.requested_count is not None and (
            isinstance(self.requested_count, bool)
            or not isinstance(self.requested_count, int)
            or self.requested_count < 0
        ):
            raise ConfigurationError("moku.requested_count must be nonnegative.")
        if (
            isinstance(self.completed_count, bool)
            or not isinstance(self.completed_count, int)
            or self.completed_count < 0
        ):
            raise ConfigurationError("moku.completed_count must be nonnegative.")
        if (
            self.requested_count is not None
            and self.completed_count > self.requested_count
        ):
            raise ConfigurationError("moku completed count cannot exceed requested count.")
        if not isinstance(self.safe_resume_boundary, bool) or not isinstance(
            self.action_indeterminate, bool
        ):
            raise ConfigurationError("Moku checkpoint safety flags must be boolean.")
        if self.action_indeterminate != (self.phase == "indeterminate"):
            raise ConfigurationError(
                "moku.action_indeterminate must match the indeterminate phase."
            )
        if self.phase in {"indeterminate", "failed"} and self.safe_resume_boundary:
            raise ConfigurationError(
                "An indeterminate or failed Moku phase cannot be safely resumable."
            )
        _validate_utc_timestamp(
            self.last_valid_sample_timestamp_utc,
            "moku.last_valid_sample_timestamp_utc",
        )
        if self.last_confirmed_output_state not in OUTPUT_STATES:
            raise ConfigurationError(
                "moku.last_confirmed_output_state must be enabled, disabled, or unknown."
            )
        if self.phase_continuity not in PHASE_CONTINUITY_STATES:
            raise ConfigurationError("moku.phase_continuity is invalid.")
        for value, field in (
            (self.delivered_lower_bound, "delivered_lower_bound"),
            (self.delivered_upper_bound, "delivered_upper_bound"),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ConfigurationError(f"moku.{field} must be nonnegative or null.")
        if (
            self.delivered_lower_bound is not None
            and self.delivered_upper_bound is not None
            and self.delivered_lower_bound > self.delivered_upper_bound
        ):
            raise ConfigurationError("moku delivered interval is inverted.")
        if (self.delivered_lower_bound is None) != (
            self.delivered_upper_bound is None
        ):
            raise ConfigurationError(
                "moku delivered interval requires both lower and upper bounds."
            )
        if (
            self.requested_count is not None
            and self.delivered_upper_bound is not None
            and self.delivered_upper_bound > self.requested_count
        ):
            raise ConfigurationError(
                "moku delivered upper bound cannot exceed requested_count."
            )
        if (
            isinstance(self.cumulative_ambiguous_cycles, bool)
            or not isinstance(self.cumulative_ambiguous_cycles, int)
            or self.cumulative_ambiguous_cycles < 0
        ):
            raise ConfigurationError(
                "moku.cumulative_ambiguous_cycles must be nonnegative."
            )
        if not isinstance(self.uncertainty_budget_exhausted, bool):
            raise ConfigurationError(
                "moku.uncertainty_budget_exhausted must be boolean."
            )
        if self.count_recovery_mode not in {None, "strict", "bounded_uncertainty"}:
            raise ConfigurationError("moku.count_recovery_mode is invalid.")
        if (
            self.delivered_lower_bound is not None
            and self.delivered_upper_bound is not None
            and self.delivered_upper_bound - self.delivered_lower_bound
            != self.cumulative_ambiguous_cycles
        ):
            raise ConfigurationError(
                "moku delivered interval width must equal cumulative ambiguity."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_index": self.action_index,
            "action_name": self.action_name,
            "waveform_name": self.waveform_name,
            "waveform_run_id": self.waveform_run_id,
            "waveform_session_id": self.waveform_session_id,
            "phase": self.phase,
            "repeat_mode": self.repeat_mode,
            "requested_duration_s": self.requested_duration_s,
            "completed_duration_s": self.completed_duration_s,
            "requested_count": self.requested_count,
            "completed_count": self.completed_count,
            "safe_resume_boundary": self.safe_resume_boundary,
            "action_indeterminate": self.action_indeterminate,
            "last_valid_sample_timestamp_utc": self.last_valid_sample_timestamp_utc,
            "last_confirmed_output_state": self.last_confirmed_output_state,
            "phase_continuity": self.phase_continuity,
            "delivered_lower_bound": self.delivered_lower_bound,
            "delivered_upper_bound": self.delivered_upper_bound,
            "cumulative_ambiguous_cycles": self.cumulative_ambiguous_cycles,
            "uncertainty_budget_exhausted": self.uncertainty_budget_exhausted,
            "count_recovery_mode": self.count_recovery_mode,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "MokuCheckpoint":
        fields = set(cls.__dataclass_fields__)
        legacy_required = fields - {
            "delivered_lower_bound",
            "delivered_upper_bound",
            "cumulative_ambiguous_cycles",
            "uncertainty_budget_exhausted",
            "count_recovery_mode",
        }
        mapping = _strict_mapping(
            value, allowed=fields, required=legacy_required, context="checkpoint.moku"
        )
        return cls(**{key: mapping[key] for key in fields if key in mapping})


@dataclass(frozen=True)
class RuntimeCheckpoint:
    """Complete atomic state for independently resumable schedules."""

    configuration_hash: str
    source_hashes: Mapping[str, str]
    lut_hashes: Mapping[str, str]
    experiment_elapsed_s: float
    updated_at_utc: str
    temperature: TemperatureCheckpoint | None = None
    moku: MokuCheckpoint | None = None
    version: int = 2

    def __post_init__(self) -> None:
        if self.version not in {1, 2} or isinstance(self.version, bool):
            raise ConfigurationError("checkpoint.version must be integer 1 or 2.")
        _validate_sha256(self.configuration_hash, "checkpoint.configuration_hash")
        _validate_hash_mapping(self.source_hashes, "checkpoint.source_hashes")
        _validate_hash_mapping(self.lut_hashes, "checkpoint.lut_hashes")
        _finite_nonnegative(self.experiment_elapsed_s, "checkpoint.experiment_elapsed_s")
        _validate_utc_timestamp(self.updated_at_utc, "checkpoint.updated_at_utc")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "configuration_hash": self.configuration_hash,
            "source_hashes": dict(self.source_hashes),
            "lut_hashes": dict(self.lut_hashes),
            "experiment_elapsed_s": self.experiment_elapsed_s,
            "updated_at_utc": _validate_utc_timestamp(
                self.updated_at_utc, "checkpoint.updated_at_utc"
            ),
            "temperature": (
                None if self.temperature is None else self.temperature.to_dict()
            ),
            "moku": None if self.moku is None else self.moku.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "RuntimeCheckpoint":
        fields = {
            "version",
            "configuration_hash",
            "source_hashes",
            "lut_hashes",
            "experiment_elapsed_s",
            "updated_at_utc",
            "temperature",
            "moku",
        }
        mapping = _strict_mapping(
            value, allowed=fields, required=fields, context="checkpoint"
        )
        version = mapping["version"]
        temperature = (
            None
            if mapping["temperature"] is None
            else TemperatureCheckpoint.from_dict(mapping["temperature"])
        )
        moku = (
            None
            if mapping["moku"] is None
            else MokuCheckpoint.from_dict(mapping["moku"])
        )
        if version == 1 and moku is not None and moku.phase == "running":
            raise ConfigurationError(
                "checkpoint version 1 cannot resume a running Moku action: it "
                "does not record continuous-duration or count delivery bounds; "
                "start a new run or resume from a version 2 safe boundary."
            )
        if (
            version == 2
            and moku is not None
            and moku.count_recovery_mode == "bounded_uncertainty"
            and (
                moku.delivered_lower_bound is None
                or moku.delivered_upper_bound is None
            )
        ):
            raise ConfigurationError(
                "checkpoint version 2 bounded count state requires delivered bounds."
            )
        return cls(
            version=version,
            configuration_hash=mapping["configuration_hash"],
            source_hashes=_validate_hash_mapping(
                mapping["source_hashes"], "checkpoint.source_hashes"
            ),
            lut_hashes=_validate_hash_mapping(
                mapping["lut_hashes"], "checkpoint.lut_hashes"
            ),
            experiment_elapsed_s=mapping["experiment_elapsed_s"],
            updated_at_utc=mapping["updated_at_utc"],
            temperature=temperature,
            moku=moku,
        )


class AtomicCheckpointStore:
    """Read and atomically replace one runtime checkpoint JSON file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(self, checkpoint: RuntimeCheckpoint) -> Path:
        if not isinstance(checkpoint, RuntimeCheckpoint):
            raise TypeError("checkpoint must be a RuntimeCheckpoint.")
        return atomic_write_json(self.path, checkpoint.to_dict())

    def load(self) -> RuntimeCheckpoint:
        if not self.path.is_file():
            raise FileNotFoundError(f"Runtime checkpoint does not exist: {self.path}")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigurationError(
                f"Could not read runtime checkpoint {self.path}: {error}"
            ) from error
        return RuntimeCheckpoint.from_dict(document)


def validate_resume_hashes(
    checkpoint: RuntimeCheckpoint,
    *,
    configuration_hash: str,
    source_hashes: Mapping[str, str],
    lut_hashes: Mapping[str, str],
) -> None:
    """Reject every silent source, effective-config, or LUT change."""

    _validate_sha256(configuration_hash, "resume configuration_hash")
    expected_sources = _validate_hash_mapping(source_hashes, "resume source_hashes")
    expected_luts = _validate_hash_mapping(lut_hashes, "resume lut_hashes")
    mismatches = []
    if checkpoint.configuration_hash != configuration_hash:
        mismatches.append("effective configuration hash")
    if dict(checkpoint.source_hashes) != expected_sources:
        mismatches.append("source configuration hashes")
    if dict(checkpoint.lut_hashes) != expected_luts:
        mismatches.append("compiled LUT hashes")
    if mismatches:
        raise ResumeConfigurationMismatch(
            "Resume snapshot mismatch: " + ", ".join(mismatches) + "."
        )


def validate_resume_checkpoint(
    checkpoint: RuntimeCheckpoint,
    *,
    configuration_hash: str,
    source_hashes: Mapping[str, str],
    lut_hashes: Mapping[str, str],
) -> None:
    """Validate hashes plus safe independent resume boundaries."""

    validate_resume_hashes(
        checkpoint,
        configuration_hash=configuration_hash,
        source_hashes=source_hashes,
        lut_hashes=lut_hashes,
    )
    if checkpoint.temperature is not None and not checkpoint.temperature.safe_resume_boundary:
        raise ConfigurationError("Temperature checkpoint is not safely resumable.")
    if checkpoint.moku is not None:
        if checkpoint.moku.action_indeterminate:
            raise ConfigurationError(
                "An indeterminate finite Moku action cannot be resumed silently."
            )
        if not checkpoint.moku.safe_resume_boundary:
            raise ConfigurationError("Moku checkpoint is not safely resumable.")
