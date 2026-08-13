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
from eom_stabilisation.config.models import RawCaptureSettings  # noqa: E402
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

        plateau = dict(frame)
        plateau_reference = np.asarray(frame["waveform_reference_v"]).copy()
        near_zero = np.argsort(np.abs(np.asarray(frame["time"])))[:2]
        plateau_reference[near_zero] = 0.5
        plateau["waveform_reference_v"] = plateau_reference
        with self.assertRaisesRegex(InvalidReferenceTraceError, "plateau"):
            measure_frame(plateau, plan)

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
        self.assertIn(
            "high_level",
            measure_frame({**frame, "photodiode_v": voltage}, plan).values_by_role,
        )

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

    def test_sustained_optical_edge_wins_over_earlier_one_sample_spike(self):
        _, plan = aligned_plan()
        frame = delayed_frame()
        time_axis = np.asarray(frame["time"])
        optical = np.zeros_like(time_axis)
        spike_index = int(np.argmin(np.abs(time_axis - 0.5e-6)))
        optical[spike_index] = 20.0
        optical[(time_axis >= 2e-6) & (time_axis < 12e-6)] = 5.0
        frame["photodiode_v"] = optical

        result = measure_frame(frame, plan)

        self.assertAlmostEqual(result.alignment.optical_delay_s, 2e-6, places=7)
        self.assertAlmostEqual(result.values_by_role["high_level"], 5.0)

    def test_fixed_calibrated_delay_does_not_require_a_per_frame_optical_edge(self):
        waveform = square()
        plan = compile_measurement_plan(
            waveform,
            trigger_phase_s=0.0,
            alignment_required=True,
            trigger_level_v=0.5,
            trigger_edge="Rising",
            reference_edge_tolerance_s=1e-6,
            maximum_optical_delay_s=3e-6,
            minimum_valid_points_per_role=3,
            include_square_low_before=True,
            optical_delay_mode="fixed",
            fixed_optical_delay_s=2e-6,
        )
        frame = delayed_frame()
        frame["photodiode_v"] = np.full_like(frame["time"], 0.25)

        result = measure_frame(frame, plan)

        self.assertEqual(result.alignment.optical_delay_s, 2e-6)
        self.assertIsNone(result.alignment.alignment_quality)
        self.assertEqual(result.values_by_role["minimum"], 0.25)
        self.assertEqual(result.values_by_role["high_level"], 0.25)

    def test_settling_guard_excludes_the_start_of_each_segment_plateau(self):
        waveform = compile_waveform(
            "guarded",
            {
                "type": "square",
                "low_level_v": 0.0,
                "high_level_v": 1.0,
                "period_us": 100,
                "pulse_duration_us": 10,
                "edge_time_us": 1,
            },
        )
        plan = compile_measurement_plan(
            waveform,
            trigger_phase_s=0.0,
            optical_settling_guard_s=2e-6,
        )
        high = next(window for window in plan.windows if window.role == "high_level")
        low = next(window for window in plan.windows if window.role == "minimum")
        high_segment = waveform.segments[0]
        low_segment = waveform.segments[1]
        self.assertAlmostEqual(
            high.phase_start_s,
            high_segment.achieved_start_s + high_segment.achieved_edge_time_s + 2e-6,
        )
        self.assertAlmostEqual(
            low.phase_start_s,
            low_segment.achieved_start_s + 2e-6,
        )


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
                "edge_time_ns": 100,
            },
        )
        values = waveform.connector_voltage_v
        following = np.roll(values, -1)
        crossing = int(np.flatnonzero((values < 0.6) & (following > 0.6))[0])
        fraction = (0.6 - values[crossing]) / (following[crossing] - values[crossing])
        trigger_phase = (
            (crossing + fraction) * waveform.point_interval_s
        ) % waveform.achieved_period_s
        plan = compile_measurement_plan(
            waveform,
            trigger_phase_s=trigger_phase,
            alignment_required=True,
            trigger_level_v=0.6,
            trigger_edge="Rising",
            reference_edge_tolerance_s=1e-6,
            maximum_optical_delay_s=2e-6,
            minimum_valid_points_per_role=10,
            include_square_low_before=True,
            trigger_candidates=((trigger_phase, "Rising"),),
        )
        settings = SimpleNamespace(
            timebase_mode="automatic",
            timebase_start_s=None,
            timebase_end_s=None,
            timebase_max_length=16_384,
            automatic_timebase_max_duration_s=0.0026,
            raw_capture=RawCaptureSettings(),
        )

        timebase = _compile_action_timebase(
            waveform=waveform,
            measurement_plan=plan,
            moku_settings=settings,
        )

        self.assertGreaterEqual(timebase.expected_points_by_role["high_level"], 10)
        self.assertGreaterEqual(timebase.expected_points_by_role["minimum"], 10)
        self.assertLessEqual(timebase.start_s, 0.0)
        self.assertGreaterEqual(timebase.end_s, 0.0)
        self.assertGreater(timebase.start_s, -waveform.achieved_period_s)
        self.assertLess(timebase.end_s, waveform.achieved_period_s)

        time_axis = np.linspace(
            timebase.start_s,
            timebase.end_s,
            timebase.max_length,
        )
        reference = np.where(
            (time_axis >= 0.0) & (time_axis < 10e-6),
            1.0,
            0.0,
        )
        optical = np.where(
            (time_axis >= 2e-6) & (time_axis < 12e-6),
            1.0,
            0.0,
        )
        result = measure_frame(
            {
                "time": time_axis,
                "photodiode_v": optical,
                "waveform_reference_v": reference,
            },
            plan,
            timebase,
        )
        self.assertAlmostEqual(result.values_by_role["high_level"], 1.0)
        self.assertAlmostEqual(result.values_by_role["minimum"], 0.0)

    def test_raw_only_full_period_is_explicit_and_never_silently_capped(self):
        waveform = compile_waveform(
            "raw_square",
            {
                "type": "square",
                "low_level_v": 0.0,
                "high_level_v": 1.0,
                "frequency_hz": 100.0,
                "duty_cycle_percent": 10.0,
                "measurement_roles": {"low": None, "high": None},
            },
        )
        plan = compile_measurement_plan(waveform, raw_only=True)
        capped = SimpleNamespace(
            timebase_mode="automatic",
            timebase_max_length=16_384,
            automatic_timebase_max_duration_s=0.0026,
            raw_capture=RawCaptureSettings(raw_only_window="full_period"),
        )
        with self.assertRaisesRegex(ValueError, "cannot be shortened"):
            _compile_action_timebase(
                waveform=waveform,
                measurement_plan=plan,
                moku_settings=capped,
            )

        full = SimpleNamespace(
            **{
                **vars(capped),
                "automatic_timebase_max_duration_s": None,
            }
        )
        timebase = _compile_action_timebase(
            waveform=waveform,
            measurement_plan=plan,
            moku_settings=full,
        )
        self.assertAlmostEqual(
            timebase.end_s - timebase.start_s,
            waveform.achieved_period_s,
        )
        self.assertLess(timebase.start_s, 0.0)
        self.assertGreater(timebase.end_s, 0.0)


