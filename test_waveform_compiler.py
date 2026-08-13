"""Hardware-free tests for generalized Moku waveform compilation."""

from __future__ import annotations

import builtins
import csv
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

from eom_stabilisation.moku.models import RunMode
from eom_stabilisation.moku.waveform_compiler import (
    MOKU_GO_AWG_MEMORY_MODES,
    compile_waveform,
    compile_waveform_program,
    parse_run_spec,
)
from eom_stabilisation.moku.waveform_registry import register_waveform


class SquareCompilationTests(unittest.TestCase):
    def test_current_official_moku_go_memory_table(self):
        self.assertEqual(
            [
                (mode.api_name, mode.sample_rate_hz, mode.max_points)
                for mode in MOKU_GO_AWG_MEMORY_MODES
            ],
            [
                ("125Ms", 125e6, 16_384),
                ("62.5Ms", 62.5e6, 32_768),
                ("31.25Ms", 31.25e6, 65_536),
            ],
        )

    def test_all_three_square_parameterizations_are_equivalent(self):
        definitions = (
            {
                "type": "square",
                "low_level_v": 0.0,
                "high_level_v": 5.0,
                "frequency_hz": 100_000,
                "duty_cycle_percent": 10,
                "edge_time_ns": 100,
            },
            {
                "type": "square",
                "low_level_v": 0.0,
                "high_level_v": 5.0,
                "period_us": 10,
                "pulse_duration_us": 1,
                "edge_time_ns": 100,
            },
            {
                "type": "square",
                "low_level_v": 0.0,
                "high_level_v": 5.0,
                "pulse_duration_us": 1,
                "gap_duration_us": 9,
                "edge_time_ns": 100,
            },
        )
        compiled = [compile_waveform(f"square_{index}", item) for index, item in enumerate(definitions)]
        for waveform in compiled:
            self.assertAlmostEqual(waveform.requested_period_s, 10e-6)
            self.assertAlmostEqual(waveform.segments[0].requested_duration_s, 1e-6)
            self.assertAlmostEqual(waveform.segments[1].requested_duration_s, 9e-6)
            self.assertEqual(waveform.segments[0].measurement_role, "high_level")
            self.assertEqual(waveform.segments[1].measurement_role, "minimum")
        np.testing.assert_allclose(
            compiled[0].connector_voltage_v,
            compiled[1].connector_voltage_v,
        )

    def test_conflicting_units_and_timing_forms_are_rejected(self):
        base = {
            "type": "square",
            "low_level_v": 0,
            "high_level_v": 1,
            "period_us": 10,
            "pulse_duration_us": 1,
        }
        with self.assertRaisesRegex(ValueError, "conflicting duration"):
            compile_waveform("bad_units", {**base, "period_ns": 10_000})
        with self.assertRaisesRegex(ValueError, "exactly one square timing form"):
            compile_waveform(
                "bad_forms",
                {**base, "frequency_hz": 100_000, "duty_cycle_percent": 10},
            )

    def test_exact_fifty_microsecond_run_is_five_cycle_hardware_burst(self):
        run = parse_run_spec(
            {
                "mode": "duration",
                "duration_us": 50,
                "end_policy": "reject_partial_cycle",
            },
            10e-6,
        )
        self.assertEqual(run.repeat_count, 5)
        self.assertTrue(run.exact_hardware_burst)
        self.assertAlmostEqual(run.achieved_duration_s, 50e-6)

    def test_non_integral_duration_policies_never_round_silently(self):
        with self.assertRaisesRegex(ValueError, "partial cycles"):
            parse_run_spec({"mode": "duration", "duration_us": 55}, 10e-6)
        down = parse_run_spec(
            {"mode": "duration", "duration_us": 55, "end_policy": "round_down"},
            10e-6,
        )
        up = parse_run_spec(
            {"mode": "duration", "duration_us": 55, "end_policy": "round_up"},
            10e-6,
        )
        self.assertEqual((down.repeat_count, up.repeat_count), (5, 6))
        with self.assertRaisesRegex(ValueError, "truncate.*not supported"):
            parse_run_spec(
                {"mode": "duration", "duration_us": 55, "end_policy": "truncate"},
                10e-6,
            )


