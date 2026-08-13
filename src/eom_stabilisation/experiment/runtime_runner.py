"""Confirmed hardware execution for a fully snapshotted experiment plan.

This module is imported only after an operator types ``EXECUTE``.  The public
``run_experiment_loop`` remains dependency-injected so every automated test can
use fake TEC/Moku devices and a fake clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
import json
import inspect
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Mapping
import uuid
from zoneinfo import ZoneInfo

import numpy as np

from eom_stabilisation.experiment.checkpoint import (
    AtomicCheckpointStore,
    RuntimeCheckpoint,
    validate_resume_checkpoint,
)
from eom_stabilisation.experiment.clock import ExperimentClock
from eom_stabilisation.experiment.data_writer import (
    ConfiguredMokuDataWriter,
    TecDataWriter,
)
from eom_stabilisation.experiment.events import JsonlExperimentEventWriter
from eom_stabilisation.experiment.planner import EffectiveExperimentPlan
from eom_stabilisation.experiment.supervisor import (
    ExperimentSupervisor,
    SupervisorEvent,
    SupervisorPhase,
)
from eom_stabilisation.experiment.waveform_state import WaveformPhase
from eom_stabilisation.experiment.timeline import write_actual_timeline
from eom_stabilisation.moku.measurement import (
    FrameGeometryError,
    FrameMeasurementError,
    InvalidReferenceTraceError,
    OpticalAlignmentError,
    UnusableVoltageSamplesError,
    measure_frame,
)
from eom_stabilisation.moku.models import OutputState, RunMode
from eom_stabilisation.moku.acquisition import (
    AcquisitionFailureKind,
    classify_acquisition_exception,
)


LOGGER = logging.getLogger(__name__)


class ScientificDataHealthError(RuntimeError):
    """The schedule is running but valid reduced optical data has gone stale."""


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
LONDON_TIMEZONE = ZoneInfo("Europe/London")


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sdk_version() -> str:
    try:
        return importlib_metadata.version("moku")
    except importlib_metadata.PackageNotFoundError:
        return "not-installed"


class MokuRuntimeEventWriter:
    """Append SDK/runtime events with serialisable exception diagnostics."""

    def __init__(
        self,
        path: Path,
        configuration: Mapping[str, Any],
        *,
        resume: bool = False,
    ) -> None:
        self.path = path
        self.configuration = dict(configuration)
        if self.path.exists() and not resume:
            raise FileExistsError(
                f"Refusing to append to existing Moku runtime events: {self.path}"
            )

    def write(
        self,
        event: str,
        *,
        error: BaseException | None = None,
        include_traceback: bool = False,
        **fields: Any,
    ) -> None:
        now = datetime.now(timezone.utc)
        payload = {
            "timestamp_utc": now.isoformat().replace("+00:00", "Z"),
            "timestamp_local": now.astimezone(LONDON_TIMEZONE).isoformat(),
            "event": event,
            "moku_sdk_version": _sdk_version(),
            "configuration": self.configuration,
            **fields,
        }
        if error is not None:
            payload[
                "exception_class"
            ] = f"{type(error).__module__}.{type(error).__name__}"
            payload["exception_repr"] = repr(error)
            if include_traceback:
                import traceback

                payload["traceback"] = "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(payload, sort_keys=True, allow_nan=False) + "\n")


class MokuRecoveryLogWriter:
    """Append compact human-readable recovery decisions beside JSONL events."""

    def __init__(self, path: Path, *, resume: bool = False) -> None:
        self.path = path
        if path.exists() and not resume:
            raise FileExistsError(f"Refusing to append to recovery log: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.open("a", encoding="utf-8").close()

    def write(self, event: str, **facts: Any) -> None:
        now = datetime.now(timezone.utc)
        utc = now.isoformat().replace("+00:00", "Z")
        local = now.astimezone(LONDON_TIMEZONE).isoformat()
        rendered = " ".join(
            f"{key}={json.dumps(value, sort_keys=True, allow_nan=False)}"
            for key, value in sorted(facts.items())
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as output:
            output.write(f"{utc} | {local} | {event} | {rendered}\n")


@dataclass
class MokuRecoveryCoordinator:
    """Poll one bounded reconnect attempt at a time from the main event loop."""

    supervisor: ExperimentSupervisor
    runtime: Any
    moku_writer: ConfiguredMokuDataWriter
    event_writer: JsonlExperimentEventWriter
    recovery_log: MokuRecoveryLogWriter
    monotonic: Callable[[], float]
    active: bool = False
    lost_at: float = 0.0
    next_attempt_at: float = 0.0
    attempt: int = 0
    reason: str | None = None
    original_action_name: str | None = None
    failure: BaseException | None = None
    future: Future[Any] | None = None
    executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="moku-recovery"
        ),
        repr=False,
    )

    def begin(self, *, reason: str, cause: BaseException) -> None:
        if self.active:
            return
        now = float(self.monotonic())
        action = self.supervisor.moku.current_action
        self.original_action_name = None if action is None else action.name
        transitions = self.supervisor.mark_moku_connection_lost(now=now)
        # Once this boundary is declared, the last software-side output flag is
        # no longer evidence of the physical connector state.
        self.runtime.state.output_state = OutputState.UNKNOWN
        _record_supervisor_events(self.event_writer, transitions)
        _record_moku_recovery_log_events(self.recovery_log, transitions)
        self.moku_writer.flush()
        self.active = True
        self.lost_at = now
        self.next_attempt_at = now
        self.attempt = 0
        self.reason = reason
        self.failure = cause
        facts = self._facts()
        self.event_writer.write(
            "moku_recovery_started",
            outage_reason=reason,
            physical_output_during_outage="unknown",
            **facts,
        )
        self.recovery_log.write(
            "recovery_started",
            outage_reason=reason,
            physical_output_during_outage="unknown",
            **facts,
        )

    def _facts(self) -> dict[str, Any]:
        machine = self.supervisor.moku
        action = machine.current_action
        now = float(self.monotonic())
        snapshot = machine.snapshot_state(now=now)
        duration_state = None
        if action is not None and action.run.mode is RunMode.DURATION:
            requested = action.run.requested_duration_s
            elapsed = snapshot.completed_runtime_s
            duration_state = {
                "requested_duration_s": requested,
                "elapsed_duration_s": elapsed,
                "remaining_duration_s": (
                    None if requested is None else max(0.0, requested - elapsed)
                ),
                "expired": requested is not None and elapsed >= requested,
                "phase": machine.phase.value,
            }
        facts = {
            "action_name": None if action is None else action.name,
            "waveform_name": None if action is None else action.waveform_name,
            "waveform_run_id": machine.waveform_run_id,
            "waveform_session_id": machine.waveform_session_id,
            "runtime_session_id": self.runtime.state.session_id,
            "physical_output_state": self.runtime.state.output_state.value,
            "duration_state": duration_state,
        }
        facts.update(machine.count_facts())
        return facts

    def poll(self) -> None:
        if not self.active:
            return
        now = float(self.monotonic())
        maximum = (
            self.supervisor.plan.experiment.run_settings.recovery.maximum_moku_outage_s
        )
        if maximum is not None and now - self.lost_at >= maximum:
            raise TimeoutError(
                f"Moku recovery exceeded configured maximum outage {maximum:g} s."
            ) from self.failure
        if self.future is not None and not self.future.done():
            return
        if self.future is None:
            if now < self.next_attempt_at:
                return
            self.attempt += 1
            # Every attempt first establishes a new session with output
            # confirmed disabled. The main loop remains free to service TEC
            # and Linien while this bounded worker call is pending.
            self.future = self.executor.submit(
                _recover_runtime,
                self.runtime,
                start=False,
                tolerate_finite_ambiguity=True,
            )
            self.event_writer.write(
                "moku_reconnect_attempt_started",
                outage_reason=self.reason,
                reconnect_attempt=self.attempt,
                **self._facts(),
            )
            return

        completed_future = self.future
        self.future = None
        try:
            completed_future.result()
        except Exception as error:
            self.failure = error
            delay = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)[min(self.attempt - 1, 5)]
            self.next_attempt_at = now + min(30.0, delay)
            facts = self._facts()
            self.event_writer.write(
                "moku_recovery_attempt_failed",
                error=f"{type(error).__name__}: {error}",
                outage_reason=self.reason,
                reconnect_attempt=self.attempt,
                retry_delay_s=delay,
                **facts,
            )
            self.recovery_log.write(
                "recovery_attempt_failed",
                outage_reason=self.reason,
                reconnect_attempt=self.attempt,
                retry_delay_s=delay,
                error=f"{type(error).__name__}: {error}",
                **facts,
            )
            return

        machine = self.supervisor.moku
        action = machine.current_action
        same_action = bool(
            action is not None and action.name == self.original_action_name
        )
        bounded_count = machine.count_delivery is not None and same_action
        strict_indeterminate = machine.phase is WaveformPhase.INDETERMINATE
        restart_allowed = (
            self.supervisor.plan.experiment.run_settings.recovery.continuous_waveform
            == "restart_from_phase_zero"
        )
        should_restart = bool(
            not bounded_count
            and not strict_indeterminate
            and restart_allowed
            and action is not None
            and action.name == self.original_action_name
            and machine.phase is WaveformPhase.RUNNING
        )
        try:
            recovery_event_facts: dict[str, Any] = {}
            decision = "output_disabled"
            if bounded_count:
                recovered = self.supervisor.mark_moku_count_recovered(now=now)
                _record_supervisor_events(self.event_writer, recovered)
                _record_moku_recovery_log_events(self.recovery_log, recovered)
                for event in recovered:
                    recovery_event_facts.update(dict(event.fields))
                _dispatch_commands(
                    recovered,
                    supervisor=self.supervisor,
                    moku_runtime=self.runtime,
                    tec_controller=None,
                )
                decision = (
                    "next_count_chunk_started"
                    if any(
                        event.command == "start_next_count_chunk" for event in recovered
                    )
                    else "output_disabled"
                )
            elif should_restart:
                assert action is not None
                assert self.supervisor.plan.waveform_program is not None
                waveform = self.supervisor.plan.waveform_program.waveforms[
                    action.waveform_name
                ]
                _switch_moku_waveform(
                    self.runtime,
                    waveform,
                    machine.active_runtime_run(),
                    timebase=self.supervisor.plan.action_timebases[action.name],
                )
                recovered = self.supervisor.mark_moku_continuous_restarted(now=now)
                _record_supervisor_events(self.event_writer, recovered)
                _record_moku_recovery_log_events(self.recovery_log, recovered)
                decision = "waveform_restarted_from_phase_zero"
            elif (
                action is not None
                and action.name != self.original_action_name
                and machine.phase is WaveformPhase.RUNNING
            ):
                assert self.supervisor.plan.waveform_program is not None
                waveform = self.supervisor.plan.waveform_program.waveforms[
                    action.waveform_name
                ]
                _switch_moku_waveform(
                    self.runtime,
                    waveform,
                    machine.active_runtime_run(),
                    timebase=self.supervisor.plan.action_timebases[action.name],
                )
                decision = "next_action_started_from_phase_zero"
            facts = self._facts()
            facts.update(recovery_event_facts)
            self.event_writer.write(
                "moku_recovery_completed",
                outage_reason=self.reason,
                reconnect_attempt=self.attempt,
                output_decision=decision,
                outage_duration_s=now - self.lost_at,
                **facts,
            )
            self.recovery_log.write(
                "recovery_completed",
                outage_reason=self.reason,
                reconnect_attempt=self.attempt,
                output_decision=decision,
                outage_duration_s=now - self.lost_at,
                **facts,
            )
            self.active = False
            if strict_indeterminate:
                raise RuntimeError(
                    "Strict count action became indeterminate; output-disabled "
                    "control was recovered and the ambiguous burst was not replayed."
                )
            if (
                action is not None
                and action.name == self.original_action_name
                and machine.phase is WaveformPhase.RUNNING
                and not restart_allowed
            ):
                raise RuntimeError(
                    "Continuous Moku connection was lost; configured recovery "
                    "policy is abort and output was recovered disabled."
                )
            if machine.phase is WaveformPhase.FAILED:
                raise RuntimeError(
                    "Count action incomplete: uncertainty_budget_exhausted."
                )
        except Exception as error:
            if isinstance(error, RuntimeError) and (
                "Strict count action became indeterminate" in str(error)
                or "uncertainty_budget_exhausted" in str(error)
                or "configured recovery policy is abort" in str(error)
            ):
                raise
            failure_kind = classify_acquisition_exception(error)
            if failure_kind is AcquisitionFailureKind.UNRECOVERABLE:
                raise
            # A reconnect can succeed but activation of the selected action can
            # still lose transport. Account for that new boundary before the
            # next attempt. For count chunks this conservatively makes the
            # newly allocated chunk ambiguous; it is never replayed.
            lost = self.supervisor.mark_moku_connection_lost(now=now)
            _record_supervisor_events(self.event_writer, lost)
            _record_moku_recovery_log_events(self.recovery_log, lost)
            current_action = self.supervisor.moku.current_action
            self.original_action_name = (
                None if current_action is None else current_action.name
            )
            self.failure = error
            delay = min(
                30.0,
                (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)[min(self.attempt - 1, 5)],
            )
            self.next_attempt_at = now + delay
            facts = self._facts()
            self.event_writer.write(
                "moku_recovery_attempt_failed",
                error=f"{type(error).__name__}: {error}",
                outage_reason=self.reason,
                reconnect_attempt=self.attempt,
                retry_delay_s=delay,
                **facts,
            )
            self.recovery_log.write(
                "recovery_attempt_failed",
                outage_reason=self.reason,
                reconnect_attempt=self.attempt,
                retry_delay_s=delay,
                error=f"{type(error).__name__}: {error}",
                **facts,
            )

    def close(self) -> None:
        """Cancel queued work without waiting beyond the runtime's own deadlines."""

        if self.active:
            try:
                self.event_writer.write(
                    "moku_recovery_cancelled",
                    physical_output_during_cleanup="unknown",
                    **self._facts(),
                )
                self.recovery_log.write(
                    "recovery_cancelled",
                    physical_output_during_cleanup="unknown",
                    **self._facts(),
                )
            except Exception:
                LOGGER.exception("Could not record Moku recovery cancellation")
        if self.future is not None and not self.future.done():
            force = getattr(self.runtime, "force_terminate", None)
            if callable(force):
                try:
                    force("recovery_cancelled")
                except Exception:
                    LOGGER.exception(
                        "Could not terminate the active Moku recovery worker"
                    )
            self.future.cancel()
            try:
                self.future.result(timeout=5.0)
            except FutureTimeout:
                LOGGER.error("Moku recovery worker did not stop within 5 seconds")
            except Exception as error:
                LOGGER.debug("Moku recovery worker ended during cleanup: %s", error)
        self.executor.shutdown(wait=False, cancel_futures=True)


