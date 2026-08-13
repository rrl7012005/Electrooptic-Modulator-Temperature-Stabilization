"""Start and supervise the EOM experiment programs from one command.

Examples:
    python run_experiment.py full
    python run_experiment.py temperature
    python run_experiment.py temperature-lock
    python run_experiment.py drift
    python run_experiment.py --components lock moku
    python run_experiment.py --resume-latest
    python run_experiment.py full --dry-run

Run without arguments for an interactive preset menu.
"""

import argparse
import csv
from datetime import datetime
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from zoneinfo import ZoneInfo


SRC_DIRECTORY = Path(__file__).resolve().parent / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.output_layout import (
    APPEND_OUTPUT_ENVIRONMENT_VARIABLE,
    COMPONENT_DIRECTORY_NAMES,
    COMPONENT_OUTPUT_FILE_ENVIRONMENT_VARIABLE,
    RUN_DIRECTORY_ENVIRONMENT_VARIABLE,
    component_plot_directory,
    create_experiment_run_directory,
    experiment_results_directory,
)


# =========================
# Experiment definitions
# =========================

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
MASTER_OUTPUT_DIRECTORY = experiment_results_directory(SCRIPT_DIRECTORY)
UK_TIME = ZoneInfo("Europe/London")

COMPONENTS = {
    "lock": {
        "label": "Linien lock point drift logger",
        "script": "linien_logger.py",
        "arguments": [],
        "output_directory": COMPONENT_DIRECTORY_NAMES["lock"],
    },
    "moku": {
        "label": "Moku pulse generator and photovoltage logger",
        "script": "collect_data.py",
        "arguments": [],
        "output_directory": COMPONENT_DIRECTORY_NAMES["moku"],
    },
    "temp-control": {
        "label": "TEC scheduled temperature controller and logger",
        "script": "tec_temperature_controller.py",
        "arguments": ["--yes"],
        "output_directory": COMPONENT_DIRECTORY_NAMES["temp-control"],
    },
    "temp-log": {
        "label": "TEC passive temperature/Peltier logger",
        "script": "tec_temp_logger.py",
        "arguments": ["--mode", "temperature_and_peltier"],
        "output_directory": COMPONENT_DIRECTORY_NAMES["temp-log"],
    },
}

PRESETS = {
    "full": ("lock", "moku", "temp-control"),
    "both": ("lock", "moku", "temp-control"),
    "temperature": ("temp-control",),
    "temperature-lock": ("lock", "temp-control"),
    "drift": ("temp-log", "lock", "moku"),
    "temperature-log": ("temp-log",),
}

# Start passive loggers first, then sources/controllers that change hardware.
START_ORDER = ("temp-log", "lock", "moku", "temp-control")

GRACEFUL_SHUTDOWN_SECONDS = 20.0
TERMINATE_WAIT_SECONDS = 5.0
MONITOR_INTERVAL_SECONDS = 0.5
STARTUP_TIMEOUT_SECONDS = 60.0
EMERGENCY_CLEANUP_TIMEOUT_SECONDS = 30.0
MOKU_DURATION_ENVIRONMENT_VARIABLE = (
    "EOM_MOKU_EXPERIMENT_LENGTH_SECONDS"
)
CONFIGURED_MANIFEST_FORMAT = "eom_configured_experiment"

# Set this to a positive number of seconds to make the master runner impose an
# overall experiment-duration limit after every selected component is ready.
# For example, use 48 * 3600 for 48 hours. Use None to retain the normal
# component-led duration behaviour.
MASTER_EXPERIMENT_LENGTH_SECONDS = None

# Set this to the desired live-snapshot interval.  Use None to disable live
# snapshots while retaining automatic final plots.
AUTO_PLOT_INTERVAL_MINUTES = 10.0
AUTO_PLOT_AT_END = True
PLOT_PROCESS_TIMEOUT_SECONDS = 120.0
PLOT_SHUTDOWN_WAIT_SECONDS = 10.0

# Each plotter is passed the exact CSV reported by its experiment component.
# This avoids accidentally plotting a different run merely because its file is
# newer elsewhere in the output tree.
PLOTTERS = {
    "lock": "plot_control.py",
    "moku": "analyse_eom_csv.py",
    "temp-control": "plot_temp_log.py",
    "temp-log": "plot_temp_log.py",
}


# =========================
# Command-line selection
# =========================

def parse_arguments():
    """Parse and return the command line arguments for the experiment"""
    parser = argparse.ArgumentParser(
        description="Start and supervise an EOM temperature experiment."
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=sorted(PRESETS),
        help="experiment preset; omit for an interactive menu",
    )
    parser.add_argument(
        "--components",
        nargs="+",
        choices=sorted(COMPONENTS),
        metavar="COMPONENT",
        help="run an custom combination of experiments instead of a preset",
    )
    parser.add_argument(
        "--config",
        type=Path,
        metavar="EXPERIMENT_YAML",
        help=(
            "load a versioned configuration-driven experiment; this mode "
            "defaults to a hardware-free dry run"
        ),
    )
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--resume-latest",
        action="store_true",
        help="continue the most recent master run if it is incomplete",
    )
    resume_group.add_argument(
        "--resume",
        type=Path,
        metavar="PREVIOUS_RUN_OR_MANIFEST",
        help=(
            "continue a specific legacy run or validate/resume a configured "
            "experiment manifest"
        ),
    )
    parser.add_argument(
        "--resume-warning-minutes",
        type=float,
        default=30.0,
        metavar="MINUTES",
        help="warn when the last run activity is older than this (default 30)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "start a legacy experiment without requiring START; configured "
            "--execute always requires its own explicit confirmation"
        ),
    )
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "--dry-run",
        action="store_true",
        help="show and validate the plan without connecting to hardware",
    )
    action_group.add_argument(
        "--preview",
        action="store_true",
        help=(
            "validate a configured experiment and write waveform previews "
            "without connecting to hardware"
        ),
    )
    action_group.add_argument(
        "--execute",
        action="store_true",
        help=(
            "explicitly request real execution of a configured experiment; "
            "an operator confirmation is always required"
        ),
    )
    args = parser.parse_args()

    configured_action = args.preview or args.execute
    if configured_action and args.config is None and args.resume is None:
        parser.error("--preview/--execute requires --config or --resume")
    if args.config is not None and (
        args.mode is not None
        or args.components is not None
        or args.resume_latest
        or args.resume is not None
    ):
        parser.error(
            "--config cannot be combined with legacy mode/component/resume options"
        )

    return args


