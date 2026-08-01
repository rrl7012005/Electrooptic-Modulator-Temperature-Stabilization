"""Hardware-free tests for Moku:Go pulse validation and execution ordering."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parent
SRC_DIRECTORY = REPOSITORY_ROOT / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

import pulse_control
from eom_stabilisation.moku.pulse_sequences import (
    MOKU_GO_AWG_MEMORY_MODES,
    MOKU_GO_MAX_BURST_CYCLES,
    TraditionalPulse,
    compile_custom_program,
    parse_custom_sequence,
    validate_traditional_pulse,
)


EXAMPLE_SEQUENCE = {
    "name": "two_pulse_sequence",
    "segments": [
        {"type": "pulse", "duration_s": 10e-6, "edge_time_s": 100e-9},
        {"type": "gap", "duration_s": 5e-6},
        {"type": "pulse", "duration_s": 20e-6, "edge_time_s": 100e-9},
        {"type": "gap", "duration_s": 50e-6},
        {"type": "gap", "duration_s": 250e-6},
    ],
    "repeat_count": 10,
}


def compile_example():
    return compile_custom_program(
        [EXAMPLE_SEQUENCE],
        low_level_v=0.0,
        high_level_v=5.0,
        default_edge_time_s=100e-9,
    )[0]


class RecordingWriter:
    def __init__(self):
        self.rows = []

    def writerow(self, row):
        self.rows.append(row)


class FakeAwg:
    def __init__(self, address, force_connect):
        self.calls = [("connect", address, force_connect)]

    def enable_output(self, **kwargs):
        self.calls.append(("enable_output", kwargs))

    def generate_waveform(self, **kwargs):
        self.calls.append(("generate_waveform", kwargs))

    def disable_modulation(self, **kwargs):
        self.calls.append(("disable_modulation", kwargs))

    def burst_modulate(self, **kwargs):
        self.calls.append(("burst_modulate", kwargs))

    def manual_trigger(self):
        self.calls.append(("manual_trigger",))

    def relinquish_ownership(self):
        self.calls.append(("relinquish_ownership",))


class FakeOscilloscope:
    def __init__(self, address, force_connect):
        self.calls = [("connect", address, force_connect)]

    def generate_waveform(self, **kwargs):
        self.calls.append(("generate_waveform", kwargs))

    def relinquish_ownership(self):
        self.calls.append(("relinquish_ownership",))


class PulseValidationTests(unittest.TestCase):
    def test_traditional_pulse_preserves_original_width(self):
        pulse = validate_traditional_pulse(
            TraditionalPulse(0.0, 5.0, 100.0, 0.1, 100e-9)
        )
        self.assertAlmostEqual(pulse.pulse_width_s, 10e-6)
        self.assertAlmostEqual(pulse.amplitude_vpp, 5.0)
        self.assertAlmostEqual(pulse.offset_v, 2.5)

    def test_traditional_limits_are_rejected_before_connection(self):
        invalid = (
            TraditionalPulse(0.0, 5.01, 100.0, 0.1, 100e-9),
            TraditionalPulse(0.0, 5.0, 20e6 + 1.0, 10.0, 100e-9),
            TraditionalPulse(0.0, 5.0, 100.0, 0.1, 15e-9),
        )
        for pulse in invalid:
            with self.subTest(pulse=pulse), self.assertRaises(ValueError):
                validate_traditional_pulse(pulse)

    def test_example_sequence_compiles_to_safe_moku_go_lut(self):
        compiled = compile_example()
        self.assertAlmostEqual(compiled.sequence.period_s, 335e-6)
        self.assertAlmostEqual(compiled.burst_duration_s, 3.35e-3)
        self.assertEqual(compiled.sequence.repeat_count, 10)
        self.assertTrue(np.all(np.isfinite(compiled.lut_data)))
        self.assertGreaterEqual(float(np.min(compiled.lut_data)), -1.0)
        self.assertLessEqual(float(np.max(compiled.lut_data)), 1.0)
        self.assertIn(-1.0, compiled.lut_data)
        self.assertIn(1.0, compiled.lut_data)

        selected_mode = next(
            mode
            for mode in MOKU_GO_AWG_MEMORY_MODES
            if mode.api_name == compiled.sample_rate_name
        )
        self.assertLessEqual(len(compiled.lut_data), selected_mode.max_points)
        self.assertLessEqual(
            len(compiled.lut_data) * compiled.frequency_hz,
            selected_mode.sample_rate_hz,
        )
        for requested, achieved in zip(
            EXAMPLE_SEQUENCE["segments"], compiled.quantized_segments
        ):
            self.assertLessEqual(
                abs(requested["duration_s"] - achieved.actual_duration_s),
                compiled.point_interval_s + 1e-18,
            )

    def test_repeat_count_uses_documented_ncycle_range(self):
        raw = dict(EXAMPLE_SEQUENCE, repeat_count=MOKU_GO_MAX_BURST_CYCLES)
        sequence = parse_custom_sequence(raw, default_edge_time_s=100e-9)
        self.assertEqual(sequence.repeat_count, MOKU_GO_MAX_BURST_CYCLES)

        raw = dict(EXAMPLE_SEQUENCE, repeat_count=MOKU_GO_MAX_BURST_CYCLES + 1)
        with self.assertRaisesRegex(ValueError, "repeat_count"):
            parse_custom_sequence(raw, default_edge_time_s=100e-9)

    def test_custom_voltage_frequency_and_edge_limits_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "high_level_v"):
            compile_custom_program(
                [EXAMPLE_SEQUENCE],
                low_level_v=0.0,
                high_level_v=5.01,
                default_edge_time_s=100e-9,
            )

        too_fast = {
            "name": "too_fast",
            "segments": [
                {"type": "pulse", "duration_s": 34e-9, "edge_time_s": 16e-9},
                {"type": "gap", "duration_s": 16e-9},
            ],
            "repeat_count": 1,
        }
        with self.assertRaisesRegex(ValueError, "AWG range"):
            compile_custom_program(
                [too_fast],
                low_level_v=0.0,
                high_level_v=5.0,
                default_edge_time_s=100e-9,
            )

        short_edge = dict(
            EXAMPLE_SEQUENCE,
            segments=[
                {"type": "pulse", "duration_s": 10e-6, "edge_time_s": 15e-9},
                {"type": "gap", "duration_s": 5e-6},
            ],
        )
        with self.assertRaisesRegex(ValueError, "at least 16 ns"):
            compile_custom_program(
                [short_edge],
                low_level_v=0.0,
                high_level_v=5.0,
                default_edge_time_s=100e-9,
            )

    def test_continuous_sequence_must_be_last(self):
        continuous = dict(EXAMPLE_SEQUENCE, name="continuous", repeat_count=None)
        later = dict(EXAMPLE_SEQUENCE, name="later", repeat_count=1)
        with self.assertRaisesRegex(ValueError, "must be the final"):
            compile_custom_program(
                [continuous, later],
                low_level_v=0.0,
                high_level_v=5.0,
                default_edge_time_s=100e-9,
            )

    def test_unrepresentable_edge_is_rejected(self):
        raw = {
            "name": "resolution_test",
            "segments": [
                {"type": "pulse", "duration_s": 100e-6, "edge_time_s": 16e-9},
                {"type": "gap", "duration_s": 400e-6},
            ],
            "repeat_count": 1,
        }
        with self.assertRaisesRegex(ValueError, "fewer than two LUT points"):
            compile_custom_program(
                [raw],
                low_level_v=0.0,
                high_level_v=5.0,
                default_edge_time_s=100e-9,
            )

    def test_sequence_name_is_safe_for_windows_output_files(self):
        raw = dict(EXAMPLE_SEQUENCE, name="bad/name")
        with self.assertRaisesRegex(ValueError, "sequence names"):
            parse_custom_sequence(raw, default_edge_time_s=100e-9)


class FakeHardwareSequenceTests(unittest.TestCase):
    def test_finite_custom_burst_is_configured_before_trigger_and_cleaned_up(self):
        compiled = compile_example()
        instances = []
        waited = []

        def factory(address, force_connect):
            instance = FakeAwg(address, force_connect)
            instances.append(instance)
            return instance

        pulse_control.run_custom_program(
            (compiled,),
            RecordingWriter(),
            instrument_factory=factory,
            wait_for_duration=waited.append,
        )

        calls = instances[0].calls
        call_names = [call[0] for call in calls]
        self.assertEqual(
            call_names,
            [
                "connect",
                "enable_output",
                "enable_output",
                "generate_waveform",
                "enable_output",
                "disable_modulation",
                "burst_modulate",
                "enable_output",
                "manual_trigger",
                "enable_output",
                "enable_output",
                "enable_output",
                "relinquish_ownership",
            ],
        )
        burst = next(call[1] for call in calls if call[0] == "burst_modulate")
        self.assertEqual(burst["trigger_source"], "Manual")
        self.assertEqual(burst["trigger_mode"], "NCycle")
        self.assertEqual(burst["burst_cycles"], 10)
        self.assertTrue(burst["strict"])
        self.assertAlmostEqual(
            waited[0], compiled.burst_duration_s + pulse_control.COMPLETION_MARGIN_S
        )
        cleanup_calls = calls[-3:-1]
        self.assertEqual(
            [call[1]["channel"] for call in cleanup_calls],
            [1, 2],
        )
        self.assertTrue(all(not call[1]["enable"] for call in cleanup_calls))

    def test_custom_trigger_failure_still_turns_output_off_and_relinquishes(self):
        class FailingAwg(FakeAwg):
            def manual_trigger(self):
                self.calls.append(("manual_trigger",))
                raise RuntimeError("simulated trigger failure")

        instances = []

        def factory(address, force_connect):
            instance = FailingAwg(address, force_connect)
            instances.append(instance)
            return instance

        with self.assertRaisesRegex(RuntimeError, "simulated trigger failure"):
            pulse_control.run_custom_program(
                (compile_example(),),
                RecordingWriter(),
                instrument_factory=factory,
                wait_for_duration=lambda duration: None,
            )

        cleanup_calls = instances[0].calls[-3:-1]
        self.assertEqual(
            [call[1]["channel"] for call in cleanup_calls],
            [1, 2],
        )
        self.assertTrue(all(not call[1]["enable"] for call in cleanup_calls))
        self.assertEqual(instances[0].calls[-1], ("relinquish_ownership",))

    def test_traditional_mode_uses_strict_pulse_and_switches_off(self):
        pulse = TraditionalPulse(0.0, 5.0, 100.0, 0.1, 100e-9)
        instances = []
        waited = []

        def factory(address, force_connect):
            instance = FakeOscilloscope(address, force_connect)
            instances.append(instance)
            return instance

        with patch.object(pulse_control, "TRADITIONAL_RUN_DURATION_S", 0.25):
            pulse_control.run_traditional_pulse(
                pulse,
                RecordingWriter(),
                instrument_factory=factory,
                wait_for_duration=waited.append,
            )

        waveform_calls = [
            call[1]
            for call in instances[0].calls
            if call[0] == "generate_waveform"
        ]
        self.assertEqual(waveform_calls[0]["type"], "Pulse")
        self.assertTrue(waveform_calls[0]["strict"])
        self.assertEqual(waveform_calls[1]["type"], "Off")
        self.assertEqual(instances[0].calls[-1], ("relinquish_ownership",))
        self.assertEqual(waited, [0.25])

    def test_preview_creation_does_not_require_moku_package(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.png"
            pulse_control.save_custom_preview(compile_example(), output)
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