class LinienSubprocess:
    """Bounded compatibility wrapper around the retained root Linien logger."""

    def __init__(
        self,
        run_directory: Path,
        *,
        host: str,
        resume: bool = False,
    ) -> None:
        if not isinstance(host, str) or not host.strip():
            raise ValueError("Configured Linien host must be a non-empty string.")
        self.run_directory = run_directory
        self.host = host.strip()
        self.resume = resume
        self.ready_path = (
            run_directory / "readiness" / f"linien_{uuid.uuid4().hex}.ready"
        )
        self.output_path = run_directory / "linien" / "linien_log.csv"
        self.process: subprocess.Popen[Any] | None = None

    def start(self, *, timeout_s: float = 60.0) -> None:
        if self.process is not None:
            raise RuntimeError("Linien logger is already running.")
        self.ready_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.output_path.exists() and not self.resume:
            raise FileExistsError(
                f"Refusing to overwrite Linien raw output: {self.output_path}"
            )
        environment = os.environ.copy()
        environment.update(
            {
                "EOM_EXPERIMENT_RUN_DIRECTORY": str(self.run_directory),
                "EOM_COMPONENT_OUTPUT_FILE": str(self.output_path),
                "EOM_READY_FILE": str(self.ready_path),
                "LINIEN_HOST": self.host,
            }
        )
        if self.resume:
            environment["EOM_EXPERIMENT_APPEND_OUTPUT"] = "1"
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [sys.executable, str(REPOSITORY_ROOT / "linien_logger.py")],
            cwd=REPOSITORY_ROOT,
            env=environment,
            creationflags=creation_flags,
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            return_code = self.process.poll()
            if return_code is not None:
                raise RuntimeError(
                    f"Linien logger exited before ready (code {return_code})."
                )
            if self.ready_path.is_file():
                reported = Path(self.ready_path.read_text(encoding="utf-8").strip())
                if reported.resolve() != self.output_path.resolve():
                    raise RuntimeError(
                        "Linien ready file reported an unexpected output."
                    )
                return
            time.sleep(0.1)
        raise TimeoutError("Linien logger did not become ready within 60 seconds.")

    def raise_if_exited(self) -> None:
        """Fail the configured run if the lock logger ended unexpectedly."""

        if self.process is None:
            raise RuntimeError("Linien logger has not been started.")
        return_code = self.process.poll()
        if return_code is not None:
            raise RuntimeError(
                "Linien logger exited unexpectedly during the experiment "
                f"(code {return_code})."
            )

    def close(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        if os.name == "nt":
            try:
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            except (AttributeError, OSError):
                self.process.terminate()
        else:
            self.process.terminate()
        try:
            self.process.wait(timeout=20.0)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5.0)


