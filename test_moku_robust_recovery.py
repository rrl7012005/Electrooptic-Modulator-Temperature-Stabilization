from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parent
SRC_DIRECTORY = REPOSITORY_ROOT / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.experiment.planner import _compile_action_timebase  # noqa: E402
from eom_stabilisation.experiment.waveform_state import (  # noqa: E402
    CountDeliveryTracker,
    WaveformPhase,
    WaveformScheduleStateMachine,
)
from eom_stabilisation.moku.measurement import (  # noqa: E402
    InvalidReferenceTraceError,
    OpticalAlignmentError,
    UnusableVoltageSamplesError,
    compile_measurement_plan,
    measure_frame,
)
from eom_stabilisation.moku.waveform_compiler import (  # noqa: E402
    compile_waveform,
    compile_waveform_program,
    parse_run_spec,
)


def square(*, period_us: float = 100.0, pulse_us: float = 10.0):
    return compile_waveform(
        "aligned_square",
        {
            "type": "square",
            "low_level_v": 0.0,
            "high_level_v": 1.0,
            "period_us": period_us,
            "pulse_duration_us": pulse_us,
        },
    )


def aligned_plan(*, minimum_points: int = 3, explicit_windows=None):
    waveform = square()
    return waveform, compile_measurement_plan(
        waveform,
        explicit_windows,
        trigger_phase_s=0.0,
        alignment_required=True,
        trigger_level_v=0.5,
        trigger_edge="Rising",
        reference_edge_tolerance_s=1e-6,
        maximum_optical_delay_s=3e-6,
        minimum_valid_points_per_role=minimum_points,
        minimum_optical_edge_snr=3.0,
        include_square_low_before=explicit_windows is None,
    )


def delayed_frame(*, delay_s: float = 2e-6):
    time_axis = np.linspace(-40e-6, 80e-6, 1201)
    reference = np.where((time_axis >= 0) & (time_axis < 10e-6), 1.0, 0.0)
    optical = np.where(
        (time_axis >= delay_s) & (time_axis < 10e-6 + delay_s),
        1.0,
        0.0,
    )
    return {
        "time": time_axis,
        "photodiode_v": optical,
        "waveform_reference_v": reference,
    }


class PerFrameAlignmentTests(unittest.TestCase):
    def test_known_optical_delay_shifts_windows_and_combines_both_low_regions(self):
        _, plan = aligned_plan()
        minimum_windows = [
            window for window in plan.windows if window.role == "minimum"
        ]
        self.assertEqual(len(minimum_windows), 2)
        self.assertLess(minimum_windows[0].start_s, 0.0)
        self.assertGreaterEqual(minimum_windows[1].start_s, 0.0)
        frame = delayed_frame()
        time_axis = frame["time"]
        optical = np.asarray(frame["photodiode_v"]).copy()
        optical[time_axis < 2e-6] = 1.0
        optical[(time_axis >= 2e-6) & (time_axis < 12e-6)] = 10.0
        optical[time_axis >= 12e-6] = 3.0
        frame["photodiode_v"] = optical

        result = measure_frame(frame, plan)

        self.assertAlmostEqual(result.alignment.optical_delay_s, 2e-6, places=7)
        self.assertAlmostEqual(result.values_by_role["high_level"], 10.0)
        self.assertGreater(result.values_by_role["minimum"], 1.0)
        self.assertLess(result.values_by_role["minimum"], 3.0)
        self.assertGreater(result.point_counts_by_role["minimum"], 500)

    def test_missing_bad_and_incorrectly_phased_reference_never_reduce(self):
        _, plan = aligned_plan()
        frame = delayed_frame()
        missing = dict(frame)
        missing.pop("waveform_reference_v")
        with self.assertRaises(InvalidReferenceTraceError):
            measure_frame(missing, plan)

        bad = dict(frame)
        bad["waveform_reference_v"] = [0.0, 1.0]
        with self.assertRaises(InvalidReferenceTraceError):
            measure_frame(bad, plan)

        shifted = dict(frame)
        time_axis = frame["time"]
        shifted["waveform_reference_v"] = np.where(
            (time_axis >= 5e-6) & (time_axis < 15e-6), 1.0, 0.0
        )
        with self.assertRaises(InvalidReferenceTraceError):
            measure_frame(shifted, plan)

    def test_nans_are_filtered_only_inside_selected_regions(self):
        windows = [
            {"name": "high", "role": "high_level", "start_us": 0, "end_us": 8},
            {"name": "low", "role": "minimum", "start_us": 20, "end_us": 30},
        ]
        _, plan = aligned_plan(minimum_points=20, explicit_windows=windows)
        frame = delayed_frame()
        time_axis = frame["time"]
        voltage = np.asarray(frame["photodiode_v"]).copy()
        voltage[np.argmin(np.abs(time_axis - 60e-6))] = np.nan
        self.assertIn("high_level", measure_frame({**frame, "photodiode_v": voltage}, plan).values_by_role)

        inside = voltage.copy()
        inside[(time_axis > 4e-6) & (time_axis < 5e-6)] = np.nan
        accepted = measure_frame({**frame, "photodiode_v": inside}, plan)
        self.assertGreater(accepted.alignment.rejected_points_by_role["high_level"], 0)

        inside[(time_axis >= 3e-6) & (time_axis < 10e-6)] = np.nan
        with self.assertRaises(UnusableVoltageSamplesError):
            measure_frame({**frame, "photodiode_v": inside}, plan)

    def test_optical_quality_failure_is_distinct_from_reference_failure(self):
        _, plan = aligned_plan()
        frame = delayed_frame()
        frame["photodiode_v"] = np.zeros_like(frame["time"])
        with self.assertRaises(OpticalAlignmentError):
            measure_frame(frame, plan)


