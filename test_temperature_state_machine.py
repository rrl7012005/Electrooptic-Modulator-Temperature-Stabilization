"""Fake-clock tests for independent temperature schedule progression."""

from pathlib import Path
import sys
import unittest


SRC_DIRECTORY = Path(__file__).resolve().parent / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.tec import (  # noqa: E402
    StabilitySpec,
    TecSnapshot,
    TemperatureMachineSnapshot,
    TemperaturePhase,
    TemperatureSchedule,
    TemperatureStage,
    TemperatureStateMachine,
)
from eom_stabilisation.tec.state_machine import (  # noqa: E402
    InvalidTecSnapshot,
    TemperatureSettlingTimeout,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _snapshot(*, temperature: float = 25.0, stable: bool = True) -> TecSnapshot:
    return TecSnapshot(
        object_temperature_c=temperature,
        sink_temperature_c=23.0,
        temperature_stable=stable,
        active_target_c=25.0,
    )


class TemperatureStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.schedule = TemperatureSchedule(
            name="test",
            completion_behavior="disable_output",
            stages=(
                TemperatureStage(
                    name="hold_25",
                    target_c=25.0,
                    hold_duration_s=5.0,
                    stability=StabilitySpec(
                        required=True,
                        tolerance_c=0.2,
                        stable_duration_s=3.0,
                        timeout_s=10.0,
                    ),
                ),
                TemperatureStage(
                    name="output_off",
                    target_c=None,
                    hold_duration_s=2.0,
                    stability=StabilitySpec(required=False),
                    role="output_off",
                ),
            ),
        )

    def test_stable_duration_precedes_hold_and_stages_are_independent(self):
        machine = TemperatureStateMachine(self.schedule, monotonic=self.clock)
        started = machine.start()
        self.assertEqual(machine.phase, TemperaturePhase.WAITING_FOR_STABILITY)
        self.assertEqual(started[0].command, "set_target_and_enable")

        machine.update(_snapshot())
        self.clock.advance(2.9)
        machine.update(_snapshot())
        self.assertEqual(machine.phase, TemperaturePhase.WAITING_FOR_STABILITY)

        self.clock.advance(0.1)
        events = machine.update(_snapshot())
        self.assertEqual(machine.phase, TemperaturePhase.HOLDING)
        self.assertIn("temperature_became_stable", [event.event for event in events])

        self.clock.advance(5.0)
        events = machine.update(_snapshot())
        self.assertEqual(machine.current_stage.name, "output_off")
        self.assertEqual(machine.phase, TemperaturePhase.HOLDING)
        self.assertEqual(events[-1].command, "disable_output")

        self.clock.advance(2.0)
        events = machine.tick()
        self.assertEqual(machine.phase, TemperaturePhase.COMPLETE)
        self.assertEqual(events[-1].command, "disable_output")

    def test_stability_loss_resets_qualification_timer(self):
        machine = TemperatureStateMachine(self.schedule, monotonic=self.clock)
        machine.start()
        machine.update(_snapshot())
        self.clock.advance(2.0)
        events = machine.update(_snapshot(stable=False))
        self.assertIn("temperature_stability_lost", [event.event for event in events])
        self.clock.advance(2.0)
        machine.update(_snapshot())
        self.clock.advance(2.0)
        machine.update(_snapshot())
        self.assertEqual(machine.phase, TemperaturePhase.WAITING_FOR_STABILITY)

    def test_settling_timeout_is_terminal(self):
        machine = TemperatureStateMachine(self.schedule, monotonic=self.clock)
        machine.start()
        self.clock.advance(10.0)
        with self.assertRaises(TemperatureSettlingTimeout):
            machine.update(_snapshot(stable=False))
        self.assertEqual(machine.phase, TemperaturePhase.FAILED)

    def test_invalid_sensor_value_fails_without_hardware_action(self):
        machine = TemperatureStateMachine(self.schedule, monotonic=self.clock)
        machine.start()
        with self.assertRaises(InvalidTecSnapshot):
            machine.update(_snapshot(temperature=float("nan")))
        self.assertEqual(machine.phase, TemperaturePhase.FAILED)

    def test_snapshot_restore_preserves_completed_hold(self):
        machine = TemperatureStateMachine(self.schedule, monotonic=self.clock)
        machine.start()
        machine.update(_snapshot())
        self.clock.advance(3.0)
        machine.update(_snapshot())
        self.clock.advance(2.0)
        state = machine.snapshot_state()
        self.assertEqual(state.completed_hold_s, 2.0)

        resumed_clock = FakeClock()
        resumed_clock.now = 100.0
        resumed = TemperatureStateMachine(self.schedule, monotonic=resumed_clock)
        events = resumed.restore(state)
        self.assertEqual(events[0].command, "set_target_and_enable")
        resumed_clock.advance(3.0)
        resumed.update(_snapshot())
        self.assertEqual(resumed.current_stage.name, "output_off")

    def test_restore_rejects_mismatched_stage_name(self):
        machine = TemperatureStateMachine(self.schedule, monotonic=self.clock)
        state = TemperatureMachineSnapshot(
            phase=TemperaturePhase.HOLDING,
            stage_index=0,
            stage_name="wrong",
            requested_target_c=25.0,
            completed_hold_s=1,
            stable_elapsed_s=0,
            phase_elapsed_s=1,
            schedule_elapsed_s=1,
            safe_resume_boundary=True,
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            machine.restore(state)


if __name__ == "__main__":
    unittest.main()
