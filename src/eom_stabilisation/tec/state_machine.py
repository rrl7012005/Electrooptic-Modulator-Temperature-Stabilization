"""A pure, fake-clock-friendly temperature schedule state machine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import time
from typing import Callable

from eom_stabilisation.config.errors import ConfigurationError

from .interface import TecSnapshot
from .schedule import TemperatureSchedule, TemperatureStage


class TemperatureStateError(RuntimeError):
    """Base class for temperature state-machine failures."""


class TemperatureSettlingTimeout(TemperatureStateError, TimeoutError):
    """Raised when a stage fails to qualify as stable in time."""


class InvalidTecSnapshot(TemperatureStateError):
    """Raised when a controller observation is invalid or reports an error."""


class TemperaturePhase(str, Enum):
    """Hardware-independent phases of a temperature schedule."""

    IDLE = "idle"
    WAITING_FOR_STABILITY = "waiting_for_stability"
    HOLDING = "holding"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True)
class TemperatureTransition:
    """One scheduler event for logging and supervisor command dispatch."""

    event: str
    at_elapsed_s: float
    stage_index: int | None
    stage_name: str | None
    phase: TemperaturePhase
    requested_target_c: float | None
    command: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class TemperatureMachineSnapshot:
    """Serializable progress sufficient to restore a safe software boundary."""

    phase: TemperaturePhase
    stage_index: int | None
    stage_name: str | None
    requested_target_c: float | None
    completed_hold_s: float
    stable_elapsed_s: float
    phase_elapsed_s: float
    schedule_elapsed_s: float
    safe_resume_boundary: bool


class TemperatureStateMachine:
    """Advance temperature stages without sleeping or touching hardware."""

    def __init__(
        self,
        schedule: TemperatureSchedule,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.schedule = schedule
        self._monotonic = monotonic
        self.phase = TemperaturePhase.IDLE
        self.stage_index: int | None = None
        self._schedule_started_at: float | None = None
        self._phase_started_at: float | None = None
        self._hold_started_at: float | None = None
        self._stable_since: float | None = None
        self._last_now: float | None = None

    @property
    def current_stage(self) -> TemperatureStage | None:
        if self.stage_index is None:
            return None
        return self.schedule.stages[self.stage_index]

    @property
    def is_terminal(self) -> bool:
        return self.phase in {TemperaturePhase.COMPLETE, TemperaturePhase.FAILED}

    def _now(self, supplied: float | None) -> float:
        now = self._monotonic() if supplied is None else float(supplied)
        if not math.isfinite(now):
            raise TemperatureStateError("Monotonic time must be finite.")
        if self._last_now is not None and now < self._last_now:
            raise TemperatureStateError("Monotonic time moved backwards.")
        self._last_now = now
        return now

    def _elapsed(self, now: float) -> float:
        if self._schedule_started_at is None:
            return 0.0
        return max(0.0, now - self._schedule_started_at)

    def _transition(
        self,
        event: str,
        now: float,
        *,
        command: str | None = None,
        detail: str | None = None,
    ) -> TemperatureTransition:
        stage = self.current_stage
        return TemperatureTransition(
            event=event,
            at_elapsed_s=self._elapsed(now),
            stage_index=self.stage_index,
            stage_name=None if stage is None else stage.name,
            phase=self.phase,
            requested_target_c=None if stage is None else stage.target_c,
            command=command,
            detail=detail,
        )

    def start(self, *, now: float | None = None) -> tuple[TemperatureTransition, ...]:
        """Enter the first stage and return the requested hardware action."""

        timestamp = self._now(now)
        if self.phase is not TemperaturePhase.IDLE:
            raise TemperatureStateError("Temperature schedule has already started.")
        self._schedule_started_at = timestamp
        return self._enter_stage(0, timestamp)

    def _enter_stage(
        self, index: int, now: float
    ) -> tuple[TemperatureTransition, ...]:
        self.stage_index = index
        self._phase_started_at = now
        self._hold_started_at = None
        self._stable_since = None
        stage = self.schedule.stages[index]
        if stage.target_c is None:
            self.phase = TemperaturePhase.HOLDING
            self._hold_started_at = now
            command = "disable_output"
        elif stage.stability.required:
            self.phase = TemperaturePhase.WAITING_FOR_STABILITY
            command = "set_target_and_enable"
        else:
            self.phase = TemperaturePhase.HOLDING
            self._hold_started_at = now
            command = "set_target_and_enable"
        return (self._transition("temperature_stage_started", now, command=command),)

    @staticmethod
    def _validate_snapshot(snapshot: TecSnapshot) -> None:
        for name in ("object_temperature_c", "sink_temperature_c"):
            value = getattr(snapshot, name)
            if not math.isfinite(value):
                raise InvalidTecSnapshot(f"TEC {name} is not finite: {value!r}.")
        for name in ("output_current_a", "output_voltage_v", "active_target_c"):
            value = getattr(snapshot, name)
            if value is not None and not math.isfinite(value):
                raise InvalidTecSnapshot(f"TEC {name} is not finite: {value!r}.")
        if not isinstance(snapshot.temperature_stable, bool):
            raise InvalidTecSnapshot("TEC temperature_stable must be boolean.")
        if snapshot.error_message:
            raise InvalidTecSnapshot(
                f"TEC snapshot reports an error: {snapshot.error_message}"
            )

    def _is_stable(self, stage: TemperatureStage, snapshot: TecSnapshot) -> bool:
        if not snapshot.temperature_stable:
            return False
        tolerance_c = stage.stability.tolerance_c
        if tolerance_c is None:
            return True
        assert stage.target_c is not None
        return abs(snapshot.object_temperature_c - stage.target_c) <= tolerance_c

    def update(
        self,
        snapshot: TecSnapshot | None = None,
        *,
        now: float | None = None,
    ) -> tuple[TemperatureTransition, ...]:
        """Advance using one optional controller observation and current time."""

        timestamp = self._now(now)
        if self.phase is TemperaturePhase.IDLE:
            raise TemperatureStateError("Call start() before update().")
        if self.is_terminal:
            return ()
        stage = self.current_stage
        assert stage is not None and self._phase_started_at is not None
        transitions: list[TemperatureTransition] = []

        try:
            if snapshot is not None:
                self._validate_snapshot(snapshot)

            if self.phase is TemperaturePhase.WAITING_FOR_STABILITY:
                if snapshot is not None and self._is_stable(stage, snapshot):
                    if self._stable_since is None:
                        self._stable_since = timestamp
                        transitions.append(
                            self._transition("temperature_stability_started", timestamp)
                        )
                    stable_elapsed = timestamp - self._stable_since
                    if stable_elapsed >= stage.stability.stable_duration_s:
                        self.phase = TemperaturePhase.HOLDING
                        self._phase_started_at = timestamp
                        self._hold_started_at = timestamp
                        transitions.append(
                            self._transition("temperature_became_stable", timestamp)
                        )
                elif snapshot is not None and self._stable_since is not None:
                    self._stable_since = None
                    transitions.append(
                        self._transition("temperature_stability_lost", timestamp)
                    )

                if self.phase is TemperaturePhase.WAITING_FOR_STABILITY:
                    settling_elapsed = timestamp - self._phase_started_at
                    if settling_elapsed >= stage.stability.timeout_s:
                        self.phase = TemperaturePhase.FAILED
                        transitions.append(
                            self._transition(
                                "temperature_settling_timeout",
                                timestamp,
                                command="apply_error_completion_behavior",
                            )
                        )
                        raise TemperatureSettlingTimeout(
                            f"Temperature stage {stage.name!r} did not remain "
                            f"stable for {stage.stability.stable_duration_s:g} s "
                            f"within {stage.stability.timeout_s:g} s."
                        )

            if self.phase is TemperaturePhase.HOLDING:
                assert self._hold_started_at is not None
                if timestamp - self._hold_started_at >= stage.hold_duration_s:
                    transitions.append(
                        self._transition("temperature_stage_completed", timestamp)
                    )
                    if (
                        stage.completion_behavior == "stop_schedule"
                        or self.stage_index == len(self.schedule.stages) - 1
                    ):
                        self.phase = TemperaturePhase.COMPLETE
                        self._phase_started_at = timestamp
                        transitions.append(
                            self._transition(
                                "temperature_schedule_completed",
                                timestamp,
                                command=self.schedule.completion_behavior,
                            )
                        )
                    else:
                        transitions.extend(
                            self._enter_stage(self.stage_index + 1, timestamp)
                        )
        except InvalidTecSnapshot:
            self.phase = TemperaturePhase.FAILED
            self._phase_started_at = timestamp
            raise

        return tuple(transitions)

    tick = update

    def snapshot_state(
        self, *, now: float | None = None
    ) -> TemperatureMachineSnapshot:
        """Capture independent temperature progress for a runtime checkpoint."""

        timestamp = self._now(now)
        stage = self.current_stage
        phase_elapsed_s = (
            0.0
            if self._phase_started_at is None
            else max(0.0, timestamp - self._phase_started_at)
        )
        completed_hold_s = 0.0
        if self.phase is TemperaturePhase.HOLDING and self._hold_started_at is not None:
            assert stage is not None
            completed_hold_s = min(
                stage.hold_duration_s,
                max(0.0, timestamp - self._hold_started_at),
            )
        stable_elapsed_s = (
            0.0
            if self._stable_since is None
            else max(0.0, timestamp - self._stable_since)
        )
        return TemperatureMachineSnapshot(
            phase=self.phase,
            stage_index=self.stage_index,
            stage_name=None if stage is None else stage.name,
            requested_target_c=None if stage is None else stage.target_c,
            completed_hold_s=completed_hold_s,
            stable_elapsed_s=stable_elapsed_s,
            phase_elapsed_s=phase_elapsed_s,
            schedule_elapsed_s=self._elapsed(timestamp),
            safe_resume_boundary=self.phase is not TemperaturePhase.FAILED,
        )

    def restore(
        self,
        state: TemperatureMachineSnapshot,
        *,
        now: float | None = None,
    ) -> tuple[TemperatureTransition, ...]:
        """Restore software timing; the supervisor must replay hardware safely."""

        timestamp = self._now(now)
        if self.phase is not TemperaturePhase.IDLE:
            raise TemperatureStateError("Restore requires a new idle state machine.")
        if not isinstance(state.phase, TemperaturePhase):
            raise ConfigurationError("Checkpoint temperature phase is invalid.")
        if not state.safe_resume_boundary:
            raise TemperatureStateError("Checkpoint is not a safe resume boundary.")
        if state.stage_index is not None:
            if not 0 <= state.stage_index < len(self.schedule.stages):
                raise ConfigurationError("Checkpoint temperature stage index is invalid.")
            expected = self.schedule.stages[state.stage_index]
            if state.stage_name != expected.name:
                raise ConfigurationError(
                    "Checkpoint temperature stage name does not match the schedule."
                )
            if state.requested_target_c != expected.target_c:
                raise ConfigurationError(
                    "Checkpoint temperature target does not match the schedule."
                )
            if state.completed_hold_s > expected.hold_duration_s + 1e-9:
                raise ConfigurationError(
                    "Checkpoint completed temperature hold exceeds the stage "
                    "duration."
                )
        elif state.phase not in {TemperaturePhase.IDLE, TemperaturePhase.COMPLETE}:
            raise ConfigurationError("Checkpoint phase requires a temperature stage.")
        for value, label in (
            (state.completed_hold_s, "completed_hold_s"),
            (state.stable_elapsed_s, "stable_elapsed_s"),
            (state.phase_elapsed_s, "phase_elapsed_s"),
            (state.schedule_elapsed_s, "schedule_elapsed_s"),
        ):
            if not math.isfinite(value) or value < 0:
                raise ConfigurationError(f"Checkpoint {label} is invalid.")

        self.phase = state.phase
        self.stage_index = state.stage_index
        self._schedule_started_at = timestamp - state.schedule_elapsed_s
        self._phase_started_at = timestamp - state.phase_elapsed_s
        self._hold_started_at = (
            timestamp - state.completed_hold_s
            if state.phase is TemperaturePhase.HOLDING
            else None
        )
        self._stable_since = (
            timestamp - state.stable_elapsed_s
            if state.phase is TemperaturePhase.WAITING_FOR_STABILITY
            and state.stable_elapsed_s > 0
            else None
        )
        command = None
        if state.stage_index is not None and state.phase not in {
            TemperaturePhase.COMPLETE,
            TemperaturePhase.IDLE,
        }:
            command = (
                "disable_output"
                if self.current_stage is not None
                and self.current_stage.target_c is None
                else "set_target_and_enable"
            )
        return (
            self._transition(
                "temperature_schedule_restored",
                timestamp,
                command=command,
                detail="Hardware state must be re-established and verified.",
            ),
        )
