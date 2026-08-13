"""Confirmed hardware execution for a fully snapshotted experiment plan.

This module is imported only after an operator types ``EXECUTE``.  The public
``run_experiment_loop`` remains dependency-injected so every automated test can
use fake TEC/Moku devices and a fake clock.
"""

from __future__ import annotations

from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
import json
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
from eom_stabilisation.moku.measurement import measure_frame
from eom_stabilisation.moku.models import OutputState
from eom_stabilisation.moku.acquisition import (
    AcquisitionFailureKind,
    classify_acquisition_exception,
)


LOGGER = logging.getLogger(__name__)
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
            payload["exception_class"] = (
                f"{type(error).__module__}.{type(error).__name__}"
            )
            payload["exception_repr"] = repr(error)
            if include_traceback:
                import traceback

                payload["traceback"] = "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(payload, sort_keys=True, allow_nan=False) + "\n")
            output.flush()


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
            run_directory
            / "readiness"
            / f"linien_{uuid.uuid4().hex}.ready"
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
        creation_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        )
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
                    raise RuntimeError("Linien ready file reported an unexpected output.")
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


def _dispatch_commands(
    events: tuple[SupervisorEvent, ...] | list[SupervisorEvent],
    *,
    supervisor: ExperimentSupervisor,
    moku_runtime: Any | None,
    tec_controller: Any | None,
) -> None:
    for event in events:
        command = event.command
        if command is None:
            continue
        if event.source == "moku":
            if moku_runtime is None:
                raise RuntimeError("Moku command was emitted without a Moku runtime.")
            if command == "load_and_start":
                action = supervisor.moku.current_action
                assert action is not None and supervisor.plan.waveform_program is not None
                waveform = supervisor.plan.waveform_program.waveforms[
                    action.waveform_name
                ]
                moku_runtime.switch_waveform(waveform, action.run, start=True)
            elif command == "stop_output":
                moku_runtime.disable()
            elif command == "restore_from_phase_zero":
                action = supervisor.moku.current_action
                assert action is not None and supervisor.plan.waveform_program is not None
                waveform = supervisor.plan.waveform_program.waveforms[
                    action.waveform_name
                ]
                moku_runtime.switch_waveform(waveform, action.run, start=True)
            elif command == "retire_worker_and_disable_output":
                # Recovery commands are executed by _recover_moku(), which
                # must also prove a valid acquisition frame before returning.
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
                tec_controller.apply_completion_behavior(
                    schedule.completion_behavior
                )
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
) -> str | None:
    action = supervisor.moku.current_action
    if action is None or supervisor.moku.phase is not WaveformPhase.RUNNING:
        return None
    plan = supervisor.plan.measurement_plans[action.waveform_name]
    # An exact finite burst is triggered once and is never retriggered merely
    # to satisfy an averaging preference.  Its one complete acquired frame is
    # scientifically preferable to guessing whether another trigger occurred.
    target_frames = 1 if action.run.exact_hardware_burst else frames_per_sample
    results: list[Any] = []
    raw_recorded = False
    accepted_clock: dict[str, Any] | None = None
    runtime_session_id = runtime.state.session_id
    existing_state_rows = writer.provenance + writer.raw_index
    prior_waveform_session = (
        None
        if not existing_state_rows
        else max(
            existing_state_rows,
            key=lambda row: float(row.get("elapsed_s") or -1.0),
        ).get("waveform_session_id")
    )
    first_sample = (
        prior_waveform_session is None
        or int(prior_waveform_session) != supervisor.moku.waveform_session_id
    )
    attempts = 0
    while len(results) < target_frames and not raw_recorded:
        attempts += 1
        if attempts > target_frames + 4:
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
        state = supervisor.sample_state(
            first_sample_after_waveform_change=first_sample
        )
        now = _clock_mapping(clock)
        if plan.raw_only:
            writer.record_raw_trace(
                clock=now,
                frame=frame.data,
                state=state,
                runtime_session_id=frame.session_id,
            )
            accepted_clock = now
            raw_recorded = True
        else:
            results.append(measure_frame(frame.data, plan))

    if results:
        values, counts = _average_results(results)
        accepted_clock = _clock_mapping(clock)
        writer.record_measurement(
            clock=accepted_clock,
            values_by_role=values,
            point_counts_by_role=counts,
            state=supervisor.sample_state(
                first_sample_after_waveform_change=first_sample
            ),
            runtime_session_id=runtime_session_id,
        )
    writer.flush()
    return None if accepted_clock is None else str(accepted_clock["timestamp_utc"])