def _clock_mapping(clock: ExperimentClock) -> dict[str, Any]:
    return clock.read().to_dict()


def _moku_output_state(runtime: Any | None) -> OutputState:
    if runtime is None:
        return OutputState.UNKNOWN
    value = getattr(getattr(runtime, "state", None), "output_state", None)
    try:
        return value if isinstance(value, OutputState) else OutputState(value)
    except (TypeError, ValueError):
        return OutputState.UNKNOWN


def _temperature_output_state(controller: Any | None) -> OutputState:
    if controller is None:
        return OutputState.UNKNOWN
    enabled = getattr(controller, "output_enabled", None)
    if enabled is True:
        return OutputState.ENABLED
    if enabled is False:
        return OutputState.DISABLED
    return OutputState.UNKNOWN


def _best_effort_event(
    writer: JsonlExperimentEventWriter,
    event: str,
    **fields: Any,
) -> None:
    """Log cleanup diagnostics without preventing later safety actions."""

    try:
        writer.write(event, **fields)
    except Exception:
        LOGGER.exception("Could not record runtime event %s", event)


def _record_supervisor_events(
    writer: JsonlExperimentEventWriter,
    events: tuple[SupervisorEvent, ...] | list[SupervisorEvent],
) -> None:
    for event in events:
        writer.write(
            event.name,
            source=event.source,
            command=event.command,
            supervisor_elapsed_s=event.elapsed_s,
            **dict(event.fields),
        )


def _temperature_state(supervisor: ExperimentSupervisor) -> dict[str, Any]:
    machine = supervisor.temperature
    stage = None if machine is None else machine.current_stage
    return {
        "stage_index": None if machine is None else machine.stage_index,
        "stage_name": None if stage is None else stage.name,
        "phase": "read_only_logging" if machine is None else machine.phase.value,
        "requested_target_c": None if stage is None else stage.target_c,
    }


def _switch_moku_waveform(
    runtime: Any,
    waveform: Any,
    run: Any,
    *,
    timebase: Any,
) -> None:
    """Apply an action timebase while retaining compatibility with old fakes."""

    parameters = inspect.signature(runtime.switch_waveform).parameters
    if "timebase" in parameters:
        runtime.switch_waveform(waveform, run, start=True, timebase=timebase)
    else:
        runtime.switch_waveform(waveform, run, start=True)


def _record_moku_recovery_log_events(
    writer: MokuRecoveryLogWriter | None,
    events: tuple[SupervisorEvent, ...] | list[SupervisorEvent],
) -> None:
    if writer is None:
        return
    interesting = {
        "waveform_action_started",
        "waveform_connection_lost",
        "continuous_waveform_restarted",
        "count_chunk_started",
        "count_chunk_completed",
        "count_chunk_interrupted",
        "count_chunk_recovery_started",
        "count_uncertainty_budget_exhausted",
        "count_action_incomplete",
        "count_delivery_interval_final",
    }
    for event in events:
        if event.source == "moku" and event.name in interesting:
            writer.write(event.name, **dict(event.fields))


def _recover_runtime(
    runtime: Any, *, start: bool, tolerate_finite_ambiguity: bool = False
) -> None:
    parameters = inspect.signature(runtime.recover).parameters
    if "tolerate_finite_ambiguity" in parameters:
        runtime.recover(
            start=start,
            tolerate_finite_ambiguity=tolerate_finite_ambiguity,
        )
    else:
        runtime.recover(start=start)


