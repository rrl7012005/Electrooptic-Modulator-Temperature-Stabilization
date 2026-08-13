"""Coordinate independent experiment schedules without owning hardware.

The :class:`ExperimentSupervisor` is deliberately a pure state coordinator.
It accepts observations, emits explicit commands/events, and can therefore be
tested with a fake clock.  Hardware adapters execute the emitted commands in a
separate layer; importing this module never imports ``moku`` or ``mecom``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import math
import time
from typing import Any, Callable, Iterable, Mapping

from eom_stabilisation.config.models import CompletionMode
from eom_stabilisation.experiment.checkpoint import (
    MokuCheckpoint,
    RuntimeCheckpoint,
    TemperatureCheckpoint,
)
from eom_stabilisation.experiment.planner import EffectiveExperimentPlan
from eom_stabilisation.experiment.waveform_state import (
    WaveformMachineSnapshot,
    WaveformPhase,
    WaveformScheduleStateMachine,
    WaveformTransition,
)
from eom_stabilisation.moku.models import OutputState, WaveformContinuity
from eom_stabilisation.tec.interface import TecSnapshot
from eom_stabilisation.tec.state_machine import (
    TemperatureMachineSnapshot,
    TemperaturePhase,
    TemperatureStateMachine,
    TemperatureTransition,
)


class SupervisorError(RuntimeError):
    """Base error for invalid supervisor operation."""


class SupervisorPhase(str, Enum):
    """Lifecycle of the master experiment coordinator."""

    IDLE = "idle"
    RUNNING = "running"
    COMPLETE = "complete"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


@dataclass(frozen=True)
class SupervisorEvent:
    """One ordered supervisor event or hardware command."""

    name: str
    elapsed_s: float
    source: str
    fields: Mapping[str, Any]
    command: str | None = None


@dataclass(frozen=True)
class SupervisorSnapshot:
    """Current independent schedule positions."""

    phase: SupervisorPhase
    elapsed_s: float
    temperature: TemperatureMachineSnapshot | None
    moku: WaveformMachineSnapshot | None
    stop_reason: str | None


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ExperimentSupervisor:
    """Advance temperature and waveform schedules on one monotonic clock.

    Temperature transitions are forwarded to the waveform state machine only
    as named events.  They never implicitly stop or restart a waveform.  In
    the other direction, waveform transitions never change a temperature
    target.
    """

    def __init__(
        self,
        plan: EffectiveExperimentPlan,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.plan = plan
        self._monotonic = monotonic
        self.phase = SupervisorPhase.IDLE
        self.stop_reason: str | None = None
        self._started_at: float | None = None
        self._last_now: float | None = None
        self._events: list[SupervisorEvent] = []
        self._moku_outage_started_at: float | None = None
        self._temperature_paused_s = 0.0
        recovery = plan.experiment.run_settings.recovery
        self.temperature = (
            None
            if plan.experiment.temperature_schedule is None
            else TemperatureStateMachine(
                plan.experiment.temperature_schedule,
                monotonic=monotonic,
            )
        )
        self.moku = (
            None
            if plan.waveform_program is None
            else WaveformScheduleStateMachine(
                plan.waveform_program,
                monotonic=monotonic,
                pause_duration_during_outage=(
                    recovery.waveform_duration_during_outage == "pause_timer"
                ),
            )
        )

    @property
    def events(self) -> tuple[SupervisorEvent, ...]:
        """Return the immutable ordered event history."""

        return tuple(self._events)

    @property
    def is_terminal(self) -> bool:
        return self.phase in {
            SupervisorPhase.COMPLETE,
            SupervisorPhase.INTERRUPTED,
            SupervisorPhase.FAILED,
        }

    def _now(self, supplied: float | None) -> float:
        value = float(self._monotonic() if supplied is None else supplied)
        if not math.isfinite(value):
            raise SupervisorError("Monotonic time must be finite.")
        if self._last_now is not None and value < self._last_now:
            raise SupervisorError("Monotonic time moved backwards.")
        self._last_now = value
        return value

    def _elapsed(self, now: float) -> float:
        return 0.0 if self._started_at is None else max(0.0, now - self._started_at)

    def _temperature_now(self, now: float) -> float:
        """Map wall monotonic time onto the configured temperature timer."""

        adjusted = now - self._temperature_paused_s
        recovery = self.plan.experiment.run_settings.recovery
        if (
            recovery.temperature_hold_during_moku_outage == "pause_timer"
            and self._moku_outage_started_at is not None
        ):
            adjusted -= now - self._moku_outage_started_at
        return adjusted

    def _record(
        self,
        name: str,
        now: float,
        source: str,
        fields: Mapping[str, Any] | None = None,
        command: str | None = None,
    ) -> SupervisorEvent:
        event = SupervisorEvent(
            name=name,
            elapsed_s=self._elapsed(now),
            source=source,
            fields=dict(fields or {}),
            command=command,
        )
        self._events.append(event)
        return event

    def _temperature_records(
        self,
        transitions: Iterable[TemperatureTransition],
        now: float,
    ) -> list[SupervisorEvent]:
        records: list[SupervisorEvent] = []
        for transition in transitions:
            fields = {
                "stage_index": transition.stage_index,
                "stage_name": transition.stage_name,
                "temperature_phase": transition.phase.value,
                "requested_target_c": transition.requested_target_c,
                "detail": transition.detail,
            }
            records.append(
                self._record(
                    transition.event,
                    now,
                    "temperature",
                    fields,
                    transition.command,
                )
            )
            if self.moku is not None:
                linked = self.moku.notify(
                    transition.event,
                    fields=fields,
                    now=now,
                )
                records.extend(self._waveform_records(linked, now))
        return records

    def _waveform_records(
        self,
        transitions: Iterable[WaveformTransition],
        now: float,
    ) -> list[SupervisorEvent]:
        records: list[SupervisorEvent] = []
        for transition in transitions:
            records.append(
                self._record(
                    transition.event,
                    now,
                    "moku",
                    {
                        "action_index": transition.action_index,
                        "action_name": transition.action_name,
                        "waveform_name": transition.waveform_name,
                        "waveform_run_id": transition.waveform_run_id,
                        "waveform_session_id": transition.waveform_session_id,
                        "phase_continuity": transition.phase_continuity.value,
                        "moku_phase": transition.phase.value,
                        "detail": transition.detail,
                        **dict(transition.facts),
                    },
                    transition.command,
                )
            )
        return records

    def start(self, *, now: float | None = None) -> tuple[SupervisorEvent, ...]:
        """Start both selected timelines at the same monotonic instant."""

        timestamp = self._now(now)
        if self.phase is not SupervisorPhase.IDLE:
            raise SupervisorError("Experiment supervisor has already started.")
        self._started_at = timestamp
        self.phase = SupervisorPhase.RUNNING
        records = [self._record("experiment_started", timestamp, "supervisor")]

        # Start both machines before forwarding temperature events.  This lets
        # a waveform linked to the first temperature-stage event start at the
        # same monotonic time while retaining independent state machines.
        temperature_transitions = (
            ()
            if self.temperature is None
            else self.temperature.start(now=self._temperature_now(timestamp))
        )
        waveform_transitions = (
            () if self.moku is None else self.moku.start(now=timestamp)
        )
        records.extend(self._waveform_records(waveform_transitions, timestamp))
        records.extend(self._temperature_records(temperature_transitions, timestamp))
        records.extend(self._finish_if_required(timestamp))
        return tuple(records)

    def restore(
        self,
        checkpoint: RuntimeCheckpoint,
        *,
        now: float | None = None,
    ) -> tuple[SupervisorEvent, ...]:
        """Restore both timelines from a previously hash-validated checkpoint.

        This method restores software positions only.  Returned commands tell
        the runtime runner which output-disabled hardware state must be replayed
        and verified before acquisition resumes.
        """

        timestamp = self._now(now)
        if self.phase is not SupervisorPhase.IDLE:
            raise SupervisorError("Restore requires a new idle supervisor.")
        if checkpoint.configuration_hash != self.plan.experiment.configuration_hash:
            raise SupervisorError("Checkpoint configuration hash does not match plan.")
        self._started_at = timestamp - checkpoint.experiment_elapsed_s
        self.phase = SupervisorPhase.RUNNING
        self.stop_reason = None
        self._moku_outage_started_at = None
        records = [
            self._record(
                "experiment_resumed",
                timestamp,
                "supervisor",
                {
                    "checkpoint_updated_at_utc": checkpoint.updated_at_utc,
                    "resumed_elapsed_s": checkpoint.experiment_elapsed_s,
                },
            )
        ]

        if self.temperature is None:
            if checkpoint.temperature is not None:
                raise SupervisorError(
                    "Checkpoint has temperature state but the plan has no schedule."
                )
        else:
            if checkpoint.temperature is None:
                raise SupervisorError("Checkpoint is missing temperature state.")
            temperature_checkpoint = checkpoint.temperature
            temperature_state = TemperatureMachineSnapshot(
                phase=TemperaturePhase(temperature_checkpoint.phase),
                stage_index=temperature_checkpoint.stage_index,
                stage_name=temperature_checkpoint.stage_name,
                requested_target_c=temperature_checkpoint.requested_target_c,
                completed_hold_s=temperature_checkpoint.completed_hold_s,
                stable_elapsed_s=temperature_checkpoint.stable_elapsed_s,
                phase_elapsed_s=temperature_checkpoint.phase_elapsed_s,
                schedule_elapsed_s=temperature_checkpoint.schedule_elapsed_s,
                safe_resume_boundary=temperature_checkpoint.safe_resume_boundary,
            )
            self._temperature_paused_s = max(
                0.0,
                checkpoint.experiment_elapsed_s
                - temperature_checkpoint.schedule_elapsed_s,
            )
            temperature_transitions = self.temperature.restore(
                temperature_state,
                now=self._temperature_now(timestamp),
            )
            # A restore notification is not a temperature schedule event and
            # must not satisfy a temperature-linked waveform start condition.
            for transition in temperature_transitions:
                records.append(
                    self._record(
                        transition.event,
                        timestamp,
                        "temperature",
                        {
                            "stage_index": transition.stage_index,
                            "stage_name": transition.stage_name,
                            "temperature_phase": transition.phase.value,
                            "requested_target_c": transition.requested_target_c,
                            "detail": transition.detail,
                        },
                        transition.command,
                    )
                )

        if self.moku is None:
            if checkpoint.moku is not None:
                raise SupervisorError(
                    "Checkpoint has Moku state but the plan has no waveform schedule."
                )
        else:
            if checkpoint.moku is None:
                raise SupervisorError("Checkpoint is missing Moku state.")
            moku_checkpoint = checkpoint.moku
            continuity = {
                "continuous": WaveformContinuity.CONTINUOUS,
                "reset_to_phase_zero": WaveformContinuity.RESTARTED_FROM_PHASE_ZERO,
                "unknown": WaveformContinuity.UNCONFIRMED,
                "not_applicable": WaveformContinuity.CONTINUOUS,
            }[moku_checkpoint.phase_continuity]
            moku_state = WaveformMachineSnapshot(
                phase=WaveformPhase(moku_checkpoint.phase),
                action_index=moku_checkpoint.action_index,
                action_name=moku_checkpoint.action_name,
                waveform_name=moku_checkpoint.waveform_name,
                waveform_run_id=moku_checkpoint.waveform_run_id,
                waveform_session_id=moku_checkpoint.waveform_session_id,
                completed_runtime_s=moku_checkpoint.completed_duration_s,
                schedule_elapsed_s=checkpoint.experiment_elapsed_s,
                repeat_mode=moku_checkpoint.repeat_mode,
                requested_duration_s=moku_checkpoint.requested_duration_s,
                requested_count=moku_checkpoint.requested_count,
                safe_resume_boundary=moku_checkpoint.safe_resume_boundary,
                phase_continuity=continuity,
                delivered_lower_bound=moku_checkpoint.delivered_lower_bound,
                delivered_upper_bound=moku_checkpoint.delivered_upper_bound,
                cumulative_ambiguous_cycles=(
                    moku_checkpoint.cumulative_ambiguous_cycles
                ),
                uncertainty_budget_exhausted=(
                    moku_checkpoint.uncertainty_budget_exhausted
                ),
                count_recovery_mode=moku_checkpoint.count_recovery_mode,
            )
            records.extend(
                self._waveform_records(
                    self.moku.restore(moku_state, now=timestamp),
                    timestamp,
                )
            )
        records.extend(self._finish_if_required(timestamp))
        return tuple(records)

    def emit_named_event(
        self,
        name: str,
        *,
        fields: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> tuple[SupervisorEvent, ...]:
        """Publish an explicit named event and notify the Moku schedule."""

        timestamp = self._now(now)
        if self.phase is not SupervisorPhase.RUNNING:
            raise SupervisorError("Named events require a running experiment.")
        records = [self._record(name, timestamp, "external", fields)]
        if self.moku is not None:
            transitions = self.moku.notify(name, fields=fields, now=timestamp)
            records.extend(self._waveform_records(transitions, timestamp))
        return tuple(records)

    def update(
        self,
        *,
        tec_snapshot: TecSnapshot | None = None,
        now: float | None = None,
    ) -> tuple[SupervisorEvent, ...]:
        """Advance each schedule once and apply the master completion policy."""

        timestamp = self._now(now)
        if self.phase is SupervisorPhase.IDLE:
            raise SupervisorError("Call start() before update().")
        if self.is_terminal:
            return ()
        records: list[SupervisorEvent] = []
        try:
            recovery = self.plan.experiment.run_settings.recovery
            pause_temperature = (
                self._moku_outage_started_at is not None
                and recovery.temperature_hold_during_moku_outage == "pause_timer"
            )
            if self.temperature is not None and not pause_temperature:
                records.extend(
                    self._temperature_records(
                        self.temperature.update(
                            tec_snapshot,
                            now=self._temperature_now(timestamp),
                        ),
                        timestamp,
                    )
                )
            if self.moku is not None:
                records.extend(
                    self._waveform_records(self.moku.update(now=timestamp), timestamp)
                )
            records.extend(self._finish_if_required(timestamp))
        except Exception as error:
            self.phase = SupervisorPhase.FAILED
            self.stop_reason = f"{type(error).__name__}: {error}"
            records.append(
                self._record(
                    "experiment_failed",
                    timestamp,
                    "supervisor",
                    {"error": self.stop_reason},
                    "bounded_safe_cleanup",
                )
            )
            raise
        return tuple(records)

    def _completion_condition(self, now: float) -> bool:
        policy = self.plan.experiment.completion_policy
        temperature_complete = (
            self.temperature is None
            or self.temperature.phase is TemperaturePhase.COMPLETE
        )
        moku_complete = self.moku is None or self.moku.phase is WaveformPhase.COMPLETE
        if policy.mode is CompletionMode.FIXED_DURATION:
            assert policy.duration_s is not None
            return self._elapsed(now) >= policy.duration_s
        if policy.mode is CompletionMode.ALL_SCHEDULES_COMPLETE:
            return temperature_complete and moku_complete
        if policy.mode is CompletionMode.MOKU_SCHEDULE_COMPLETE:
            return self.moku is not None and moku_complete
        if policy.mode is CompletionMode.TEMPERATURE_SCHEDULE_COMPLETE:
            return self.temperature is not None and temperature_complete
        if policy.mode is CompletionMode.OPERATOR_CTRL_C:
            return False
        raise SupervisorError(f"Unknown completion policy {policy.mode!r}.")

    def _finish_if_required(self, now: float) -> list[SupervisorEvent]:
        if self.phase is not SupervisorPhase.RUNNING or not self._completion_condition(
            now
        ):
            return []
        records: list[SupervisorEvent] = []
        if self.moku is not None and not self.moku.is_terminal:
            records.extend(
                self._waveform_records(
                    self.moku.update(now=now, experiment_ending=True),
                    now,
                )
            )
        self.phase = SupervisorPhase.COMPLETE
        self.stop_reason = self.plan.experiment.completion_policy.mode.value
        records.append(
            self._record(
                "experiment_completed",
                now,
                "supervisor",
                {"reason": self.stop_reason},
                "apply_configured_completion_and_cleanup",
            )
        )
        return records

    def interrupt(self, *, now: float | None = None) -> tuple[SupervisorEvent, ...]:
        """Record Ctrl+C and request explicit bounded cleanup actions."""

        timestamp = self._now(now)
        if self.phase is SupervisorPhase.IDLE:
            raise SupervisorError("Cannot interrupt an experiment before start.")
        if self.is_terminal:
            return ()
        records: list[SupervisorEvent] = []
        if self.moku is not None and not self.moku.is_terminal:
            records.extend(
                self._waveform_records(
                    self.moku.update(now=timestamp, experiment_ending=True),
                    timestamp,
                )
            )
        self.phase = SupervisorPhase.INTERRUPTED
        self.stop_reason = "operator_ctrl_c"
        records.append(
            self._record(
                "experiment_interrupted",
                timestamp,
                "supervisor",
                {"reason": self.stop_reason},
                "apply_configured_interrupt_and_cleanup",
            )
        )
        return tuple(records)

    def mark_moku_connection_lost(
        self, *, now: float | None = None
    ) -> tuple[SupervisorEvent, ...]:
        """Apply finite-burst uncertainty rules at a connection boundary."""

        timestamp = self._now(now)
        if self.moku is None:
            raise SupervisorError("The experiment has no Moku schedule.")
        transitions = self.moku.mark_connection_lost(now=timestamp)
        if self._moku_outage_started_at is None and any(
            item.event
            in {
                "waveform_connection_lost",
                "count_chunk_interrupted",
                "count_uncertainty_budget_exhausted",
            }
            for item in transitions
        ):
            self._moku_outage_started_at = timestamp
        return tuple(self._waveform_records(transitions, timestamp))

    def mark_moku_count_recovered(
        self, *, now: float | None = None
    ) -> tuple[SupervisorEvent, ...]:
        """Resume a bounded count at its next untriggered chunk boundary."""

        timestamp = self._now(now)
        if self.moku is None:
            raise SupervisorError("The experiment has no Moku schedule.")
        transitions = self.moku.mark_count_recovered(now=timestamp)
        if self._moku_outage_started_at is not None:
            recovery = self.plan.experiment.run_settings.recovery
            if recovery.temperature_hold_during_moku_outage == "pause_timer":
                self._temperature_paused_s += timestamp - self._moku_outage_started_at
            self._moku_outage_started_at = None
        return tuple(self._waveform_records(transitions, timestamp))

    def mark_moku_continuous_restarted(
        self, *, now: float | None = None
    ) -> tuple[SupervisorEvent, ...]:
        """Record a permitted phase-zero restart after complete replay."""

        timestamp = self._now(now)
        if self.moku is None:
            raise SupervisorError("The experiment has no Moku schedule.")
        recovery = self.plan.experiment.run_settings.recovery
        if recovery.continuous_waveform != "restart_from_phase_zero":
            raise SupervisorError(
                "Configured recovery policy does not permit a continuous "
                "waveform restart."
            )
        transitions = self.moku.mark_continuous_restarted(now=timestamp)
        if self._moku_outage_started_at is not None:
            if recovery.temperature_hold_during_moku_outage == "pause_timer":
                self._temperature_paused_s += timestamp - self._moku_outage_started_at
            self._moku_outage_started_at = None
        return tuple(self._waveform_records(transitions, timestamp))

    def snapshot(self, *, now: float | None = None) -> SupervisorSnapshot:
        """Return the current independent software schedule state."""

        timestamp = self._now(now)
        return SupervisorSnapshot(
            phase=self.phase,
            elapsed_s=self._elapsed(timestamp),
            temperature=(
                None
                if self.temperature is None
                else self.temperature.snapshot_state(
                    now=self._temperature_now(timestamp)
                )
            ),
            moku=(
                None if self.moku is None else self.moku.snapshot_state(now=timestamp)
            ),
            stop_reason=self.stop_reason,
        )

    def build_checkpoint(
        self,
        *,
        lut_hashes: Mapping[str, str],
        last_valid_sample_timestamp_utc: str | None = None,
        output_state: OutputState = OutputState.UNKNOWN,
        temperature_output_state: OutputState = OutputState.UNKNOWN,
        updated_at_utc: str | None = None,
        now: float | None = None,
    ) -> RuntimeCheckpoint:
        """Build a strict atomic-checkpoint value from both state machines."""

        state = self.snapshot(now=now)
        temperature_checkpoint = None
        if state.temperature is not None:
            temperature_checkpoint = TemperatureCheckpoint(
                stage_index=state.temperature.stage_index,
                stage_name=state.temperature.stage_name,
                phase=state.temperature.phase.value,
                requested_target_c=state.temperature.requested_target_c,
                completed_hold_s=state.temperature.completed_hold_s,
                stable_elapsed_s=state.temperature.stable_elapsed_s,
                phase_elapsed_s=state.temperature.phase_elapsed_s,
                schedule_elapsed_s=state.temperature.schedule_elapsed_s,
                safe_resume_boundary=state.temperature.safe_resume_boundary,
                last_confirmed_output_state=temperature_output_state.value,
            )
        moku_checkpoint = None
        if state.moku is not None:
            continuity = {
                WaveformContinuity.CONTINUOUS: "continuous",
                WaveformContinuity.RESTARTED_FROM_PHASE_ZERO: "reset_to_phase_zero",
                WaveformContinuity.UNCONFIRMED: "unknown",
            }[state.moku.phase_continuity]
            requested_count = state.moku.requested_count
            delivered_lower_bound = state.moku.delivered_lower_bound
            completed_count = (
                delivered_lower_bound
                if delivered_lower_bound is not None
                and state.moku.count_recovery_mode == "bounded_uncertainty"
                and state.moku.phase is WaveformPhase.COMPLETE
                else requested_count
                if requested_count is not None
                and state.moku.phase is WaveformPhase.COMPLETE
                else 0
            )
            moku_checkpoint = MokuCheckpoint(
                action_index=state.moku.action_index,
                action_name=state.moku.action_name,
                waveform_name=state.moku.waveform_name,
                waveform_run_id=state.moku.waveform_run_id,
                waveform_session_id=state.moku.waveform_session_id,
                phase=state.moku.phase.value,
                repeat_mode=state.moku.repeat_mode,
                requested_duration_s=state.moku.requested_duration_s,
                completed_duration_s=state.moku.completed_runtime_s,
                requested_count=requested_count,
                completed_count=completed_count,
                safe_resume_boundary=state.moku.safe_resume_boundary,
                action_indeterminate=state.moku.phase is WaveformPhase.INDETERMINATE,
                last_valid_sample_timestamp_utc=last_valid_sample_timestamp_utc,
                last_confirmed_output_state=output_state.value,
                phase_continuity=continuity,
                delivered_lower_bound=delivered_lower_bound,
                delivered_upper_bound=state.moku.delivered_upper_bound,
                cumulative_ambiguous_cycles=(state.moku.cumulative_ambiguous_cycles),
                uncertainty_budget_exhausted=(state.moku.uncertainty_budget_exhausted),
                count_recovery_mode=state.moku.count_recovery_mode,
            )
        return RuntimeCheckpoint(
            configuration_hash=self.plan.experiment.configuration_hash,
            source_hashes=self.plan.experiment.source_hashes,
            lut_hashes=lut_hashes,
            experiment_elapsed_s=state.elapsed_s,
            updated_at_utc=updated_at_utc or _utc_timestamp(),
            temperature=temperature_checkpoint,
            moku=moku_checkpoint,
        )

    def sample_state(
        self,
        *,
        first_sample_after_waveform_change: bool,
    ) -> dict[str, Any]:
        """Return independent state tags for one accepted Moku sample."""

        temperature = (
            None if self.temperature is None else self.temperature.current_stage
        )
        action = None if self.moku is None else self.moku.current_action
        waveform_name = None if action is None else action.waveform_name
        measurement = (
            None
            if waveform_name is None
            else self.plan.measurement_plans.get(waveform_name)
        )
        count_facts = {} if self.moku is None else self.moku.count_facts()
        return {
            "temperature_stage_index": (
                None if self.temperature is None else self.temperature.stage_index
            ),
            "temperature_stage_name": None if temperature is None else temperature.name,
            "temperature_phase": (
                None if self.temperature is None else self.temperature.phase.value
            ),
            "moku_action_index": None if self.moku is None else self.moku.action_index,
            "moku_action_name": None if action is None else action.name,
            "waveform_name": waveform_name,
            "waveform_run_id": None if self.moku is None else self.moku.waveform_run_id,
            "waveform_session_id": (
                None if self.moku is None else self.moku.waveform_session_id
            ),
            "first_sample_after_waveform_change": bool(
                first_sample_after_waveform_change
            ),
            "waveform_phase_continuity": (
                None if self.moku is None else self.moku.phase_continuity.value
            ),
            "measurement_profile": (
                None
                if measurement is None
                else "raw_only"
                if measurement.raw_only
                else ",".join(window.name for window in measurement.windows)
            ),
            "measurement_plan_sha256": (
                None if measurement is None else measurement.measurement_plan_sha256
            ),
            "waveform_timing_sha256": (
                None if measurement is None else measurement.waveform_timing_sha256
            ),
            "count_chunk_index": count_facts.get("count_chunk_index"),
            "count_chunk_size": count_facts.get("chunk_size"),
            "delivered_lower_bound": count_facts.get("delivered_lower_bound"),
            "delivered_upper_bound": count_facts.get("delivered_upper_bound"),
        }


def execute_effective_plan(
    plan: EffectiveExperimentPlan,
    artifact_directory: Any,
) -> int:
    """Execute through the separately isolated hardware adapters.

    The concrete runtime is imported only here, after the CLI has validated,
    snapshotted, displayed, and explicitly confirmed the complete plan.
    """

    from eom_stabilisation.experiment.runtime_runner import run_hardware_experiment

    return run_hardware_experiment(plan, artifact_directory)


__all__ = [
    "ExperimentSupervisor",
    "SupervisorError",
    "SupervisorEvent",
    "SupervisorPhase",
    "SupervisorSnapshot",
    "execute_effective_plan",
]
