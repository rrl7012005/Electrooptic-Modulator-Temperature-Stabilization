"""Non-blocking automatic plotting for configured experiment runs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping


LOGGER = logging.getLogger(__name__)
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _csv_has_data(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open(encoding="utf-8", errors="replace") as source:
        return bool(source.readline() and source.readline())


def _timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _update_plotting_manifest(run_directory: Path, plotting: Mapping[str, Any]) -> None:
    path = run_directory / "experiment_manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["plotting"] = dict(plotting)
    _atomic_json(path, document)


class ConfiguredPlotManager:
    """Launch plotters asynchronously and retain commands, logs, and results."""

    def __init__(
        self,
        run_directory: str | Path,
        components: tuple[str, ...],
        *,
        interval_s: float | None,
        final_plots: bool,
        monotonic=time.monotonic,
        popen=subprocess.Popen,
        run=subprocess.run,
    ) -> None:
        self.run_directory = Path(run_directory).resolve()
        self.components = tuple(components)
        self.interval_s = interval_s
        self.final_plots = bool(final_plots)
        self.monotonic = monotonic
        self._popen = popen
        self._run = run
        self.next_plot_at: float | None = None
        self.active: dict[str, Any] = {}
        self.active_record: dict[str, Any] | None = None
        manifest_path = self.run_directory / "experiment_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        existing = manifest.get("plotting")
        self.plotting = dict(existing) if isinstance(existing, Mapping) else {}
        self.plotting.update(
            {
                "interval_s": interval_s,
                "automatic_final_plots": self.final_plots,
                "finished_at_utc": None,
            }
        )
        self.plotting.setdefault("periodic_batches", [])
        self.plotting.setdefault("final_results", [])

    def start(self) -> None:
        now = float(self.monotonic())
        self.next_plot_at = None if self.interval_s is None else now + self.interval_s
        self._save()

    def poll(self) -> None:
        self._refresh()
        now = float(self.monotonic())
        if self.next_plot_at is None or now < self.next_plot_at:
            return
        if self.active:
            LOGGER.warning(
                "Previous configured plot snapshot is still running; skipping this interval"
            )
        else:
            self._start_batch()
        self.next_plot_at = now + float(self.interval_s)

    def finish(self) -> None:
        self._stop_active()
        if self.final_plots:
            LOGGER.info("Generating final configured plots and analysis")
            for component in self.components:
                for job_name, command, log_path in self._jobs(
                    component, in_progress=False
                ):
                    record = self._process_record(command, log_path)
                    started = _timestamp_utc()
                    try:
                        with log_path.open("a", encoding="utf-8") as output:
                            result = self._run(
                                command,
                                cwd=REPOSITORY_ROOT,
                                env=self._environment(),
                                stdout=output,
                                stderr=subprocess.STDOUT,
                                check=False,
                                timeout=120.0,
                            )
                        record["exit_code"] = result.returncode
                        if result.returncode != 0:
                            record["error"] = (
                                f"plotter exited with code {result.returncode}"
                            )
                    except (OSError, subprocess.TimeoutExpired) as error:
                        record["error"] = f"{type(error).__name__}: {error}"
                        LOGGER.warning("Final %s plotting failed: %s", job_name, error)
                    record["started_at_utc"] = started
                    record["finished_at_utc"] = _timestamp_utc()
                    self.plotting["final_results"].append(
                        {"component": job_name, **record}
                    )
                    self._save()
        self.plotting["finished_at_utc"] = _timestamp_utc()
        self._save()

    def _start_batch(self) -> None:
        batch = {
            "batch_number": len(self.plotting["periodic_batches"]) + 1,
            "started_at_utc": _timestamp_utc(),
            "finished_at_utc": None,
            "processes": {},
        }
        LOGGER.info("Creating configured in-progress plot snapshot %d", batch["batch_number"])
        for component in self.components:
            for job_name, command, log_path in self._jobs(
                component, in_progress=True
            ):
                record = self._process_record(command, log_path)
                batch["processes"][job_name] = record
                try:
                    with log_path.open("a", encoding="utf-8") as output:
                        process = self._popen(
                            command,
                            cwd=REPOSITORY_ROOT,
                            env=self._environment(),
                            stdout=output,
                            stderr=subprocess.STDOUT,
                        )
                    record["pid"] = process.pid
                    self.active[job_name] = process
                except OSError as error:
                    record["error"] = f"{type(error).__name__}: {error}"
                    LOGGER.warning("Could not start live %s plot: %s", job_name, error)
        if not self.active:
            batch["finished_at_utc"] = _timestamp_utc()
        self.active_record = batch if self.active else None
        self.plotting["periodic_batches"].append(batch)
        self._save()

    def _refresh(self) -> None:
        if not self.active:
            return
        for component, process in tuple(self.active.items()):
            return_code = process.poll()
            if return_code is None:
                continue
            record = self.active_record["processes"][component]
            record["exit_code"] = return_code
            if return_code != 0:
                record["error"] = f"plotter exited with code {return_code}"
                LOGGER.warning("Live %s plotting exited with code %d", component, return_code)
            del self.active[component]
        if not self.active and self.active_record is not None:
            self.active_record["finished_at_utc"] = _timestamp_utc()
            self.active_record = None
            self._save()

    def _stop_active(self) -> None:
        if not self.active:
            return
        deadline = time.monotonic() + 10.0
        while self.active and time.monotonic() < deadline:
            self._refresh()
            if self.active:
                time.sleep(0.1)
        for component, process in tuple(self.active.items()):
            try:
                process.terminate()
                process.wait(timeout=5.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
            record = self.active_record["processes"][component]
            record["exit_code"] = process.poll()
            record["error"] = "stopped before final plotting"
            del self.active[component]
        if self.active_record is not None:
            self.active_record["finished_at_utc"] = _timestamp_utc()
            self.active_record = None
        self._save()

    def _jobs(self, component: str, *, in_progress: bool) -> list[tuple[str, list[str], Path]]:
        state = "in_progress" if in_progress else "final"
        jobs: list[tuple[str, list[str], Path]] = []
        if component == "moku":
            csv_path = self.run_directory / "moku" / "moku_samples.csv"
            output_dir = self.run_directory / "moku" / "plots" / state
            if _csv_has_data(csv_path):
                jobs.append((
                    "moku",
                    [
                        sys.executable,
                        str(REPOSITORY_ROOT / "analyse_eom_csv.py"),
                        str(csv_path),
                        "--output-dir",
                        str(output_dir),
                        "--analysis-dir",
                        str(self.run_directory / "moku"),
                        "--no-show",
                    ],
                    output_dir / "moku_plotting.log",
                ))
            raw_index = self.run_directory / "moku" / "raw_trace_index.csv"
            if _csv_has_data(raw_index):
                jobs.append((
                    "moku-trace",
                    [
                        sys.executable,
                        str(REPOSITORY_ROOT / "plot_moku_trace.py"),
                        str(raw_index),
                        "--output-dir",
                        str(output_dir),
                        "--no-show",
                    ],
                    output_dir / "moku-trace_plotting.log",
                ))
        elif component == "lock":
            csv_path = self.run_directory / "linien" / "linien_log.csv"
            if not _csv_has_data(csv_path):
                return []
            output_dir = self.run_directory / "linien" / "plots" / state
            command = [
                sys.executable,
                str(REPOSITORY_ROOT / "plot_control.py"),
                str(csv_path),
                "--output-dir",
                str(output_dir),
                "--no-show",
            ]
        elif component in {"temp-control", "temp-log"}:
            csv_path = self.run_directory / "temperature" / "tec_log.csv"
            if not _csv_has_data(csv_path):
                return []
            output_dir = self.run_directory / "temperature" / "plots" / state
            command = [
                sys.executable,
                str(REPOSITORY_ROOT / "plot_temp_log.py"),
                str(csv_path),
                "--output-dir",
                str(output_dir),
                "--no-show",
            ]
        else:
            return []
        output_dir.mkdir(parents=True, exist_ok=True)
        if component != "moku":
            jobs.append((component, command, output_dir / f"{component}_plotting.log"))
        if in_progress:
            for _, command, _ in jobs:
                command.append("--in-progress")
        return jobs

    @staticmethod
    def _environment() -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        environment["MPLBACKEND"] = "Agg"
        return environment

    @staticmethod
    def _process_record(command: list[str], log_path: Path) -> dict[str, Any]:
        return {
            "command": command,
            "pid": None,
            "exit_code": None,
            "error": None,
            "log_file": str(log_path),
        }

    def _save(self) -> None:
        _update_plotting_manifest(self.run_directory, self.plotting)


__all__ = ["ConfiguredPlotManager"]