def _dispatch_commands(
    events: tuple[SupervisorEvent, ...] | list[SupervisorEvent],
    *,
    supervisor: ExperimentSupervisor,
    moku_runtime: Any | None,
    tec_controller: Any | None,
    moku_recovery_active: bool = False,
) -> None:
    for event in events:
        command = event.command
        if command is None:
            continue
        if event.source == "moku":
            if moku_recovery_active:
                # The coordinator will decide whether the current action is
                # still eligible to restart after output-disabled reconnect.
                continue
            if moku_runtime is None:
                raise RuntimeError("Moku command was emitted without a Moku runtime.")
            if command == "load_and_start":
                action = supervisor.moku.current_action
                assert (
                    action is not None and supervisor.plan.waveform_program is not None
                )
                waveform = supervisor.plan.waveform_program.waveforms[
                    action.waveform_name
                ]
                selected_run = supervisor.moku.active_runtime_run()
                _switch_moku_waveform(
                    moku_runtime,
                    waveform,
                    selected_run,
                    timebase=supervisor.plan.action_timebases[action.name],
                )
            elif command == "stop_output":
                moku_runtime.disable()
            elif command == "start_next_count_chunk":
                action = supervisor.moku.current_action
                assert (
                    action is not None and supervisor.plan.waveform_program is not None
                )
                waveform = supervisor.plan.waveform_program.waveforms[
                    action.waveform_name
                ]
                _switch_moku_waveform(
                    moku_runtime,
                    waveform,
                    supervisor.moku.active_runtime_run(),
                    timebase=supervisor.plan.action_timebases[action.name],
                )
            elif command == "restore_from_phase_zero":
                action = supervisor.moku.current_action
                assert (
                    action is not None and supervisor.plan.waveform_program is not None
                )
                waveform = supervisor.plan.waveform_program.waveforms[
                    action.waveform_name
                ]
                _switch_moku_waveform(
                    moku_runtime,
                    waveform,
                    action.run,
                    timebase=supervisor.plan.action_timebases[action.name],
                )
            elif command == "retire_worker_and_disable_output":
                # The non-blocking recovery coordinator owns this command.
                # Normal acquisition validates later frames independently.
                continue
            else:
                raise RuntimeError(f"Unknown Moku supervisor command {command!r}.")
        elif event.source == "temperature":
            if tec_controller is None:
                raise RuntimeError("TEC command was emitted without a controller.")
            if command == "set_target_and_enable":
                target = event.fields.get("requested_target_c")
                if target is None:
                    raise RuntimeError("TEC set-target command has no target.")
                tec_controller.set_target_temperature(float(target))
                tec_controller.set_output_enabled(True)
            elif command == "disable_output":
                tec_controller.set_output_enabled(False)
            elif command == "apply_error_completion_behavior":
                schedule = supervisor.plan.experiment.temperature_schedule
                assert schedule is not None
                tec_controller.apply_completion_behavior(schedule.completion_behavior)
            elif command in {
                "hold_current_target",
                "return_to_safe_target",
                "revert_to_stored_target",
            }:
                tec_controller.apply_completion_behavior(command)
            else:
                raise RuntimeError(f"Unknown TEC supervisor command {command!r}.")


def _average_results(results: list[Any]) -> tuple[dict[str, float], dict[str, int]]:
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for result in results:
        for role, value in result.values_by_role.items():
            point_count = int(result.point_counts_by_role[role])
            totals[role] = totals.get(role, 0.0) + float(value) * point_count
            counts[role] = counts.get(role, 0) + point_count
    return (
        {role: totals[role] / counts[role] for role in totals},
        counts,
    )


def _acquire_moku_sample(
    *,
    supervisor: ExperimentSupervisor,
    runtime: Any,
    writer: ConfiguredMokuDataWriter,
    clock: ExperimentClock,
    event_writer: JsonlExperimentEventWriter,
    frames_per_sample: int,
    runtime_process_id: str,
) -> str | None:
    action = supervisor.moku.current_action
    if action is None or supervisor.moku.phase is not WaveformPhase.RUNNING:
        return None
    plan = supervisor.plan.measurement_plans[action.waveform_name]
    # An exact finite burst is triggered once and is never retriggered merely
    # to satisfy an averaging preference.  Its one complete acquired frame is
    # scientifically preferable to guessing whether another trigger occurred.
    target_frames = 1 if action.run.mode is RunMode.COUNT else frames_per_sample
    results: list[Any] = []
    raw_recorded = False
    reduced_raw_recorded = False
    accepted_clock: dict[str, Any] | None = None
    sample_id = str(uuid.uuid4())
    runtime_session_id = runtime.state.session_id
    existing_state_rows = writer.raw_index if plan.raw_only else writer.provenance
    prior_state = (
        None
        if not existing_state_rows
        else max(
            existing_state_rows,
            key=lambda row: float(row.get("elapsed_s") or -1.0),
        )
    )
    first_action = (
        prior_state is None or str(prior_state.get("moku_action_name")) != action.name
    )
    prior_waveform_session = (
        None if prior_state is None else prior_state.get("waveform_session_id")
    )
    first_session = (
        prior_waveform_session is None
        or int(prior_waveform_session) != supervisor.moku.waveform_session_id
    )
    first_sample = first_action or first_session
    raw_policy = supervisor.plan.experiment.run_settings.moku.raw_capture
    timebase = supervisor.plan.action_timebases[action.name]
    trigger_metadata = {
        "source": supervisor.plan.experiment.run_settings.moku.trigger_source,
        "level_v": supervisor.plan.experiment.run_settings.moku.trigger_level_v,
        "edge": supervisor.plan.experiment.run_settings.moku.trigger_edge,
    }

    def current_state() -> dict[str, Any]:
        result = dict(
            supervisor.sample_state(first_sample_after_waveform_change=first_sample)
        )
        result["first_sample_after_action_change"] = first_action
        result["first_sample_after_session_change"] = first_session
        return result

    def periodic_raw_due() -> bool:
        if raw_policy.reduced_mode == "all":
            return True
        if raw_policy.reduced_mode != "periodic":
            return False
        if raw_policy.first_after_action and first_action:
            return True
        if raw_policy.first_after_session and first_session:
            return True
        if raw_policy.every_n_accepted_samples is not None:
            sample_number = len(writer.provenance) + 1
            return sample_number % raw_policy.every_n_accepted_samples == 0
        assert raw_policy.interval_s is not None
        accepted_reduced_raw = [
            row
            for row in writer.raw_index
            if row.get("frame_status") == "accepted"
            and row.get("measurement_profile") != "raw_only"
        ]
        if not accepted_reduced_raw:
            return True
        latest_raw = max(
            accepted_reduced_raw,
            key=lambda row: float(row.get("elapsed_s") or -1.0),
        )
        return (
            float(_clock_mapping(clock)["elapsed_s"]) - float(latest_raw["elapsed_s"])
            >= raw_policy.interval_s
        )

    attempts = 0
    last_frame_error: FrameMeasurementError | None = None
    while len(results) < target_frames and not raw_recorded:
        attempts += 1
        if attempts > target_frames + 8:
            if last_frame_error is not None:
                raise last_frame_error
            raise RuntimeError("Too many Moku frames were discarded during one sample.")
        frame = runtime.get_frame(
            wait_reacquire=True,
            wait_complete=False,
            timeout=10,
        )
        if not frame.accepted:
            event_writer.write(
                "moku_frame_discarded",
                discard_reason=frame.discard_reason,
                waveform_name=frame.waveform_name,
                runtime_session_id=frame.session_id,
            )
            continue
        frame_id = str(uuid.uuid4())
        state = current_state()
        now = _clock_mapping(clock)
        if plan.raw_only:
            writer.record_raw_trace(
                clock=now,
                frame=frame.data,
                state=state,
                runtime_session_id=frame.session_id,
                sample_id=sample_id,
                frame_id=frame_id,
                runtime_process_id=runtime_process_id,
                runtime_waveform_run_id=frame.waveform_run_id,
                frame_timestamp_utc=frame.data.get("timestamp_utc"),
                timebase=timebase,
                trigger=trigger_metadata,
            )
            accepted_clock = now
            raw_recorded = True
        else:
            try:
                result = measure_frame(
                    frame.data,
                    plan,
                    supervisor.plan.action_timebases[action.name],
                )
            except FrameMeasurementError as error:
                last_frame_error = error
                diagnostic_clock = _clock_mapping(clock)
                if error.diagnostics is not None:
                    writer.record_alignment(
                        clock=diagnostic_clock,
                        state=state,
                        runtime_session_id=frame.session_id,
                        diagnostics=error.diagnostics,
                        frame_timestamp_utc=frame.data.get("timestamp_utc"),
                        sample_id=sample_id,
                        frame_id=frame_id,
                        runtime_process_id=runtime_process_id,
                        runtime_waveform_run_id=frame.waveform_run_id,
                    )
                    if (
                        raw_policy.save_rejected
                        and sum(
                            row.get("sample_id") == sample_id
                            and row.get("frame_status") == "rejected"
                            for row in writer.raw_index
                        )
                        < raw_policy.maximum_rejected_frames_per_sample
                    ):
                        try:
                            writer.record_raw_trace(
                                clock=diagnostic_clock,
                                frame=frame.data,
                                state=state,
                                runtime_session_id=frame.session_id,
                                sample_id=sample_id,
                                frame_id=frame_id,
                                runtime_process_id=runtime_process_id,
                                runtime_waveform_run_id=frame.waveform_run_id,
                                frame_timestamp_utc=frame.data.get("timestamp_utc"),
                                frame_status="rejected",
                                rejection_reason=(
                                    None
                                    if error.diagnostics is None
                                    else error.diagnostics.rejection_reason
                                ),
                                timebase=timebase,
                                trigger=trigger_metadata,
                            )
                        except Exception as raw_error:
                            event_writer.write(
                                "moku_rejected_raw_trace_save_failed",
                                error=f"{type(raw_error).__name__}: {raw_error}",
                            )
                    writer.flush()
                event_writer.write(
                    "moku_frame_rejected",
                    failure_kind=error.failure_kind,
                    rejection_reason=(
                        None
                        if error.diagnostics is None
                        else error.diagnostics.rejection_reason
                    ),
                    waveform_name=frame.waveform_name,
                    runtime_session_id=frame.session_id,
                )
                if isinstance(error, InvalidReferenceTraceError):
                    # Let the outer health counter observe each structurally
                    # invalid ChannelB frame. Five such frames, rather than five
                    # batches of internal retries, may trigger reconnection.
                    raise
                continue
            if result.alignment is not None:
                writer.record_alignment(
                    clock=now,
                    state=state,
                    runtime_session_id=frame.session_id,
                    diagnostics=result.alignment,
                    frame_timestamp_utc=frame.data.get("timestamp_utc"),
                    sample_id=sample_id,
                    frame_id=frame_id,
                    runtime_process_id=runtime_process_id,
                    runtime_waveform_run_id=frame.waveform_run_id,
                )
            results.append(result)
            if periodic_raw_due() and (
                raw_policy.reduced_mode == "all" or not reduced_raw_recorded
            ):
                writer.record_raw_trace(
                    clock=now,
                    frame=frame.data,
                    state=state,
                    runtime_session_id=frame.session_id,
                    sample_id=sample_id,
                    frame_id=frame_id,
                    runtime_process_id=runtime_process_id,
                    runtime_waveform_run_id=frame.waveform_run_id,
                    frame_timestamp_utc=frame.data.get("timestamp_utc"),
                    timebase=timebase,
                    trigger=trigger_metadata,
                )
                reduced_raw_recorded = True

    if results:
        values, counts = _average_results(results)
        accepted_clock = _clock_mapping(clock)
        writer.record_measurement(
            clock=accepted_clock,
            values_by_role=values,
            point_counts_by_role=counts,
            state=current_state(),
            runtime_session_id=runtime_session_id,
            sample_id=sample_id,
            runtime_process_id=runtime_process_id,
            runtime_waveform_run_id=getattr(runtime.state, "waveform_run_id", None),
        )
    writer.flush()
    return None if accepted_clock is None else str(accepted_clock["timestamp_utc"])


