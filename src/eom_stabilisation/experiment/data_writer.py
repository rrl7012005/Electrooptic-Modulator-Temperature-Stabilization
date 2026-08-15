"""Atomic configured-run sample, provenance, raw-frame, and TEC writers."""

from __future__ import annotations

import csv
from datetime import datetime
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence
import uuid

import numpy as np


def _atomic_csv(
    path: Path, fields: Sequence[str], rows: Iterable[Mapping[str, Any]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)
        output.flush()
    temporary.replace(path)


def _load_csv(path: Path, fields: Sequence[str]) -> list[dict[str, str]]:
    """Load an atomically written table and reject any schema drift."""

    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != tuple(fields):
            raise ValueError(
                f"Existing output schema differs from the configured schema: {path}"
            )
        rows = list(reader)
    for index, row in enumerate(rows, start=2):
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"Malformed interior record at {path}:{index}.")
    return rows


def _validate_elapsed_order(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    previous = -math.inf
    for index, row in enumerate(rows, start=2):
        try:
            elapsed_s = float(row["elapsed_s"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid elapsed_s at {path}:{index}.") from error
        if not math.isfinite(elapsed_s) or elapsed_s < 0:
            raise ValueError(f"Invalid elapsed_s at {path}:{index}.")
        if elapsed_s < previous:
            raise ValueError(f"Elapsed time moved backwards at {path}:{index}.")
        previous = elapsed_s


def _validate_utc_timestamp(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} must be a timezone-aware UTC timestamp.")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValueError(f"{context} is not a valid ISO timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{context} must be timezone-aware.")
    return value


def _role_column(role: str) -> str:
    if role == "minimum":
        return "minimum_voltage"
    if role == "high_level":
        return "high_level_voltage"
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", role).strip("_").lower()
    if not safe:
        raise ValueError(f"Measurement role has no usable column name: {role!r}")
    return f"{safe}_voltage"


class ConfiguredMokuDataWriter:
    """Keep canonical measurements and full independent-state provenance aligned."""

    BASE_SAMPLE_FIELDS = (
        "sample_id",
        "wall_time",
        "timestamp_utc",
        "timestamp_local",
        "elapsed_s",
    )
    PROVENANCE_FIELDS = BASE_SAMPLE_FIELDS + (
        "temperature_stage_index",
        "temperature_stage_name",
        "temperature_phase",
        "moku_action_index",
        "moku_action_name",
        "waveform_name",
        "waveform_run_id",
        "waveform_session_id",
        "runtime_session_id",
        "runtime_process_id",
        "runtime_waveform_run_id",
        "first_sample_after_waveform_change",
        "first_sample_after_action_change",
        "first_sample_after_session_change",
        "waveform_phase_continuity",
        "measurement_profile",
        "measurement_plan_sha256",
        "waveform_timing_sha256",
        "measurement_point_counts_json",
        "count_chunk_index",
        "count_chunk_size",
        "delivered_lower_bound",
        "delivered_upper_bound",
    )
    RAW_INDEX_FIELDS = PROVENANCE_FIELDS + (
        "frame_id",
        "frame_timestamp_utc",
        "frame_status",
        "rejection_reason",
        "timebase_mode",
        "timebase_start_s",
        "timebase_end_s",
        "actual_frame_start_s",
        "actual_frame_end_s",
        "actual_frame_point_count",
        "actual_frame_point_interval_s",
        "trigger_source",
        "trigger_level_v",
        "trigger_edge",
        "raw_trace_file",
    )
    ALIGNMENT_FIELDS = (
        "sample_id",
        "frame_id",
        "frame_timestamp_utc",
        "sample_timestamp_utc",
        "timestamp_local",
        "elapsed_s",
        "temperature_stage_index",
        "temperature_stage_name",
        "temperature_phase",
        "moku_action_index",
        "moku_action_name",
        "waveform_name",
        "waveform_run_id",
        "waveform_session_id",
        "runtime_session_id",
        "runtime_process_id",
        "runtime_waveform_run_id",
        "measurement_plan_sha256",
        "waveform_timing_sha256",
        "channel_b_edge_time_s",
        "optical_delay_s",
        "alignment_quality",
        "selected_points_by_role_json",
        "finite_points_by_role_json",
        "rejected_points_by_role_json",
        "accepted",
        "rejection_reason",
    )

    def __init__(
        self,
        run_directory: str | Path,
        roles: Iterable[str],
        *,
        resume: bool = False,
    ) -> None:
        root = Path(run_directory)
        self.directory = root / "moku"
        self.samples_path = self.directory / "moku_samples.csv"
        self.provenance_path = self.directory / "moku_sample_provenance.csv"
        self.raw_index_path = self.directory / "raw_trace_index.csv"
        self.alignment_path = self.directory / "measurement_alignment.csv"
        unique_roles = tuple(sorted(set(roles)))
        columns = [_role_column(role) for role in unique_roles]
        if len(set(columns)) != len(columns):
            raise ValueError(
                "Measurement role names collide after column normalization."
            )
        self.role_columns = dict(zip(unique_roles, columns))
        self.sample_fields = self.BASE_SAMPLE_FIELDS + tuple(columns)
        if not resume:
            for path in (
                self.samples_path,
                self.provenance_path,
                self.raw_index_path,
                self.alignment_path,
            ):
                if path.exists():
                    raise FileExistsError(f"Refusing to overwrite raw output: {path}")
        self.samples = (
            _load_csv(self.samples_path, self.sample_fields) if resume else []
        )
        self.provenance = (
            _load_csv(self.provenance_path, self.PROVENANCE_FIELDS) if resume else []
        )
        self.raw_index = (
            _load_csv(self.raw_index_path, self.RAW_INDEX_FIELDS)
            if resume and self.raw_index_path.exists()
            else []
        )
        self.alignment = (
            _load_csv(self.alignment_path, self.ALIGNMENT_FIELDS)
            if resume and self.alignment_path.exists()
            else []
        )
        if len(self.samples) != len(self.provenance):
            raise ValueError("Moku samples and provenance row counts do not match.")
        self._validate_existing_outputs(resume=resume)

    def _validate_existing_outputs(self, *, resume: bool) -> None:
        _validate_elapsed_order(self.samples_path, self.samples)
        _validate_elapsed_order(self.provenance_path, self.provenance)
        _validate_elapsed_order(self.raw_index_path, self.raw_index)
        _validate_elapsed_order(self.alignment_path, self.alignment)
        for index, (sample, provenance) in enumerate(
            zip(self.samples, self.provenance),
            start=2,
        ):
            for field in self.BASE_SAMPLE_FIELDS:
                if sample[field] != provenance[field]:
                    raise ValueError(
                        "Moku sample/provenance clock mismatch at "
                        f"row {index}, field {field}."
                    )

        trace_directory = self.directory / "raw_traces"
        indexed_paths: set[Path] = set()
        for index, row in enumerate(self.raw_index, start=1):
            relative = Path(row["raw_trace_file"])
            destination = (self.directory / relative).resolve()
            if relative.is_absolute() or not destination.is_relative_to(
                self.directory.resolve()
            ):
                raise ValueError(
                    f"Raw trace index path escapes output directory: {relative}"
                )
            expected = (trace_directory / f"frame_{index:08d}.npz").resolve()
            if destination != expected:
                raise ValueError(
                    "Raw trace index is not contiguous at "
                    f"row {index + 1}: {relative}"
                )
            if not destination.is_file():
                raise ValueError(f"Indexed raw trace is missing: {destination}")
            if destination in indexed_paths:
                raise ValueError(f"Raw trace is indexed more than once: {destination}")
            indexed_paths.add(destination)

        existing_paths = (
            {path.resolve() for path in trace_directory.rglob("*.npz")}
            if trace_directory.is_dir()
            else set()
        )
        if existing_paths != indexed_paths:
            extras = sorted(str(path) for path in existing_paths - indexed_paths)
            if extras:
                label = "resume" if resume else "new run"
                raise FileExistsError(
                    f"Unindexed raw trace exists during {label}: {extras[0]}"
                )
            missing = sorted(str(path) for path in indexed_paths - existing_paths)
            raise ValueError(f"Indexed raw trace is missing: {missing[0]}")

    @property
    def latest_elapsed_s(self) -> float | None:
        """Return the latest persisted valid-sample elapsed time."""

        rows = [*self.provenance, *self.raw_index]
        if not rows:
            return None
        return max(float(row["elapsed_s"]) for row in rows)

    @property
    def last_valid_sample_timestamp_utc(self) -> str | None:
        """Return the timestamp of the latest persisted measurement/raw frame."""

        rows = [*self.provenance, *self.raw_index]
        if not rows:
            return None
        row = max(rows, key=lambda item: float(item["elapsed_s"]))
        return _validate_utc_timestamp(
            row["timestamp_utc"],
            context="latest Moku output timestamp_utc",
        )

    def validate_resume_position(
        self,
        *,
        checkpoint_timestamp_utc: str | None,
        checkpoint_elapsed_s: float,
    ) -> None:
        """Reject output data that is missing from or ahead of the checkpoint."""

        persisted_timestamp = self.last_valid_sample_timestamp_utc
        if persisted_timestamp != checkpoint_timestamp_utc:
            raise ValueError(
                "Moku output/checkpoint last-valid timestamp mismatch; refusing resume."
            )
        latest_elapsed_s = self.latest_elapsed_s
        if (
            latest_elapsed_s is not None
            and latest_elapsed_s > float(checkpoint_elapsed_s) + 1e-9
        ):
            raise ValueError(
                "Moku output extends beyond the runtime checkpoint; refusing resume."
            )

    @staticmethod
    def _time_fields(clock: Mapping[str, Any]) -> dict[str, Any]:
        local = str(clock["timestamp_local"])
        utc_text = str(clock["timestamp_utc"])
        parsed_utc = datetime.fromisoformat(
            utc_text[:-1] + "+00:00" if utc_text.endswith("Z") else utc_text
        )
        if parsed_utc.tzinfo is None:
            raise ValueError("Configured sample timestamp_utc must be timezone-aware.")
        return {
            # Numeric epoch seconds preserve compatibility with the historical
            # analyser while the two explicit ISO columns retain time zones.
            "wall_time": parsed_utc.timestamp(),
            "timestamp_utc": utc_text,
            "timestamp_local": local,
            "elapsed_s": float(clock["elapsed_s"]),
        }

    def record_measurement(
        self,
        *,
        clock: Mapping[str, Any],
        values_by_role: Mapping[str, float],
        point_counts_by_role: Mapping[str, int],
        state: Mapping[str, Any],
        runtime_session_id: int,
        sample_id: str | None = None,
        runtime_process_id: str = "legacy",
        runtime_waveform_run_id: int | None = None,
    ) -> None:
        """Add exactly one sample and one matching provenance row."""

        unknown = set(values_by_role) - set(self.role_columns)
        if unknown:
            raise ValueError(f"Measurement returned unknown role(s): {sorted(unknown)}")
        sample_id = sample_id or str(uuid.uuid4())
        time_fields = self._time_fields(clock)
        sample: dict[str, Any] = {"sample_id": sample_id, **time_fields}
        for role, column in self.role_columns.items():
            sample[column] = values_by_role.get(role, "")
        provenance = {
            "sample_id": sample_id,
            **time_fields,
            **{
                field: state.get(field)
                for field in self.PROVENANCE_FIELDS
                if field not in time_fields and field != "sample_id"
            },
            "runtime_session_id": int(runtime_session_id),
            "runtime_process_id": runtime_process_id,
            "runtime_waveform_run_id": runtime_waveform_run_id,
            "measurement_point_counts_json": json.dumps(
                dict(point_counts_by_role), sort_keys=True, allow_nan=False
            ),
        }
        self.samples.append(sample)
        self.provenance.append(provenance)

    def record_raw_trace(
        self,
        *,
        clock: Mapping[str, Any],
        frame: Mapping[str, Any],
        state: Mapping[str, Any],
        runtime_session_id: int,
        sample_id: str | None = None,
        frame_id: str | None = None,
        runtime_process_id: str = "legacy",
        runtime_waveform_run_id: int | None = None,
        frame_timestamp_utc: str | None = None,
        frame_status: str = "accepted",
        rejection_reason: str | None = None,
        timebase: Any | None = None,
        trigger: Mapping[str, Any] | None = None,
    ) -> Path:
        """Write one immutable raw trace plus self-contained frame provenance."""

        try:
            time_axis = np.asarray(frame["time"], dtype=float)
            photodiode_value = (
                frame["photodiode_v"] if "photodiode_v" in frame else frame["ch1"]
            )
            photodiode = np.asarray(photodiode_value, dtype=float)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                "Raw frame requires numeric time and photodiode/ch1."
            ) from error
        reference_value = frame.get("waveform_reference_v", frame.get("ch2"))
        reference = (
            np.asarray([], dtype=float)
            if reference_value is None
            else np.asarray(reference_value, dtype=float)
        )
        if time_axis.ndim != 1 or photodiode.shape != time_axis.shape:
            raise ValueError(
                "Raw time and photodiode traces must be equal-length vectors."
            )
        if reference.size and reference.shape != time_axis.shape:
            raise ValueError("Raw waveform-reference trace length does not match time.")
        trace_directory = self.directory / "raw_traces"
        trace_directory.mkdir(parents=True, exist_ok=True)
        index = len(self.raw_index) + 1
        destination = trace_directory / f"frame_{index:08d}.npz"
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite raw trace: {destination}")
        temporary = trace_directory / f"frame_{index:08d}.tmp.npz"
        sample_id = sample_id or str(uuid.uuid4())
        frame_id = frame_id or str(uuid.uuid4())
        time_fields = self._time_fields(clock)
        actual_spacing = (
            float(np.median(np.diff(time_axis))) if len(time_axis) > 1 else None
        )
        trigger_values = dict(trigger or {})
        metadata = {
            "sample_id": sample_id,
            "frame_id": frame_id,
            "frame_timestamp_utc": frame_timestamp_utc,
            "frame_status": frame_status,
            "rejection_reason": rejection_reason,
            "runtime_session_id": int(runtime_session_id),
            "runtime_process_id": runtime_process_id,
            "runtime_waveform_run_id": runtime_waveform_run_id,
            "measurement_plan_sha256": state.get("measurement_plan_sha256"),
            "waveform_timing_sha256": state.get("waveform_timing_sha256"),
            "timebase": None if timebase is None else timebase.summary_dict(),
            "trigger": trigger_values,
            "state": dict(state),
            **time_fields,
        }
        np.savez_compressed(
            temporary,
            time_s=time_axis,
            photodiode_v=photodiode,
            waveform_reference_v=reference,
            metadata_json=np.asarray(
                json.dumps(metadata, sort_keys=True, allow_nan=False)
            ),
        )
        temporary.replace(destination)
        self.raw_index.append(
            {
                "sample_id": sample_id,
                **time_fields,
                **{
                    field: state.get(field)
                    for field in self.PROVENANCE_FIELDS
                    if field not in time_fields and field != "sample_id"
                },
                "runtime_session_id": int(runtime_session_id),
                "runtime_process_id": runtime_process_id,
                "runtime_waveform_run_id": runtime_waveform_run_id,
                "measurement_point_counts_json": "{}",
                "frame_id": frame_id,
                "frame_timestamp_utc": frame_timestamp_utc
                or time_fields["timestamp_utc"],
                "frame_status": frame_status,
                "rejection_reason": rejection_reason,
                "timebase_mode": None if timebase is None else timebase.mode,
                "timebase_start_s": None if timebase is None else timebase.start_s,
                "timebase_end_s": None if timebase is None else timebase.end_s,
                "actual_frame_start_s": (
                    None if len(time_axis) == 0 else float(time_axis[0])
                ),
                "actual_frame_end_s": (
                    None if len(time_axis) == 0 else float(time_axis[-1])
                ),
                "actual_frame_point_count": len(time_axis),
                "actual_frame_point_interval_s": actual_spacing,
                "trigger_source": trigger_values.get("source"),
                "trigger_level_v": trigger_values.get("level_v"),
                "trigger_edge": trigger_values.get("edge"),
                "raw_trace_file": str(destination.relative_to(self.directory)),
            }
        )
        return destination

    def record_alignment(
        self,
        *,
        clock: Mapping[str, Any],
        state: Mapping[str, Any],
        runtime_session_id: int,
        diagnostics: Any,
        frame_timestamp_utc: str | None = None,
        sample_id: str | None = None,
        frame_id: str | None = None,
        runtime_process_id: str = "legacy",
        runtime_waveform_run_id: int | None = None,
    ) -> None:
        """Append one accepted or rejected per-frame alignment sidecar row."""

        time_fields = self._time_fields(clock)
        self.alignment.append(
            {
                "sample_id": sample_id or str(uuid.uuid4()),
                "frame_id": frame_id or str(uuid.uuid4()),
                "frame_timestamp_utc": frame_timestamp_utc
                or time_fields["timestamp_utc"],
                "sample_timestamp_utc": time_fields["timestamp_utc"],
                "timestamp_local": time_fields["timestamp_local"],
                "elapsed_s": time_fields["elapsed_s"],
                "temperature_stage_index": state.get("temperature_stage_index"),
                "temperature_stage_name": state.get("temperature_stage_name"),
                "temperature_phase": state.get("temperature_phase"),
                "moku_action_index": state.get("moku_action_index"),
                "moku_action_name": state.get("moku_action_name"),
                "waveform_name": state.get("waveform_name"),
                "waveform_run_id": state.get("waveform_run_id"),
                "waveform_session_id": state.get("waveform_session_id"),
                "runtime_session_id": int(runtime_session_id),
                "runtime_process_id": runtime_process_id,
                "runtime_waveform_run_id": runtime_waveform_run_id,
                "measurement_plan_sha256": state.get("measurement_plan_sha256"),
                "waveform_timing_sha256": state.get("waveform_timing_sha256"),
                "channel_b_edge_time_s": diagnostics.channel_b_edge_time_s,
                "optical_delay_s": diagnostics.optical_delay_s,
                "alignment_quality": diagnostics.alignment_quality,
                "selected_points_by_role_json": json.dumps(
                    dict(diagnostics.selected_points_by_role),
                    sort_keys=True,
                    allow_nan=False,
                ),
                "finite_points_by_role_json": json.dumps(
                    dict(diagnostics.finite_points_by_role),
                    sort_keys=True,
                    allow_nan=False,
                ),
                "rejected_points_by_role_json": json.dumps(
                    dict(diagnostics.rejected_points_by_role),
                    sort_keys=True,
                    allow_nan=False,
                ),
                "accepted": bool(diagnostics.accepted),
                "rejection_reason": diagnostics.rejection_reason,
            }
        )

    def flush(self) -> None:
        """Atomically replace each complete table; never expose partial rows."""

        _atomic_csv(self.samples_path, self.sample_fields, self.samples)
        _atomic_csv(self.provenance_path, self.PROVENANCE_FIELDS, self.provenance)
        if self.raw_index:
            _atomic_csv(self.raw_index_path, self.RAW_INDEX_FIELDS, self.raw_index)
        if self.alignment:
            _atomic_csv(self.alignment_path, self.ALIGNMENT_FIELDS, self.alignment)


class TecDataWriter:
    """Atomic machine-readable TEC log with explicit failed-read records."""

    FIELDS = (
        "timestamp_utc",
        "timestamp_local",
        "elapsed_s",
        "stage_index",
        "stage_name",
        "schedule_state",
        "requested_target_c",
        "active_target_c",
        "object_temperature_c",
        "sink_temperature_c",
        "output_current_a",
        "output_voltage_v",
        "temperature_stable",
        "controller_status",
        "error_message",
    )

    def __init__(self, run_directory: str | Path, *, resume: bool = False) -> None:
        self.path = Path(run_directory) / "temperature" / "tec_log.csv"
        if self.path.exists() and not resume:
            raise FileExistsError(f"Refusing to overwrite raw output: {self.path}")
        self.rows = _load_csv(self.path, self.FIELDS) if resume else []
        _validate_elapsed_order(self.path, self.rows)

    @property
    def latest_elapsed_s(self) -> float | None:
        if not self.rows:
            return None
        return float(self.rows[-1]["elapsed_s"])

    def validate_resume_position(self, *, checkpoint_elapsed_s: float) -> None:
        latest = self.latest_elapsed_s
        if latest is not None and latest > float(checkpoint_elapsed_s) + 1e-9:
            raise ValueError(
                "TEC output extends beyond the runtime checkpoint; refusing resume."
            )

    def record(
        self,
        *,
        clock: Mapping[str, Any],
        state: Mapping[str, Any],
        snapshot: Any | None,
        error: BaseException | None = None,
    ) -> None:
        row = {
            "timestamp_utc": clock["timestamp_utc"],
            "timestamp_local": clock["timestamp_local"],
            "elapsed_s": clock["elapsed_s"],
            "stage_index": state.get("stage_index"),
            "stage_name": state.get("stage_name"),
            "schedule_state": state.get("phase"),
            "requested_target_c": state.get("requested_target_c"),
            "active_target_c": None if snapshot is None else snapshot.active_target_c,
            "object_temperature_c": None
            if snapshot is None
            else snapshot.object_temperature_c,
            "sink_temperature_c": None
            if snapshot is None
            else snapshot.sink_temperature_c,
            "output_current_a": None if snapshot is None else snapshot.output_current_a,
            "output_voltage_v": None if snapshot is None else snapshot.output_voltage_v,
            "temperature_stable": None
            if snapshot is None
            else snapshot.temperature_stable,
            "controller_status": None
            if snapshot is None
            else snapshot.controller_status,
            "error_message": (
                f"{type(error).__name__}: {error}"
                if error is not None
                else None
                if snapshot is None
                else snapshot.error_message
            ),
        }
        self.rows.append(row)

    def flush(self) -> None:
        _atomic_csv(self.path, self.FIELDS, self.rows)


__all__ = ["ConfiguredMokuDataWriter", "TecDataWriter"]
