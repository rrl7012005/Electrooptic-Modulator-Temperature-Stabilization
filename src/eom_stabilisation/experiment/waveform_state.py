"""Pure independent waveform-schedule state machine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import time
from typing import Any, Callable, Mapping
import uuid

from eom_stabilisation.moku.models import (
    CompiledWaveformAction,
    CompiledWaveformProgram,
    RunMode,
    WaveformContinuity,
)


class WaveformScheduleError(RuntimeError):
    """Base waveform scheduler error."""


class FiniteBurstIndeterminate(WaveformScheduleError):
    """A connection boundary made a triggered finite burst unknowable."""


class WaveformPhase(str, Enum):
    IDLE = "idle"
    WAITING = "waiting"
    RUNNING = "running"
    COMPLETE = "complete"
    INDETERMINATE = "indeterminate"
    FAILED = "failed"


@dataclass(frozen=True)
class WaveformTransition:
    event: str
    at_elapsed_s: float
    action_index: int | None
    action_name: str | None
    waveform_name: str | None
    phase: WaveformPhase
    waveform_run_id: str | None
    waveform_session_id: int
    phase_continuity: WaveformContinuity
    command: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class WaveformMachineSnapshot:
    phase: WaveformPhase
    action_index: int | None
    action_name: str | None
    waveform_name: str | None
    waveform_run_id: str | None
    waveform_session_id: int
    completed_runtime_s: float
    schedule_elapsed_s: float
    repeat_mode: str | None
    requested_duration_s: float | None
    requested_count: int | None
    safe_resume_boundary: bool
    phase_continuity: WaveformContinuity


class WaveformScheduleStateMachine:
    """Advance Moku actions independently using monotonic time and events."""

    def __init__(
        self,
        program: CompiledWaveformProgram,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        run_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        pause_duration_during_outage: bool = True,
    ) -> None:
        if not isinstance(pause_duration_during_outage, bool):
            raise TypeError("pause_duration_during_outage must be boolean.")
        self.program = program
        self._monotonic = monotonic
        self._run_id_factory = run_id_factory
        self._pause_duration_during_outage = pause_duration_during_outage
        self.phase = WaveformPhase.IDLE
        self.action_index: int | None = None
        self.waveform_run_id: str | None = None
        self.waveform_session_id = 0
        self.phase_continuity = WaveformContinuity.CONTINUOUS
        self._schedule_started_at: float | None = None
        self._action_started_at: float | None = None
        self._outage_started_at: float | None = None
        self._completed_runtime_s = 0.0
        self._last_now: float | None = None
        self._events: list[tuple[str, Mapping[str, Any]]] = []

    @property
    def current_action(self) -> CompiledWaveformAction | None:
        if self.action_index is None:
            return None
        return self.program.actions[self.action_index]

    @property
    def is_terminal(self) -> bool:
        return self.phase in {
            WaveformPhase.COMPLETE,
            WaveformPhase.INDETERMINATE,
            WaveformPhase.FAILED,
        }

    def _now(self, supplied: float | None) -> float:
        now = float(self._monotonic() if supplied is None else supplied)
        if not math.isfinite(now):
            raise WaveformScheduleError("Monotonic time must be finite.")
        if self._last_now is not None and now < self._last_now:
            raise WaveformScheduleError("Monotonic time moved backwards.")
        self._last_now = now
        return now

    def _elapsed(self, now: float) -> float:
        return 0.0 if self._schedule_started_at is None else now - self._schedule_started_at

    def _active_runtime(self, now: float) -> float:
        """Return delivered schedule runtime, excluding a configured outage pause."""

        if self._action_started_at is None:
            return self._completed_runtime_s
        end = now
        if self._pause_duration_during_outage and self._outage_started_at is not None:
            end = self._outage_started_at
        return max(0.0, end - self._action_started_at)

    def _transition(
        self,
        event: str,
        now: float,
        *,
        command: str | None = None,
        detail: str | None = None,
    ) -> WaveformTransition:
        action = self.current_action
        return WaveformTransition(
            event=event,
            at_elapsed_s=self._elapsed(now),
            action_index=self.action_index,
            action_name=None if action is None else action.name,
            waveform_name=None if action is None else action.waveform_name,
            phase=self.phase,
            waveform_run_id=self.waveform_run_id,
            waveform_session_id=self.waveform_session_id,
            phase_continuity=self.phase_continuity,
            command=command,
            detail=detail,
        )

    @staticmethod
    def _canonical_start_mode(value: Any) -> str:
        aliases = {
            "elapsed": "elapsed_experiment_time",
            "after_previous": "after_previous_waveform_action",
            "temperature_stable": "temperature_became_stable",
        }
        text = str(value or "immediately").strip().lower()
        return aliases.get(text, text)

    def _start_condition_met(self, action: CompiledWaveformAction, now: float) -> bool:
        start = action.start
        mode = self._canonical_start_mode(start.get("mode"))
        if mode == "immediately":
            return True
        if mode == "after_previous_waveform_action":
            return self.action_index is not None and self.action_index > 0
        if mode == "elapsed_experiment_time":
            return self._elapsed(now) >= float(start["elapsed_s"])
        if mode == "named_event":
            return any(
                name == str(start["event"])
                for name, _ in self._events
            )
        event_by_mode = {
            "temperature_stage_started": "temperature_stage_started",
            "temperature_became_stable": "temperature_became_stable",
            "temperature_stage_completed": "temperature_stage_completed",
        }
        if mode in event_by_mode:
            expected_event = event_by_mode[mode]
            expected_stage = str(start["temperature_stage"])
            return any(
                name == expected_event
                and str(fields.get("stage_name")) == expected_stage
                for name, fields in self._events
            )
        raise WaveformScheduleError(f"Unsupported start condition {mode!r}.")

    def start(self, *, now: float | None = None) -> tuple[WaveformTransition, ...]:
        timestamp = self._now(now)
        if self.phase is not WaveformPhase.IDLE:
            raise WaveformScheduleError("Waveform schedule has already started.")
        self._schedule_started_at = timestamp
        self.action_index = 0
        self.phase = WaveformPhase.WAITING
        transitions = [self._transition("waveform_schedule_started", timestamp)]
        transitions.extend(self._try_start(timestamp))
        return tuple(transitions)

    def _try_start(self, now: float) -> list[WaveformTransition]:
        action = self.current_action
        if action is None or self.phase is not WaveformPhase.WAITING:
            return []
        if not self._start_condition_met(action, now):
            return []
        self.phase = WaveformPhase.RUNNING
        self._action_started_at = now
        self._outage_started_at = None
        self._completed_runtime_s = 0.0
        self.waveform_run_id = self._run_id_factory()
        self.waveform_session_id += 1
        self.phase_continuity = WaveformContinuity.CONTINUOUS
        return [self._transition("waveform_action_started", now, command="load_and_start")]

    def notify(
        self,
        event: str,
        *,
        fields: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> tuple[WaveformTransition, ...]:
        timestamp = self._now(now)
        event_fields = dict(fields or {})
        self._events.append((event, event_fields))
        transitions: list[WaveformTransition] = []
        action = self.current_action
        if self.phase is WaveformPhase.RUNNING and action is not None:
            if action.run.mode in {
                RunMode.UNTIL_TEMPERATURE_STAGE_END,
                RunMode.FILL_TEMPERATURE_STAGE,
            } and event == "temperature_stage_completed" and str(
                event_fields.get("stage_name")
            ) == action.run.temperature_stage:
                transitions.extend(self._complete_action(timestamp))
        if self.phase is WaveformPhase.WAITING:
            transitions.extend(self._try_start(timestamp))
        return tuple(transitions)

    def update(
        self,
        *,
        now: float | None = None,
        experiment_ending: bool = False,
    ) -> tuple[WaveformTransition, ...]:
        timestamp = self._now(now)
        if self.phase is WaveformPhase.IDLE:
            raise WaveformScheduleError("Call start() before update().")
        if self.is_terminal:
            return ()
        if self.phase is WaveformPhase.WAITING:
            return tuple(self._try_start(timestamp))
        if (
            self._pause_duration_during_outage
            and self._outage_started_at is not None
        ):
            # Default outage policy pauses schedule duration until a complete
            # replay has produced a valid frame and the supervisor calls
            # mark_continuous_restarted().
            return ()
        action = self.current_action
        assert action is not None
        transitions: list[WaveformTransition] = []
        if experiment_ending and action.run.mode in {
            RunMode.UNTIL_EXPERIMENT_END,
            RunMode.CONTINUOUS,
            RunMode.FOREVER,
        }:
            transitions.extend(self._complete_action(timestamp))
        elif (
            action.run.achieved_duration_s is not None
            and self._action_started_at is not None
            and timestamp - self._action_started_at >= action.run.achieved_duration_s
        ):
            transitions.extend(self._complete_action(timestamp))
        return tuple(transitions)

    tick = update

    def _complete_action(self, now: float) -> list[WaveformTransition]:
        action = self.current_action
        assert action is not None
        completed_runtime_s = self._active_runtime(now)
        if action.run.achieved_duration_s is not None:
            completed_runtime_s = min(
                completed_runtime_s,
                action.run.achieved_duration_s,
            )
        self._completed_runtime_s = completed_runtime_s
        transitions = [
            self._transition("waveform_action_completed", now, command="stop_output")
        ]
        assert self.action_index is not None
        if self.action_index == len(self.program.actions) - 1:
            self.phase = WaveformPhase.COMPLETE
            self._action_started_at = None
            transitions.append(self._transition("waveform_schedule_completed", now))
            return transitions
        self.action_index += 1
        self.phase = WaveformPhase.WAITING
        self._action_started_at = None
        self._outage_started_at = None
        self._completed_runtime_s = 0.0
        self.waveform_run_id = None
        transitions.extend(self._try_start(now))
        return transitions

    def mark_connection_lost(
        self, *, now: float | None = None
    ) -> tuple[WaveformTransition, ...]:
        timestamp = self._now(now)
        if self.phase is not WaveformPhase.RUNNING:
            return ()
        if self._outage_started_at is not None:
            return ()
        action = self.current_action
        assert action is not None
        self.phase_continuity = WaveformContinuity.UNCONFIRMED
        if action.run.exact_hardware_burst:
            self.phase = WaveformPhase.INDETERMINATE
            return (
                self._transition(
                    "finite_burst_indeterminate",
                    timestamp,
                    command="retire_worker_and_disable_output",
                    detail="Delivered cycle count is unknowable; the burst is not replayed.",
                ),
            )
        self._outage_started_at = timestamp
        return (self._transition("waveform_connection_lost", timestamp),)

    def mark_continuous_restarted(
        self, *, now: float | None = None
    ) -> tuple[WaveformTransition, ...]:
        timestamp = self._now(now)
        if self.phase is not WaveformPhase.RUNNING:
            raise WaveformScheduleError("No running continuous waveform to restore.")
        action = self.current_action
        assert action is not None
        if action.run.exact_hardware_burst:
            raise FiniteBurstIndeterminate(
                "A finite exact burst cannot be silently replayed after reconnect."
            )
        if self._outage_started_at is None:
            raise WaveformScheduleError(
                "Continuous restart requires a preceding connection-loss boundary."
            )
        if self._pause_duration_during_outage and self._action_started_at is not None:
            self._action_started_at += timestamp - self._outage_started_at
        self._outage_started_at = None
        self.waveform_session_id += 1
        self.phase_continuity = WaveformContinuity.RESTARTED_FROM_PHASE_ZERO
        return (
            self._transition(
                "continuous_waveform_restarted",
                timestamp,
                command="restore_from_phase_zero",
            ),
        )

    def snapshot_state(self, *, now: float | None = None) -> WaveformMachineSnapshot:
        timestamp = self._now(now)
        action = self.current_action
        completed = self._active_runtime(timestamp)
        exact_burst_running = bool(
            action is not None
            and action.run.exact_hardware_burst
            and self.phase is WaveformPhase.RUNNING
        )
        return WaveformMachineSnapshot(
            phase=self.phase,
            action_index=self.action_index,
            action_name=None if action is None else action.name,
            waveform_name=None if action is None else action.waveform_name,
            waveform_run_id=self.waveform_run_id,
            waveform_session_id=self.waveform_session_id,
            completed_runtime_s=completed,
            schedule_elapsed_s=self._elapsed(timestamp),
            repeat_mode=None if action is None else action.run.mode.value,
            requested_duration_s=(
                None if action is None else action.run.requested_duration_s
            ),
            requested_count=None if action is None else action.run.repeat_count,
            safe_resume_boundary=(
                self.phase
                not in {WaveformPhase.INDETERMINATE, WaveformPhase.FAILED}
                and not exact_burst_running
            ),
            phase_continuity=self.phase_continuity,
        )

    def restore(
        self,
        state: WaveformMachineSnapshot,
        *,
        now: float | None = None,
    ) -> tuple[WaveformTransition, ...]:
        """Restore a safe boundary and request explicit hardware replay.

        A running exact-count/NCycle burst is never a safe boundary because a
        stopped process cannot prove how many cycles reached the connector.
        Continuous actions resume from phase zero and increment the session ID.
        """

        timestamp = self._now(now)
        if self.phase is not WaveformPhase.IDLE:
            raise WaveformScheduleError("Restore requires a new idle state machine.")
        if not isinstance(state.phase, WaveformPhase):
            raise WaveformScheduleError("Checkpoint waveform phase is invalid.")
        if not isinstance(state.safe_resume_boundary, bool):
            raise WaveformScheduleError(
                "Checkpoint safe-resume boundary flag must be boolean."
            )
        if not isinstance(state.phase_continuity, WaveformContinuity):
            raise WaveformScheduleError("Checkpoint phase continuity is invalid.")
        if (
            isinstance(state.waveform_session_id, bool)
            or not isinstance(state.waveform_session_id, int)
            or state.waveform_session_id < 0
        ):
            raise WaveformScheduleError("Checkpoint waveform session ID is invalid.")
        if not state.safe_resume_boundary or state.phase is WaveformPhase.INDETERMINATE:
            raise FiniteBurstIndeterminate(
                "The waveform checkpoint is not a safely resumable boundary."
            )
        expected: CompiledWaveformAction | None = None
        if state.action_index is not None:
            if (
                isinstance(state.action_index, bool)
                or not isinstance(state.action_index, int)
                or not 0 <= state.action_index < len(self.program.actions)
            ):
                raise WaveformScheduleError("Checkpoint action index is invalid.")
            expected = self.program.actions[state.action_index]
            if (
                state.action_name != expected.name
                or state.waveform_name != expected.waveform_name
            ):
                raise WaveformScheduleError(
                    "Checkpoint action identity does not match the waveform program."
                )
            if state.repeat_mode != expected.run.mode.value:
                raise WaveformScheduleError(
                    "Checkpoint repeat mode does not match the waveform program."
                )
            if state.requested_count != expected.run.repeat_count:
                raise WaveformScheduleError(
                    "Checkpoint cycle count does not match the waveform program."
                )
            if state.requested_duration_s != expected.run.requested_duration_s:
                raise WaveformScheduleError(
                    "Checkpoint requested duration does not match the waveform program."
                )
        elif state.phase not in {WaveformPhase.IDLE, WaveformPhase.COMPLETE}:
            raise WaveformScheduleError("Checkpoint phase requires an action index.")
        for value, label in (
            (state.completed_runtime_s, "completed_runtime_s"),
            (state.schedule_elapsed_s, "schedule_elapsed_s"),
        ):
            if not math.isfinite(value) or value < 0:
                raise WaveformScheduleError(f"Checkpoint {label} is invalid.")
        if (
            expected is not None
            and expected.run.achieved_duration_s is not None
            and state.completed_runtime_s > expected.run.achieved_duration_s + 1e-9
        ):
            raise WaveformScheduleError(
                "Checkpoint completed runtime exceeds the compiled action duration."
            )
        if state.phase is WaveformPhase.RUNNING and (
            not isinstance(state.waveform_run_id, str) or not state.waveform_run_id
        ):
            raise WaveformScheduleError(
                "A running checkpoint requires a waveform run ID."
            )
        if (
            state.phase is WaveformPhase.RUNNING
            and expected is not None
            and expected.run.exact_hardware_burst
        ):
            raise FiniteBurstIndeterminate(
                "A running exact burst cannot be restored safely."
            )

        self.phase = state.phase
        self.action_index = state.action_index
        self.waveform_run_id = state.waveform_run_id
        self.waveform_session_id = state.waveform_session_id
        self._completed_runtime_s = state.completed_runtime_s
        self._schedule_started_at = timestamp - state.schedule_elapsed_s
        self._action_started_at = (
            timestamp - state.completed_runtime_s
            if state.phase is WaveformPhase.RUNNING
            else None
        )
        self._outage_started_at = None
        self.phase_continuity = state.phase_continuity
        command = None
        if state.phase is WaveformPhase.RUNNING:
            action = self.current_action
            assert action is not None
            self.waveform_session_id += 1
            self.phase_continuity = WaveformContinuity.RESTARTED_FROM_PHASE_ZERO
            command = "restore_from_phase_zero"
        elif state.phase is WaveformPhase.WAITING:
            command = None
        return (
            self._transition(
                "waveform_schedule_restored",
                timestamp,
                command=command,
                detail=(
                    "Continuous hardware state must be replayed with output "
                    "disabled until a valid acquisition frame is verified."
                    if command is not None
                    else None
                ),
            ),
        )