class WaveformModeTests(unittest.TestCase):
    def test_segments_keep_per_segment_voltage_and_no_trailing_gap(self):
        waveform = compile_waveform(
            "two_pulse",
            {
                "type": "segments",
                "low_level_v": 0,
                "segments": [
                    {"type": "pulse", "name": "first", "level_v": 5, "duration_us": 20, "edge_time_ns": 100},
                    {"type": "gap", "name": "middle", "duration_us": 80, "measurement_role": "minimum"},
                    {"type": "pulse", "name": "second", "level_v": 3, "duration_us": 40, "edge_time_ns": 100, "measurement_role": "high_level"},
                ],
            },
        )
        self.assertEqual([segment.level_v for segment in waveform.segments], [5, 0, 3])
        self.assertEqual(waveform.segments[-1].name, "second")
        self.assertIn("No trailing gap", waveform.warnings[0])
        self.assertAlmostEqual(waveform.requested_period_s, 140e-6)

    def test_pulse_train_generation(self):
        waveform = compile_waveform(
            "train",
            {
                "type": "pulse_train",
                "pulse_count": 5,
                "low_level_v": 0,
                "high_level_v": 5,
                "pulse_duration_us": 10,
                "inter_pulse_gap_us": 5,
                "final_gap_us": 250,
                "edge_time_ns": 100,
            },
        )
        self.assertEqual(sum(segment.kind == "pulse" for segment in waveform.segments), 5)
        self.assertEqual(waveform.segments[-1].name, "final_gap")
        self.assertAlmostEqual(waveform.requested_period_s, 320e-6)

    def test_staircase_generation_and_explicit_levels(self):
        generated = compile_waveform(
            "stairs",
            {
                "type": "staircase",
                "start_v": 0,
                "finish_v": 1,
                "step_v": 0.5,
                "dwell_us": 100,
                "direction": "up_and_down",
                "repeat_endpoints": False,
                "final_gap_us": 50,
            },
        )
        self.assertEqual([segment.level_v for segment in generated.segments[:-1]], [0, 0.5, 1, 0.5])
        explicit = compile_waveform(
            "explicit_stairs",
            {
                "type": "staircase",
                "levels_v": [0, 0.5, 1.7, 3.2, 5],
                "dwell_us": 100,
            },
        )
        self.assertEqual(len(explicit.segments), 5)

    def test_registered_function_is_validated_without_eval(self):
        def validator(parameters):
            if set(parameters) != {"amplitude_v"}:
                raise ValueError("expected amplitude_v")
            return {"amplitude_v": float(parameters["amplitude_v"])}

        @register_waveform("unit_test_triangle", parameter_validator=validator, replace=True)
        def triangle(phase, parameters):
            return parameters["amplitude_v"] * (1 - 2 * np.abs(phase - 0.5))

        waveform = compile_waveform(
            "custom",
            {
                "type": "custom_python",
                "function": "unit_test_triangle",
                "parameters": {"amplitude_v": 1.0},
                "period_us": 100,
                "point_count": 1000,
            },
        )
        self.assertEqual(waveform.point_count, 1000)
        self.assertTrue(np.all(np.isfinite(waveform.connector_voltage_v)))
        with self.assertRaisesRegex(ValueError, "unknown registered"):
            compile_waveform(
                "unsafe",
                {"type": "custom_python", "function": "os.system", "parameters": {}, "period_us": 100},
            )

    def test_csv_lut_requires_uniform_finite_strictly_increasing_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good.csv"
            with good.open("w", newline="", encoding="utf-8") as output:
                writer = csv.writer(output)
                writer.writerow(["time_s", "voltage_v", "note"])
                writer.writerows([(0, 0, "a"), (1e-6, 1, "b"), (2e-6, 0, "c")])
            waveform = compile_waveform(
                "imported",
                {"type": "csv_lut", "path": "good.csv"},
                base_path=root,
            )
            self.assertEqual(waveform.point_count, 3)
            self.assertAlmostEqual(waveform.requested_period_s, 3e-6)
            self.assertEqual(waveform.source_path, good.resolve())
            self.assertEqual(len(waveform.source_sha256), 64)

            bad = root / "bad.csv"
            bad.write_text("time_s,voltage_v\n0,0\n1e-6,1\n3e-6,0\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "spacing must be uniform"):
                compile_waveform(
                    "bad_import", {"type": "csv_lut", "path": bad.name}, base_path=root
                )

    def test_hashes_are_deterministic_and_dry_compile_never_imports_moku(self):
        definition = {
            "type": "square", "low_level_v": 0, "high_level_v": 1,
            "frequency_hz": 10_000, "duty_cycle_percent": 25,
        }
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "moku" or name.startswith("moku."):
                raise AssertionError("dry compiler imported Moku SDK")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            first = compile_waveform("hash_test", definition)
            second = compile_waveform("hash_test", definition)
        self.assertEqual(first.lut_sha256, second.lut_sha256)
        self.assertEqual(first.timing_sha256, second.timing_sha256)

    def test_program_normalizes_fill_experiment_and_requires_forever_last(self):
        waveforms = {
            "square": {
                "type": "square", "low_level_v": 0, "high_level_v": 1,
                "frequency_hz": 1000, "duty_cycle_percent": 50,
            }
        }
        program = compile_waveform_program(
            waveforms,
            [{"waveform": "square", "run": {"mode": "fill_experiment"}}],
        )
        self.assertIs(program.actions[0].run.mode, RunMode.UNTIL_EXPERIMENT_END)
        with self.assertRaisesRegex(ValueError, "forever.*final"):
            compile_waveform_program(
                waveforms,
                [
                    {"waveform": "square", "run": {"mode": "forever"}},
                    {"waveform": "square", "run": {"mode": "count", "count": 1}},
                ],
            )


if __name__ == "__main__":
    unittest.main()