class AutomaticTimebaseTests(unittest.TestCase):
    def test_100_hz_point_one_percent_retains_high_and_low_points(self):
        waveform = compile_waveform(
            "slow_narrow_pulse",
            {
                "type": "square",
                "low_level_v": 0.0,
                "high_level_v": 1.0,
                "frequency_hz": 100.0,
                "duty_cycle_percent": 0.1,
            },
        )
        plan = compile_measurement_plan(
            waveform,
            trigger_phase_s=0.0,
            alignment_required=True,
            trigger_level_v=0.5,
            trigger_edge="Rising",
            reference_edge_tolerance_s=1e-6,
            maximum_optical_delay_s=2e-6,
            minimum_valid_points_per_role=10,
            include_square_low_before=True,
        )
        settings = SimpleNamespace(
            timebase_mode="automatic",
            timebase_start_s=None,
            timebase_end_s=None,
            timebase_max_length=16_384,
            automatic_timebase_max_duration_s=0.02,
        )

        timebase = _compile_action_timebase(
            waveform=waveform,
            measurement_plan=plan,
            moku_settings=settings,
        )

        self.assertGreaterEqual(timebase.expected_points_by_role["high_level"], 10)
        self.assertGreaterEqual(timebase.expected_points_by_role["minimum"], 10)
        self.assertGreater(timebase.start_s, -waveform.achieved_period_s)
        self.assertLess(timebase.end_s, waveform.achieved_period_s)


class DurationAndCountRecoveryTests(unittest.TestCase):
    def test_duration_is_continuous_and_expires_during_outage(self):
        waveform = square()
        run = parse_run_spec(
            {"mode": "duration", "duration_ms": 100},
            waveform.achieved_period_s,
        )
        self.assertFalse(run.exact_hardware_burst)
        program = compile_waveform_program(
            {"aligned_square": waveform.requested_parameters},
            [
                {
                    "name": "timed",
                    "waveform": "aligned_square",
                    "run": {"mode": "duration", "duration_ms": 100},
                }
            ],
        )
        machine = WaveformScheduleStateMachine(program)
        machine.start(now=0.0)
        machine.mark_connection_lost(now=0.02)
        events = machine.update(now=0.11)
        self.assertEqual(machine.phase, WaveformPhase.COMPLETE)
        self.assertTrue(any(event.command == "stop_output" for event in events))

    def test_bounded_count_chunks_never_replay_and_stay_inside_budget(self):
        tracker = CountDeliveryTracker(10_000, 100, 50)
        interrupted = []
        for _ in range(8):
            interrupted.append(tracker.mark_interrupted())
            if tracker.uncertainty_budget_exhausted:
                break
        self.assertEqual(interrupted[:6], [50, 25, 12, 6, 3, 1])
        self.assertLessEqual(sum(interrupted), 100)
        self.assertEqual(tracker.interrupted_chunk_bounds, interrupted)

    def test_too_small_count_tolerance_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "too small"):
            parse_run_spec(
                {
                    "mode": "count",
                    "count": 100,
                    "recovery": {
                        "mode": "bounded_uncertainty",
                        "maximum_uncertain_fraction": 0.01,
                    },
                },
                1e-3,
            )


if __name__ == "__main__":
    unittest.main()