def _recover_moku(
    *,
    supervisor: ExperimentSupervisor,
    runtime: Any,
    moku_writer: Any,
    event_writer: Any,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    """Compatibility one-attempt boundary; the live runner uses the coordinator.

    This helper intentionally contains no retry or backoff loop. It remains for
    callers of the v2 internal test seam while persistent recovery is polled by
    :class:`MokuRecoveryCoordinator`.
    """

    del sleep
    lost = supervisor.mark_moku_connection_lost(now=float(monotonic()))
    _record_supervisor_events(event_writer, lost)
    moku_writer.flush()
    recovery = supervisor.plan.experiment.run_settings.recovery
    if recovery.continuous_waveform == "abort":
        _recover_runtime(runtime, start=False)
        raise RuntimeError(
            "Continuous Moku connection was lost; configured recovery policy "
            "is abort, so output was recovered disabled."
        )
    raise RuntimeError(
        "Persistent Moku recovery must be driven by MokuRecoveryCoordinator."
    )


def _lut_hashes(plan: EffectiveExperimentPlan) -> dict[str, str]:
    if plan.waveform_program is None:
        return {}
    return {
        name: waveform.lut_sha256
        for name, waveform in plan.waveform_program.waveforms.items()
    }


def _measurement_roles(plan: EffectiveExperimentPlan) -> set[str]:
    return {
        window.role
        for measurement_plan in plan.measurement_plans.values()
        for window in measurement_plan.windows
    }


def _temperature_component_selected(plan: EffectiveExperimentPlan) -> bool:
    return bool({"temp-control", "temp-log"} & set(plan.experiment.components))


def _validate_real_component_settings(plan: EffectiveExperimentPlan) -> None:
    """Reject incomplete real-only settings before any worker is constructed."""

    if "lock" in plan.experiment.components:
        if plan.experiment.run_settings.linien is None:
            raise ValueError(
                "Configured hardware execution with the lock component requires "
                "run_settings.linien.host."
            )
    if _temperature_component_selected(plan):
        settings = plan.experiment.run_settings.temperature
        if settings is None:
            raise ValueError(
                "Configured TEC execution requires run_settings.temperature."
            )
        if not settings.has_sensor_bounds:
            raise ValueError(
                "Configured TEC execution requires all four apparatus-verified "
                "object/sink temperature plausibility bounds."
            )


def _validate_runtime_transaction(
    plan: EffectiveExperimentPlan,
    run_directory: Path,
    *,
    resume: bool,
    checkpoint: RuntimeCheckpoint | None,
) -> None:
    """Validate hashes and persisted outputs before any device interaction."""

    checkpoint_store = AtomicCheckpointStore(run_directory / "runtime_checkpoint.json")
    if resume != (checkpoint is not None):
        raise ValueError(
            "resume=True and a typed checkpoint must be supplied together."
        )
    if checkpoint is None:
        for path in (
            checkpoint_store.path,
            run_directory / "experiment_events.jsonl",
        ):
            if path.exists():
                raise FileExistsError(f"Refusing to overwrite runtime output: {path}")
    else:
        stored_checkpoint = checkpoint_store.load()
        if stored_checkpoint != checkpoint:
            raise ValueError(
                "The supplied resume checkpoint differs from the checkpoint on disk."
            )
        validate_resume_checkpoint(
            checkpoint,
            configuration_hash=plan.experiment.configuration_hash,
            source_hashes=plan.experiment.source_hashes,
            lut_hashes=_lut_hashes(plan),
        )
        if (plan.experiment.temperature_schedule is None) != (
            checkpoint.temperature is None
        ):
            raise ValueError(
                "Checkpoint temperature state does not match the experiment plan."
            )
        if (plan.waveform_program is None) != (checkpoint.moku is None):
            raise ValueError(
                "Checkpoint Moku state does not match the experiment plan."
            )

    if plan.waveform_program is not None:
        moku_data_writer = ConfiguredMokuDataWriter(
            run_directory,
            _measurement_roles(plan),
            resume=resume,
        )
        if checkpoint is not None:
            assert checkpoint.moku is not None
            moku_data_writer.validate_resume_position(
                checkpoint_timestamp_utc=(
                    checkpoint.moku.last_valid_sample_timestamp_utc
                ),
                checkpoint_elapsed_s=checkpoint.experiment_elapsed_s,
            )
    if _temperature_component_selected(plan):
        tec_data_writer = TecDataWriter(run_directory, resume=resume)
        if checkpoint is not None:
            tec_data_writer.validate_resume_position(
                checkpoint_elapsed_s=checkpoint.experiment_elapsed_s
            )


def _update_manifest(run_directory: Path, **fields: Any) -> None:
    path = run_directory / "experiment_manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(fields)
    _atomic_json(path, document)


def run_experiment_loop(
    plan: EffectiveExperimentPlan,
    run_directory: str | Path,
    *,
    moku_runtime: Any | None,
    tec_controller: Any | None,
    linien: Any | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    utc_now: Callable[[], datetime] | None = None,
    resume: bool = False,
    checkpoint: RuntimeCheckpoint | None = None,
) -> int:
    """Run a plan with injected real or fake devices and bounded cleanup."""

    run_directory = Path(run_directory).resolve()
    checkpoint_store = AtomicCheckpointStore(run_directory / "runtime_checkpoint.json")
    _validate_runtime_transaction(
        plan,
        run_directory,
        resume=resume,
        checkpoint=checkpoint,
    )

    clock = ExperimentClock(monotonic=monotonic, utc_now=utc_now)
    runtime_process_id = str(uuid.uuid4())
    if checkpoint is not None:
        # Keep elapsed time continuous across processes while retaining the
        # new process's monotonic-order checks.
        clock.started_monotonic -= checkpoint.experiment_elapsed_s
    supervisor = ExperimentSupervisor(plan, monotonic=monotonic)
    master_events = JsonlExperimentEventWriter(
        run_directory / "experiment_events.jsonl", clock
    )
    roles = _measurement_roles(plan)
    moku_writer = (
        None
        if plan.waveform_program is None
        else ConfiguredMokuDataWriter(run_directory, roles, resume=resume)
    )
    recovery_log = (
        None
        if moku_writer is None
        else MokuRecoveryLogWriter(
            run_directory / "moku" / "moku_recovery.log",
            resume=resume,
        )
    )
    moku_recovery = (
        None
        if moku_runtime is None or moku_writer is None or recovery_log is None
        else MokuRecoveryCoordinator(
            supervisor=supervisor,
            runtime=moku_runtime,
            moku_writer=moku_writer,
            event_writer=master_events,
            recovery_log=recovery_log,
            monotonic=monotonic,
        )
    )
    tec_writer = (
        None
        if not _temperature_component_selected(plan)
        else TecDataWriter(run_directory, resume=resume)
    )
    last_valid_sample_timestamp = (
        None
        if checkpoint is None or checkpoint.moku is None
        else checkpoint.moku.last_valid_sample_timestamp_utc
    )
    completion_behavior_applied = False
    final_moku_output_state: OutputState | None = None
    final_temperature_output_state: OutputState | None = None
    exit_code = 0

    try:
        if linien is not None:
            linien.start()
        preflight_snapshot = None
        if tec_controller is not None:
            preflight_snapshot = tec_controller.connect()
            if tec_writer is not None:
                tec_writer.record(
                    clock=_clock_mapping(clock),
                    state={
                        "stage_index": None,
                        "stage_name": None,
                        "phase": "preflight",
                        "requested_target_c": None,
                    },
                    snapshot=preflight_snapshot,
                )
                tec_writer.flush()
        if moku_runtime is not None:
            # This proves API responsiveness and records selected ownership;
            # exact analogue routing remains part of the documented smoke test.
            master_events.write(
                "moku_preflight_summary", summary=moku_runtime.summary()
            )

        started = (
            supervisor.start(now=float(monotonic()))
            if checkpoint is None
            else supervisor.restore(checkpoint, now=float(monotonic()))
        )
        _record_supervisor_events(master_events, started)
        _record_moku_recovery_log_events(recovery_log, started)
        _dispatch_commands(
            started,
            supervisor=supervisor,
            moku_runtime=moku_runtime,
            tec_controller=tec_controller,
        )
        checkpoint_store.save(
            supervisor.build_checkpoint(
                lut_hashes=_lut_hashes(plan),
                last_valid_sample_timestamp_utc=last_valid_sample_timestamp,
                output_state=_moku_output_state(moku_runtime),
                temperature_output_state=_temperature_output_state(tec_controller),
                now=float(monotonic()),
            )
        )
        running_timestamp = _clock_mapping(clock)["timestamp_utc"]
        manifest_fields: dict[str, Any] = {
            "status": "running",
            "processes": {
                "master_pid": os.getpid(),
                "moku_worker_pid": (
                    None
                    if moku_runtime is None
                    else getattr(moku_runtime, "worker_pid", None)
                ),
                "linien_pid": (
                    None
                    if linien is None or getattr(linien, "process", None) is None
                    else linien.process.pid
                ),
            },
        }
        manifest_fields[
            "last_resumed_timestamp_utc" if resume else "started_timestamp_utc"
        ] = running_timestamp
        _update_manifest(run_directory, **manifest_fields)
        next_temperature_sample = float(monotonic())
        next_moku_sample = float(monotonic())
        consecutive_transport_errors = 0
        consecutive_malformed_frames = 0
        consecutive_invalid_optical_samples = 0
        invalid_optical_started_at: float | None = None

        while not supervisor.is_terminal or (
            moku_recovery is not None and moku_recovery.active
        ):
            if linien is not None:
                check_linien = getattr(linien, "raise_if_exited", None)
                if callable(check_linien):
                    check_linien()
            now = float(monotonic())
            tec_snapshot = None
            if tec_controller is not None and now >= next_temperature_sample:
                try:
                    tec_snapshot = tec_controller.read_snapshot()
                    if tec_writer is not None:
                        tec_writer.record(
                            clock=_clock_mapping(clock),
                            state=_temperature_state(supervisor),
                            snapshot=tec_snapshot,
                        )
                        tec_writer.flush()
                except Exception as error:
                    if tec_writer is not None:
                        tec_writer.record(
                            clock=_clock_mapping(clock),
                            state=_temperature_state(supervisor),
                            snapshot=None,
                            error=error,
                        )
                        tec_writer.flush()
                    raise
                stage = (
                    None
                    if supervisor.temperature is None
                    else supervisor.temperature.current_stage
                )
                temperature_settings = plan.experiment.run_settings.temperature
                assert temperature_settings is not None
                next_temperature_sample = now + (
                    temperature_settings.sampling_interval_s
                    if stage is None
                    else stage.sampling_interval_s
                )

            if (
                moku_runtime is not None
                and moku_writer is not None
                and (moku_recovery is None or not moku_recovery.active)
                and supervisor.moku.phase is WaveformPhase.RUNNING
                and now >= next_moku_sample
            ):
                settings = plan.experiment.run_settings.moku
                assert settings is not None
                retry_delay_s = settings.sample_period_s
                try:
                    last_valid_sample_timestamp = _acquire_moku_sample(
                        supervisor=supervisor,
                        runtime=moku_runtime,
                        writer=moku_writer,
                        clock=clock,
                        event_writer=master_events,
                        frames_per_sample=settings.frames_per_sample,
                        runtime_process_id=runtime_process_id,
                    )
                    consecutive_transport_errors = 0
                    consecutive_malformed_frames = 0
                    current_action = supervisor.moku.current_action
                    if current_action is not None:
                        current_plan = plan.measurement_plans[
                            current_action.waveform_name
                        ]
                        if current_plan.raw_only:
                            consecutive_invalid_optical_samples = 0
                            invalid_optical_started_at = None
                        elif last_valid_sample_timestamp is not None:
                            consecutive_invalid_optical_samples = 0
                            invalid_optical_started_at = None
                except Exception as error:
                    if isinstance(error, FrameGeometryError):
                        raise
                    if isinstance(error, InvalidReferenceTraceError):
                        failure_kind = AcquisitionFailureKind.INVALID_REFERENCE_TRACE
                    elif isinstance(error, OpticalAlignmentError):
                        failure_kind = AcquisitionFailureKind.OPTICAL_ALIGNMENT_FAILURE
                    elif isinstance(error, UnusableVoltageSamplesError):
                        failure_kind = AcquisitionFailureKind.UNUSABLE_VOLTAGE_SAMPLES
                    else:
                        failure_kind = (
                            AcquisitionFailureKind.MALFORMED_FRAME
                            if isinstance(error, ValueError)
                            else classify_acquisition_exception(error)
                        )
                    master_events.write(
                        "moku_acquisition_failed",
                        error=f"{type(error).__name__}: {error}",
                        failure_kind=failure_kind.value,
                    )
                    reconnect = False
                    if failure_kind is AcquisitionFailureKind.EXPECTED_TRIGGER_TIMEOUT:
                        # A missing optical/trigger edge is reported but is not
                        # evidence that ownership or transport was lost.
                        retry_delay_s = 0.1
                    elif failure_kind in {
                        AcquisitionFailureKind.OPTICAL_ALIGNMENT_FAILURE,
                        AcquisitionFailureKind.UNUSABLE_VOLTAGE_SAMPLES,
                    }:
                        # Scientifically unusable optical data says nothing
                        # about SDK ownership or transport health.
                        retry_delay_s = min(settings.sample_period_s, 0.1)
                        consecutive_invalid_optical_samples += 1
                        if invalid_optical_started_at is None:
                            invalid_optical_started_at = float(monotonic())
                        measurement_health = plan.experiment.run_settings.measurement
                        count_limit = (
                            measurement_health.maximum_consecutive_invalid_optical_samples
                        )
                        duration_limit = (
                            measurement_health.maximum_invalid_optical_duration_s
                        )
                        invalid_duration_s = (
                            float(monotonic()) - invalid_optical_started_at
                        )
                        if (
                            count_limit is not None
                            and consecutive_invalid_optical_samples >= count_limit
                        ) or (
                            duration_limit is not None
                            and invalid_duration_s >= duration_limit
                        ):
                            master_events.write(
                                "scientific_data_health_failed",
                                consecutive_invalid_optical_samples=(
                                    consecutive_invalid_optical_samples
                                ),
                                invalid_optical_duration_s=invalid_duration_s,
                                last_valid_sample_timestamp_utc=(
                                    last_valid_sample_timestamp
                                ),
                            )
                            raise ScientificDataHealthError(
                                "Moku/TEC schedules were stopped because reduced "
                                "optical data remained invalid beyond the configured "
                                "scientific-data health limit"
                            ) from error
                    elif failure_kind is AcquisitionFailureKind.INVALID_REFERENCE_TRACE:
                        consecutive_malformed_frames += 1
                        reconnect = consecutive_malformed_frames >= 5
                    elif failure_kind is AcquisitionFailureKind.MALFORMED_FRAME:
                        consecutive_malformed_frames += 1
                        reconnect = consecutive_malformed_frames >= 5
                    elif failure_kind is AcquisitionFailureKind.TRANSIENT_TRANSPORT:
                        consecutive_transport_errors += 1
                        reconnect = consecutive_transport_errors >= 2
                    elif failure_kind in {
                        AcquisitionFailureKind.WATCHDOG_EXPIRED,
                        AcquisitionFailureKind.STALE_API_CONNECTION,
                        AcquisitionFailureKind.OWNERSHIP_LOSS,
                    }:
                        reconnect = True
                    else:
                        raise
                    if reconnect:
                        assert moku_recovery is not None
                        moku_recovery.begin(
                            reason=failure_kind.value,
                            cause=error,
                        )
                        consecutive_transport_errors = 0
                        consecutive_malformed_frames = 0
                next_moku_sample = float(monotonic()) + retry_delay_s

            transitions = supervisor.update(
                tec_snapshot=tec_snapshot,
                now=float(monotonic()),
            )
            _record_supervisor_events(master_events, transitions)
            _record_moku_recovery_log_events(recovery_log, transitions)
            try:
                _dispatch_commands(
                    transitions,
                    supervisor=supervisor,
                    moku_runtime=moku_runtime,
                    tec_controller=tec_controller,
                    moku_recovery_active=(
                        moku_recovery is not None and moku_recovery.active
                    ),
                )
            except Exception as error:
                has_moku_command = any(
                    event.source == "moku" and event.command is not None
                    for event in transitions
                )
                if not has_moku_command or moku_recovery is None:
                    raise
                failure_kind = classify_acquisition_exception(error)
                if failure_kind is AcquisitionFailureKind.UNRECOVERABLE:
                    raise
                moku_recovery.begin(
                    reason=f"runtime_command_{failure_kind.value}",
                    cause=error,
                )
            if moku_recovery is not None and moku_recovery.active:
                moku_recovery.poll()
                # Injected fake clocks often advance without a real sleep;
                # yield once so the bounded recovery worker can report back.
                time.sleep(0)
            if any(
                event.source == "temperature"
                and event.command
                in {
                    "disable_output",
                    "hold_current_target",
                    "return_to_safe_target",
                    "revert_to_stored_target",
                    "apply_error_completion_behavior",
                }
                and event.name
                in {
                    "temperature_schedule_completed",
                    "temperature_settling_timeout",
                }
                for event in transitions
            ):
                completion_behavior_applied = True
            checkpoint_store.save(
                supervisor.build_checkpoint(
                    lut_hashes=_lut_hashes(plan),
                    last_valid_sample_timestamp_utc=last_valid_sample_timestamp,
                    output_state=_moku_output_state(moku_runtime),
                    temperature_output_state=_temperature_output_state(tec_controller),
                    now=float(monotonic()),
                )
            )
            if not supervisor.is_terminal:
                sleep(0.05)

    except KeyboardInterrupt:
        exit_code = 130
        if supervisor.phase is SupervisorPhase.IDLE:
            supervisor.stop_reason = "operator_ctrl_c_during_preflight"
            _best_effort_event(
                master_events,
                "experiment_interrupted_during_preflight",
            )
        else:
            interrupted = supervisor.interrupt(now=float(monotonic()))
            _record_supervisor_events(master_events, interrupted)
            try:
                _dispatch_commands(
                    interrupted,
                    supervisor=supervisor,
                    moku_runtime=moku_runtime,
                    tec_controller=tec_controller,
                )
            except Exception as error:
                _best_effort_event(
                    master_events,
                    "interrupt_command_failed",
                    error=f"{type(error).__name__}: {error}",
                )
        LOGGER.warning("Configured experiment interrupted by operator.")
    except Exception as error:
        exit_code = 1
        runtime_reason = f"runtime_failure: {type(error).__name__}: {error}"
        supervisor.stop_reason = (
            runtime_reason
            if supervisor.stop_reason is None
            else f"{supervisor.stop_reason}; {runtime_reason}"
        )
        LOGGER.exception("Configured experiment failed")
        _best_effort_event(
            master_events,
            "experiment_runtime_failed",
            error=f"{type(error).__name__}: {error}",
        )
    finally:
        for label, writer in (
            ("moku", moku_writer),
            ("tec", tec_writer),
        ):
            if writer is None:
                continue
            try:
                writer.flush()
            except Exception as error:
                exit_code = 1
                _best_effort_event(
                    master_events,
                    f"{label}_final_flush_failed",
                    error=f"{type(error).__name__}: {error}",
                )
        if moku_recovery is not None:
            moku_recovery.close()
        if moku_runtime is not None:
            try:
                moku_runtime.close()
            except Exception as error:
                exit_code = 1
                final_moku_output_state = OutputState.UNKNOWN
                _best_effort_event(
                    master_events,
                    "moku_final_cleanup_failed",
                    error=f"{type(error).__name__}: {error}",
                )
                force = getattr(moku_runtime, "force_terminate", None)
                if callable(force):
                    try:
                        force("final_cleanup_failed")
                    except Exception as force_error:
                        _best_effort_event(
                            master_events,
                            "moku_force_terminate_failed",
                            error=(f"{type(force_error).__name__}: {force_error}"),
                        )
        schedule = plan.experiment.temperature_schedule
        if tec_controller is not None:
            try:
                if schedule is not None and not completion_behavior_applied:
                    tec_controller.apply_completion_behavior(
                        schedule.completion_behavior
                    )
                    completion_behavior_applied = True
            except Exception as error:
                exit_code = 1
                final_temperature_output_state = OutputState.UNKNOWN
                _best_effort_event(
                    master_events,
                    "tec_completion_behavior_failed",
                    error=f"{type(error).__name__}: {error}",
                )
            try:
                tec_controller.close()
            except Exception as error:
                exit_code = 1
                _best_effort_event(
                    master_events,
                    "tec_close_failed",
                    error=f"{type(error).__name__}: {error}",
                )
        if linien is not None:
            try:
                linien.close()
            except Exception as error:
                exit_code = 1
                _best_effort_event(
                    master_events,
                    "linien_cleanup_failed",
                    error=f"{type(error).__name__}: {error}",
                )
        if supervisor.phase is not SupervisorPhase.IDLE:
            try:
                checkpoint_store.save(
                    supervisor.build_checkpoint(
                        lut_hashes=_lut_hashes(plan),
                        last_valid_sample_timestamp_utc=(last_valid_sample_timestamp),
                        output_state=(
                            final_moku_output_state or _moku_output_state(moku_runtime)
                        ),
                        temperature_output_state=(
                            final_temperature_output_state
                            or _temperature_output_state(tec_controller)
                        ),
                        now=float(monotonic()),
                    )
                )
            except Exception as error:
                exit_code = 1
                _best_effort_event(
                    master_events,
                    "final_checkpoint_failed",
                    error=f"{type(error).__name__}: {error}",
                )
        try:
            event_count = write_actual_timeline(
                master_events.path,
                run_directory / "waveform_timeline.csv",
                run_directory / "waveform_timeline.png",
            )
            _best_effort_event(
                master_events,
                "actual_timeline_written",
                event_count=event_count,
            )
        except Exception as error:
            exit_code = 1
            _best_effort_event(
                master_events,
                "actual_timeline_failed",
                error=f"{type(error).__name__}: {error}",
            )
        finished = _clock_mapping(clock)
        _update_manifest(
            run_directory,
            status=(
                "completed"
                if exit_code == 0 and supervisor.phase is SupervisorPhase.COMPLETE
                else "interrupted"
                if exit_code == 130
                else "failed"
            ),
            stop_reason=supervisor.stop_reason,
            exit_code=exit_code,
            finished_timestamp_utc=finished["timestamp_utc"],
            finished_timestamp_local=finished["timestamp_local"],
            elapsed_s=finished["elapsed_s"],
            temperature_completion_behavior_applied=completion_behavior_applied,
        )
    return exit_code


def run_hardware_experiment(
    plan: EffectiveExperimentPlan,
    artifact_directory: str | Path,
) -> int:
    """Construct real adapters only after validation/snapshot/confirmation."""

    run_directory = Path(artifact_directory).resolve()
    _validate_runtime_transaction(
        plan,
        run_directory,
        resume=False,
        checkpoint=None,
    )
    _validate_real_component_settings(plan)
    from eom_stabilisation.moku.process_worker import ProcessIsolatedMokuRuntime
    from eom_stabilisation.moku.sdk_adapter import MokuRuntimeConfiguration
    from eom_stabilisation.tec.mecom_adapter import MeComTecController

    moku_runtime = None
    tec_controller = None
    linien = None
    if "lock" in plan.experiment.components:
        linien_settings = plan.experiment.run_settings.linien
        assert linien_settings is not None
        linien = LinienSubprocess(run_directory, host=linien_settings.host)
    if plan.waveform_program is not None:
        settings = plan.experiment.run_settings.moku
        assert settings is not None
        configuration = MokuRuntimeConfiguration.from_settings(settings)
        moku_events = MokuRuntimeEventWriter(
            run_directory / "moku" / "acquisition_events.jsonl",
            configuration.metadata(),
            resume=False,
        )
        moku_runtime = ProcessIsolatedMokuRuntime(
            configuration,
            event_writer=moku_events,
        )
    if _temperature_component_selected(plan):
        settings = plan.experiment.run_settings.temperature
        assert settings is not None
        tec_controller = MeComTecController(settings)
    return run_experiment_loop(
        plan,
        run_directory,
        moku_runtime=moku_runtime,
        tec_controller=tec_controller,
        linien=linien,
    )


def resume_hardware_experiment(
    plan: EffectiveExperimentPlan,
    run_directory: str | Path,
    checkpoint: RuntimeCheckpoint,
) -> int:
    """Resume a hash-validated safe snapshot with output initially disabled.

    Full state restoration is performed by the same supervisor/runtime replay
    path.  The caller must already have rejected an indeterminate exact burst.
    """

    run_directory = Path(run_directory).resolve()
    _validate_runtime_transaction(
        plan,
        run_directory,
        resume=True,
        checkpoint=checkpoint,
    )
    _validate_real_component_settings(plan)
    from eom_stabilisation.moku.process_worker import ProcessIsolatedMokuRuntime
    from eom_stabilisation.moku.sdk_adapter import MokuRuntimeConfiguration
    from eom_stabilisation.tec.mecom_adapter import MeComTecController

    moku_runtime = None
    tec_controller = None
    linien = None
    if "lock" in plan.experiment.components:
        linien_settings = plan.experiment.run_settings.linien
        assert linien_settings is not None
        linien = LinienSubprocess(
            run_directory,
            host=linien_settings.host,
            resume=True,
        )
    if plan.waveform_program is not None:
        settings = plan.experiment.run_settings.moku
        assert settings is not None
        configuration = MokuRuntimeConfiguration.from_settings(settings)
        moku_events = MokuRuntimeEventWriter(
            run_directory / "moku" / "acquisition_events.jsonl",
            configuration.metadata(),
            resume=True,
        )
        moku_runtime = ProcessIsolatedMokuRuntime(
            configuration,
            event_writer=moku_events,
        )
    if _temperature_component_selected(plan):
        settings = plan.experiment.run_settings.temperature
        assert settings is not None
        tec_controller = MeComTecController(settings)
    return run_experiment_loop(
        plan,
        run_directory,
        moku_runtime=moku_runtime,
        tec_controller=tec_controller,
        linien=linien,
        resume=True,
        checkpoint=checkpoint,
    )


__all__ = [
    "LinienSubprocess",
    "MokuRuntimeEventWriter",
    "resume_hardware_experiment",
    "run_experiment_loop",
    "run_hardware_experiment",
]