def choose_interactively():
    """Generate a menu showcasing options for the experiment type and prompt user to select"""
    menu = [
        ("1", "full", "lock drift + Moku pulsing + temperature control"),
        ("2", "temperature", "temperature control only"),
        ("3", "temperature-lock", "lock drift + temperature control; no pulsing"),
        ("4", "drift", "lock drift + Moku pulsing + passive temperature log"),
        ("5", "temperature-log", "passive temperature log only"),
        ("6", "resume-latest", "continue the latest run, whichever experiment type it was, if it was interrupted"),
    ]

    print("\nChoose experiment mode:")
    for number, _, description in menu:
        print(f"{number}: {description}")

    valid_choices = {number: mode for number, mode, _ in menu}
    while True:
        choice = input("Enter 1, 2, 3, 4, 5, or 6: ").strip()
        if choice in valid_choices:
            return valid_choices[choice]
        print("Invalid choice.")


def prompt_for_confirmation(action_word, prompt):
    """Wait for an explicit action word or let the operator cancel safely."""
    while True:
        try:
            response = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled; no hardware process was launched.")
            return False

        if response == action_word:
            return True
        if response == "CANCEL":
            print("Cancelled; no hardware process was launched.")
            return False

        print(
            f"Invalid response. Type {action_word} to continue or "
            "CANCEL to stop."
        )


def confirm_stale_resume(gap_minutes, warning_minutes):
    """Require acknowledgement when a resume may have stale physical state."""
    if gap_minutes <= warning_minutes:
        return True

    print(
        "WARNING: This exceeds the configured "
        f"{warning_minutes:g}-minute resume window. "
        "The optical lock and thermal state may no longer match "
        "the previous segment."
    )
    return prompt_for_confirmation(
        "CONTINUE",
        "Type CONTINUE to acknowledge this risk, or CANCEL to stop: ",
    )


def resolve_selection(args):
    """Resolve the command line arguments into experiment types"""
    if args.mode is not None and args.components is not None:
        raise ValueError("Choose either a preset mode or --components, not both.")

    if args.components is not None:
        mode = "custom"
        selected = tuple(dict.fromkeys(args.components)) #Remove duplicates
    else:
        mode = args.mode or choose_interactively()
        selected = PRESETS[mode]

    return mode, order_and_validate_components(selected)


def order_and_validate_components(selected):
    """Validate the given experiment types"""
    selected = set(selected)
    unknown = selected - set(COMPONENTS)
    if unknown:
        raise ValueError(
            "Unknown components in previous manifest: "
            + ", ".join(sorted(unknown))
        )

    if "temp-control" in selected and "temp-log" in selected:
        print(
            "temp-control and temp-log were selected. "
            "Temperature control already includes logging. "
            "Removing temp-log argument..."
        )
        selected.discard("temp-log")

    return tuple(
        component for component in START_ORDER if component in selected
    )


def resolve_manifest_path(path):
    """Determine path of experiment_manifest.json containing information from previous runs"""
    manifest_path = Path(path).expanduser().resolve()
    if manifest_path.is_dir():
        manifest_path = manifest_path / "experiment_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Resume manifest does not exist: {manifest_path}")
    return manifest_path


def read_manifest(path):
    """Read the experiment manifest json file from a previous run"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read resume manifest {path}: {error}") from error

    if not isinstance(data, dict):
        raise ValueError(f"Resume manifest is not a JSON object: {path}")
    return data


def classify_resume_manifest(path):
    """Return the resolved manifest, its document, and its runner family."""

    manifest_path = resolve_manifest_path(path)
    manifest = read_manifest(manifest_path)
    manifest_format = manifest.get("format")
    if manifest_format == CONFIGURED_MANIFEST_FORMAT:
        family = "configured"
    elif manifest_format is None:
        family = "legacy"
    else:
        raise ValueError(
            f"Unsupported experiment manifest format {manifest_format!r}: "
            f"{manifest_path}"
        )
    return manifest_path, manifest, family


def find_latest_manifest():
    """Finds the latest experiment_manifest.json file from the latest run and retuns the json contents"""
    manifests = sorted(
        list(MASTER_OUTPUT_DIRECTORY.glob("run_*/experiment_manifest.json"))
        + list(
            MASTER_OUTPUT_DIRECTORY.glob(
                "master_runs/run_*/experiment_manifest.json"
            )
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    for manifest_path in manifests:
        try:
            manifest = read_manifest(manifest_path)
        except ValueError:
            continue
        return manifest_path, manifest

    raise FileNotFoundError("No readable master run is available to resume.")


def process_is_running(pid):
    """Determine if a process with a certain Process ID is still running"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and f'"{pid}"' in result.stdout

    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def find_active_previous_processes(manifest):
    """Check if processes from previous run are still active"""
    active = []
    for component, process_info in manifest.get("processes", {}).items():
        if process_info.get("exit_code") is not None:
            continue
        pid = process_info.get("pid")
        if process_is_running(pid):
            active.append((component, int(pid)))
    return active


def last_activity_time(manifest_path, manifest):
    """Finds the time of the most recently modified file assosciated with a run"""
    timestamps = [manifest_path.stat().st_mtime]
    for process_info in manifest.get("processes", {}).values():
        output_file = process_info.get("output_file")
        if output_file:
            output_path = Path(output_file)
            if not output_path.is_absolute():
                output_path = SCRIPT_DIRECTORY / output_path
            if output_path.is_file():
                timestamps.append(output_path.stat().st_mtime)
    return datetime.fromtimestamp(max(timestamps), tz=UK_TIME)


def load_resume_context(args):
    """Setup and prepare a resumed run"""
    if not args.resume_latest and args.resume is None:
        return None

    if args.mode is not None or args.components is not None:
        raise ValueError(
            "Resume options cannot be combined with a mode or --components."
        )

    if args.resume_latest:
        manifest_path, manifest = find_latest_manifest()
    else:
        manifest_path = resolve_manifest_path(args.resume)
        manifest = read_manifest(manifest_path)

    if manifest.get("status") == "completed":
        raise ValueError(
            "That run completed normally and should not be resumed. Start a "
            "new run instead."
        )

    selected = order_and_validate_components(
        tuple(manifest.get("selected_components", []))
    )
    if not selected:
        raise ValueError("Resume manifest contains no experiment components.")

    active_processes = find_active_previous_processes(manifest)
    if active_processes:
        active_text = ", ".join(
            f"{component} PID {pid}" for component, pid in active_processes
        )
        raise RuntimeError(
            "Previous experiment processes may still be running: "
            f"{active_text}. Stop them and verify the hardware state before "
            "resuming."
        )

    activity_time = last_activity_time(manifest_path, manifest)
    gap_minutes = max(
        0.0,
        (datetime.now(UK_TIME) - activity_time).total_seconds() / 60.0,
    )

    return {
        "manifest_path": manifest_path,
        "manifest": manifest,
        "selected": selected,
        "gap_minutes": gap_minutes,
        "active_processes": active_processes,
    }


