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


# =========================
# Experiment definitions
# =========================

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
MASTER_OUTPUT_DIRECTORY = (
    SCRIPT_DIRECTORY / "Experiment Results" / "master_runs"
)
UK_TIME = ZoneInfo("Europe/London")

COMPONENTS = {
    "lock": {
        "label": "Linien lock-point drift logger",
        "script": "linien_logger.py",
        "arguments": [],
        "output_root": "RP_control_logs",
    },
    "moku": {
        "label": "Moku pulse generator and photovoltage logger",
        "script": "collect_data.py",
        "arguments": [],
        "output_root": "Experiment Results/moku_pulse_runs",
    },
    "temp-control": {
        "label": "TEC scheduled temperature controller and logger",
        "script": "tec_temperature_controller.py",
        "arguments": ["--yes"],
        "output_root": "tec_temperature_logs",
    },
    "temp-log": {
        "label": "TEC passive temperature/Peltier logger",
        "script": "tec_temp_logger.py",
        "arguments": ["--mode", "temperature_and_peltier"],
        "output_root": "tec_temperature_logs",
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
        help="run an explicit component combination instead of a preset",
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
        metavar="MANIFEST_OR_RUN_FOLDER",
        help="continue a specific master run",
    )
    parser.add_argument(
        "--resume-warning-minutes",
        type=float,
        default=30.0,
        metavar="MINUTES",
        help="warn when the last run activity is older than this (15-30; default 30)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="start without requiring the operator to type START",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show and validate the plan without connecting to hardware",
    )
    args = parser.parse_args()
    if not 15.0 <= args.resume_warning_minutes <= 30.0:
        parser.error("--resume-warning-minutes must be between 15 and 30")
    return args


def choose_interactively():
    menu = [
        ("1", "full", "lock drift + Moku + temperature control"),
        ("2", "temperature", "temperature control only"),
        ("3", "temperature-lock", "lock drift + temperature control; no Moku"),
        ("4", "drift", "lock drift + Moku + passive temperature log"),
        ("5", "temperature-log", "passive temperature log only"),
        ("6", "resume-latest", "continue the latest run if it was interrupted"),
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


def resolve_selection(args):
    if args.mode is not None and args.components is not None:
        raise ValueError("Choose either a preset mode or --components, not both.")

    if args.components is not None:
        mode = "custom"
        selected = tuple(dict.fromkeys(args.components))
    else:
        mode = args.mode or choose_interactively()
        selected = PRESETS[mode]

    return mode, order_and_validate_components(selected)


def order_and_validate_components(selected):
    unknown = set(selected) - set(COMPONENTS)
    if unknown:
        raise ValueError(
            "Unknown components in previous manifest: "
            + ", ".join(sorted(unknown))
        )

    if "temp-control" in selected and "temp-log" in selected:
        raise ValueError(
            "temp-control and temp-log cannot run together because both use "
            "the TEC serial port. Temperature control already includes logging."
        )

    return tuple(
        component for component in START_ORDER if component in selected
    )


def resolve_manifest_path(path):
    manifest_path = Path(path).expanduser().resolve()
    if manifest_path.is_dir():
        manifest_path = manifest_path / "experiment_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Resume manifest does not exist: {manifest_path}")
    return manifest_path


def read_manifest(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read resume manifest {path}: {error}") from error

    if not isinstance(data, dict):
        raise ValueError(f"Resume manifest is not a JSON object: {path}")
    return data


def find_latest_manifest():
    manifests = sorted(
        MASTER_OUTPUT_DIRECTORY.glob("run_*/experiment_manifest.json"),
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
    active = []
    for component, process_info in manifest.get("processes", {}).items():
        if process_info.get("exit_code") is not None:
            continue
        pid = process_info.get("pid")
        if process_is_running(pid):
            active.append((component, int(pid)))
    return active


def last_activity_time(manifest_path, manifest):
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
    definition = COMPONENTS[component]
    script_path = SCRIPT_DIRECTORY / definition["script"]
    return [
        sys.executable,
        str(script_path),
        *definition["arguments"],
        *extra_arguments,
    ]


def validate_scripts(selected):
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
    """Validate and convert the user-set live plot interval."""
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


def select_leader(selected):
    """Choose the finite process whose completion ends the experiment."""
    if "temp-control" in selected:
        return "temp-control"
    if "moku" in selected:
        return "moku"
    return None


def display_plan(mode, selected, leader):
    print("\nExperiment plan")
    print("===============")
    print(f"Mode: {mode}")
    for component in selected:
        definition = COMPONENTS[component]
        suffix = " (defines experiment duration)" if component == leader else ""
        print(f"- {component}: {definition['label']}{suffix}")

    if leader is None:
        print("- Duration: runs until Ctrl+C")

    print("\nOutput locations")
    for component in selected:
        output_root = SCRIPT_DIRECTORY / COMPONENTS[component]["output_root"]
        print(f"- {component}: {output_root}")

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
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def make_run_folder():
    timestamp = datetime.now(UK_TIME).strftime("%Y%m%d_%H%M%S")
    for suffix in range(100):
        suffix_text = "" if suffix == 0 else f"_{suffix:02d}"
        run_folder = MASTER_OUTPUT_DIRECTORY / f"run_{timestamp}{suffix_text}"
        try:
            run_folder.mkdir(parents=True, exist_ok=False)
            return run_folder
        except FileExistsError:
            continue

    raise RuntimeError("Could not create a unique master run folder.")


def run_temperature_dry_run(extra_arguments=()):
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
    command = [
        sys.executable,
        str(SCRIPT_DIRECTORY / "collect_data.py"),
        "--print-experiment-length",
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


def read_moku_csv_duration(csv_path):
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
    process_info = resume_context["manifest"].get("processes", {}).get(component)
    if process_info is None:
        return None

    output_file = process_info.get("output_file")
    if not output_file:
        if process_info.get("ready"):
            raise ValueError(
                f"Previous {component} process was ready but its output file "
                "is missing from the manifest; it cannot be resumed safely."
            )
        return None

    output_path = Path(output_file)
    if not output_path.is_absolute():
        output_path = SCRIPT_DIRECTORY / output_path
    output_path = output_path.resolve()
    if not output_path.is_file():
        raise FileNotFoundError(
            f"Previous {component} output file does not exist: {output_path}"
        )
    return output_path


def prepare_resume_execution(resume_context, leader, moku_target_duration):
    component_arguments = {}
    component_environments = {}
    moku_elapsed_before = 0.0

    if resume_context is None:
        return component_arguments, component_environments, moku_elapsed_before

    selected = resume_context["selected"]
    previous_manifest = resume_context["manifest"]

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
        component_environments["moku"] = {
            "EOM_MOKU_EXPERIMENT_LENGTH_SECONDS": f"{remaining:.9f}",
        }
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
    """Build a non-interactive plotting command for one component."""
    command = [
        sys.executable,
        str(SCRIPT_DIRECTORY / PLOTTERS[component]),
        str(csv_path),
        "--output-dir",
        str(output_directory),
        "--no-show",
    ]
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
    """Launch one asynchronous live-plot batch and return its runtime state."""
    output_directory = manifest_path.parent / "in_progress_plots"
    output_directory.mkdir(exist_ok=True)
    batch_number = len(manifest["plotting"]["periodic_batches"]) + 1
    batch_record = {
        "batch_number": batch_number,
        "started_at": datetime.now(UK_TIME).isoformat(),
        "finished_at": None,
        "processes": {},
    }
    runtime_processes = {}

    print(f"\nCreating in-progress plot snapshot {batch_number}...")
    for component in selected:
        if component not in PLOTTERS:
            continue
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
    output_directory = manifest_path.parent / "final_plots"
    output_directory.mkdir(exist_ok=True)
    results = []
    print(f"\nGenerating final plots in:\n{output_directory}")

    for component in selected:
        if component not in PLOTTERS:
            continue
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
    command = component_command(component, extra_arguments)
    environment = os.environ.copy()
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
            print(f"{component} is ready.")
            return output_file or None

        time.sleep(0.1)

    raise TimeoutError(
        f"{component} did not report ready within "
        f"{STARTUP_TIMEOUT_SECONDS:g} seconds"
    )


def request_graceful_stop(component, process):
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
):
    processes = {}
    exit_code = 0
    stop_reason = None
    final_status = "completed"
    monitoring_finished = False
    plot_interval_seconds = get_auto_plot_interval_seconds()
    next_plot_time = None
    active_plot_batch = None
    readiness_directory = manifest_path.parent / "readiness"
    readiness_directory.mkdir(exist_ok=True)
    component_arguments = component_arguments or {}
    component_environments = component_environments or {}

    try:
        for component in selected:
            ready_file = readiness_directory / f"{component}.ready"
            process, command = start_component(
                component,
                ready_file,
                extra_arguments=component_arguments.get(component, ()),
                environment_overrides=component_environments.get(component),
            )
            processes[component] = process
            manifest["processes"][component] = {
                "pid": process.pid,
                "command": command,
                "output_root": str(
                    SCRIPT_DIRECTORY / COMPONENTS[component]["output_root"]
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
        print("\nAll selected components are running. Press Ctrl+C to stop.\n")
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
                time.sleep(MONITOR_INTERVAL_SECONDS)

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
        display_plan(mode, selected, leader)

        if resume_context is not None:
            print(f"\nResuming master run: {resume_context['manifest_path']}")
            print(
                f"Last recorded activity was "
                f"{resume_context['gap_minutes']:.1f} minutes ago."
            )
            if (
                resume_context["gap_minutes"]
                > args.resume_warning_minutes
            ):
                print(
                    "WARNING: This exceeds the configured "
                    f"{args.resume_warning_minutes:g}-minute resume window. "
                    "The optical lock and thermal state may no longer match "
                    "the previous segment."
                )
        moku_target_duration = None
        if "moku" in selected:
            moku_target_duration = get_configured_moku_duration()
            if resume_context is not None and leader == "moku":
                previous_target = resume_context["manifest"].get(
                    "moku_target_duration_seconds"
                )
                if previous_target is not None and not math.isclose(
                    float(previous_target),
                    moku_target_duration,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                ):
                    raise ValueError(
                        "The configured Moku experiment length differs from "
                        "the previous run. Restore it before resuming, or "
                        "start a new experiment."
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
            confirmation = input(
                "\nCheck the optical and electrical setup, then type "
                f"{confirmation_word}: "
            ).strip()
            if confirmation != confirmation_word:
                print("Start cancelled; no hardware process was launched.")
                return 0

        run_folder = make_run_folder()
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
            "moku_target_duration_seconds": moku_target_duration,
            "moku_elapsed_before_seconds": moku_elapsed_before,
            "temperature_schedule": temperature_schedule,
            "forced_termination": [],
            "emergency_cleanup": {},
            "processes": {},
            "plotting": {
                "interval_minutes": AUTO_PLOT_INTERVAL_MINUTES,
                "automatic_final_plots": AUTO_PLOT_AT_END,
                "in_progress_directory": str(
                    run_folder / "in_progress_plots"
                ),
                "final_directory": str(run_folder / "final_plots"),
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
        )

    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
