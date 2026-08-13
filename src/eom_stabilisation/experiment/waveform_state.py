"""Pure independent waveform-schedule state machine."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class CountDeliveryTracker:
    """Track bounded-uncertainty NCycle chunks without replaying ambiguity."""

    requested_count: int
    maximum_ambiguous_cycles: int
    initial_chunk_size: int
    scheduled_remaining: int = field(init=False)
    confirmed_cycles: int = 0
    cumulative_ambiguous_cycles: int = 0
    next_chunk_limit: int = field(init=False)
    current_chunk_size: int | None = field(init=False, default=None)
    interrupted_chunk_bounds: list[int] = field(default_factory=list)
    uncertainty_budget_exhausted: bool = False

    def __post_init__(self) -> None:
        for value, name in (
            (self.requested_count, "requested_count"),
            (self.maximum_ambiguous_cycles, "maximum_ambiguous_cycles"),
            (self.initial_chunk_size, "initial_chunk_size"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.initial_chunk_size > self.maximum_ambiguous_cycles:
            raise ValueError("initial chunk exceeds the ambiguity budget")
        self.scheduled_remaining = self.requested_count
        self.next_chunk_limit = self.initial_chunk_size
        self._select_next_chunk()

    def _select_next_chunk(self) -> None:
        if self.scheduled_remaining == 0:
            self.current_chunk_size = None
            return
        remaining_budget = (
            self.maximum_ambiguous_cycles - self.cumulative_ambiguous_cycles
        )
        if remaining_budget < 1:
            self.current_chunk_size = None
            self.uncertainty_budget_exhausted = True
            return
        self.current_chunk_size = min(
            self.scheduled_remaining,
            self.next_chunk_limit,
            remaining_budget,
        )

    @property
    def complete(self) -> bool:
        return self.scheduled_remaining == 0

    @property
    def delivered_bounds(self) -> tuple[int, int]:
        return (
            self.confirmed_cycles,
            self.confirmed_cycles + self.cumulative_ambiguous_cycles,
        )

    @property
    def successful_final_bounds(self) -> tuple[int, int] | None:
        if not self.complete:
            return None
        return (
            self.requested_count - self.cumulative_ambiguous_cycles,
            self.requested_count,
        )

    def mark_completed(self) -> int:
        chunk = self._require_chunk()
        self.confirmed_cycles += chunk
        self.scheduled_remaining -= chunk
        self._select_next_chunk()
        return chunk

    def mark_interrupted(self) -> int:
        chunk = self._require_chunk()
        if self.cumulative_ambiguous_cycles + chunk > self.maximum_ambiguous_cycles:
            raise RuntimeError("interrupted chunk would exceed uncertainty budget")
        self.cumulative_ambiguous_cycles += chunk
        self.interrupted_chunk_bounds.append(chunk)
        self.scheduled_remaining -= chunk
        self.next_chunk_limit = max(1, chunk // 2)
        self._select_next_chunk()
        return chunk

    def _require_chunk(self) -> int:
        if self.current_chunk_size is None:
            raise RuntimeError("no count chunk is currently allocated")
        return self.current_chunk_size

    def chunk_run(self, run: Any, period_s: float) -> Any:
        chunk = self._require_chunk()
        return replace(
            run,
            repeat_count=chunk,
            achieved_duration_s=chunk * period_s,
            exact_hardware_burst=True,
        )


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
    delivered_lower_bound: int | None = None
    delivered_upper_bound: int | None = None
    cumulative_ambiguous_cycles: int = 0
    uncertainty_budget_exhausted: bool = False
    count_recovery_mode: str | None = None


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
        self.count_delivery: CountDeliveryTracker | None = None
        self._retained_delivery_bounds: tuple[int, int] | None = None
        self._retained_cumulative_ambiguous_cycles = 0
        self._retained_uncertainty_budget_exhausted = False
        self._chunk_started_at: float | None = None
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
        action = self.current_action
        if (
            self._pause_duration_during_outage
            and self._outage_started_at is not None
            and (action is None or action.run.mode is not RunMode.DURATION)
        ):
            end = self._outage_started_at
        return max(0.0, end - self._action_started_at)

    def _transition(
        self,
        event: str,
        now: float,
        *,
        command: str | None = None,
        detail: str | None = None,
        facts: Mapping[str, Any] | None = None,
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
            facts=dict(facts or {}),
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
        self.count_delivery = None
        self._retained_delivery_bounds = None
        self._retained_cumulative_ambiguous_cycles = 0
        self._retained_uncertainty_budget_exhausted = False
        self._chunk_started_at = None
        facts: dict[str, Any] = {}
        if action.run.is_bounded_uncertainty_count:
            assert action.run.repeat_count is not None
            assert action.run.initial_chunk_size is not None
            self.count_delivery = CountDeliveryTracker(
                action.run.repeat_count,
                action.run.maximum_ambiguous_cycles,
                action.run.initial_chunk_size,
            )
            self._chunk_started_at = now
            facts = self.count_facts()
        return [
            self._transition(
                "waveform_action_started",
                now,
                command="load_and_start",
                facts=facts,
            )
        ]

    def count_facts(self) -> dict[str, Any]:
        tracker = self.count_delivery
        if tracker is None:
            return {}
        lower, upper = tracker.delivered_bounds
        return {
            "chunk_size": tracker.current_chunk_size,
            "chunk_ambiguity_lower_bound": 0,
            "chunk_ambiguity_upper_bound": tracker.current_chunk_size,
            "cumulative_uncertain_cycles": tracker.cumulative_ambiguous_cycles,
            "maximum_ambiguous_cycles": tracker.maximum_ambiguous_cycles,
            "next_chunk_size": tracker.current_chunk_size,
            "delivered_lower_bound": lower,
            "delivered_upper_bound": upper,
            "scheduled_remaining": tracker.scheduled_remaining,
            "uncertainty_budget_exhausted": tracker.uncertainty_budget_exhausted,
        }

    def active_runtime_run(self) -> Any:
        """Return the action run or its currently allocated safe NCycle chunk."""

        action = self.current_action
        if action is None:
            raise WaveformScheduleError("No waveform action is active.")
        if self.count_delivery is None:
            return action.run
        waveform = self.program.waveforms[action.waveform_name]
        return self.count_delivery.chunk_run(action.run, waveform.achieved_period_s)

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
        action = self.current_action
        assert action is not None
        if (
            self._pause_duration_during_outage
            and self._outage_started_at is not None
            and action.run.mode is not RunMode.DURATION
        ):
            # Non-duration continuous actions may retain the compatibility
            # pause policy until the supervisor calls mark_continuous_restarted().
            return ()
        transitions: list[WaveformTransition] = []
        if experiment_ending and action.run.mode in {
            RunMode.UNTIL_EXPERIMENT_END,
            RunMode.CONTINUOUS,
            RunMode.FOREVER,
        }:
            transitions.extend(self._complete_action(timestamp))
        elif (
            self.count_delivery is not None
            and self._chunk_started_at is not None
            and self.count_delivery.current_chunk_size is not None
            and timestamp - self._chunk_started_at
            >= self.count_delivery.current_chunk_size
            * self.program.waveforms[action.waveform_name].achieved_period_s
        ):
            completed_chunk = self.count_delivery.mark_completed()
            transitions.append(
                self._transition(
                    "count_chunk_completed",
                    timestamp,
                    facts={"completed_chunk_size": completed_chunk, **self.count_facts()},
                )
            )
            if self.count_delivery.complete:
                final_bounds = self.count_delivery.successful_final_bounds
                transitions.append(
                    self._transition(
                        "count_delivery_interval_final",
                        timestamp,
                        facts={
                            **self.count_facts(),
                            "final_delivered_lower_bound": final_bounds[0],
                            "final_delivered_upper_bound": final_bounds[1],
                        },
                    )
                )
                transitions.extend(self._complete_action(timestamp))
            else:
                self._chunk_started_at = timestamp
                transitions.append(
                    self._transition(
                        "count_chunk_started",
                        timestamp,
                        command="start_next_count_chunk",
                        facts=self.count_facts(),
                    )
                )
        elif (
            self.count_delivery is None
            and
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
        self.count_delivery = None
        self._chunk_started_at = None
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
        if self.count_delivery is not None:
            interrupted = self.count_delivery.mark_interrupted()
            self._outage_started_at = timestamp
            self._chunk_started_at = None
            event = (
                "count_uncertainty_budget_exhausted"
                if self.count_delivery.uncertainty_budget_exhausted
                else "count_chunk_interrupted"
            )
            if self.count_delivery.uncertainty_budget_exhausted:
                self.phase = WaveformPhase.FAILED
            return (
                self._transition(
                    event,
                    timestamp,
                    command="retire_worker_and_disable_output",
                    detail=(
                        "The ambiguous chunk is never replayed; physical delivery "
                        "within it is bounded from zero through chunk size."
                    ),
                    facts={
                        **self.count_facts(),
                        "interrupted_chunk_size": interrupted,
                        "ambiguous_cycle_lower_bound": 0,
                        "ambiguous_cycle_upper_bound": interrupted,
                    },
                ),
            )
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

    def mark_count_recovered(
        self, *, now: float | None = None
    ) -> tuple[WaveformTransition, ...]:
        """Resume the next never-before-triggered chunk after output-safe replay."""

        timestamp = self._now(now)
        if self.count_delivery is None or self._outage_started_at is None:
            raise WaveformScheduleError("No bounded count outage is active.")
        self.waveform_session_id += 1
        self.phase_continuity = WaveformContinuity.RESTARTED_FROM_PHASE_ZERO
        self._outage_started_at = None
        if self.count_delivery.uncertainty_budget_exhausted:
            return (
                self._transition(
                    "count_action_incomplete",
                    timestamp,
                    facts=self.count_facts(),
                ),
            )
        if self.count_delivery.complete:
            final_bounds = self.count_delivery.successful_final_bounds
            transitions = [
                self._transition(
                    "count_delivery_interval_final",
                    timestamp,
                    facts={
                        **self.count_facts(),
                        "final_delivered_lower_bound": final_bounds[0],
                        "final_delivered_upper_bound": final_bounds[1],
                    },
                )
            ]
            transitions.extend(self._complete_action(timestamp))
            return tuple(transitions)
        self._chunk_started_at = timestamp
        return (
            self._transition(
                "count_chunk_recovery_started",
                timestamp,
                command="start_next_count_chunk",
                facts=self.count_facts(),
            ),
        )

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
        if (
            self._pause_duration_during_outage
            and action.run.mode is not RunMode.DURATION
            and self._action_started_at is not None
        ):
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
            and action.run.mode is RunMode.COUNT
            and self.phase is WaveformPhase.RUNNING
        )
        delivery_bounds = (
            self.count_delivery.delivered_bounds
            if self.count_delivery is not None
            else self._retained_delivery_bounds
        )
        cumulative_ambiguous_cycles = (
            self.count_delivery.cumulative_ambiguous_cycles
            if self.count_delivery is not None
            else self._retained_cumulative_ambiguous_cycles
        )
        uncertainty_budget_exhausted = (
            self.count_delivery.uncertainty_budget_exhausted
            if self.count_delivery is not None
            else self._retained_uncertainty_budget_exhausted
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
            requested_count=(
                action.run.repeat_count
                if action is not None and action.run.mode is RunMode.COUNT
                else None
            ),
            safe_resume_boundary=(
                self.phase
                not in {WaveformPhase.INDETERMINATE, WaveformPhase.FAILED}
                and not exact_burst_running
            ),
            phase_continuity=self.phase_continuity,
            delivered_lower_bound=(
                None if delivery_bounds is None else delivery_bounds[0]
            ),
            delivered_upper_bound=(
                None if delivery_bounds is None else delivery_bounds[1]
            ),
            cumulative_ambiguous_cycles=cumulative_ambiguous_cycles,
            uncertainty_budget_exhausted=uncertainty_budget_exhausted,
            count_recovery_mode=(
                None
                if action is None or action.run.count_recovery_mode is None
                else action.run.count_recovery_mode.value
            ),
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
            expected_count = (
                expected.run.repeat_count
                if expected.run.mode is RunMode.COUNT
                else None
            )
            if state.requested_count != expected_count:
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
            and expected.run.mode is RunMode.COUNT
        ):
            raise FiniteBurstIndeterminate(
                "A running count action cannot be restored safely without a "
                "confirmed chunk boundary."
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
        self._retained_delivery_bounds = (
            None
            if state.delivered_lower_bound is None
            or state.delivered_upper_bound is None
            else (state.delivered_lower_bound, state.delivered_upper_bound)
        )
        self._retained_cumulative_ambiguous_cycles = (
            state.cumulative_ambiguous_cycles
        )
        self._retained_uncertainty_budget_exhausted = (
            state.uncertainty_budget_exhausted
        )
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
                    "disabled until the replacement session is configured."
                    if command is not None
                    else None
                ),
            ),
        )
