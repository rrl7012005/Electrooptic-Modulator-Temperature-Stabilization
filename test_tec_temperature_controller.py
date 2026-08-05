"""Tests for manual and generated TEC temperature schedules."""

import unittest
from unittest.mock import patch

import tec_temperature_controller as controller


class GeneratedTemperatureScheduleTests(unittest.TestCase):
    def test_initial_tec_off_hold_precedes_generated_measurements(self):
        schedule = controller.generate_temperature_schedule(
            25.0,
            30.0,
            5.0,
            2.5,
            10.0,
            360.0,
            True,
            360.0,
        )

        self.assertEqual(
            schedule,
            [
                (None, 360.0),
                (25.0, 360.0),
                (27.5, 10.0),
                (30.0, 360.0),
                (27.5, 10.0),
                (25.0, 360.0),
            ],
        )

    def test_measurement_targets_and_transition_steps_use_distinct_holds(self):
        schedule = controller.generate_temperature_schedule(
            20.0,
            30.0,
            5.0,
            2.0,
            0.5,
            10.0,
            False,
        )

        self.assertEqual(
            schedule,
            [
                (20.0, 10.0),
                (22.0, 0.5),
                (24.0, 0.5),
                (25.0, 10.0),
                (27.0, 0.5),
                (29.0, 0.5),
                (30.0, 10.0),
            ],
        )

    def test_reverse_sweep_avoids_duplicate_finish_target(self):
        schedule = controller.generate_temperature_schedule(
            20.0,
            30.0,
            5.0,
            2.0,
            0.5,
            10.0,
            True,
        )

        self.assertEqual(
            schedule,
            [
                (20.0, 10.0),
                (22.0, 0.5),
                (24.0, 0.5),
                (25.0, 10.0),
                (27.0, 0.5),
                (29.0, 0.5),
                (30.0, 10.0),
                (28.0, 0.5),
                (26.0, 0.5),
                (25.0, 10.0),
                (23.0, 0.5),
                (21.0, 0.5),
                (20.0, 10.0),
            ],
        )

    def test_descending_schedule_reaches_exact_measurement_targets(self):
        schedule = controller.generate_temperature_schedule(
            30.0,
            20.0,
            6.0,
            2.0,
            0.5,
            10.0,
            False,
        )

        self.assertEqual(
            schedule,
            [
                (30.0, 10.0),
                (28.0, 0.5),
                (26.0, 0.5),
                (24.0, 10.0),
                (22.0, 0.5),
                (20.0, 10.0),
            ],
        )

    def test_no_intermediate_step_when_transition_increment_covers_gap(self):
        schedule = controller.generate_temperature_schedule(
            20.0,
            30.0,
            5.0,
            10.0,
            0.5,
            10.0,
            False,
        )

        self.assertEqual(
            schedule,
            [(20.0, 10.0), (25.0, 10.0), (30.0, 10.0)],
        )

    def test_generated_settings_must_be_complete_and_valid(self):
        valid = (20.0, 30.0, 5.0, 1.0, 2.0, 120.0, True)
        invalid_cases = (
            (None, *valid[1:]),
            (20.0, 20.0, *valid[2:]),
            (*valid[:2], 0.0, *valid[3:]),
            (*valid[:3], 0.0, *valid[4:]),
            (*valid[:4], 0.0, *valid[5:]),
            (*valid[:5], 0.0, valid[6]),
            (*valid[:6], "yes"),
        )

        for settings in invalid_cases:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                controller.generate_temperature_schedule(*settings)

        for invalid_initial_hold in (0.0, -1.0, float("nan"), "bad"):
            with (
                self.subTest(initial_hold=invalid_initial_hold),
                self.assertRaises(ValueError),
            ):
                controller.generate_temperature_schedule(
                    *valid,
                    invalid_initial_hold,
                )

    def test_manual_mode_preserves_the_written_schedule(self):
        manual_schedule = [(24.0, 3.0), (26.0, 4.0)]
        with (
            patch.object(controller, "TEMPERATURE_SCHEDULE_MODE", "manual"),
            patch.object(controller, "TEMPERATURE_SCHEDULE", manual_schedule),
        ):
            selected = controller.get_configured_temperature_schedule()

        self.assertIs(selected, manual_schedule)

    def test_generated_mode_uses_the_configured_sweep(self):
        with (
            patch.object(controller, "TEMPERATURE_SCHEDULE_MODE", "generated"),
            patch.object(controller, "START_TEMPERATURE_C", 20.0),
            patch.object(controller, "FINISH_TEMPERATURE_C", 25.0),
            patch.object(
                controller,
                "MEASUREMENT_TEMPERATURE_INTERVAL_C",
                5.0,
            ),
            patch.object(
                controller,
                "TRANSITION_TEMPERATURE_INCREMENT_C",
                2.0,
            ),
            patch.object(controller, "TRANSITION_HOLD_MINUTES", 0.5),
            patch.object(controller, "MEASUREMENT_HOLD_MINUTES", 10.0),
            patch.object(controller, "INCLUDE_REVERSE_SWEEP", False),
            patch.object(controller, "INITIAL_TEC_OFF_HOLD_MINUTES", None),
        ):
            selected = controller.get_configured_temperature_schedule()

        self.assertEqual(
            selected,
            [
                (20.0, 10.0),
                (22.0, 0.5),
                (24.0, 0.5),
                (25.0, 10.0),
            ],
        )

    def test_existing_target_limits_validate_generated_steps(self):
        generated = controller.generate_temperature_schedule(
            5.0,
            20.0,
            5.0,
            1.0,
            2.0,
            120.0,
            False,
        )

        with self.assertRaisesRegex(ValueError, "outside the allowed"):
            controller.validate_schedule(generated)


if __name__ == "__main__":
    unittest.main()
