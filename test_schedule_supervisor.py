"""Fake-clock tests for independent master experiment scheduling."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parent
SRC_DIRECTORY = REPOSITORY_ROOT / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.config import load_experiment  # noqa: E402
from eom_stabilisation.config.models import (  # noqa: E402
    CompletionMode,
    CompletionPolicy,
)
from eom_stabilisation.experiment.planner import build_effective_plan  # noqa: E402
from eom_stabilisation.experiment.checkpoint import RuntimeCheckpoint  # noqa: E402
from eom_stabilisation.experiment.supervisor import (  # noqa: E402
    ExperimentSupervisor,
    SupervisorPhase,
)
from eom_stabilisation.experiment.waveform_state import (  # noqa: E402
    WaveformPhase,
    WaveformScheduleError,
    WaveformScheduleStateMachine,
)
from eom_stabilisation.moku.models import (  # noqa: E402
    CountRecoveryMode,
    OutputState,
    RunMode,
    WaveformContinuity,
)
from eom_stabilisation.tec import TecSnapshot, TemperaturePhase  # noqa: E402


EXAMPLES = REPOSITORY_ROOT / "configs" / "examples"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def set(self, value: float) -> None:
        self.now = float(value)


def snapshot(target: float, *, stable: bool = True) -> TecSnapshot:
    return TecSnapshot(
        object_temperature_c=target,
        sink_temperature_c=22.0,
        temperature_stable=stable,
        output_current_a=0.2,
        output_voltage_v=1.0,
        active_target_c=target,
        controller_status="ready",
    )


def supervisor_for(filename: str) -> tuple[FakeClock, ExperimentSupervisor]:
    clock = FakeClock()
    plan = build_effective_plan(load_experiment(EXAMPLES / filename))
    return clock, ExperimentSupervisor(plan, monotonic=clock)


def continuous_combined_supervisor() -> tuple[FakeClock, ExperimentSupervisor]:
    """Adapt the combined example to a safely replayable continuous action."""

    experiment = load_experiment(EXAMPLES / "independent_temperature_and_moku.yaml")
    plan = build_effective_plan(experiment)
    action = plan.waveform_program.actions[0]
    continuous_run = replace(
        action.run,
        mode=RunMode.FOREVER,
        repeat_count=None,
        requested_duration_s=None,
        achieved_duration_s=None,
        end_policy=None,
        exact_hardware_burst=False,
    )
    continuous_program = replace(
        plan.waveform_program,
        actions=(replace(action, run=continuous_run),),
    )
    clock = FakeClock()
    return clock, ExperimentSupervisor(
        replace(plan, waveform_program=continuous_program),
        monotonic=clock,
    )


def pause_timer_duration_supervisor() -> tuple[FakeClock, ExperimentSupervisor]:
    experiment = load_experiment(EXAMPLES / "independent_temperature_and_moku.yaml")
    recovery = replace(
        experiment.run_settings.recovery,
        waveform_duration_during_outage="pause_timer",
    )
    plan = build_effective_plan(
        replace(
            experiment,
            run_settings=replace(experiment.run_settings, recovery=recovery),
        )
    )
    clock = FakeClock()
    return clock, ExperimentSupervisor(plan, monotonic=clock)


class ExperimentSupervisorTests(unittest.TestCase):
    def test_temperature_stability_event_starts_waveform(self):
        clock, supervisor = supervisor_for(
            "waveform_started_when_temperature_stable.yaml"
        )
        supervisor.start()
        self.assertEqual(supervisor.temperature.phase, TemperaturePhase.WAITING_FOR_STABILITY)
        self.assertEqual(supervisor.moku.phase, WaveformPhase.WAITING)

        supervisor.update(tec_snapshot=snapshot(30.0))
        clock.set(30.0)
        events = supervisor.update(tec_snapshot=snapshot(30.0))
        self.assertEqual(supervisor.temperature.phase, TemperaturePhase.HOLDING)
        self.assertEqual(supervisor.moku.phase, WaveformPhase.RUNNING)
        self.assertIn("temperature_became_stable", [event.name for event in events])
        self.assertIn("waveform_action_started", [event.name for event in events])

        clock.set(210.0)
        supervisor.update(tec_snapshot=snapshot(30.0))
        self.assertEqual(supervisor.phase, SupervisorPhase.COMPLETE)
        self.assertEqual(supervisor.temperature.phase, TemperaturePhase.COMPLETE)
        self.assertEqual(supervisor.moku.phase, WaveformPhase.COMPLETE)

    def test_temperature_transition_does_not_restart_active_waveform(self):
        clock, supervisor = supervisor_for("independent_temperature_and_moku.yaml")
        supervisor.start()
        run_id = supervisor.moku.waveform_run_id
        session_id = supervisor.moku.waveform_session_id

        clock.set(120.0)
        supervisor.update(tec_snapshot=snapshot(25.0))
        self.assertEqual(supervisor.temperature.stage_index, 1)
        self.assertEqual(supervisor.moku.phase, WaveformPhase.RUNNING)
        self.assertEqual(supervisor.moku.waveform_run_id, run_id)
        self.assertEqual(supervisor.moku.waveform_session_id, session_id)

        clock.set(240.0)
        supervisor.update(tec_snapshot=snapshot(30.0))
        self.assertEqual(supervisor.temperature.phase, TemperaturePhase.COMPLETE)
        self.assertEqual(supervisor.moku.phase, WaveformPhase.RUNNING)
        self.assertEqual(supervisor.phase, SupervisorPhase.RUNNING)

        clock.set(300.0)
        supervisor.update(tec_snapshot=snapshot(30.0))
        self.assertEqual(supervisor.moku.phase, WaveformPhase.COMPLETE)
        self.assertEqual(supervisor.phase, SupervisorPhase.COMPLETE)

    def test_fixed_master_duration_stops_an_unbounded_waveform(self):
        experiment = load_experiment(EXAMPLES / "continuous_staircase.yaml")
        experiment = replace(
            experiment,
            completion_policy=CompletionPolicy(CompletionMode.FIXED_DURATION, 5.0),
        )
        plan = build_effective_plan(experiment)
        clock = FakeClock()
        supervisor = ExperimentSupervisor(plan, monotonic=clock)
        supervisor.start()
        clock.set(5.0)
        events = supervisor.update()
        self.assertEqual(supervisor.phase, SupervisorPhase.COMPLETE)
        self.assertIn("experiment_completed", [event.name for event in events])

    def test_finite_exact_burst_is_indeterminate_after_connection_loss(self):
        clock, supervisor = supervisor_for(
            "moku_only_100khz_10percent_for_50us.yaml"
        )
        supervisor.start()
        events = supervisor.mark_moku_connection_lost()
        self.assertEqual(supervisor.moku.phase, WaveformPhase.INDETERMINATE)
        self.assertEqual(events[-1].name, "finite_burst_indeterminate")

        checkpoint = supervisor.build_checkpoint(
            lut_hashes={
                name: waveform.lut_sha256
                for name, waveform in supervisor.plan.waveform_program.waveforms.items()
            },
            output_state=OutputState.UNKNOWN,
        )
        self.assertTrue(checkpoint.moku.action_indeterminate)
        self.assertFalse(checkpoint.moku.safe_resume_boundary)
        self.assertEqual(checkpoint.moku.last_confirmed_output_state, "unknown")

    def test_bounded_count_checkpoint_preserves_delivery_interval(self):
        experiment = load_experiment(
            EXAMPLES / "moku_only_100khz_10percent_for_50us.yaml"
        )
        plan = build_effective_plan(experiment)
        action = plan.waveform_program.actions[0]
        period_s = plan.waveform_program.waveforms[
            action.waveform_name
        ].achieved_period_s
        bounded_run = replace(
            action.run,
            repeat_count=3,
            achieved_duration_s=3 * period_s,
            count_recovery_mode=CountRecoveryMode.BOUNDED_UNCERTAINTY,
            maximum_uncertain_fraction=2 / 3,
            maximum_ambiguous_cycles=2,
            initial_chunk_size=1,
        )
        program = replace(
            plan.waveform_program,
            actions=(replace(action, run=bounded_run),),
        )
        clock = FakeClock()
        supervisor = ExperimentSupervisor(
            replace(plan, waveform_program=program), monotonic=clock
        )

        supervisor.start()
        supervisor.mark_moku_connection_lost(now=0.0)
        supervisor.mark_moku_count_recovered(now=0.0)
        clock.set(period_s)
        supervisor.update()
        clock.set(2 * period_s)
        supervisor.update()

        checkpoint = supervisor.build_checkpoint(
            lut_hashes={
                name: waveform.lut_sha256
                for name, waveform in program.waveforms.items()
            },
            output_state=OutputState.DISABLED,
        )
        self.assertEqual(checkpoint.moku.completed_count, 2)
        self.assertEqual(checkpoint.moku.delivered_lower_bound, 2)
        self.assertEqual(checkpoint.moku.delivered_upper_bound, 3)
        self.assertEqual(checkpoint.moku.cumulative_ambiguous_cycles, 1)

        restored = ExperimentSupervisor(
            replace(plan, waveform_program=program), monotonic=FakeClock()
        )
        restored.restore(checkpoint, now=10.0)
        rewritten = restored.build_checkpoint(
            lut_hashes={
                name: waveform.lut_sha256
                for name, waveform in program.waveforms.items()
            },
            output_state=OutputState.DISABLED,
            now=10.0,
        )
        self.assertEqual(rewritten.moku.completed_count, 2)
        self.assertEqual(rewritten.moku.delivered_lower_bound, 2)
        self.assertEqual(rewritten.moku.delivered_upper_bound, 3)

    def test_moku_outage_pauses_temperature_and_waveform_timers(self):
        clock, supervisor = continuous_combined_supervisor()
        supervisor.start()
        clock.set(30.0)
        supervisor.update(tec_snapshot=snapshot(25.0))
        supervisor.mark_moku_connection_lost()

        clock.set(230.0)
        self.assertEqual(supervisor.update(tec_snapshot=snapshot(25.0)), ())
        paused = supervisor.snapshot()
        self.assertEqual(paused.temperature.completed_hold_s, 30.0)
        # Duration actions use the monotonic wall-clock timer through outages.
        self.assertEqual(paused.moku.completed_runtime_s, 230.0)

        restart_events = supervisor.mark_moku_continuous_restarted()
        self.assertEqual(restart_events[-1].name, "continuous_waveform_restarted")
        clock.set(319.0)
        supervisor.update(tec_snapshot=snapshot(25.0))
        before_boundary = supervisor.snapshot()
        self.assertEqual(before_boundary.temperature.stage_index, 0)
        self.assertEqual(before_boundary.moku.completed_runtime_s, 319.0)

        clock.set(320.0)
        supervisor.update(tec_snapshot=snapshot(25.0))
        self.assertEqual(supervisor.temperature.stage_index, 1)
        self.assertEqual(supervisor.moku.phase, WaveformPhase.RUNNING)

        clock.set(499.0)
        supervisor.update(tec_snapshot=snapshot(30.0))
        self.assertEqual(supervisor.moku.phase, WaveformPhase.RUNNING)
        clock.set(500.0)
        supervisor.update(tec_snapshot=snapshot(30.0))
        final_state = supervisor.snapshot()
        self.assertEqual(final_state.moku.completed_runtime_s, 500.0)
        self.assertEqual(supervisor.temperature.phase, TemperaturePhase.COMPLETE)
        self.assertEqual(supervisor.moku.phase, WaveformPhase.RUNNING)

    def test_duration_pause_timer_resumes_remaining_time_from_phase_zero(self):
        clock, supervisor = pause_timer_duration_supervisor()
        supervisor.start()
        clock.set(30.0)
        supervisor.update(tec_snapshot=snapshot(25.0))
        previous_session_id = supervisor.moku.waveform_session_id
        supervisor.mark_moku_connection_lost()

        clock.set(230.0)
        supervisor.update(tec_snapshot=snapshot(25.0))
        self.assertEqual(supervisor.moku.snapshot_state().completed_runtime_s, 30.0)

        restarted = supervisor.mark_moku_continuous_restarted()
        self.assertEqual(restarted[-1].command, "restore_from_phase_zero")
        self.assertEqual(supervisor.moku.waveform_session_id, previous_session_id + 1)
        self.assertEqual(
            supervisor.moku.phase_continuity,
            WaveformContinuity.RESTARTED_FROM_PHASE_ZERO,
        )

        clock.set(499.0)
        supervisor.update(tec_snapshot=snapshot(30.0))
        self.assertEqual(supervisor.moku.phase, WaveformPhase.RUNNING)
        self.assertEqual(supervisor.moku.snapshot_state().completed_runtime_s, 299.0)

        clock.set(500.0)
        supervisor.update(tec_snapshot=snapshot(30.0))
        self.assertEqual(supervisor.moku.phase, WaveformPhase.COMPLETE)

    def test_safe_continuous_restore_replays_from_phase_zero(self):
        clock, supervisor = continuous_combined_supervisor()
        supervisor.start()
        clock.set(60.0)
        state = supervisor.moku.snapshot_state()
        original_run_id = state.waveform_run_id
        original_session_id = state.waveform_session_id

        resume_clock = FakeClock()
        resume_clock.set(1000.0)
        restored = WaveformScheduleStateMachine(
            supervisor.plan.waveform_program,
            monotonic=resume_clock,
        )
        transitions = restored.restore(state)
        self.assertEqual(transitions[-1].command, "restore_from_phase_zero")
        self.assertIn("output disabled", transitions[-1].detail)
        self.assertEqual(restored.waveform_run_id, original_run_id)
        self.assertEqual(restored.waveform_session_id, original_session_id + 1)
        self.assertEqual(
            restored.phase_continuity,
            WaveformContinuity.RESTARTED_FROM_PHASE_ZERO,
        )

        resume_clock.set(1010.0)
        restored.update()
        self.assertEqual(restored.phase, WaveformPhase.RUNNING)
        self.assertEqual(restored.snapshot_state().completed_runtime_s, 70.0)

    def test_checkpoint_fields_match_independent_machine_snapshots(self):
        clock, supervisor = supervisor_for("independent_temperature_and_moku.yaml")
        supervisor.start()
        clock.set(30.0)
        supervisor.update(tec_snapshot=snapshot(25.0))
        state = supervisor.snapshot()
        checkpoint = supervisor.build_checkpoint(
            lut_hashes={
                name: waveform.lut_sha256
                for name, waveform in supervisor.plan.waveform_program.waveforms.items()
            },
            last_valid_sample_timestamp_utc="2026-08-11T12:00:00Z",
            output_state=OutputState.DISABLED,
            temperature_output_state=OutputState.ENABLED,
            updated_at_utc="2026-08-11T12:00:01Z",
        )

        self.assertEqual(checkpoint.experiment_elapsed_s, state.elapsed_s)
        self.assertEqual(checkpoint.temperature.stage_index, state.temperature.stage_index)
        self.assertEqual(checkpoint.temperature.phase, state.temperature.phase.value)
        self.assertEqual(
            checkpoint.temperature.completed_hold_s,
            state.temperature.completed_hold_s,
        )
        self.assertEqual(
            checkpoint.temperature.phase_elapsed_s,
            state.temperature.phase_elapsed_s,
        )
        self.assertEqual(
            checkpoint.temperature.schedule_elapsed_s,
            state.temperature.schedule_elapsed_s,
        )
        self.assertEqual(checkpoint.temperature.last_confirmed_output_state, "enabled")
        self.assertEqual(checkpoint.moku.action_index, state.moku.action_index)
        self.assertEqual(checkpoint.moku.phase, state.moku.phase.value)
        self.assertEqual(
            checkpoint.moku.completed_duration_s,
            state.moku.completed_runtime_s,
        )
        self.assertEqual(
            checkpoint.moku.requested_duration_s,
            state.moku.requested_duration_s,
        )
        self.assertEqual(checkpoint.moku.last_confirmed_output_state, "disabled")
        self.assertEqual(RuntimeCheckpoint.from_dict(checkpoint.to_dict()), checkpoint)

    def test_restore_rejects_duration_inconsistent_with_compiled_action(self):
        clock, supervisor = supervisor_for("independent_temperature_and_moku.yaml")
        supervisor.start()
        clock.set(300.0)
        supervisor.update(tec_snapshot=snapshot(25.0))
        state = supervisor.moku.snapshot_state()
        self.assertEqual(state.phase, WaveformPhase.COMPLETE)
        self.assertTrue(state.safe_resume_boundary)
        self.assertEqual(state.completed_runtime_s, 300.0)
        inconsistent = replace(state, requested_duration_s=301.0)
        restored = WaveformScheduleStateMachine(supervisor.plan.waveform_program)

        with self.assertRaisesRegex(WaveformScheduleError, "requested duration"):
            restored.restore(inconsistent, now=1000.0)

    def test_sample_tags_include_both_independent_states(self):
        _, supervisor = supervisor_for("independent_temperature_and_moku.yaml")
        supervisor.start()
        tags = supervisor.sample_state(first_sample_after_waveform_change=True)
        self.assertEqual(tags["temperature_stage_index"], 0)
        self.assertEqual(tags["moku_action_index"], 0)
        self.assertEqual(tags["waveform_name"], "independent_square")
        self.assertTrue(tags["first_sample_after_waveform_change"])
        self.assertEqual(len(tags["waveform_timing_sha256"]), 64)

    def test_temperature_only_and_moku_only_are_supported(self):
        _, temperature_only = supervisor_for("temperature_only_sweep.yaml")
        temperature_only.start()
        self.assertIsNotNone(temperature_only.temperature)
        self.assertIsNone(temperature_only.moku)

        _, moku_only = supervisor_for("moku_only_duty_cycle.yaml")
        moku_only.start()
        self.assertIsNone(moku_only.temperature)
        self.assertIsNotNone(moku_only.moku)

    def test_supervisor_restores_continuous_action_without_repeating_prior_work(self):
        clock, original = supervisor_for("continuous_staircase.yaml")
        original.start()
        clock.set(5.0)
        checkpoint = original.build_checkpoint(
            lut_hashes={
                name: waveform.lut_sha256
                for name, waveform in original.plan.waveform_program.waveforms.items()
            },
            output_state=OutputState.UNKNOWN,
            now=5.0,
        )
        previous_run_id = checkpoint.moku.waveform_run_id
        previous_session_id = checkpoint.moku.waveform_session_id

        restored_clock = FakeClock()
        restored_clock.set(100.0)
        restored = ExperimentSupervisor(original.plan, monotonic=restored_clock)
        events = restored.restore(checkpoint, now=100.0)
        self.assertEqual(restored.moku.phase, WaveformPhase.RUNNING)
        self.assertEqual(restored.moku.waveform_run_id, previous_run_id)
        self.assertEqual(
            restored.moku.waveform_session_id,
            previous_session_id + 1,
        )
        self.assertIn("experiment_resumed", [event.name for event in events])
        replay = next(
            event for event in events if event.name == "waveform_schedule_restored"
        )
        self.assertEqual(replay.command, "restore_from_phase_zero")


if __name__ == "__main__":
    unittest.main()
