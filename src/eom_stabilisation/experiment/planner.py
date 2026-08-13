"""Build, display, and persist a hardware-free effective experiment plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import numpy as np
import yaml

from eom_stabilisation.config.models import LoadedExperiment
from eom_stabilisation.moku.models import (
    CompiledWaveformProgram,
    MeasurementPlan,
    OscilloscopeTimebase,
)


LONDON_TIMEZONE = ZoneInfo("Europe/London")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_provenance() -> dict[str, Any]:
    """Return best-effort repository revision metadata without mutating Git."""

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=Path(__file__).resolve().parents[3],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        )
        return {"git_commit": commit, "git_worktree_dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": None, "git_worktree_dirty": None}


def _save_reloadable_source_tree(
    plan: EffectiveExperimentPlan,
    directory: Path,
) -> tuple[Path, dict[str, str]]:
    """Copy configs/assets without changing their relative references.

    The flat ``original_configs`` inventory is convenient for review but cannot
    be loaded safely when a source refers to a sibling file or CSV LUT.  This
    second tree preserves the original relative layout and is therefore the
    only configuration input used by configured resume.
    """

    source_paths = {
        Path(path).resolve() for path in plan.experiment.sources.values()
    }
    if plan.waveform_program is not None:
        source_paths.update(
            waveform.source_path.resolve()
            for waveform in plan.waveform_program.waveforms.values()
            if waveform.source_path is not None
        )
    if not source_paths:
        raise ValueError("An experiment snapshot has no configuration sources.")

    try:
        common_root = Path(
            os.path.commonpath([str(path.parent) for path in source_paths])
        ).resolve()
    except ValueError as error:
        raise ValueError(
            "Configuration sources on different filesystem volumes cannot be "
            "snapshotted into a reloadable resume tree."
        ) from error

    reloadable_root = directory / "reloadable_config"
    file_hashes: dict[str, str] = {}
    destinations: dict[Path, Path] = {}
    for source in sorted(source_paths, key=str):
        relative = source.relative_to(common_root)
        destination = reloadable_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(
                f"Reloadable configuration snapshot already exists: {destination}"
            )
        shutil.copy2(source, destination)
        destinations[source] = destination
        file_hashes[str(destination.relative_to(directory))] = _sha256_file(
            destination
        )

    master_source = Path(plan.experiment.sources["experiment"]).resolve()
    return destinations[master_source], file_hashes


@dataclass(frozen=True)
class EffectiveExperimentPlan:
    """Fully validated configuration, LUTs, actions, and measurements."""

    experiment: LoadedExperiment
    waveform_program: CompiledWaveformProgram | None
    measurement_plans: Mapping[str, MeasurementPlan]
    action_timebases: Mapping[str, OscilloscopeTimebase] = field(default_factory=dict)

    def effective_dict(self) -> dict[str, Any]:
        result = self.experiment.effective_dict()
        result["configuration_hash"] = self.experiment.configuration_hash
        result["configuration_sources"] = {
            role: {
                "path": str(path),
                "sha256": self.experiment.source_hashes[role],
            }
            for role, path in self.experiment.sources.items()
        }
        if self.waveform_program is None:
            result["compiled_waveform_program"] = None
            result["measurement_plans"] = {}
            result["action_timebases"] = {}
            return result

        result["compiled_waveform_program"] = {
            "program_sha256": self.waveform_program.program_sha256,
            "waveforms": {
                name: waveform.summary_dict()
                for name, waveform in self.waveform_program.waveforms.items()
            },
            "actions": [
                {
                    "name": action.name,
                    "waveform": action.waveform_name,
                    "start": dict(action.start),
                    "run": action.run.summary_dict(),
                }
                for action in self.waveform_program.actions
            ],
        }
        result["measurement_plans"] = {
            name: {
                "measurement_plan_sha256": plan.measurement_plan_sha256,
                "waveform_timing_sha256": plan.waveform_timing_sha256,
                "period_s": plan.period_s,
                "trigger_phase_s": plan.trigger_phase_s,
                "raw_only": plan.raw_only,
                "alignment_required": plan.alignment_required,
                "trigger_level_v": plan.trigger_level_v,
                "trigger_edge": plan.trigger_edge,
                "reference_edge_tolerance_s": plan.reference_edge_tolerance_s,
                "maximum_optical_delay_s": plan.maximum_optical_delay_s,
                "minimum_valid_points_per_role": plan.minimum_valid_points_per_role,
                "minimum_optical_edge_snr": plan.minimum_optical_edge_snr,
                "expected_reference_edges": [
                    list(item) for item in plan.expected_reference_edges
                ],
                "windows": [
                    {
                        "name": window.name,
                        "role": window.role,
                        "start_s": window.start_s,
                        "end_s": window.end_s,
                        "source_segment": window.source_segment,
                    }
                    for window in plan.windows
                ],
            }
            for name, plan in self.measurement_plans.items()
        }
        result["action_timebases"] = {
            name: timebase.summary_dict()
            for name, timebase in self.action_timebases.items()
        }
        return result


def _expected_points_by_role(
    measurement_plan: MeasurementPlan,
    *,
    start_s: float,
    end_s: float,
    max_length: int,
    delay_s: float,
) -> dict[str, int]:
    spacing = (end_s - start_s) / max(1, max_length - 1)
    durations: dict[str, float] = {}
    for window in measurement_plan.windows:
        overlap = max(
            0.0,
            min(end_s, window.end_s + delay_s)
            - max(start_s, window.start_s + delay_s),
        )
        durations[window.role] = durations.get(window.role, 0.0) + overlap
    return {
        role: int(math.floor(duration / spacing + 1e-12))
        for role, duration in durations.items()
    }


def _compile_action_timebase(
    *,
    waveform: Any,
    measurement_plan: MeasurementPlan,
    moku_settings: Any,
) -> OscilloscopeTimebase:
    """Choose a unique-cycle frame with enough points for every role."""

    max_length = moku_settings.timebase_max_length
    if moku_settings.timebase_mode == "manual":
        assert moku_settings.timebase_start_s is not None
        assert moku_settings.timebase_end_s is not None
        start = moku_settings.timebase_start_s
        end = moku_settings.timebase_end_s
        counts = _expected_points_by_role(
            measurement_plan,
            start_s=start,
            end_s=end,
            max_length=max_length,
            delay_s=measurement_plan.maximum_optical_delay_s,
        )
        for role in {window.role for window in measurement_plan.windows}:
            if counts.get(role, 0) < measurement_plan.minimum_valid_points_per_role:
                raise ValueError(
                    f"manual Oscilloscope timebase provides only "
                    f"{counts.get(role, 0)} expected points for role {role!r} "
                    f"after maximum optical delay; at least "
                    f"{measurement_plan.minimum_valid_points_per_role} are required"
                )
        return OscilloscopeTimebase(
            mode="manual",
            start_s=start,
            end_s=end,
            max_length=max_length,
            expected_point_interval_s=(end - start) / (max_length - 1),
            expected_points_by_role=counts,
        )

    period = waveform.achieved_period_s
    margin = max(
        waveform.point_interval_s,
        measurement_plan.reference_edge_tolerance_s or waveform.point_interval_s,
    )
    lower_limit = -period + margin
    upper_limit = period - margin
    if lower_limit >= 0 or upper_limit <= 0:
        raise ValueError("automatic timebase has no unique-cycle interval")
    role_widths: dict[str, float] = {}
    for window in measurement_plan.windows:
        role_widths[window.role] = role_widths.get(window.role, 0.0) + (
            window.end_s - window.start_s
        )
    resolution_span = (
        period
        if not role_widths
        else min(
            width
            * (max_length - 1)
            / measurement_plan.minimum_valid_points_per_role
            for width in role_widths.values()
        )
    )
    configured_cap = moku_settings.automatic_timebase_max_duration_s
    span = min(
        upper_limit - lower_limit,
        resolution_span,
        configured_cap if configured_cap is not None else math.inf,
    )
    if span <= 0:
        raise ValueError("automatic timebase maximum duration is not usable")
    end = upper_limit
    start = end - span
    required_non_low = [
        window for window in measurement_plan.windows if window.role != "minimum"
    ] or list(measurement_plan.windows)
    if required_non_low:
        earliest = min(window.start_s for window in required_non_low)
        latest = max(
            window.end_s + measurement_plan.maximum_optical_delay_s
            for window in required_non_low
        )
        if latest - earliest > span:
            raise ValueError(
                "automatic timebase cannot retain required role resolution within "
                "the configured maximum frame duration"
            )
        if earliest < start:
            start = max(lower_limit, earliest)
            end = start + span
        if latest > end:
            end = min(upper_limit, latest)
            start = end - span
    counts = _expected_points_by_role(
        measurement_plan,
        start_s=start,
        end_s=end,
        max_length=max_length,
        delay_s=measurement_plan.maximum_optical_delay_s,
    )
    for role in role_widths:
        if counts.get(role, 0) < measurement_plan.minimum_valid_points_per_role:
            raise ValueError(
                f"automatic timebase can provide only {counts.get(role, 0)} "
                f"expected points for role {role!r}; requested minimum is "
                f"{measurement_plan.minimum_valid_points_per_role}"
            )
    return OscilloscopeTimebase(
        mode="automatic",
        start_s=start,
        end_s=end,
        max_length=max_length,
        expected_point_interval_s=(end - start) / (max_length - 1),
        expected_points_by_role=counts,
    )


def build_effective_plan(experiment: LoadedExperiment) -> EffectiveExperimentPlan:
    """Compile every configured LUT and measurement plan without SDK imports."""

    if experiment.pulse_schedule is None:
        return EffectiveExperimentPlan(experiment, None, {})

    from eom_stabilisation.moku.measurement import compile_measurement_plan
    from eom_stabilisation.moku.waveform_compiler import compile_waveform_program

    pulse = experiment.pulse_schedule
    program = compile_waveform_program(
        pulse.waveforms,
        pulse.actions,
        base_path=pulse.source_path.parent,
    )
    moku_settings = experiment.run_settings.moku
    assert moku_settings is not None
    measurement_settings = experiment.run_settings.measurement
    measurement_plans: dict[str, MeasurementPlan] = {}
    for name, waveform in program.waveforms.items():
        has_roles = any(segment.measurement_role for segment in waveform.segments)
        has_explicit_windows = (
            waveform.requested_parameters.get("measurement_windows") is not None
        )
        # A role-free waveform is deliberately recorded as raw-only. It is
        # never assigned fabricated minimum/high-level semantics.
        raw_only = not has_roles and not has_explicit_windows
        trigger_phase_s: float | None = None
        if moku_settings.trigger_source == "ChannelB":
            level = moku_settings.trigger_level_v
            values = waveform.connector_voltage_v
            following = np.roll(values, -1)
            if moku_settings.trigger_edge == "Rising":
                crossing_indices = np.flatnonzero(
                    (values < level) & (following >= level)
                )
            elif moku_settings.trigger_edge == "Falling":
                crossing_indices = np.flatnonzero(
                    (values > level) & (following <= level)
                )
            else:
                crossing_indices = np.flatnonzero(
                    ((values < level) & (following >= level))
                    | ((values > level) & (following <= level))
                )
            if len(crossing_indices) == 0:
                raise ValueError(
                    f"waveform {name!r} never crosses configured internal "
                    f"trigger level {level:g} V on a "
                    f"{moku_settings.trigger_edge} edge"
                )
            if len(crossing_indices) != 1 and not raw_only:
                raise ValueError(
                    f"waveform {name!r} has {len(crossing_indices)} matching "
                    "internal trigger edges per period; reduced measurement "
                    "windows require exactly one deterministic trigger edge"
                )
            if len(crossing_indices) == 1:
                index = int(crossing_indices[0])
                start_v = float(values[index])
                end_v = float(following[index])
                fraction = (level - start_v) / (end_v - start_v)
                trigger_phase_s = (
                    (index + fraction) * waveform.point_interval_s
                ) % waveform.achieved_period_s
        elif not raw_only:
            raise ValueError(
                f"waveform {name!r} has reduced measurement windows, but "
                "trigger_source ChannelA does not establish deterministic LUT "
                "phase; use the internal ChannelB reference or raw-only capture"
            )
        measurement_plan = compile_measurement_plan(
            waveform,
            raw_only=raw_only,
            trigger_phase_s=trigger_phase_s,
            alignment_required=not raw_only,
            trigger_level_v=moku_settings.trigger_level_v,
            trigger_edge=moku_settings.trigger_edge,
            reference_edge_tolerance_s=(
                measurement_settings.reference_edge_tolerance_s
            ),
            maximum_optical_delay_s=(
                measurement_settings.maximum_optical_delay_s
            ),
            minimum_valid_points_per_role=(
                measurement_settings.minimum_valid_points_per_role
            ),
            minimum_optical_edge_snr=(
                measurement_settings.minimum_optical_edge_snr
            ),
            include_square_low_before=True,
        )
        measurement_plans[name] = measurement_plan
    action_timebases = {
        action.name: _compile_action_timebase(
            waveform=program.waveforms[action.waveform_name],
            measurement_plan=measurement_plans[action.waveform_name],
            moku_settings=moku_settings,
        )
        for action in program.actions
    }
    return EffectiveExperimentPlan(
        experiment,
        program,
        measurement_plans,
        action_timebases,
    )


def render_effective_plan(plan: EffectiveExperimentPlan) -> str:
    """Return a concise operator-readable plan with requested/achieved timing."""

    experiment = plan.experiment
    lines = [
        "Configured experiment plan",
        "==========================",
        f"Name: {experiment.name}",
        f"Components: {', '.join(experiment.components)}",
        f"Completion: {experiment.completion_policy.to_dict()}",
        f"Configuration SHA-256: {experiment.configuration_hash}",
        "Configuration sources:",
    ]
    for role, path in experiment.sources.items():
        lines.append(
            f"- {role}: {path} ({experiment.source_hashes[role]})"
        )

    if experiment.temperature_schedule is not None:
        schedule = experiment.temperature_schedule
        lines.extend(
            [
                "Temperature schedule:",
                f"- {schedule.name}: {len(schedule.stages)} expanded stage(s)",
            ]
        )
        for index, stage in enumerate(schedule.stages):
            target = "output OFF" if stage.target_c is None else f"{stage.target_c:g} C"
            lines.append(
                f"  {index}: {stage.name} -> {target}; hold "
                f"{stage.hold_duration_s:g} s after stability"
            )

    if plan.waveform_program is not None:
        lines.append("Compiled waveforms:")
        for waveform in plan.waveform_program.waveforms.values():
            lines.append(
                f"- {waveform.name}: {waveform.waveform_type.value}; "
                f"period {waveform.requested_period_s:.9g} s requested / "
                f"{waveform.achieved_period_s:.9g} s achieved; "
                f"{waveform.point_count} points at {waveform.sample_rate_name}; "
                f"LUT {waveform.lut_sha256}"
            )
        lines.append("Waveform actions:")
        for index, action in enumerate(plan.waveform_program.actions):
            lines.append(
                f"- {index}: {action.name} -> {action.waveform_name}; "
                f"start={dict(action.start)}; run={action.run.summary_dict()}; "
                f"timebase={plan.action_timebases[action.name].summary_dict()}"
            )
    else:
        lines.append("Compiled waveforms: none")
    lines.append("All voltages above are requested Moku connector voltages.")
    return "\n".join(lines)


def make_artifact_directory(root: Path, experiment_name: str) -> Path:
    """Create one unique UTC/local-labelled artifact directory."""

    now = datetime.now(LONDON_TIMEZONE)
    base_name = f"{now:%Y%m%d_%H%M%S}_{experiment_name}"
    candidate = root / base_name
    suffix = 1
    while candidate.exists():
        candidate = root / f"{base_name}_{suffix:02d}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def _save_waveform_preview(waveform: Any, path: Path) -> None:
    # Matplotlib is intentionally imported only for an explicit artifact
    # operation, never merely to load or validate configuration.
    import matplotlib.pyplot as plt

    time_s = np.arange(waveform.point_count, dtype=float) * waveform.point_interval_s
    figure, axis = plt.subplots(figsize=(9, 3.5))
    axis.step(time_s, waveform.connector_voltage_v, where="post", linewidth=1.0)
    axis.set_xlabel("Time within achieved cycle (s)")
    axis.set_ylabel("Requested connector voltage (V)")
    axis.set_title(f"{waveform.name}: compiled digital LUT preview")
    axis.grid(True, alpha=0.25)
    figure.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.stem + ".tmp" + path.suffix)
    figure.savefig(temporary, dpi=160)
    plt.close(figure)
    temporary.replace(path)


def _timeline_rows(plan: EffectiveExperimentPlan) -> list[dict[str, Any]]:
    if plan.waveform_program is None:
        return []
    rows = []
    for index, action in enumerate(plan.waveform_program.actions):
        rows.append(
            {
                "action_index": index,
                "action_name": action.name,
                "waveform_name": action.waveform_name,
                "start": json.dumps(dict(action.start), sort_keys=True),
                "run_mode": action.run.mode.value,
                "repeat_count": action.run.repeat_count,
                "requested_duration_s": action.run.requested_duration_s,
                "achieved_duration_s": action.run.achieved_duration_s,
                "exact_hardware_burst": action.run.exact_hardware_burst,
            }
        )
    return rows


def _save_timeline(plan: EffectiveExperimentPlan, directory: Path) -> None:
    import csv

    rows = _timeline_rows(plan)
    field_names = (
        "action_index",
        "action_name",
        "waveform_name",
        "start",
        "run_mode",
        "repeat_count",
        "requested_duration_s",
        "achieved_duration_s",
        "exact_hardware_burst",
    )
    csv_path = directory / "waveform_timeline.csv"
    temporary = csv_path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=field_names)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(csv_path)

    if not rows:
        return
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(9, max(2.5, 0.55 * len(rows) + 1.5)))
    labels = [f"{row['action_index']}: {row['action_name']}" for row in rows]
    widths = [
        row["achieved_duration_s"]
        if row["achieved_duration_s"] is not None
        else 1.0
        for row in rows
    ]
    axis.barh(range(len(rows)), widths, color="#4472C4")
    axis.set_yticks(range(len(rows)), labels)
    axis.invert_yaxis()
    axis.set_xlabel("Achieved duration (s); unit width means event/unbounded")
    axis.set_title("Waveform action timeline (separate from measurement plots)")
    figure.tight_layout()
    output_path = directory / "waveform_timeline.png"
    temporary_image = directory / "waveform_timeline.tmp.png"
    figure.savefig(temporary_image, dpi=160)
    plt.close(figure)
    temporary_image.replace(output_path)


def save_plan_artifacts(plan: EffectiveExperimentPlan, directory: Path) -> Path:
    """Persist immutable inputs, effective YAML, LUTs, hashes, and previews."""

    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    original_directory = directory / "original_configs"
    original_directory.mkdir(exist_ok=True)
    copied_sources: dict[str, dict[str, str]] = {}
    for role, source in plan.experiment.sources.items():
        destination = original_directory / f"{role}_{source.name}"
        shutil.copy2(source, destination)
        copied_sources[role] = {
            "source_path": str(source),
            "snapshot_path": str(destination.relative_to(directory)),
            "sha256": _sha256_file(destination),
        }

    reloadable_master, reloadable_hashes = _save_reloadable_source_tree(
        plan,
        directory,
    )

    effective = plan.effective_dict()
    effective_path = directory / "effective_experiment.yaml"
    _atomic_write_text(
        effective_path,
        yaml.safe_dump(effective, sort_keys=False, allow_unicode=True),
    )
    analysis_profile_path = directory / "analysis_profile.json"
    _atomic_write_json(
        analysis_profile_path,
        plan.experiment.run_settings.measurement.to_dict(),
    )
    _atomic_write_json(
        directory / "configuration_hashes.json",
        {
            "configuration_hash": plan.experiment.configuration_hash,
            "effective_experiment_sha256": _sha256_file(effective_path),
            "analysis_profile_sha256": _sha256_file(analysis_profile_path),
            "reloadable_experiment_file": str(
                reloadable_master.relative_to(directory)
            ),
            "reloadable_files": reloadable_hashes,
            "sources": copied_sources,
        },
    )

    if plan.waveform_program is not None:
        waveform_directory = directory / "waveforms"
        waveform_directory.mkdir(exist_ok=True)
        program_summary = effective["compiled_waveform_program"]
        _atomic_write_json(directory / "waveform_program.json", program_summary)
        lut_hashes: dict[str, str] = {}
        for name, waveform in plan.waveform_program.waveforms.items():
            lut_path = waveform_directory / f"{name}.npy"
            temporary_lut = waveform_directory / f"{name}.tmp.npy"
            np.save(temporary_lut, waveform.normalized_lut, allow_pickle=False)
            temporary_lut.replace(lut_path)
            file_hash = _sha256_file(lut_path)
            if waveform.source_path is not None:
                asset_destination = original_directory / (
                    f"waveform_asset_{name}_{waveform.source_path.name}"
                )
                shutil.copy2(waveform.source_path, asset_destination)
            lut_hashes[name] = file_hash
            _save_waveform_preview(
                waveform,
                waveform_directory / f"{name}_preview.png",
            )
        _atomic_write_json(
            directory / "lut_hashes.json",
            {
                "program_sha256": plan.waveform_program.program_sha256,
                "files": lut_hashes,
            },
        )
    else:
        _atomic_write_json(directory / "waveform_program.json", None)
        _atomic_write_json(directory / "lut_hashes.json", {"files": {}})

    _save_timeline(plan, directory)
    now_utc = datetime.now(timezone.utc)
    manifest = {
        "format": "eom_configured_experiment",
        "version": 1,
        "status": "prepared",
        "name": plan.experiment.name,
        "run_directory": str(directory),
        "configuration_hash": plan.experiment.configuration_hash,
        "program_sha256": (
            None
            if plan.waveform_program is None
            else plan.waveform_program.program_sha256
        ),
        "prepared_timestamp_utc": now_utc.isoformat(),
        "prepared_timestamp_local": now_utc.astimezone(LONDON_TIMEZONE).isoformat(),
        "effective_experiment_file": "effective_experiment.yaml",
        "configuration_hashes_file": "configuration_hashes.json",
        "lut_hashes_file": "lut_hashes.json",
        "waveform_program_file": "waveform_program.json",
        "analysis_profile_file": "analysis_profile.json",
        "reloadable_experiment_file": str(
            reloadable_master.relative_to(directory)
        ),
        "runtime_checkpoint_file": "runtime_checkpoint.json",
        **_git_provenance(),
    }
    _atomic_write_json(directory / "experiment_manifest.json", manifest)
    return directory
