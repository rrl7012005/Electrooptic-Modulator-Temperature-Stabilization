"""Configuration-driven command entry points.

Loading, compilation, and preview paths are deliberately SDK-free.  Hardware
modules are imported only after an explicit ``--execute`` request, a complete
snapshot has been written, and the operator has confirmed the effective plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

from eom_stabilisation.config import ConfigurationError, load_experiment
from eom_stabilisation.experiment.checkpoint import (
    AtomicCheckpointStore,
    RuntimeCheckpoint,
    validate_resume_checkpoint,
)
from eom_stabilisation.experiment.planner import (
    EffectiveExperimentPlan,
    build_effective_plan,
    make_artifact_directory,
    render_effective_plan,
    save_plan_artifacts,
)
from eom_stabilisation.run_store import atomic_write_json, sha256_file


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONFIGURED_MANIFEST_FORMAT = "eom_configured_experiment"
_PLACEHOLDER_MARKERS = (
    "xxxxxx",
    "hostname_or_ip",
    "replace_me",
)
_PLACEHOLDER_VALUES = {
    "com_port",
    "com_test",
    "red_pitaya_hostname_or_ip",
}


@dataclass(frozen=True)
class ConfiguredResumeContext:
    """Validated immutable inputs for one configured resume attempt."""

    manifest_path: Path
    run_directory: Path
    plan: EffectiveExperimentPlan
    checkpoint: RuntimeCheckpoint
    resume_plan: Mapping[str, Any]
    gap_minutes: float


def _is_public_placeholder(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().casefold()
    return normalized in _PLACEHOLDER_VALUES or any(
        marker in normalized for marker in _PLACEHOLDER_MARKERS
    )


def _hardware_readiness_errors(plan: EffectiveExperimentPlan) -> tuple[str, ...]:
    """Return configuration omissions that must block real connections.

    Published examples intentionally remain valid for dry-run and preview, but
    their placeholders and absent apparatus-specific plausibility bounds must
    never reach a hardware adapter.
    """

    experiment = plan.experiment
    components = set(experiment.components)
    settings = experiment.run_settings
    errors: list[str] = []

    if "moku" in components:
        moku = settings.moku
        if moku is None:
            errors.append("the Moku component has no run_settings.moku mapping")
        else:
            for field_name in ("address", "fallback_address"):
                value = getattr(moku, field_name)
                if value is not None and _is_public_placeholder(value):
                    errors.append(
                        f"run_settings.moku.{field_name} is a public placeholder"
                    )

    if {"temp-control", "temp-log"} & components:
        temperature = settings.temperature
        if temperature is None:
            errors.append(
                "the TEC component has no run_settings.temperature mapping"
            )
        else:
            if _is_public_placeholder(temperature.serial_port):
                errors.append(
                    "run_settings.temperature.serial_port is a public placeholder"
                )
            bound_fields = (
                "object_temperature_min_c",
                "object_temperature_max_c",
                "sink_temperature_min_c",
                "sink_temperature_max_c",
            )
            missing_bounds = [
                field_name
                for field_name in bound_fields
                if getattr(temperature, field_name, None) is None
            ]
            if missing_bounds:
                errors.append(
                    "run_settings.temperature must supply apparatus-verified "
                    "sensor plausibility bounds before execution: "
                    + ", ".join(missing_bounds)
                )

    if "lock" in components:
        linien = getattr(settings, "linien", None)
        if linien is None:
            errors.append("the lock component requires run_settings.linien.host")
        elif _is_public_placeholder(getattr(linien, "host", None)):
            errors.append("run_settings.linien.host is a public placeholder")

    return tuple(errors)


def _report_hardware_readiness_errors(plan: EffectiveExperimentPlan) -> bool:
    errors = _hardware_readiness_errors(plan)
    if not errors:
        return True
    print(
        "\nERROR: Real execution is blocked until these apparatus-specific "
        "settings are supplied and verified:",
        file=sys.stderr,
    )
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    print(
        "Dry-run and preview remain available without opening hardware.",
        file=sys.stderr,
    )
    return False


def _confirm_configured_execution() -> bool:
    """Require a configuration-specific confirmation immediately pre-connect."""

    try:
        response = input(
            "\nReview the saved effective plan, connector voltages, routing, "
            "TEC limits, and apparatus. Type EXECUTE to permit hardware "
            "connections, or CANCEL to stop: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled; no hardware connection was opened.")
        return False
    if response == "EXECUTE":
        return True
    print("Cancelled; no hardware connection was opened.")
    return False


def _confirm_configured_resume() -> bool:
    """Require an explicit resume word immediately before hardware imports."""

    try:
        response = input(
            "\nReview the saved resume plan and current apparatus state. "
            "Type RESUME to permit hardware connections, or CANCEL to stop: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled; no hardware connection was opened.")
        return False
    if response == "RESUME":
        return True
    print("Cancelled; no hardware connection was opened.")
    return False


def _confirm_stale_configured_resume(
    gap_minutes: float,
    warning_minutes: float,
) -> bool:
    """Require a separate acknowledgement for stale physical state."""

    if gap_minutes <= warning_minutes:
        return True
    print(
        "WARNING: The configured checkpoint is "
        f"{gap_minutes:.1f} minutes old, beyond the configured "
        f"{warning_minutes:g}-minute resume window. The optical lock and "
        "thermal state may no longer match the saved run."
    )
    try:
        response = input(
            "Type CONTINUE to acknowledge this risk, or CANCEL to stop: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled; no hardware connection was opened.")
        return False
    if response == "CONTINUE":
        return True
    print("Cancelled; no hardware connection was opened.")
    return False


def _read_json_object(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Could not read {context} {path}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigurationError(f"{context} must contain a JSON object: {path}")
    return value


def _artifact_file(run_directory: Path, value: Any, context: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ConfigurationError(f"{context} must be a non-empty relative path.")
    path = (run_directory / value).resolve()
    if not path.is_relative_to(run_directory):
        raise ConfigurationError(f"{context} escapes the saved run directory.")
    if not path.is_file():
        raise FileNotFoundError(f"Saved {context} does not exist: {path}")
    return path


def _verify_saved_hash(path: Path, expected: Any, context: str) -> str:
    if not isinstance(expected, str) or len(expected) != 64:
        raise ConfigurationError(f"{context} does not contain a SHA-256 digest.")
    actual = sha256_file(path)
    if actual != expected:
        raise ConfigurationError(
            f"Saved {context} hash mismatch for {path}; refusing resume."
        )
    return actual


def _process_is_running(pid: Any) -> bool:
    """Return whether a recorded PID still exists without changing it."""

    try:
        numeric_pid = int(pid)
    except (TypeError, ValueError):
        return False
    if numeric_pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {numeric_pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and f'"{numeric_pid}"' in result.stdout
    try:
        os.kill(numeric_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _reject_active_recorded_processes(manifest: Mapping[str, Any]) -> None:
    processes = manifest.get("processes")
    if processes is None:
        return
    if not isinstance(processes, Mapping):
        raise ConfigurationError("Configured manifest processes must be a mapping.")
    active: list[str] = []
    for name, record in processes.items():
        if not isinstance(name, str) or not name:
            raise ConfigurationError("Configured process names must be non-empty.")
        if isinstance(record, Mapping):
            if record.get("exit_code") is not None:
                continue
            pid = record.get("pid")
        else:
            pid = record
        if _process_is_running(pid):
            active.append(f"{name} PID {int(pid)}")
    if active:
        raise ConfigurationError(
            "Recorded experiment processes may still be active: "
            + ", ".join(active)
            + ". Stop them and verify hardware state before resuming."
        )


def _load_configured_resume_context(
    manifest_path: Path,
) -> ConfiguredResumeContext:
    """Rebuild and verify a plan using only the immutable copied source tree."""

    manifest_path = Path(manifest_path).expanduser().resolve()
    if manifest_path.is_dir():
        manifest_path = manifest_path / "experiment_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Resume manifest does not exist: {manifest_path}")
    run_directory = manifest_path.parent.resolve()
    manifest = _read_json_object(manifest_path, "configured resume manifest")
    if manifest.get("format") != CONFIGURED_MANIFEST_FORMAT:
        raise ConfigurationError(
            "Manifest is not a configuration-driven experiment record."
        )
    if manifest.get("version") != 1:
        raise ConfigurationError("Configured resume manifest version must be 1.")
    if manifest.get("status") == "completed":
        raise ConfigurationError(
            "That configured run completed normally and cannot be resumed."
        )
    _reject_active_recorded_processes(manifest)

    hashes_path = _artifact_file(
        run_directory,
        manifest.get("configuration_hashes_file"),
        "configuration-hash inventory",
    )
    hashes = _read_json_object(hashes_path, "configuration-hash inventory")
    configuration_hash = hashes.get("configuration_hash")
    if configuration_hash != manifest.get("configuration_hash"):
        raise ConfigurationError(
            "Manifest and configuration inventory hashes disagree; refusing resume."
        )

    sources = hashes.get("sources")
    if not isinstance(sources, dict) or "experiment" not in sources:
        raise ConfigurationError(
            "Configuration inventory must contain the experiment source."
        )
    source_hashes: dict[str, str] = {}
    for role, record in sources.items():
        if not isinstance(role, str) or not role or not isinstance(record, dict):
            raise ConfigurationError("Configuration source inventory is malformed.")
        snapshot_path = _artifact_file(
            run_directory,
            record.get("snapshot_path"),
            f"{role} configuration snapshot",
        )
        source_hashes[role] = _verify_saved_hash(
            snapshot_path,
            record.get("sha256"),
            f"{role} configuration snapshot",
        )

    reloadable_relative = hashes.get("reloadable_experiment_file")
    if reloadable_relative != manifest.get("reloadable_experiment_file"):
        raise ConfigurationError(
            "Manifest and configuration inventory identify different reloadable "
            "experiment files."
        )
    reloadable_files = hashes.get("reloadable_files")
    if not isinstance(reloadable_files, dict) or not reloadable_files:
        raise ConfigurationError(
            "This run predates reloadable configuration snapshots and cannot be "
            "resumed without consulting live source files."
        )
    for relative, digest in reloadable_files.items():
        saved_path = _artifact_file(
            run_directory,
            relative,
            "reloadable configuration file",
        )
        _verify_saved_hash(saved_path, digest, "reloadable configuration file")
    reloadable_master = _artifact_file(
        run_directory,
        reloadable_relative,
        "reloadable experiment configuration",
    )
    if str(reloadable_master.relative_to(run_directory)) not in reloadable_files:
        raise ConfigurationError(
            "Reloadable experiment file is absent from the saved hash inventory."
        )

    effective_path = _artifact_file(
        run_directory,
        manifest.get("effective_experiment_file"),
        "effective experiment",
    )
    _verify_saved_hash(
        effective_path,
        hashes.get("effective_experiment_sha256"),
        "effective experiment",
    )

    experiment = load_experiment(reloadable_master)
    plan = build_effective_plan(experiment)
    reloadable_root = (run_directory / "reloadable_config").resolve()
    if any(
        not Path(path).resolve().is_relative_to(reloadable_root)
        for path in experiment.sources.values()
    ):
        raise ConfigurationError(
            "Resume configuration unexpectedly resolved outside its copied tree."
        )
    if plan.waveform_program is not None and any(
        waveform.source_path is not None
        and not waveform.source_path.resolve().is_relative_to(reloadable_root)
        for waveform in plan.waveform_program.waveforms.values()
    ):
        raise ConfigurationError(
            "Resume waveform asset unexpectedly resolved outside its copied tree."
        )
    if dict(experiment.source_hashes) != source_hashes:
        raise ConfigurationError(
            "Reloaded source hashes do not match the immutable source inventory."
        )
    if experiment.configuration_hash != configuration_hash:
        raise ConfigurationError(
            "Reloaded effective configuration hash does not match the manifest."
        )

    analysis_profile_path = _artifact_file(
        run_directory,
        manifest.get("analysis_profile_file", "analysis_profile.json"),
        "analysis profile",
    )
    _verify_saved_hash(
        analysis_profile_path,
        hashes.get("analysis_profile_sha256"),
        "analysis profile",
    )
    saved_analysis_profile = _read_json_object(
        analysis_profile_path,
        "analysis profile",
    )
    if saved_analysis_profile != experiment.run_settings.measurement.to_dict():
        raise ConfigurationError(
            "Saved analysis profile differs from the immutable effective "
            "configuration."
        )

    lut_inventory_path = _artifact_file(
        run_directory,
        manifest.get("lut_hashes_file", "lut_hashes.json"),
        "LUT-hash inventory",
    )
    lut_inventory = _read_json_object(lut_inventory_path, "LUT-hash inventory")
    saved_lut_files = lut_inventory.get("files")
    if not isinstance(saved_lut_files, dict):
        raise ConfigurationError("LUT-hash inventory files must be a mapping.")
    expected_lut_hashes = (
        {}
        if plan.waveform_program is None
        else {
            name: waveform.lut_sha256
            for name, waveform in plan.waveform_program.waveforms.items()
        }
    )
    if set(saved_lut_files) != set(expected_lut_hashes):
        raise ConfigurationError(
            "Saved LUT file inventory does not match the rebuilt waveform plan."
        )
    for name, digest in saved_lut_files.items():
        lut_path = _artifact_file(
            run_directory,
            str(Path("waveforms") / f"{name}.npy"),
            f"compiled LUT {name}",
        )
        _verify_saved_hash(lut_path, digest, f"compiled LUT {name}")

    program_sha256 = (
        None if plan.waveform_program is None else plan.waveform_program.program_sha256
    )
    if (
        lut_inventory.get("program_sha256") != program_sha256
        or manifest.get("program_sha256") != program_sha256
    ):
        raise ConfigurationError(
            "Saved waveform program hash does not match the rebuilt program."
        )

    checkpoint_path = _artifact_file(
        run_directory,
        manifest.get("runtime_checkpoint_file"),
        "runtime checkpoint",
    )
    checkpoint = AtomicCheckpointStore(checkpoint_path).load()
    validate_resume_checkpoint(
        checkpoint,
        configuration_hash=configuration_hash,
        source_hashes=source_hashes,
        lut_hashes=expected_lut_hashes,
    )
    checkpoint_time = datetime.fromisoformat(
        checkpoint.updated_at_utc[:-1] + "+00:00"
        if checkpoint.updated_at_utc.endswith("Z")
        else checkpoint.updated_at_utc
    )
    if checkpoint_time.tzinfo is None:
        raise ConfigurationError("Checkpoint timestamp must include a timezone.")
    gap_minutes = max(
        0.0,
        (datetime.now(timezone.utc) - checkpoint_time.astimezone(timezone.utc))
        .total_seconds()
        / 60.0,
    )

    resume_plan: dict[str, Any] = {
        "version": 1,
        "manifest_path": str(manifest_path),
        "run_directory": str(run_directory),
        "previous_status": manifest.get("status"),
        "configuration_hash": configuration_hash,
        "program_sha256": program_sha256,
        "checkpoint_updated_at_utc": checkpoint.updated_at_utc,
        "resume_gap_minutes": gap_minutes,
        "experiment_elapsed_s": checkpoint.experiment_elapsed_s,
        "temperature": (
            None if checkpoint.temperature is None else checkpoint.temperature.to_dict()
        ),
        "moku": None if checkpoint.moku is None else checkpoint.moku.to_dict(),
        "hardware_precondition": "connect_with_outputs_disabled",
    }
    return ConfiguredResumeContext(
        manifest_path=manifest_path,
        run_directory=run_directory,
        plan=plan,
        checkpoint=checkpoint,
        resume_plan=resume_plan,
        gap_minutes=gap_minutes,
    )


def _render_resume_plan(context: ConfiguredResumeContext) -> str:
    checkpoint = context.checkpoint
    lines = [
        "Configured resume plan",
        "======================",
        f"Run directory: {context.run_directory}",
        f"Previous status: {context.resume_plan['previous_status']}",
        f"Checkpoint UTC: {checkpoint.updated_at_utc}",
        f"Time since checkpoint: {context.gap_minutes:.1f} minutes",
        f"Elapsed experiment time: {checkpoint.experiment_elapsed_s:g} s",
    ]
    if checkpoint.temperature is None:
        lines.append("Temperature resume state: none")
    else:
        temperature = checkpoint.temperature
        lines.append(
            "Temperature resume state: "
            f"stage={temperature.stage_name!r}, phase={temperature.phase}, "
            f"completed hold={temperature.completed_hold_s:g} s"
        )
    if checkpoint.moku is None:
        lines.append("Moku resume state: none")
    else:
        moku = checkpoint.moku
        lines.append(
            "Moku resume state: "
            f"action={moku.action_name!r}, waveform={moku.waveform_name!r}, "
            f"mode={moku.repeat_mode}, session={moku.waveform_session_id}"
        )
        if moku.repeat_mode in {"continuous", "forever", "fill_experiment"}:
            lines.append(
                "Moku phase continuity: waveform will restart from phase zero "
                "after complete configuration replay."
            )
    lines.append("Hardware precondition: reconnect with outputs disabled.")
    return "\n".join(lines)


def _save_resume_plan(context: ConfiguredResumeContext) -> Path:
    directory = context.run_directory / "resume_plans"
    directory.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    candidate = directory / f"{stamp}_resume_plan.json"
    suffix = 1
    while candidate.exists():
        candidate = directory / f"{stamp}_{suffix:02d}_resume_plan.json"
        suffix += 1
    return atomic_write_json(candidate, context.resume_plan)


def run_configured_experiment(
    config_path: Path,
    *,
    action: str = "dry-run",
    assume_yes: bool = False,
) -> int:
    """Validate, compile, preview, or explicitly execute one YAML experiment."""

    if action not in {"dry-run", "preview", "execute"}:
        raise ValueError(f"Unknown configured experiment action {action!r}.")

    try:
        experiment = load_experiment(Path(config_path))
        plan = build_effective_plan(experiment)
    except (ConfigurationError, FileNotFoundError, OSError, ValueError) as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 2

    print(render_effective_plan(plan))
    if action == "dry-run":
        print(
            "\nDry run complete; every configuration and LUT was validated. "
            "The Moku, MeCom, and Linien packages were not imported and no "
            "hardware connection was opened."
        )
        return 0

    artifact_root = REPOSITORY_ROOT / ("runs" if action == "execute" else "previews")
    artifact_directory = make_artifact_directory(artifact_root, experiment.name)
    try:
        save_plan_artifacts(plan, artifact_directory)
    except (OSError, ValueError) as error:
        print(f"\nERROR: Could not save effective plan: {error}", file=sys.stderr)
        return 2
    print(f"\nEffective plan and previews saved to:\n{artifact_directory}")

    if action == "preview":
        print("Preview complete; no hardware connection was opened.")
        return 0

    if not _report_hardware_readiness_errors(plan):
        return 2

    if assume_yes:
        # The legacy --yes flag remains valid for the old runner.  Configured
        # execution intentionally keeps the stronger prompt required by the
        # versioned workflow.
        print(
            "WARNING: --yes does not bypass the final confirmation for a "
            "configuration-driven hardware run."
        )
    if not _confirm_configured_execution():
        return 0

    from eom_stabilisation.experiment.supervisor import execute_effective_plan

    return execute_effective_plan(plan, artifact_directory)


def resume_configured_experiment(
    manifest_path: Path,
    *,
    action: str = "dry-run",
    assume_yes: bool = False,
    resume_warning_minutes: float = 30.0,
) -> int:
    """Validate, preview, or explicitly resume an immutable run snapshot."""

    if action not in {"dry-run", "preview", "execute"}:
        raise ValueError(f"Unknown configured resume action {action!r}.")
    if (
        isinstance(resume_warning_minutes, bool)
        or not math.isfinite(resume_warning_minutes)
        or resume_warning_minutes <= 0
    ):
        raise ValueError("resume_warning_minutes must be finite and above zero.")
    try:
        context = _load_configured_resume_context(Path(manifest_path))
    except (ConfigurationError, FileNotFoundError, OSError, ValueError) as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 2

    print(render_effective_plan(context.plan))
    print("\n" + _render_resume_plan(context))
    if action == "dry-run":
        print(
            "\nConfigured resume dry run complete; the copied sources, effective "
            "configuration, LUT files, and checkpoint were verified. No hardware "
            "SDK was imported and no hardware connection was opened."
        )
        return 0

    try:
        resume_plan_path = _save_resume_plan(context)
    except OSError as error:
        print(f"\nERROR: Could not save resume plan: {error}", file=sys.stderr)
        return 2
    print(f"\nExact resume plan saved to:\n{resume_plan_path}")
    if action == "preview":
        print("Resume preview complete; no hardware connection was opened.")
        return 0


    if not _report_hardware_readiness_errors(context.plan):
        return 2

    if assume_yes:
        print(
            "WARNING: --yes does not bypass the final RESUME confirmation for a "
            "configuration-driven hardware run."
        )
    if not _confirm_stale_configured_resume(
        context.gap_minutes,
        resume_warning_minutes,
    ):
        return 0
    if not _confirm_configured_resume():
        return 0

    from eom_stabilisation.experiment.runtime_runner import (
        resume_hardware_experiment,
    )

    return resume_hardware_experiment(
        context.plan,
        context.run_directory,
        context.checkpoint,
    )


__all__ = [
    "CONFIGURED_MANIFEST_FORMAT",
    "ConfiguredResumeContext",
    "resume_configured_experiment",
    "run_configured_experiment",
]
