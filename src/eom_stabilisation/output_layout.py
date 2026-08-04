"""Shared filesystem layout for EOM experiment outputs."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from zoneinfo import ZoneInfo


RUN_DIRECTORY_ENVIRONMENT_VARIABLE = "EOM_EXPERIMENT_RUN_DIRECTORY"
APPEND_OUTPUT_ENVIRONMENT_VARIABLE = "EOM_EXPERIMENT_APPEND_OUTPUT"
COMPONENT_OUTPUT_FILE_ENVIRONMENT_VARIABLE = "EOM_COMPONENT_OUTPUT_FILE"

COMPONENT_DIRECTORY_NAMES = {
    "lock": "RP_logs",
    "moku": "Moku_logs",
    "temp-control": "TEC_logs",
    "temp-log": "TEC_logs",
}

UK_TIME = ZoneInfo("Europe/London")


def experiment_results_directory(script_directory: Path) -> Path:
    """Return the repository's single experiment-output root."""

    return Path(script_directory).resolve() / "Experiment Results"


def format_run_directory_name(timestamp: datetime) -> str:
    """Return a readable, sortable and Windows-safe run-directory name."""

    local_timestamp = timestamp.astimezone(UK_TIME)
    return local_timestamp.strftime("run_%Y-%m-%d_%H-%M-%S_%Z")


def create_experiment_run_directory(
    results_directory: Path,
    *,
    timestamp: datetime | None = None,
) -> Path:
    """Create and return a uniquely named experiment run directory."""

    timestamp = timestamp or datetime.now(UK_TIME)
    base_name = format_run_directory_name(timestamp)
    results_directory = Path(results_directory)

    for suffix in range(100):
        suffix_text = "" if suffix == 0 else f"_{suffix:02d}"
        run_directory = results_directory / f"{base_name}{suffix_text}"
        try:
            run_directory.mkdir(parents=True, exist_ok=False)
            return run_directory
        except FileExistsError:
            continue

    raise RuntimeError("Could not create a unique experiment run directory.")


def configured_run_directory(script_directory: Path) -> Path | None:
    """Return the master-provided run directory, when one was configured."""

    configured = os.environ.get(RUN_DIRECTORY_ENVIRONMENT_VARIABLE, "").strip()
    if not configured:
        return None

    run_directory = Path(configured).expanduser()
    if not run_directory.is_absolute():
        run_directory = Path(script_directory).resolve() / run_directory
    return run_directory.resolve()


def resolve_component_directory(
    script_directory: Path,
    component: str,
) -> tuple[Path, Path]:
    """Return the experiment and component directories for a child script."""

    try:
        component_directory_name = COMPONENT_DIRECTORY_NAMES[component]
    except KeyError as error:
        raise ValueError(f"Unknown experiment component: {component}") from error

    run_directory = configured_run_directory(script_directory)
    if run_directory is None:
        run_directory = create_experiment_run_directory(
            experiment_results_directory(script_directory)
        )
    else:
        run_directory.mkdir(parents=True, exist_ok=True)

    component_directory = run_directory / component_directory_name
    component_directory.mkdir(parents=True, exist_ok=True)
    return run_directory, component_directory


def component_output_file(default_path: Path) -> Path:
    """Return a master-provided resume file or the component's default file."""

    configured = os.environ.get(
        COMPONENT_OUTPUT_FILE_ENVIRONMENT_VARIABLE,
        "",
    ).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(default_path)


def append_output_requested() -> bool:
    """Return whether a resumed component should extend its existing output."""

    return os.environ.get(APPEND_OUTPUT_ENVIRONMENT_VARIABLE, "") == "1"


def component_plot_directory(
    run_directory: Path,
    component: str,
    *,
    in_progress: bool,
) -> Path:
    """Return a component-local live or final plot directory."""

    component_directory_name = COMPONENT_DIRECTORY_NAMES[component]
    state = "in_progress" if in_progress else "final"
    return Path(run_directory) / component_directory_name / "plots" / state
