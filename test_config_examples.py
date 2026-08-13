"""Hardware-free regression tests for every published YAML example."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parent
SRC_DIRECTORY = REPOSITORY_ROOT / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.config import load_experiment
from eom_stabilisation.experiment.planner import build_effective_plan
from eom_stabilisation.moku.waveform_compiler import compile_waveform_program


EXPECTED_EXAMPLE_FILES = {
    "continuous_staircase.yaml",
    "custom_python_waveform.yaml",
    "duty_cycle_parameter_sweep.yaml",
    "imported_lut_waveform.yaml",
    "independent_temperature_and_moku.yaml",
    "moku_only_100khz_10percent_for_50us.yaml",
    "moku_only_duty_cycle.yaml",
    "moku_only_two_pulse_sequence.yaml",
    "pulse_train.yaml",
    "temperature_only_sweep.yaml",
    "waveform_started_when_temperature_stable.yaml",
    "waveform_until_temperature_stage_end.yaml",
}


def load_and_compile(path: Path):
    """Load one master file and compile its complete Moku program, if any."""

    experiment = load_experiment(path)
    if experiment.pulse_schedule is None:
        return experiment, None
    schedule = experiment.pulse_schedule
    program = compile_waveform_program(
        schedule.waveforms,
        schedule.actions,
        base_path=schedule.source_path.parent,
    )
    return experiment, program


class ConfigurationExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.examples_directory = REPOSITORY_ROOT / "configs" / "examples"

    def test_exact_requested_example_master_files_are_present(self):
        actual = {
            path.name for path in self.examples_directory.glob("*.yaml")
        }
        self.assertEqual(actual, EXPECTED_EXAMPLE_FILES)

    def test_every_example_loads_and_compiles_without_hardware_sdk(self):
        for filename in sorted(EXPECTED_EXAMPLE_FILES):
            with self.subTest(filename=filename):
                experiment, program = load_and_compile(
                    self.examples_directory / filename
                )
                self.assertEqual(len(experiment.configuration_hash), 64)
                self.assertTrue(experiment.sources)
                self.assertEqual(
                    set(experiment.sources), set(experiment.source_hashes)
                )
                for source_path in experiment.sources.values():
                    self.assertTrue(source_path.is_file())
                for digest in experiment.source_hashes.values():
                    self.assertEqual(len(digest), 64)

                if experiment.pulse_schedule is None:
                    self.assertIsNone(program)
                else:
                    self.assertIsNotNone(program)
                    self.assertEqual(
                        set(program.waveforms),
                        set(experiment.pulse_schedule.waveforms),
                    )
                    self.assertEqual(
                        len(program.actions),
                        len(experiment.pulse_schedule.actions),
                    )
                    self.assertEqual(len(program.program_sha256), 64)
                    for waveform in program.waveforms.values():
                        self.assertGreaterEqual(waveform.point_count, 2)
                        self.assertEqual(len(waveform.lut_sha256), 64)
                        self.assertEqual(len(waveform.timing_sha256), 64)

                effective_plan = build_effective_plan(experiment)
                if program is None:
                    self.assertIsNone(effective_plan.waveform_program)
                    self.assertFalse(effective_plan.measurement_plans)
                else:
                    self.assertEqual(
                        effective_plan.waveform_program.program_sha256,
                        program.program_sha256,
                    )
                    self.assertEqual(
                        set(effective_plan.measurement_plans),
                        set(program.waveforms),
                    )

        # The compiler and strict loaders are SDK-free. The similarly named
        # eom_stabilisation.moku package is local code; top-level `moku` is the
        # Liquid Instruments hardware SDK and must remain absent.
        self.assertNotIn("moku", sys.modules)
        self.assertNotIn("mecom", sys.modules)
        self.assertNotIn("linien_client", sys.modules)

    def test_canonical_four_file_configuration_loads_and_compiles(self):
        experiment, program = load_and_compile(
            REPOSITORY_ROOT / "configs" / "experiment.yaml"
        )
        self.assertEqual(
            experiment.components,
            ("lock", "moku", "temp-control"),
        )
        self.assertIsNotNone(experiment.temperature_schedule)
        self.assertIsNotNone(program)
        self.assertIn("square_100khz", program.waveforms)

    def test_100khz_ten_percent_fifty_microseconds_is_five_cycles(self):
        _, program = load_and_compile(
            self.examples_directory
            / "moku_only_100khz_10percent_for_50us.yaml"
        )
        self.assertIsNotNone(program)
        waveform = program.waveforms["square_100khz_10_percent"]
        self.assertAlmostEqual(waveform.requested_period_s, 10e-6)
        self.assertAlmostEqual(
            waveform.segments[0].requested_duration_s,
            1e-6,
        )
        self.assertAlmostEqual(
            waveform.segments[1].requested_duration_s,
            9e-6,
        )
        run = program.actions[0].run
        self.assertEqual(run.repeat_count, 5)
        self.assertTrue(run.exact_hardware_burst)
        self.assertIsNone(run.requested_duration_s)
        self.assertAlmostEqual(run.achieved_duration_s, 50e-6)

    def test_imported_lut_path_and_source_hash_are_preserved(self):
        _, program = load_and_compile(
            self.examples_directory / "imported_lut_waveform.yaml"
        )
        self.assertIsNotNone(program)
        waveform = program.waveforms["imported_example"]
        expected_path = (
            self.examples_directory / "assets" / "imported_lut.csv"
        ).resolve()
        self.assertEqual(waveform.source_path, expected_path)
        self.assertEqual(len(waveform.source_sha256), 64)

    def test_experiment_schema_is_valid_json_and_strict(self):
        schema_path = (
            REPOSITORY_ROOT / "schemas" / "experiment.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$schema"],
            "https://json-schema.org/draft/2020-12/schema",
        )
        self.assertNotIn("version", schema["properties"])
        self.assertNotIn("version", schema["required"])
        self.assertFalse(schema["additionalProperties"])
        temperature_settings = schema["$defs"][
            "temperatureControllerSettings"
        ]
        self.assertIn("safe_target_c", temperature_settings["properties"])
        self.assertFalse(temperature_settings["additionalProperties"])
        measurement_settings = schema["$defs"]["measurementAnalysisSettings"]
        self.assertEqual(
            measurement_settings["default"],
            {
                "dark_offset_v": 0.0,
                "minimum_high_level_v": 0.6,
                "maximum_minimum_v": 0.6,
                "minimum_sample_count": 10,
                "maximum_optical_delay_s": 0.0,
                "reference_edge_tolerance_s": 0.000001,
                "minimum_valid_points_per_role": 10,
                "minimum_optical_edge_snr": 3.0,
            },
        )
        self.assertFalse(measurement_settings["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