def _recover_moku(
    *,
    supervisor: ExperimentSupervisor,
    runtime: Any,
    moku_writer: ConfiguredMokuDataWriter,
    event_writer: JsonlExperimentEventWriter,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    lost_at = float(monotonic())
    lost_events = supervisor.mark_moku_connection_lost(now=lost_at)
    _record_supervisor_events(event_writer, lost_events)
    moku_writer.flush()
    if supervisor.moku.phase is WaveformPhase.INDETERMINATE:
        # Runtime recovery is still asked to establish an output-disabled
        # session, but it must raise rather than silently retrigger the burst.
        runtime.recover(start=False)
        raise RuntimeError("Finite Moku burst became indeterminate.")

    recovery_settings = supervisor.plan.experiment.run_settings.recovery
    if recovery_settings.continuous_waveform == "abort":
        try:
            runtime.recover(start=False)
        except Exception as error:
            raise RuntimeError(
                "Continuous Moku connection was lost; configured policy aborts "
                "without re-enabling output, and disabled-state replay failed."
            ) from error
        raise RuntimeError(
            "Continuous Moku connection was lost; configured recovery policy "
            "is abort, so output was not restarted."
        )

    maximum = recovery_settings.maximum_moku_outage_s
    backoffs = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)
    failure: BaseException | None = None
    attempt = 0
    while True:
        if maximum is not None and float(monotonic()) - lost_at >= maximum:
            raise TimeoutError(
                f"Moku recovery exceeded configured maximum outage {maximum:g} s."
            ) from failure
        try:
            runtime.recover(start=True)
            # Recovery is not complete until a valid post-replay frame exists.
            for _ in range(3):
                frame = runtime.get_frame(
                    wait_reacquire=True,
                    wait_complete=False,
                    timeout=10,
                )
                if frame.accepted:
                    break
            else:
                raise RuntimeError("No valid frame followed Moku replay.")
            recovered_at = float(monotonic())
            recovered_events = supervisor.mark_moku_continuous_restarted(
                now=recovered_at
            )
            _record_supervisor_events(event_writer, recovered_events)
            return
        except Exception as error:
            failure = error
            delay = backoffs[min(attempt, len(backoffs) - 1)]
            event_writer.write(
                "moku_recovery_attempt_failed",
                error=f"{type(error).__name__}: {error}",
                attempt=attempt + 1,
                retry_delay_s=delay,
            )
            sleep(delay)
            attempt += 1


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
    return bool(
        {"temp-control", "temp-log"} & set(plan.experiment.components)
    )


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

    checkpoint_store = AtomicCheckpointStore(
        run_directory / "runtime_checkpoint.json"
    )
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
    checkpoint_store = AtomicCheckpointStore(
        run_directory / "runtime_checkpoint.json"
    )
    _validate_runtime_transaction(
        plan,
        run_directory,
        resume=resume,
        checkpoint=checkpoint,
    )

    clock = ExperimentClock(monotonic=monotonic, utc_now=utc_now)
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
            master_events.write("moku_preflight_summary", summary=moku_runtime.summary())

        started = (
            supervisor.start(now=float(monotonic()))
            if checkpoint is None
            else supervisor.restore(checkpoint, now=float(monotonic()))
        )
        _record_supervisor_events(master_events, started)
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
                    if linien is None
                    or getattr(linien, "process", None) is None
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

        while not supervisor.is_terminal:
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
                    )
                    consecutive_transport_errors = 0
                    consecutive_malformed_frames = 0
                except Exception as error:
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
                        _recover_moku(
                            supervisor=supervisor,
                            runtime=moku_runtime,
                            moku_writer=moku_writer,
                            event_writer=master_events,
                            monotonic=monotonic,
                            sleep=sleep,
                        )
                        consecutive_transport_errors = 0
                        consecutive_malformed_frames = 0
                next_moku_sample = float(monotonic()) + retry_delay_s

            transitions = supervisor.update(
                tec_snapshot=tec_snapshot,
                now=float(monotonic()),
            )
            _record_supervisor_events(master_events, transitions)
            _dispatch_commands(
                transitions,
                supervisor=supervisor,
                moku_runtime=moku_runtime,
                tec_controller=tec_controller,
            )
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
                            error=(
                                f"{type(force_error).__name__}: {force_error}"
                            ),
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
                        last_valid_sample_timestamp_utc=(
                            last_valid_sample_timestamp
                        ),
                        output_state=(
                            final_moku_output_state
                            or _moku_output_state(moku_runtime)
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
