"""Hardware-free tests for waveform-aware measurement plans."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parent
SRC_DIRECTORY = REPOSITORY_ROOT / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.moku.measurement import (
    compile_measurement_plan,
    measure_frame,
)
from eom_stabilisation.moku.waveform_compiler import compile_waveform


def square_waveform():
    return compile_waveform(
        "square",
        {
            "type": "square",
            "low_level_v": 0,
            "high_level_v": 5,
            "period_us": 10,
            "pulse_duration_us": 2,
            "edge_time_ns": 200,
        },
    )


class MeasurementPlanTests(unittest.TestCase):
    def test_trigger_phase_shifts_windows_and_semantic_channel_wins(self):
        waveform = square_waveform()
        phase_zero = compile_measurement_plan(waveform)
        trigger_phase_s = 100e-9
        plan = compile_measurement_plan(
            waveform,
            trigger_phase_s=trigger_phase_s,
        )
        self.assertEqual(plan.trigger_phase_s, trigger_phase_s)
        self.assertNotEqual(
            plan.measurement_plan_sha256,
            phase_zero.measurement_plan_sha256,
        )
        high = next(window for window in plan.windows if window.role == "high_level")
        self.assertAlmostEqual(
            high.start_s,
            phase_zero.windows[0].start_s - trigger_phase_s,
        )
        time_axis = np.linspace(0, waveform.achieved_period_s, 5000)
        photodiode = np.full(time_axis.shape, 0.25)
        for window in plan.windows:
            selected = (time_axis >= window.start_s) & (time_axis < window.end_s)
            photodiode[selected] = 2.0 if window.role == "high_level" else 0.1
        result = measure_frame(
            {
                "time": time_axis,
                "photodiode_v": photodiode,
                "ch1": np.full(time_axis.shape, 99.0),
            },
            plan,
        )
        self.assertAlmostEqual(result.values_by_role["high_level"], 2.0)
        self.assertAlmostEqual(result.values_by_role["minimum"], 0.1)

    def test_default_roles_use_achieved_plateau_and_exclude_edges(self):
        waveform = square_waveform()
        plan = compile_measurement_plan(waveform)
        high = next(window for window in plan.windows if window.role == "high_level")
        low = next(window for window in plan.windows if window.role == "minimum")
        achieved_edge = waveform.segments[0].achieved_edge_time_s
        self.assertAlmostEqual(high.start_s, achieved_edge)
        self.assertAlmostEqual(
            high.end_s,
            waveform.segments[0].achieved_end_s - achieved_edge,
        )
        self.assertAlmostEqual(low.start_s, waveform.segments[1].achieved_start_s)

    def test_frame_reduction_combines_windows_with_the_same_role(self):
        waveform = compile_waveform(
            "two_highs",
            {
                "type": "segments",
                "low_level_v": 0,
                "segments": [
                    {"type": "pulse", "name": "a", "level_v": 1, "duration_us": 2, "measurement_role": "high_level"},
                    {"type": "gap", "name": "low", "duration_us": 2, "measurement_role": "minimum"},
                    {"type": "pulse", "name": "b", "level_v": 2, "duration_us": 2, "measurement_role": "high_level"},
                    {"type": "gap", "name": "tail", "duration_us": 2},
                ],
            },
        )
        plan = compile_measurement_plan(waveform)
        time_axis = (np.arange(waveform.point_count) + 0.5) * waveform.point_interval_s
        frame = {
            "time": time_axis,
            "ch1": waveform.connector_voltage_v,
        }
        result = measure_frame(frame, plan)
        self.assertEqual(result.measurement_plan_sha256, plan.measurement_plan_sha256)
        self.assertEqual(len(plan.measurement_plan_sha256), 64)
        self.assertAlmostEqual(result.values_by_role["high_level"], 1.5, places=2)
        self.assertAlmostEqual(result.values_by_role["minimum"], 0.0)
        self.assertGreater(result.point_counts_by_role["high_level"], 0)

    def test_no_roles_requires_explicit_plan_or_raw_only(self):
        waveform = compile_waveform(
            "unlabelled",
            {
                "type": "segments",
                "low_level_v": 0,
                "segments": [
                    {"type": "level", "level_v": 1, "duration_us": 5},
                    {"type": "level", "level_v": 2, "duration_us": 5},
                ],
            },
        )
        with self.assertRaisesRegex(ValueError, "no meaningful measurement roles"):
            compile_measurement_plan(waveform)
        plan = compile_measurement_plan(waveform, raw_only=True)
        self.assertTrue(plan.raw_only)
        self.assertEqual(plan.windows, ())

    def test_explicit_windows_support_units_and_edge_exclusions(self):
        waveform = square_waveform()
        plan = compile_measurement_plan(
            waveform,
            [
                {
                    "name": "pulse_response",
                    "role": "high_level",
                    "start_ns": 0,
                    "end_us": 2,
                    "exclude_start_ns": 200,
                    "exclude_end_ns": 200,
                },
                {
                    "name": "floor",
                    "role": "minimum",
                    "start_us": 2,
                    "duration_us": 8,
                },
            ],
        )
        self.assertAlmostEqual(plan.windows[0].start_s, 200e-9)
        self.assertAlmostEqual(plan.windows[0].end_s, 1.8e-6)

    def test_invalid_windows_and_frames_are_rejected(self):
        waveform = square_waveform()
        with self.assertRaisesRegex(ValueError, "role is required"):
            compile_measurement_plan(
                waveform,
                [{"name": "missing_role", "start_us": 0, "end_us": 1}],
            )
        with self.assertRaisesRegex(ValueError, "overlap"):
            compile_measurement_plan(
                waveform,
                [
                    {"name": "a", "role": "minimum", "start_us": 0, "end_us": 5},
                    {"name": "b", "role": "high_level", "start_us": 4, "end_us": 6},
                ],
            )
        plan = compile_measurement_plan(waveform)
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            measure_frame({"time": [0, 1, 1], "ch1": [0, 1, 2]}, plan)
        with self.assertRaisesRegex(ValueError, "no points"):
            measure_frame({"time": [-2, -1], "ch1": [0, 0]}, plan)


if __name__ == "__main__":
    unittest.main()