# =========================
# Plan and manifest
# =========================

def component_command(component, extra_arguments=()):
    """Return arguments and executables to be used to execute script for each component/process of experiment"""
    definition = COMPONENTS[component]
    script_path = SCRIPT_DIRECTORY / definition["script"]
    return [
        sys.executable,
        str(script_path),
        *definition["arguments"],
        *extra_arguments,
    ]


def validate_scripts(selected):
    """Ensure python scripts for each component of experiment and plotting exists"""
    missing = [
        COMPONENTS[component]["script"]
        for component in selected
        if not (SCRIPT_DIRECTORY / COMPONENTS[component]["script"]).is_file()
    ]

    if AUTO_PLOT_AT_END or AUTO_PLOT_INTERVAL_MINUTES is not None:
        missing.extend(
            PLOTTERS[component]
            for component in selected
            if component in PLOTTERS
            and not (SCRIPT_DIRECTORY / PLOTTERS[component]).is_file()
        )

    if missing:
        raise FileNotFoundError(
            f"Missing component or plotting scripts: {', '.join(missing)}"
        )


def get_auto_plot_interval_seconds():
    """Validate and convert the user set live plot interval to seconds."""
    if AUTO_PLOT_INTERVAL_MINUTES is None:
        return None
    try:
        interval_minutes = float(AUTO_PLOT_INTERVAL_MINUTES)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "AUTO_PLOT_INTERVAL_MINUTES must be a number above zero or None"
        ) from error
    if not math.isfinite(interval_minutes) or interval_minutes <= 0:
        raise ValueError(
            "AUTO_PLOT_INTERVAL_MINUTES must be a finite number above zero "
            "or None"
        )
    return interval_minutes * 60.0


def get_master_experiment_length_seconds():
    """Validate and return the optional master duration limit."""

    if MASTER_EXPERIMENT_LENGTH_SECONDS is None:
        return None
    try:
        duration_seconds = float(MASTER_EXPERIMENT_LENGTH_SECONDS)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "MASTER_EXPERIMENT_LENGTH_SECONDS must be a positive number or "
            "None"
        ) from error
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError(
            "MASTER_EXPERIMENT_LENGTH_SECONDS must be a finite number above "
            "zero or None"
        )
    return duration_seconds