class MultiEdgeReferenceTests(unittest.TestCase):
    @staticmethod
    def _edges(waveform, level=0.5):
        values = waveform.connector_voltage_v
        following = np.roll(values, -1)
        result = []
        for direction, mask in (
            ("Rising", (values < level) & (following > level)),
            ("Falling", (values > level) & (following < level)),
        ):
            for index_value in np.flatnonzero(mask):
                index = int(index_value)
                fraction = (level - values[index]) / (following[index] - values[index])
                phase = (
                    (index + fraction) * waveform.point_interval_s
                ) % waveform.achieved_period_s
                result.append((float(phase), direction))
        return tuple(result)

    def test_nonuniform_multi_pulse_reference_identifies_actual_trigger_ordinal(self):
        waveform = compile_waveform(
            "train",
            {
                "type": "pulse_train",
                "pulse_count": 3,
                "low_level_v": 0.0,
                "high_level_v": 1.0,
                "pulse_duration_us": 10,
                "inter_pulse_gap_us": 5,
                "final_gap_us": 40,
                "edge_time_ns": 100,
            },
        )
        all_edges = self._edges(waveform)
        trigger_candidates = tuple(edge for edge in all_edges if edge[1] == "Rising")
        plan = compile_measurement_plan(
            waveform,
            trigger_phase_s=trigger_candidates[0][0],
            trigger_candidates=trigger_candidates,
            alignment_required=True,
            trigger_level_v=0.5,
            trigger_edge="Rising",
            reference_edge_tolerance_s=0.5e-6,
            maximum_optical_delay_s=1e-6,
            minimum_valid_points_per_role=10,
        )
        settings = SimpleNamespace(
            timebase_mode="automatic",
            timebase_max_length=16_384,
            automatic_timebase_max_duration_s=None,
            raw_capture=RawCaptureSettings(),
        )
        timebase = _compile_action_timebase(
            waveform=waveform,
            measurement_plan=plan,
            moku_settings=settings,
        )
        time_axis = np.linspace(
            timebase.start_s,
            timebase.end_s,
            timebase.max_length,
        )
        actual_phase = trigger_candidates[1][0]
        lut_time = np.arange(waveform.point_count + 1) * waveform.point_interval_s
        lut_voltage = np.r_[
            waveform.connector_voltage_v,
            waveform.connector_voltage_v[0],
        ]

        def sampled(offset_s):
            phase = (time_axis - offset_s + actual_phase) % waveform.achieved_period_s
            return np.interp(phase, lut_time, lut_voltage)

        result = measure_frame(
            {
                "time": time_axis,
                "waveform_reference_v": sampled(0.0),
                "photodiode_v": sampled(0.5e-6),
            },
            plan,
            timebase,
        )
        self.assertAlmostEqual(result.alignment.channel_b_edge_time_s, 0.0, places=7)
        self.assertAlmostEqual(result.alignment.optical_delay_s, 0.5e-6, places=7)
        self.assertGreater(result.values_by_role["high_level"], 0.99)
        self.assertLess(result.values_by_role["minimum"], 0.01)


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
        self.assertEqual(tracker.chunk_index, 1)
        interrupted = []
        for _ in range(8):
            interrupted.append(tracker.mark_interrupted())
            if tracker.uncertainty_budget_exhausted:
                break
        self.assertEqual(interrupted[:6], [50, 25, 12, 6, 3, 1])
        self.assertLessEqual(sum(interrupted), 100)
        self.assertEqual(tracker.interrupted_chunk_bounds, interrupted)
        self.assertEqual(tracker.chunk_index, len(interrupted) + 1)

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
