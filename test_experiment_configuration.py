"""Tests for strict multi-file experiment configuration composition."""

from pathlib import Path
import sys
import tempfile
import textwrap
import unittest


SRC_DIRECTORY = Path(__file__).resolve().parent / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.config import (  # noqa: E402
    ConfigurationError,
    DuplicateKeyError,
    UnknownFieldError,
    load_experiment,
)
from eom_stabilisation.tec.schedule import (  # noqa: E402
    parse_temperature_schedule,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


MOKU_SETTINGS = """moku:
  address: MokuGo-test
  fallback_address: null
  force_connect: false
  platform_id: 2
  awg_slot: 1
  oscilloscope_slot: 2
  output_channel: 2
  input_channel: 1
  frontend_impedance: 1MOhm
  frontend_coupling: DC
  frontend_attenuation: 0dB
  trigger_source: ChannelB
  trigger_level_v: 0.6
  trigger_edge: Rising
  trigger_mode: Normal
  trigger_type: Edge
  timebase_start_s: -0.000045
  timebase_end_s: 0.000045
  timebase_max_length: 16384
  sample_period_s: 1
  frames_per_sample: 5
"""


class ExperimentConfigurationTests(unittest.TestCase):
    def test_optional_tec_sensor_envelope_and_linien_host_are_strict(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            run_path = _write(
                root / "run.yaml",
                """
                temperature:
                  serial_port: COM_TEST
                  channel: 1
                  min_target_c: 10
                  max_target_c: 40
                  sampling_interval_s: 0.25
                  object_temperature_min_c: 0
                  object_temperature_max_c: 50
                  sink_temperature_min_c: 5
                  sink_temperature_max_c: 45
                linien:
                  host: red-pitaya.example.test
                """,
            )
            master = _write(
                root / "experiment.yaml",
                """
                name: passive_log
                components: [temp-log, lock]
                run_settings_file: run.yaml
                end_when: operator_ctrl_c
                """,
            )

            loaded = load_experiment(master)
            temperature = loaded.run_settings.temperature
            self.assertTrue(temperature.has_sensor_bounds)
            self.assertEqual(temperature.sampling_interval_s, 0.25)
            self.assertEqual(
                loaded.run_settings.linien.host,
                "red-pitaya.example.test",
            )

            run_path.write_text(
                """temperature:
  serial_port: COM_TEST
  channel: 1
  min_target_c: 10
  max_target_c: 40
  object_temperature_min_c: 0
linien:
  host: red-pitaya.example.test
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "must either all"):
                load_experiment(master)

    def test_legacy_temperature_config_without_sensor_bounds_still_dry_runs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write(
                root / "run.yaml",
                """
                temperature:
                  serial_port: COM_TEST
                  channel: 1
                  min_target_c: 10
                  max_target_c: 40
                """,
            )
            master = _write(
                root / "experiment.yaml",
                """
                name: legacy_passive_log
                components: [temp-log]
                run_settings_file: run.yaml
                end_when: operator_ctrl_c
                """,
            )
            loaded = load_experiment(master)
            self.assertFalse(loaded.run_settings.temperature.has_sensor_bounds)
            self.assertEqual(
                loaded.run_settings.temperature.sampling_interval_s,
                1.0,
            )

    def test_measurement_analysis_settings_are_strict_and_hashed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            run_path = _write(
                root / "run.yaml",
                """
                measurement:
                  dark_offset_v: 0.025
                  minimum_high_level_v: null
                  maximum_minimum_v: 0.4
                  minimum_sample_count: 12
                """,
            )
            master = _write(
                root / "experiment.yaml",
                """
                name: analysis_profile
                components: [lock]
                run_settings_file: run.yaml
                end_when: operator_ctrl_c
                """,
            )
            loaded = load_experiment(master)
            self.assertEqual(loaded.run_settings.measurement.dark_offset_v, 0.025)
            self.assertIsNone(
                loaded.run_settings.measurement.minimum_high_level_v
            )
            self.assertEqual(
                loaded.effective_dict()["run_settings"]["measurement"],
                {
                    "dark_offset_v": 0.025,
                    "minimum_high_level_v": None,
                    "maximum_minimum_v": 0.4,
                    "minimum_sample_count": 12,
                },
            )

            run_path.write_text(
                "measurement:\n  unexpected: true\n",
                encoding="utf-8",
            )
            with self.assertRaises(UnknownFieldError):
                load_experiment(master)

    def test_relative_files_are_composed_hashed_and_expanded(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write(
                root / "run.yaml",
                """
                timezone: Europe/London
                temperature:
                  serial_port: COM_TEST
                  channel: 1
                  min_target_c: 10
                  max_target_c: 50
                recovery:
                  maximum_moku_outage_minutes: 30
                """,
            )
            _write(
                root / "schedules" / "temperature.yaml",
                """
                name: controlled_hold
                type: explicit
                completion_behavior: disable_output
                stability:
                  required: true
                  tolerance_c: 0.2
                  stable_duration_s: 3
                  timeout_s: 30
                stages:
                  - name: hold_25
                    target_c: 25
                    hold_duration_minutes: 2
                """,
            )
            master = _write(
                root / "experiment.yaml",
                """
                name: temperature_test
                components: [temp-control]
                run_settings_file: run.yaml
                temperature_schedule_file: schedules/temperature.yaml
                pulse_schedule_file: null
                end_when: temperature_schedule_complete
                """,
            )

            loaded = load_experiment(master)

            self.assertEqual(loaded.name, "temperature_test")
            self.assertEqual(loaded.components, ("temp-control",))
            self.assertEqual(loaded.temperature_schedule.stages[0].hold_duration_s, 120)
            self.assertEqual(
                loaded.temperature_schedule.stages[0].stability.stable_duration_s,
                3,
            )
            self.assertEqual(set(loaded.sources), {"experiment", "run_settings", "temperature_schedule"})
            self.assertTrue(all(len(value) == 64 for value in loaded.source_hashes.values()))
            self.assertEqual(len(loaded.configuration_hash), 64)
            self.assertEqual(
                loaded.effective_dict()["temperature_schedule"]["type"],
                "explicit",
            )

    def test_duplicate_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            master = _write(
                Path(temporary_directory) / "experiment.yaml",
                """
                name: first
                name: second
                components: [lock]
                end_when: operator_ctrl_c
                """,
            )
            with self.assertRaises(DuplicateKeyError):
                load_experiment(master)

    def test_unknown_master_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            master = _write(
                Path(temporary_directory) / "experiment.yaml",
                """
                name: bad_field
                components: [lock]
                mystery: true
                end_when: operator_ctrl_c
                """,
            )
            with self.assertRaises(UnknownFieldError):
                load_experiment(master)

    def test_passive_component_allows_null_schedule_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            master = _write(
                Path(temporary_directory) / "experiment.yaml",
                """
                name: lock_only
                components: [lock]
                run_settings_file: null
                temperature_schedule_file: null
                pulse_schedule_file: null
                end_when: operator_ctrl_c
                """,
            )
            loaded = load_experiment(master)
            self.assertIsNone(loaded.temperature_schedule)
            self.assertIsNone(loaded.pulse_schedule)

    def test_temperature_targets_are_checked_against_owned_limits(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write(
                root / "run.yaml",
                """
                temperature:
                  serial_port: COM_TEST
                  channel: 1
                  min_target_c: 20
                  max_target_c: 30
                """,
            )
            _write(
                root / "temperature.yaml",
                """
                name: unsafe_target
                type: targets
                completion_behavior: disable_output
                targets_c: [25, 31]
                hold_duration_s: 10
                """,
            )
            master = _write(
                root / "experiment.yaml",
                """
                name: limits
                components: [temp-control]
                run_settings_file: run.yaml
                temperature_schedule_file: temperature.yaml
                end_when: temperature_schedule_complete
                """,
            )
            with self.assertRaisesRegex(ConfigurationError, "outside configured limits"):
                load_experiment(master)

    def test_return_to_safe_target_requires_an_explicit_safe_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write(
                root / "run.yaml",
                """
                temperature:
                  serial_port: COM_TEST
                  channel: 1
                  min_target_c: 20
                  max_target_c: 30
                """,
            )
            _write(
                root / "temperature.yaml",
                """
                name: safe_return
                type: targets
                completion_behavior: return_to_safe_target
                targets_c: [25]
                hold_duration_s: 10
                """,
            )
            master = _write(
                root / "experiment.yaml",
                """
                name: safe_return
                components: [temp-control]
                run_settings_file: run.yaml
                temperature_schedule_file: temperature.yaml
                end_when: temperature_schedule_complete
                """,
            )
            with self.assertRaisesRegex(ConfigurationError, "safe_target_c"):
                load_experiment(master)

    def test_temperature_linked_action_requires_active_temperature_schedule(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write(root / "run.yaml", MOKU_SETTINGS)
            _write(
                root / "pulse.yaml",
                """
                waveforms:
                  pulse:
                    type: square
                    low_level_v: 0
                    high_level_v: 1
                    frequency_hz: 100
                    duty_cycle_percent: 10
                moku_schedule:
                  - waveform: pulse
                    start:
                      mode: temperature_became_stable
                      temperature_stage: hold_25
                    run:
                      mode: until_experiment_end
                """,
            )
            master = _write(
                root / "experiment.yaml",
                """
                name: invalid_link
                components: [moku]
                run_settings_file: run.yaml
                pulse_schedule_file: pulse.yaml
                end_when: moku_schedule_complete
                """,
            )
            with self.assertRaisesRegex(ConfigurationError, "no active temperature"):
                load_experiment(master)

    def test_pulse_envelope_exposes_raw_waveforms_and_actions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write(root / "run.yaml", MOKU_SETTINGS)
            _write(
                root / "pulse.yaml",
                """
                waveforms:
                  pulse:
                    type: square
                    low_level_v: 0
                    high_level_v: 1
                    frequency_hz: 100000
                    duty_cycle_percent: 10
                moku_schedule:
                  - name: five_cycles
                    waveform: pulse
                    run:
                      mode: duration
                      duration_us: 50
                      end_policy: reject_partial_cycle
                """,
            )
            master = _write(
                root / "experiment.yaml",
                """
                name: moku_only
                components: [moku]
                run_settings_file: run.yaml
                pulse_schedule_file: pulse.yaml
                end_when: moku_schedule_complete
                """,
            )
            loaded = load_experiment(master)
            self.assertEqual(loaded.pulse_schedule.waveforms["pulse"]["type"], "square")
            self.assertEqual(loaded.pulse_schedule.actions[0]["run"]["duration_us"], 50)

    def test_conflicting_duration_representations_are_rejected(self):
        document = {
            "name": "conflict",
            "type": "targets",
            "completion_behavior": "disable_output",
            "targets_c": [25],
            "hold_duration_s": 1,
            "hold_duration_minutes": 1,
        }
        with self.assertRaisesRegex(ConfigurationError, "conflicting durations"):
            parse_temperature_schedule(document)

    def test_temperature_completion_behavior_must_be_explicit(self):
        with self.assertRaisesRegex(ConfigurationError, "completion_behavior"):
            parse_temperature_schedule(
                {
                    "name": "missing_completion",
                    "type": "targets",
                    "targets_c": [25],
                    "hold_duration_s": 1,
                }
            )

    def test_sweep_targets_range_cycles_and_stability_expand(self):
        sweep = parse_temperature_schedule(
            {
                "name": "sweep",
                "type": "sweep",
                "completion_behavior": "disable_output",
                "start_c": 25,
                "finish_c": 30,
                "measurement_interval_c": 5,
                "transition_increment_c": 2.5,
                "transition_hold_minutes": 10,
                "measurement_hold_minutes": 60,
                "reverse": True,
                "cycles": 2,
                "stability": {
                    "required": True,
                    "stable_duration_s": 300,
                    "timeout_s": 1800,
                },
            }
        )
        self.assertEqual(
            [stage.target_c for stage in sweep.stages],
            [25, 27.5, 30, 27.5, 25, 27.5, 30, 27.5, 25],
        )
        self.assertEqual(sweep.stages[0].stability.stable_duration_s, 300)
        self.assertEqual(len(set(sweep.stage_names)), len(sweep.stage_names))

        targets = parse_temperature_schedule(
            {
                "name": "targets",
                "type": "targets",
                "completion_behavior": "disable_output",
                "targets_c": [20, 22, 24],
                "hold_duration_s": 1,
                "reverse": True,
                "cycles": 1,
            }
        )
        self.assertEqual([stage.target_c for stage in targets.stages], [20, 22, 24, 22, 20])

        generated_range = parse_temperature_schedule(
            {
                "name": "range",
                "type": "range",
                "completion_behavior": "disable_output",
                "start_c": 20,
                "finish_c": 25,
                "step_c": 2,
                "hold_duration_s": 1,
            }
        )
        self.assertEqual(
            [stage.target_c for stage in generated_range.stages],
            [20, 22, 24, 25],
        )


if __name__ == "__main__":
    unittest.main()