def prepare_master_duration_resume(resume_context, configured_seconds):
    """Return elapsed and remaining master-controlled time for this run."""

    if resume_context is None:
        return 0.0, configured_seconds

    previous_manifest = resume_context["manifest"]
    previous_configured = previous_manifest.get(
        "master_experiment_length_seconds"
    )
    if previous_configured is None and configured_seconds is None:
        return 0.0, None
    if previous_configured is None or configured_seconds is None:
        raise ValueError(
            "MASTER_EXPERIMENT_LENGTH_SECONDS differs from the previous run. "
            "Restore its previous value before resuming."
        )
    if not math.isclose(
        float(previous_configured),
        configured_seconds,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise ValueError(
            "MASTER_EXPERIMENT_LENGTH_SECONDS differs from the previous run. "
            "Restore its previous value before resuming."
        )

    previous_elapsed = previous_manifest.get("master_accumulated_seconds")
    if previous_elapsed is None:
        raise ValueError(
            "The previous manifest does not record elapsed master-controlled "
            "time, so this duration-limited run cannot be resumed safely."
        )
    previous_elapsed = float(previous_elapsed)
    if not math.isfinite(previous_elapsed) or previous_elapsed < 0:
        raise ValueError(
            "The previous manifest contains invalid master elapsed time."
        )

    remaining_seconds = configured_seconds - previous_elapsed
    if remaining_seconds <= 0:
        raise ValueError(
            "The previous run already reached its configured master "
            "experiment length."
        )
    return previous_elapsed, remaining_seconds


def select_leader(selected):
    """Choose the finite process whose completion ends the experiment. Moku process dominates"""
    if "temp-control" in selected:
        return "temp-control"
    if "moku" in selected:
        return "moku"
    return None


def display_plan(
    mode,
    selected,
    leader,
    master_experiment_length_seconds=None,
    master_remaining_seconds=None,
):
    """Display plan for the entire experiment"""
    print("\nExperiment plan")
    print("===============")
    print(f"Mode: {mode}")
    for component in selected:
        definition = COMPONENTS[component]
        if component != leader:
            suffix = ""
        elif master_experiment_length_seconds is None:
            suffix = " (defines experiment duration)"
        else:
            suffix = " (can complete before the master duration limit)"
        print(f"- {component}: {definition['label']}{suffix}")

    if master_experiment_length_seconds is not None:
        print(
            "- Master duration limit: "
            f"{master_experiment_length_seconds:g} s "
            "(starts after all components are ready)"
        )
        if (
            master_remaining_seconds is not None
            and not math.isclose(
                master_remaining_seconds,
                master_experiment_length_seconds,
                rel_tol=0.0,
                abs_tol=1e-6,
            )
        ):
            print(
                "- Master duration remaining after previous run: "
                f"{master_remaining_seconds:g} s"
            )
        print("- Normal component completion can still end the run earlier")
    elif leader is None:
        print("- Duration: runs until Ctrl+C")

    print("\nOutput locations")
    for component in selected:
        output_directory = (
            MASTER_OUTPUT_DIRECTORY
            / "run_<YYYY-MM-DD_HH-MM-SS_TZ>"
            / COMPONENTS[component]["output_directory"]
        )
        print(f"- {component}: {output_directory}")

    interval_seconds = get_auto_plot_interval_seconds()
    print("\nAutomatic plots")
    if interval_seconds is None:
        print("- In-progress snapshots: disabled")
    else:
        print(
            "- In-progress snapshots: every "
            f"{interval_seconds / 60.0:g} minutes"
        )
    print(
        "- Final plots: "
        + ("enabled" if AUTO_PLOT_AT_END else "disabled")
    )


def write_manifest(path, manifest):
    """Write the manifest to file"""
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def make_run_folder():
    """Create run folder"""
    return create_experiment_run_directory(MASTER_OUTPUT_DIRECTORY)


def run_temperature_dry_run(extra_arguments=()):
    """Test whether temperature control can run"""
    sys.stdout.flush()
    command = [
        sys.executable,
        str(SCRIPT_DIRECTORY / "tec_temperature_controller.py"),
        "--dry-run",
        *extra_arguments,
    ]
    result = subprocess.run(command, cwd=SCRIPT_DIRECTORY, check=False)
    if result.returncode != 0:
        raise RuntimeError("Temperature schedule validation failed.")


def get_temperature_schedule_config():
    """Extract temperature controller temperature schedule"""
    command = [
        sys.executable,
        str(SCRIPT_DIRECTORY / "tec_temperature_controller.py"),
        "--print-schedule-json",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=SCRIPT_DIRECTORY,
            capture_output=True,
            text=True,
            check=False,
            timeout=15.0,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Could not read the temperature schedule: {error}")

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            "Could not read the temperature schedule"
            + (f": {detail}" if detail else ".")
        )

    try:
        schedule_config = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Temperature controller returned invalid JSON.") from error
    if not isinstance(schedule_config, dict):
        raise RuntimeError("Temperature controller returned an invalid schedule.")
    return schedule_config


def get_configured_moku_duration():
    """Read the Moku source default without ambient duration overrides."""
    command = [
        sys.executable,
        str(SCRIPT_DIRECTORY / "collect_data.py"),
        "--print-experiment-length",
    ]
    environment = os.environ.copy()
    ignored_override = environment.pop(
        MOKU_DURATION_ENVIRONMENT_VARIABLE,
        None,
    )
    if ignored_override is not None:
        print(
            "WARNING: Ignoring ambient "
            f"{MOKU_DURATION_ENVIRONMENT_VARIABLE}={ignored_override!r} for "
            "this fresh-run configuration check. The master runner uses the "
            "duration configured in collect_data.py unless it explicitly "
            "sets a remaining duration while resuming."
        )
    try:
        result = subprocess.run(
            command,
            cwd=SCRIPT_DIRECTORY,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=15.0,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Could not read the Moku experiment length: {error}")

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            "Could not read the Moku experiment length"
            + (f": {detail}" if detail else ".")
        )

    try:
        duration = float(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as error:
        raise RuntimeError("Moku returned an invalid experiment length.") from error
    if duration <= 0:
        raise RuntimeError("Moku experiment length must be above zero.")
    return duration


def validate_moku_resume_target(previous_target, configured_target):
    """Check target-duration continuity, warning for legacy manifests."""
    if previous_target is None:
        print(
            "WARNING: The previous manifest does not record its configured "
            "Moku experiment length. The current length will be used, so a "
            "duration change since the original run cannot be detected "
            "automatically."
        )
        return

    if not math.isclose(
        float(previous_target),
        configured_target,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise ValueError(
            "The configured Moku experiment length differs from the previous "
            "run. Restore it before resuming, or start a new experiment."
        )


def read_moku_csv_duration(csv_path):
    """Determine duration of the Moku Pulsing from existing file"""
    if not csv_path:
        return 0.0
    path = Path(csv_path)
    if not path.is_absolute():
        path = SCRIPT_DIRECTORY / path
    if not path.is_file():
        return 0.0

    first_time = None
    last_time = None
    with path.open(newline="", encoding="utf-8-sig") as csv_file:
        for row in csv.DictReader(csv_file):
            try:
                wall_time = float(row["wall_time"])
            except (KeyError, TypeError, ValueError):
                continue
            if first_time is None:
                first_time = wall_time
            last_time = wall_time

    if first_time is None or last_time is None:
        return 0.0
    return max(0.0, last_time - first_time)


def previous_component_output(resume_context, component):
    """Return the newest valid recorded output for a resumed component.

    A resume attempt can fail before every component reports ready.  Its
    top-level ``processes`` record is then incomplete, while the valid paths
    from earlier executions remain under ``previous_executions``.  Search the
    complete history newest-first so another resume does not mistake those
    existing raw files for new outputs.
    """

    manifest = resume_context["manifest"]
    previous_executions = manifest.get("previous_executions", [])
    execution_records = [
        ("latest execution", manifest),
        *(
            (f"previous_executions[{index}]", execution)
            for index, execution in reversed(
                list(enumerate(previous_executions))
            )
        ),
    ]
    unusable_records = []

    for record_name, execution in execution_records:
        if not isinstance(execution, dict):
            continue
        processes = execution.get("processes", {})
        if not isinstance(processes, dict):
            continue
        process_info = processes.get(component)
        if not isinstance(process_info, dict):
            continue

        output_file = process_info.get("output_file")
        if not output_file:
            if process_info.get("ready"):
                unusable_records.append(
                    f"{record_name} says the process was ready but records "
                    "no output path"
                )
            continue

        output_path = Path(output_file)
        if not output_path.is_absolute():
            output_path = SCRIPT_DIRECTORY / output_path
        output_path = output_path.resolve()
        if output_path.is_file():
            return output_path
        unusable_records.append(
            f"{record_name} references a missing file: {output_path}"
        )

    if unusable_records:
        details = "; ".join(unusable_records)
        raise FileNotFoundError(
            f"No valid previous {component} output file is available. "
            f"{details}. Refusing to start because an existing raw log must "
            "never be replaced silently."
        )
    return None


def prepare_resume_execution(resume_context, leader, moku_target_duration):
    """Prepare settings and arguments to resume experiment"""
    component_arguments = {}
    component_environments = {}
    moku_elapsed_before = 0.0

    if resume_context is None:
        return component_arguments, component_environments, moku_elapsed_before

    selected = resume_context["selected"]
    previous_manifest = resume_context["manifest"]

    for component in selected:
        previous_output = previous_component_output(
            resume_context,
            component,
        )
        if previous_output is not None:
            component_environments.setdefault(component, {}).update(
                {
                    APPEND_OUTPUT_ENVIRONMENT_VARIABLE: "1",
                    COMPONENT_OUTPUT_FILE_ENVIRONMENT_VARIABLE: str(
                        previous_output
                    ),
                }
            )

    if "temp-control" in selected:
        previous_control_csv = previous_component_output(
            resume_context,
            "temp-control",
        )
        if previous_control_csv is None:
            print(
                "Previous temperature control never reported ready; its "
                "schedule will start from step 1."
            )
        else:
            component_arguments["temp-control"] = (
                "--resume-from",
                str(previous_control_csv),
            )

    if leader == "moku":
        previous_moku_csv = previous_component_output(resume_context, "moku")
        accumulated = previous_manifest.get("moku_accumulated_seconds")
        if accumulated is None:
            accumulated = float(
                previous_manifest.get("moku_elapsed_before_seconds", 0.0)
            ) + read_moku_csv_duration(previous_moku_csv)
        moku_elapsed_before = max(0.0, float(accumulated))
        remaining = moku_target_duration - moku_elapsed_before
        if remaining <= 0:
            raise ValueError(
                "The previous Moku-led experiment has already reached its "
                "configured duration."
            )
        component_environments.setdefault("moku", {}).update(
            {
                MOKU_DURATION_ENVIRONMENT_VARIABLE: f"{remaining:.9f}",
            }
        )
        print(
            "Moku resume duration: "
            f"{remaining / 3600.0:.3f} h remaining of "
            f"{moku_target_duration / 3600.0:.3f} h."
        )

    return component_arguments, component_environments, moku_elapsed_before


# =========================
# Process supervision
# =========================

def component_output_path(manifest, component):
    """Return an absolute component CSV path recorded in the manifest."""
    output_file = manifest["processes"].get(component, {}).get("output_file")
    if not output_file:
        return None
    output_path = Path(output_file)
    if not output_path.is_absolute():
        output_path = SCRIPT_DIRECTORY / output_path
    return output_path.resolve()


def plot_command(component, csv_path, output_directory, *, in_progress):
    """Build a plotting command for one component."""
    command = [
        sys.executable,
        str(SCRIPT_DIRECTORY / PLOTTERS[component]),
        str(csv_path),
        "--output-dir",
        str(output_directory),
        "--no-show",
    ]
    if component == "moku":
        command.extend(
            [
                "--analysis-dir",
                str(output_directory.parent.parent),
            ]
        )
    if in_progress:
        command.append("--in-progress")
    return command


def plotting_environment():
    """Return an environment that cannot open GUI windows."""
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    environment["MPLBACKEND"] = "Agg"
    return environment


def start_periodic_plot_batch(
    selected,
    manifest,
    manifest_path,
):
    """Launch one asynchronous live plot batch and return its runtime state."""
    batch_number = len(manifest["plotting"]["periodic_batches"]) + 1
    batch_record = {
        "batch_number": batch_number,
        "started_at": datetime.now(UK_TIME).isoformat(),
        "finished_at": None,
        "processes": {},
    }
    runtime_processes = {}

    print(f"\nCreating in progress plot snapshot {batch_number}...")
    for component in selected:
        if component not in PLOTTERS:
            continue
        output_directory = component_plot_directory(
            manifest_path.parent,
            component,
            in_progress=True,
        )
        output_directory.mkdir(parents=True, exist_ok=True)
        csv_path = component_output_path(manifest, component)
        log_path = output_directory / f"{component}_plotting.log"
        process_record = {
            "command": None,
            "pid": None,
            "exit_code": None,
            "error": None,
            "log_file": str(log_path),
        }
        batch_record["processes"][component] = process_record

        if csv_path is None or not csv_path.is_file():
            process_record["error"] = "component output CSV is unavailable"
            print(f"WARNING: live {component} plot skipped; CSV unavailable.")
            continue

        command = plot_command(
            component,
            csv_path,
            output_directory,
            in_progress=True,
        )
        process_record["command"] = command
        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(
                    f"\nSnapshot {batch_number} started "
                    f"{datetime.now(UK_TIME).isoformat()}\n"
                )
                process = subprocess.Popen(
                    command,
                    cwd=SCRIPT_DIRECTORY,
                    env=plotting_environment(),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
        except OSError as error:
            process_record["error"] = f"{type(error).__name__}: {error}"
            print(f"WARNING: could not start live {component} plot: {error}")
            continue

        process_record["pid"] = process.pid
        runtime_processes[component] = process

    manifest["plotting"]["periodic_batches"].append(batch_record)
    if not runtime_processes:
        batch_record["finished_at"] = datetime.now(UK_TIME).isoformat()
    write_manifest(manifest_path, manifest)
    return {
        "record": batch_record,
        "processes": runtime_processes,
    }


def refresh_periodic_plot_batch(active_batch, manifest, manifest_path):
    """Record completed plotters and return None when the batch is done."""
    if active_batch is None:
        return None

    all_finished = True
    record = active_batch["record"]
    for component, process in active_batch["processes"].items():
        return_code = process.poll()
        if return_code is None:
            all_finished = False
            continue
        process_record = record["processes"][component]
        if process_record["exit_code"] is None:
            process_record["exit_code"] = return_code
            if return_code != 0:
                print(
                    f"WARNING: live {component} plotting exited with code "
                    f"{return_code}; acquisition continues."
                )

    if not all_finished:
        return active_batch

    if record["finished_at"] is None:
        record["finished_at"] = datetime.now(UK_TIME).isoformat()
        write_manifest(manifest_path, manifest)
        print("In-progress plot snapshot finished.")
    return None


def stop_periodic_plot_batch(active_batch, manifest, manifest_path):
    """Finish or stop live-only plot processes before final plotting."""
    if active_batch is None:
        return

    deadline = time.monotonic() + PLOT_SHUTDOWN_WAIT_SECONDS
    running = dict(active_batch["processes"])
    while running and time.monotonic() < deadline:
        running = {
            component: process
            for component, process in running.items()
            if process.poll() is None
        }
        if running:
            time.sleep(0.1)

    for component, process in running.items():
        print(f"Stopping superseded in-progress {component} plotter.")
        try:
            process.terminate()
            process.wait(timeout=5.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    record = active_batch["record"]
    for component, process in active_batch["processes"].items():
        process_record = record["processes"][component]
        process_record["exit_code"] = process.poll()
        if component in running:
            process_record["error"] = "stopped before final plotting"
    record["finished_at"] = datetime.now(UK_TIME).isoformat()
    write_manifest(manifest_path, manifest)


def run_final_plots(selected, manifest, manifest_path):
    """Generate final plots after component logs have been closed."""
    results = []
    print("\nGenerating final component plots...")

    for component in selected:
        if component not in PLOTTERS:
            continue
        output_directory = component_plot_directory(
            manifest_path.parent,
            component,
            in_progress=False,
        )
        output_directory.mkdir(parents=True, exist_ok=True)
        csv_path = component_output_path(manifest, component)
        log_path = output_directory / f"{component}_plotting.log"
        result_record = {
            "component": component,
            "command": None,
            "exit_code": None,
            "error": None,
            "log_file": str(log_path),
        }
        results.append(result_record)

        if csv_path is None or not csv_path.is_file():
            result_record["error"] = "component output CSV is unavailable"
            print(f"WARNING: final {component} plot skipped; CSV unavailable.")
            continue

        command = plot_command(
            component,
            csv_path,
            output_directory,
            in_progress=False,
        )
        result_record["command"] = command
        try:
            result = subprocess.run(
                command,
                cwd=SCRIPT_DIRECTORY,
                env=plotting_environment(),
                capture_output=True,
                text=True,
                check=False,
                timeout=PLOT_PROCESS_TIMEOUT_SECONDS,
            )
            result_record["exit_code"] = result.returncode
            log_path.write_text(
                result.stdout + result.stderr,
                encoding="utf-8",
            )
            if result.returncode == 0:
                print(f"Final {component} plots saved.")
            else:
                print(
                    f"WARNING: final {component} plotting exited with code "
                    f"{result.returncode}; see {log_path}"
                )
        except subprocess.TimeoutExpired as error:
            result_record["error"] = (
                f"plotting timed out after {PLOT_PROCESS_TIMEOUT_SECONDS:g} s"
            )
            log_path.write_text(str(error), encoding="utf-8")
            print(f"WARNING: final {component} plotting timed out.")
        except (OSError, UnicodeError) as error:
            result_record["error"] = f"{type(error).__name__}: {error}"
            print(f"WARNING: final {component} plotting failed: {error}")

    manifest["plotting"]["final_results"] = results
    manifest["plotting"]["finished_at"] = datetime.now(UK_TIME).isoformat()
    write_manifest(manifest_path, manifest)


def start_component(
    component,
    ready_file,
    extra_arguments=(),
    environment_overrides=None,
):
    """Execute the script of a component of the experiment"""
    command = component_command(component, extra_arguments)
    environment = os.environ.copy()
    if component == "moku":
        # A stale shell/user environment value must not silently replace the
        # source duration for a new master-run experiment. Explicit resume
        # overrides are applied immediately below.
        environment.pop(MOKU_DURATION_ENVIRONMENT_VARIABLE, None)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["EOM_READY_FILE"] = str(ready_file)
    environment.update(environment_overrides or {})

    options = {
        "cwd": SCRIPT_DIRECTORY,
        "env": environment,
    }

    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True

    process = subprocess.Popen(command, **options)
    print(
        f"Started {component} (PID {process.pid}): "
        f"{COMPONENTS[component]['label']}"
    )
    return process, command


def wait_for_component_ready(component, process, ready_file):
    """Wait until a child confirms that its hardware and output are ready."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    print(f"Waiting for {component} to report ready...")

    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"{component} exited with code {return_code} before it "
                "reported ready"
            )

        if ready_file.is_file():
            output_file = ready_file.read_text(encoding="utf-8").strip()
            ready_file.unlink()
            print(f"{component} is ready.")
            return output_file or None

        time.sleep(0.1)

    raise TimeoutError(
        f"{component} did not report ready within "
        f"{STARTUP_TIMEOUT_SECONDS:g} seconds"
    )


def request_graceful_stop(component, process):
    """Gracefully stops a process"""
    if process.poll() is not None:
        return

    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGINT)
        print(f"Asked {component} to stop safely.")
    except (OSError, ProcessLookupError) as error:
        print(f"Could not signal {component}: {error}")


def stop_processes(processes):
    """Stop a process"""
    running = {
        component: process
        for component, process in processes.items()
        if process.poll() is None
    }

    for component, process in reversed(list(running.items())):
        request_graceful_stop(component, process)

    graceful_deadline = time.monotonic() + GRACEFUL_SHUTDOWN_SECONDS
    while running and time.monotonic() < graceful_deadline:
        running = {
            component: process
            for component, process in running.items()
            if process.poll() is None
        }
        if running:
            time.sleep(0.2)

    forcibly_terminated = set(running)

    for component, process in running.items():
        print(
            f"WARNING: {component} did not stop within "
            f"{GRACEFUL_SHUTDOWN_SECONDS:g} s; terminating it."
        )
        try:
            process.terminate()
        except OSError as error:
            print(f"Could not terminate {component}: {error}")

    terminate_deadline = time.monotonic() + TERMINATE_WAIT_SECONDS
    while running and time.monotonic() < terminate_deadline:
        running = {
            component: process
            for component, process in running.items()
            if process.poll() is None
        }
        if running:
            time.sleep(0.2)

    for component, process in running.items():
        print(f"WARNING: {component} still did not stop; killing it.")
        try:
            process.kill()
        except OSError as error:
            print(f"Could not kill {component}: {error}")

    return forcibly_terminated


def run_emergency_cleanup(component):
    """Cleanup in emergency stop"""
    cleanup_commands = {
        "temp-control": [
            sys.executable,
            str(SCRIPT_DIRECTORY / "tec_temperature_controller.py"),
            "--disable-output",
        ],
        "moku": [
            sys.executable,
            str(SCRIPT_DIRECTORY / "collect_data.py"),
            "--disable-output",
        ],
    }

    command = cleanup_commands.get(component)
    if command is None:
        return "not_required"

    print(f"Running emergency hardware cleanup for {component}...")
    try:
        result = subprocess.run(
            command,
            cwd=SCRIPT_DIRECTORY,
            check=False,
            timeout=EMERGENCY_CLEANUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        print(f"CRITICAL: emergency cleanup for {component} failed: {error}")
        return "failed"

    if result.returncode != 0:
        print(
            f"CRITICAL: emergency cleanup for {component} exited with "
            f"code {result.returncode}. Check the hardware immediately."
        )
        return "failed"

    print(f"Emergency cleanup for {component} completed successfully.")
    return "succeeded"


def supervise(
    selected,
    leader,
    manifest,
    manifest_path,
    component_arguments=None,
    component_environments=None,
    master_remaining_seconds=None,
    master_elapsed_before_seconds=0.0,
):
    """Supervise the execution and stoppinf of scripts and logs in experiment"""
    processes = {}
    exit_code = 0
    stop_reason = None
    final_status = "completed"
    monitoring_finished = False
    plot_interval_seconds = get_auto_plot_interval_seconds()
    next_plot_time = None
    active_plot_batch = None
    master_timer_start = None
    master_deadline = None
    readiness_directory = manifest_path.parent / "readiness"
    readiness_directory.mkdir(exist_ok=True)
    component_arguments = component_arguments or {}
    component_environments = component_environments or {}

    try:
        for component in selected:
            ready_file = readiness_directory / f"{component}.ready"
            if ready_file.exists():
                ready_file.unlink()
            environment_overrides = {
                RUN_DIRECTORY_ENVIRONMENT_VARIABLE: str(manifest_path.parent),
            }
            environment_overrides.update(
                component_environments.get(component, {})
            )
            process, command = start_component(
                component,
                ready_file,
                extra_arguments=component_arguments.get(component, ()),
                environment_overrides=environment_overrides,
            )
            processes[component] = process
            manifest["processes"][component] = {
                "pid": process.pid,
                "command": command,
                "output_directory": str(
                    manifest_path.parent
                    / COMPONENTS[component]["output_directory"]
                ),
                "output_file": None,
                "ready": False,
                "exit_code": None,
            }
            write_manifest(manifest_path, manifest)

            output_file = wait_for_component_ready(
                component,
                process,
                ready_file,
            )
            manifest["processes"][component]["ready"] = True
            manifest["processes"][component]["output_file"] = output_file
            write_manifest(manifest_path, manifest)

        manifest["status"] = "running"
        write_manifest(manifest_path, manifest)
        try:
            readiness_directory.rmdir()
        except OSError:
            pass
        print("\nAll selected components are running. Press Ctrl+C to stop.\n")
        if master_remaining_seconds is not None:
            master_timer_start = time.monotonic()
            master_deadline = master_timer_start + master_remaining_seconds
            manifest["master_timer_started_at"] = datetime.now(
                UK_TIME
            ).isoformat()
            write_manifest(manifest_path, manifest)
        if plot_interval_seconds is not None:
            next_plot_time = time.monotonic() + plot_interval_seconds

        while not monitoring_finished:
            active_plot_batch = refresh_periodic_plot_batch(
                active_plot_batch,
                manifest,
                manifest_path,
            )

            for component, process in processes.items():
                return_code = process.poll()
                if return_code is None:
                    continue

                manifest["processes"][component]["exit_code"] = return_code

                if component == leader and return_code == 0:
                    stop_reason = f"{component} completed normally"
                    print(f"\n{stop_reason}; stopping remaining components.")
                    monitoring_finished = True
                    break

                stop_reason = (
                    f"{component} exited unexpectedly with code {return_code}"
                )
                print(f"\nERROR: {stop_reason}; stopping the experiment.")
                exit_code = return_code if return_code != 0 else 1
                final_status = "failed"
                monitoring_finished = True
                break

            if not monitoring_finished:
                now = time.monotonic()
                if master_deadline is not None and now >= master_deadline:
                    stop_reason = (
                        "master experiment length reached "
                        f"({manifest['master_experiment_length_seconds']:g} s)"
                    )
                    print(f"\n{stop_reason}; stopping all components.")
                    monitoring_finished = True
                    continue
                if next_plot_time is not None and now >= next_plot_time:
                    if active_plot_batch is None:
                        active_plot_batch = start_periodic_plot_batch(
                            selected,
                            manifest,
                            manifest_path,
                        )
                    else:
                        print(
                            "WARNING: previous live plotting is still running; "
                            "this snapshot was skipped."
                        )
                    next_plot_time = now + plot_interval_seconds
                sleep_seconds = MONITOR_INTERVAL_SECONDS
                if master_deadline is not None:
                    sleep_seconds = min(
                        sleep_seconds,
                        max(0.0, master_deadline - now),
                    )
                time.sleep(sleep_seconds)

    except KeyboardInterrupt:
        stop_reason = "operator pressed Ctrl+C"
        final_status = "stopped"
        print("\nExperiment stop requested by operator.")

    except Exception as error:
        stop_reason = f"runner error: {type(error).__name__}: {error}"
        exit_code = 1
        final_status = "failed"
        print(f"\nERROR: {stop_reason}")

    finally:
        if master_timer_start is not None:
            master_segment_seconds = max(
                0.0,
                time.monotonic() - master_timer_start,
            )
            manifest["master_segment_seconds"] = master_segment_seconds
            manifest["master_accumulated_seconds"] = (
                master_elapsed_before_seconds + master_segment_seconds
            )

        for ready_file in readiness_directory.glob("*.ready"):
            try:
                ready_file.unlink()
            except OSError:
                pass
        try:
            readiness_directory.rmdir()
        except OSError:
            pass

        forcibly_terminated = stop_processes(processes)

        cleanup_results = {
            component: run_emergency_cleanup(component)
            for component in sorted(forcibly_terminated)
        }

        if forcibly_terminated:
            forced_text = (
                "forced termination required for "
                + ", ".join(sorted(forcibly_terminated))
            )
            stop_reason = (
                f"{stop_reason}; {forced_text}" if stop_reason else forced_text
            )
            final_status = "failed"
            if exit_code == 0:
                exit_code = 1

        for component, process in processes.items():
            try:
                return_code = process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                return_code = process.poll()
            manifest["processes"][component]["exit_code"] = return_code

        try:
            stop_periodic_plot_batch(
                active_plot_batch,
                manifest,
                manifest_path,
            )
        except Exception as error:
            manifest["plotting"]["live_shutdown_error"] = (
                f"{type(error).__name__}: {error}"
            )
            print(f"WARNING: live plotter shutdown failed: {error}")

        if "moku" in manifest["processes"]:
            moku_output = manifest["processes"]["moku"].get("output_file")
            try:
                segment_seconds = read_moku_csv_duration(moku_output)
            except (OSError, ValueError) as error:
                print(f"Could not calculate Moku segment duration: {error}")
                segment_seconds = None

            manifest["moku_segment_seconds"] = segment_seconds
            if segment_seconds is not None:
                manifest["moku_accumulated_seconds"] = (
                    float(manifest.get("moku_elapsed_before_seconds", 0.0))
                    + segment_seconds
                )

        manifest["status"] = final_status
        manifest["stop_reason"] = stop_reason
        manifest["forced_termination"] = sorted(forcibly_terminated)
        manifest["emergency_cleanup"] = cleanup_results
        manifest["finished_at"] = datetime.now(UK_TIME).isoformat()
        write_manifest(manifest_path, manifest)

        if AUTO_PLOT_AT_END:
            try:
                run_final_plots(
                    selected,
                    manifest,
                    manifest_path,
                )
            except Exception as error:
                # Plotting is deliberately isolated from experiment outcome.
                # A plot problem is recorded but must not turn off, restart,
                # or otherwise alter any hardware process.
                manifest["plotting"]["final_error"] = (
                    f"{type(error).__name__}: {error}"
                )
                print(f"WARNING: automatic final plotting failed: {error}")
                write_manifest(manifest_path, manifest)

    return exit_code


# =========================
# Main entry point
# =========================

def main():
    args = parse_arguments()

    try:
        if args.config is not None:
            # Keep the configuration-driven implementation behind a lazy
            # import so legacy dry runs and unit tests never import optional
            # YAML, plotting, or hardware-facing modules unnecessarily.
            from eom_stabilisation.cli import run_configured_experiment

            action = (
                "execute"
                if args.execute
                else "preview"
                if args.preview
                else "dry-run"
            )
            return run_configured_experiment(
                args.config,
                action=action,
                assume_yes=args.yes,
            )

        if args.resume is not None:
            manifest_path, _, manifest_family = classify_resume_manifest(
                args.resume
            )
            if manifest_family == "configured":
                if args.mode is not None or args.components is not None:
                    raise ValueError(
                        "Configured --resume cannot be combined with a legacy "
                        "mode or --components."
                    )
                from eom_stabilisation.cli import resume_configured_experiment

                action = (
                    "execute"
                    if args.execute
                    else "preview"
                    if args.preview
                    else "dry-run"
                )
                return resume_configured_experiment(
                    manifest_path,
                    action=action,
                    assume_yes=args.yes,
                )
            if args.preview or args.execute:
                raise ValueError(
                    "--preview and --execute apply to configuration-driven "
                    "manifests; legacy resume retains its existing confirmation "
                    "workflow."
                )

        if (
            args.mode is None
            and args.components is None
            and not args.resume_latest
            and args.resume is None
        ):
            interactive_choice = choose_interactively()
            if interactive_choice == "resume-latest":
                args.resume_latest = True
            else:
                args.mode = interactive_choice

        resume_context = load_resume_context(args)

        if resume_context is None:
            mode, selected = resolve_selection(args)
            base_mode = mode
        else:
            selected = resume_context["selected"]
            previous_mode = str(
                resume_context["manifest"].get("base_mode")
                or resume_context["manifest"].get("mode")
                or "custom"
            )
            base_mode = previous_mode.removeprefix("resume:")
            mode = f"resume:{base_mode}"

        validate_scripts(selected)
        leader = select_leader(selected)
        master_experiment_length_seconds = (
            get_master_experiment_length_seconds()
        )
        (
            master_elapsed_before_seconds,
            master_remaining_seconds,
        ) = prepare_master_duration_resume(
            resume_context,
            master_experiment_length_seconds,
        )
        display_plan(
            mode,
            selected,
            leader,
            master_experiment_length_seconds,
            master_remaining_seconds,
        )

        if resume_context is not None:
            print(f"\nResuming master run: {resume_context['manifest_path']}")
            print(
                f"Last recorded activity was "
                f"{resume_context['gap_minutes']:.1f} minutes ago."
            )
            if not confirm_stale_resume(
                resume_context["gap_minutes"],
                args.resume_warning_minutes,
            ):
                return 0
        moku_target_duration = None
        if "moku" in selected:
            moku_target_duration = get_configured_moku_duration()
            print(
                "\nMoku experiment length from collect_data.py: "
                f"{moku_target_duration / 3600.0:g} h "
                f"({moku_target_duration:g} s)"
            )
            if resume_context is not None and leader == "moku":
                previous_target = resume_context["manifest"].get(
                    "moku_target_duration_seconds"
                )
                validate_moku_resume_target(
                    previous_target,
                    moku_target_duration,
                )

        temperature_schedule = None
        if "temp-control" in selected:
            temperature_schedule = get_temperature_schedule_config()
            if resume_context is not None:
                previous_schedule = resume_context["manifest"].get(
                    "temperature_schedule"
                )
                if (
                    previous_schedule is not None
                    and previous_schedule != temperature_schedule
                ):
                    raise ValueError(
                        "The configured temperature schedule differs from "
                        "the previous run. Restore it before resuming, or "
                        "start a new experiment."
                    )
                if previous_schedule is None:
                    print(
                        "WARNING: The previous manifest predates schedule "
                        "snapshots. The current resume step will be checked "
                        "against its CSV, but later-step changes cannot be "
                        "detected automatically."
                    )

        (
            component_arguments,
            component_environments,
            moku_elapsed_before,
        ) = prepare_resume_execution(
            resume_context,
            leader,
            moku_target_duration,
        )

        if "temp-control" in selected:
            print("\nValidating temperature schedule...")
            run_temperature_dry_run(
                component_arguments.get("temp-control", ())
            )

        if args.dry_run:
            print("\nDry run complete; no experiment hardware was opened.")
            return 0

        if not args.yes:
            confirmation_word = "RESUME" if resume_context else "START"
            if not prompt_for_confirmation(
                confirmation_word,
                "\nCheck the optical and electrical setup, then type "
                f"{confirmation_word} to continue or CANCEL to stop: ",
            ):
                return 0

        if resume_context is None:
            run_folder = make_run_folder()
            previous_executions = []
        else:
            run_folder = resume_context["manifest_path"].parent
            previous_manifest = resume_context["manifest"]
            previous_executions = list(
                previous_manifest.get("previous_executions", [])
            )
            previous_executions.append(
                {
                    "mode": previous_manifest.get("mode"),
                    "started_at": previous_manifest.get("started_at"),
                    "finished_at": previous_manifest.get("finished_at"),
                    "status": previous_manifest.get("status"),
                    "stop_reason": previous_manifest.get("stop_reason"),
                    "processes": previous_manifest.get("processes", {}),
                    "plotting": previous_manifest.get("plotting", {}),
                }
            )
        manifest_path = run_folder / "experiment_manifest.json"
        manifest = {
            "status": "starting",
            "mode": mode,
            "base_mode": base_mode,
            "selected_components": list(selected),
            "leader": leader,
            "started_at": datetime.now(UK_TIME).isoformat(),
            "finished_at": None,
            "stop_reason": None,
            "resumed_from": (
                str(resume_context["manifest_path"])
                if resume_context is not None
                else None
            ),
            "resume_gap_minutes": (
                resume_context["gap_minutes"]
                if resume_context is not None
                else None
            ),
            "previous_executions": previous_executions,
            "master_experiment_length_seconds": (
                master_experiment_length_seconds
            ),
            "master_elapsed_before_seconds": (
                master_elapsed_before_seconds
            ),
            "master_remaining_at_start_seconds": master_remaining_seconds,
            "master_timer_started_at": None,
            "master_segment_seconds": None,
            "master_accumulated_seconds": master_elapsed_before_seconds,
            "moku_target_duration_seconds": moku_target_duration,
            "moku_elapsed_before_seconds": moku_elapsed_before,
            "temperature_schedule": temperature_schedule,
            "forced_termination": [],
            "emergency_cleanup": {},
            "processes": {},
            "plotting": {
                "interval_minutes": AUTO_PLOT_INTERVAL_MINUTES,
                "automatic_final_plots": AUTO_PLOT_AT_END,
                "component_directories": {
                    component: {
                        "in_progress": str(
                            component_plot_directory(
                                run_folder,
                                component,
                                in_progress=True,
                            )
                        ),
                        "final": str(
                            component_plot_directory(
                                run_folder,
                                component,
                                in_progress=False,
                            )
                        ),
                    }
                    for component in selected
                },
                "periodic_batches": [],
                "final_results": [],
                "live_shutdown_error": None,
                "final_error": None,
                "finished_at": None,
            },
        }
        write_manifest(manifest_path, manifest)

        print(f"\nMaster run record: {manifest_path}")
        return supervise(
            selected,
            leader,
            manifest,
            manifest_path,
            component_arguments=component_arguments,
            component_environments=component_environments,
            master_remaining_seconds=master_remaining_seconds,
            master_elapsed_before_seconds=master_elapsed_before_seconds,
        )

    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
