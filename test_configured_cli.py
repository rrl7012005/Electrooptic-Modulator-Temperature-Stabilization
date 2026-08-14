"""Hardware-free CLI, preview, snapshot, and configured-resume tests."""

from __future__ import annotations

import argparse
import builtins
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parent
SRC_DIRECTORY = REPOSITORY_ROOT / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

import matplotlib

matplotlib.use("Agg")

import run_experiment as root_runner  # noqa: E402
from eom_stabilisation import cli  # noqa: E402
from eom_stabilisation.config import load_experiment  # noqa: E402
from eom_stabilisation.experiment import (  # noqa: E402
    AtomicCheckpointStore,
    MokuCheckpoint,
    RuntimeCheckpoint,
)
from eom_stabilisation.experiment import planner  # noqa: E402
from eom_stabilisation.experiment.planner import build_effective_plan  # noqa: E402


REAL_DATETIME = datetime


class FixedDateTime(REAL_DATETIME):
    """Freeze artifact names so collision handling is deterministic."""

    @classmethod
    def now(cls, tz=None):
        value = REAL_DATETIME(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
        return value.replace(tzinfo=None) if tz is None else value.astimezone(tz)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


def _make_moku_config(root: Path) -> Path:
    _write(
        root / "run.yaml",
        """
        timezone: Europe/London
        moku:
          address: MokuGo-XXXXXX
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
          timebase_start_s: -0.001
          timebase_end_s: 0.001
          timebase_max_length: 4096
          sample_period_s: 1
          frames_per_sample: 1
        """,
    )
    _write(
        root / "pulse.yaml",
        """
        waveforms:
          pulse:
            type: square
            low_level_v: 0
            high_level_v: 1
            frequency_hz: 1000
            duty_cycle_percent: 50
        moku_schedule:
          - name: one_cycle
            waveform: pulse
            start: immediately
            run:
              mode: count
              count: 1
        """,
    )
    return _write(
        root / "experiment.yaml",
        """
        name: cli_test
        components: [moku]
        run_settings_file: run.yaml
        pulse_schedule_file: pulse.yaml
        end_when: moku_schedule_complete
        """,
    )


def _preview(output_root: Path, config_path: Path) -> Path:
    with (
        mock.patch.object(cli, "REPOSITORY_ROOT", output_root),
        mock.patch.object(planner, "datetime", FixedDateTime),
        redirect_stdout(io.StringIO()),
        redirect_stderr(io.StringIO()),
    ):
        result = cli.run_configured_experiment(config_path, action="preview")
    if result != 0:
        raise AssertionError(f"Preview failed with exit code {result}.")
    candidates = sorted((output_root / "previews").iterdir())
    return candidates[-1]


def _add_resumable_checkpoint(config_path: Path, run_directory: Path) -> None:
    plan = build_effective_plan(load_experiment(config_path))
    assert plan.waveform_program is not None
    waveform = plan.waveform_program.waveforms["pulse"]
    checkpoint = RuntimeCheckpoint(
        configuration_hash=plan.experiment.configuration_hash,
        source_hashes=plan.experiment.source_hashes,
        lut_hashes={"pulse": waveform.lut_sha256},
        experiment_elapsed_s=3.5,
        updated_at_utc="2026-08-11T11:59:59Z",
        temperature=None,
        moku=MokuCheckpoint(
            action_index=0,
            action_name="one_cycle",
            waveform_name="pulse",
            waveform_run_id="run-1",
            waveform_session_id=1,
            phase="waiting",
            repeat_mode="count",
            requested_duration_s=None,
            completed_duration_s=0,
            requested_count=1,
            completed_count=0,
            safe_resume_boundary=True,
            action_indeterminate=False,
            last_valid_sample_timestamp_utc="2026-08-11T11:59:58Z",
            last_confirmed_output_state="disabled",
            phase_continuity="unknown",
        ),
    )
    AtomicCheckpointStore(run_directory / "runtime_checkpoint.json").save(
        checkpoint
    )
    manifest_path = run_directory / "experiment_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "interrupted"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class ConfiguredCliTests(unittest.TestCase):
    def test_public_placeholder_blocks_execute_before_confirmation_or_import(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _make_moku_config(root / "source")
            errors = io.StringIO()
            with (
                mock.patch.object(cli, "REPOSITORY_ROOT", root),
                mock.patch.object(
                    cli,
                    "_confirm_configured_execution",
                    side_effect=AssertionError("confirmation must not be reached"),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(errors),
            ):
                result = cli.run_configured_experiment(config, action="execute")
            self.assertEqual(result, 2)
            self.assertIn("public placeholder", errors.getvalue())

    def test_nonplaceholder_moku_reaches_but_does_not_bypass_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _make_moku_config(root / "source")
            run_settings = config.parent / "run.yaml"
            run_settings.write_text(
                run_settings.read_text(encoding="utf-8").replace(
                    "MokuGo-XXXXXX", "verified-test-address"
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(cli, "REPOSITORY_ROOT", root),
                mock.patch.object(
                    cli,
                    "_confirm_configured_execution",
                    return_value=False,
                ) as confirmation,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = cli.run_configured_experiment(config, action="execute")
            self.assertEqual(result, 0)
            confirmation.assert_called_once_with()

    def test_dry_run_does_not_import_hardware_sdks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = _make_moku_config(Path(temporary_directory) / "source")
            original_import = builtins.__import__

            def guarded_import(name, *args, **kwargs):
                if name.split(".", 1)[0] in {"moku", "mecom", "linien_client"}:
                    raise AssertionError(f"Hardware SDK import attempted: {name}")
                return original_import(name, *args, **kwargs)

            with (
                mock.patch("builtins.__import__", side_effect=guarded_import),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = cli.run_configured_experiment(config, action="dry-run")
            self.assertEqual(result, 0)

    def test_preview_inventory_is_complete_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _make_moku_config(root / "source")
            first = _preview(root, config)
            first_contents = {
                path.relative_to(first): path.read_bytes()
                for path in first.rglob("*")
                if path.is_file()
            }
            second = _preview(root, config)

            self.assertEqual(first.name, "20260811_130000_cli_test")
            self.assertEqual(second.name, "20260811_130000_cli_test_01")
            self.assertNotEqual(first, second)
            self.assertEqual(
                first_contents,
                {
                    path.relative_to(first): path.read_bytes()
                    for path in first.rglob("*")
                    if path.is_file()
                },
            )
            expected = {
                Path("analysis_profile.json"),
                Path("configuration_hashes.json"),
                Path("effective_experiment.yaml"),
                Path("experiment_manifest.json"),
                Path("lut_hashes.json"),
                Path("original_configs/experiment_experiment.yaml"),
                Path("original_configs/pulse_schedule_pulse.yaml"),
                Path("original_configs/run_settings_run.yaml"),
                Path("reloadable_config/experiment.yaml"),
                Path("reloadable_config/pulse.yaml"),
                Path("reloadable_config/run.yaml"),
                Path("waveform_program.json"),
                Path("waveform_timeline.csv"),
                Path("waveform_timeline.png"),
                Path("waveforms/pulse.npy"),
                Path("waveforms/pulse_preview.png"),
            }
            self.assertEqual(set(first_contents), expected)

    def test_resume_uses_copied_tree_and_rejects_snapshot_tampering(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _make_moku_config(root / "source")
            run_directory = _preview(root, config)
            _add_resumable_checkpoint(config, run_directory)

            # Later source edits must be irrelevant to configured resume.
            (config.parent / "pulse.yaml").write_text(
                "this is no longer valid YAML for the experiment\n",
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = cli.resume_configured_experiment(
                    run_directory / "experiment_manifest.json",
                    action="dry-run",
                )
            self.assertEqual(result, 0)
            self.assertFalse((run_directory / "resume_plans").exists())

            saved_pulse = run_directory / "reloadable_config" / "pulse.yaml"
            saved_pulse.write_text("tampered: true\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = cli.resume_configured_experiment(
                    run_directory / "experiment_manifest.json",
                    action="dry-run",
                )
            self.assertEqual(result, 2)

    def test_resume_rejects_tampered_analysis_profile(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _make_moku_config(root / "source")
            run_directory = _preview(root, config)
            _add_resumable_checkpoint(config, run_directory)
            (run_directory / "analysis_profile.json").write_text(
                json.dumps(
                    {
                        "dark_offset_v": 0.25,
                        "minimum_high_level_v": 0.6,
                        "maximum_minimum_v": 0.6,
                        "minimum_sample_count": 10,
                    }
                ),
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = cli.resume_configured_experiment(
                    run_directory / "experiment_manifest.json",
                    action="dry-run",
                )
            self.assertEqual(result, 2)

    def test_resume_preview_saves_unique_plans_without_hardware(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _make_moku_config(root / "source")
            run_directory = _preview(root, config)
            _add_resumable_checkpoint(config, run_directory)
            manifest = run_directory / "experiment_manifest.json"

            with (
                mock.patch.object(cli, "datetime", FixedDateTime),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                first_result = cli.resume_configured_experiment(
                    manifest, action="preview"
                )
                second_result = cli.resume_configured_experiment(
                    manifest, action="preview"
                )
            self.assertEqual((first_result, second_result), (0, 0))
            plans = sorted((run_directory / "resume_plans").glob("*.json"))
            self.assertEqual(len(plans), 2)
            self.assertNotEqual(plans[0].name, plans[1].name)

    def test_resume_rejects_a_still_active_recorded_process(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _make_moku_config(root / "source")
            run_directory = _preview(root, config)
            _add_resumable_checkpoint(config, run_directory)
            manifest_path = run_directory / "experiment_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["processes"] = {
                "master": {"pid": 12345, "exit_code": None}
            }
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            error_output = io.StringIO()
            with (
                mock.patch.object(cli, "_process_is_running", return_value=True),
                redirect_stdout(io.StringIO()),
                redirect_stderr(error_output),
            ):
                result = cli.resume_configured_experiment(
                    manifest_path, action="dry-run"
                )
            self.assertEqual(result, 2)
            self.assertIn("may still be active", error_output.getvalue())

    def test_reloadable_snapshot_preserves_imported_csv_reference(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = (
                REPOSITORY_ROOT
                / "configs"
                / "examples"
                / "imported_lut_waveform.yaml"
            )
            run_directory = _preview(root, config)
            manifest = json.loads(
                (run_directory / "experiment_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            copied_master = run_directory / manifest["reloadable_experiment_file"]
            copied_plan = build_effective_plan(load_experiment(copied_master))

            self.assertIsNotNone(copied_plan.waveform_program)
            waveform = copied_plan.waveform_program.waveforms["imported_example"]
            self.assertTrue(waveform.source_path.is_relative_to(run_directory))
            self.assertTrue(waveform.source_path.is_file())
            self.assertEqual(len(waveform.source_sha256), 64)

    def test_root_runner_dispatches_configured_manifest_to_new_cli(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest = _write(
                Path(temporary_directory) / "run" / "experiment_manifest.json",
                json.dumps(
                    {
                        "format": cli.CONFIGURED_MANIFEST_FORMAT,
                        "version": 1,
                    }
                ),
            )
            arguments = argparse.Namespace(
                mode=None,
                components=None,
                config=None,
                resume_latest=False,
                resume=manifest,
                resume_warning_minutes=30.0,
                yes=False,
                dry_run=False,
                preview=True,
                execute=False,
            )
            with (
                mock.patch.object(root_runner, "parse_arguments", return_value=arguments),
                mock.patch(
                    "eom_stabilisation.cli.resume_configured_experiment",
                    return_value=17,
                ) as configured_resume,
                mock.patch.object(
                    root_runner,
                    "load_resume_context",
                    side_effect=AssertionError("legacy loader must not run"),
                ),
            ):
                result = root_runner.main()

            self.assertEqual(result, 17)
            configured_resume.assert_called_once_with(
                manifest.resolve(),
                action="preview",
                assume_yes=False,
                resume_warning_minutes=30.0,
            )

    def test_stale_configured_execute_requires_separate_acknowledgement(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _make_moku_config(root / "source")
            run_directory = _preview(root, config)
            _add_resumable_checkpoint(config, run_directory)
            context = cli._load_configured_resume_context(
                run_directory / "experiment_manifest.json"
            )
            context = cli.ConfiguredResumeContext(
                manifest_path=context.manifest_path,
                run_directory=context.run_directory,
                plan=context.plan,
                checkpoint=context.checkpoint,
                resume_plan=context.resume_plan,
                gap_minutes=31.0,
            )
            with (
                mock.patch.object(
                    cli, "_load_configured_resume_context", return_value=context
                ),
                mock.patch.object(cli, "_report_hardware_readiness_errors", return_value=True),
                mock.patch.object(
                    cli, "_confirm_stale_configured_resume", return_value=False
                ) as stale_confirmation,
                mock.patch.object(
                    cli,
                    "_confirm_configured_resume",
                    side_effect=AssertionError("RESUME prompt must not be reached"),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = cli.resume_configured_experiment(
                    run_directory / "experiment_manifest.json",
                    action="execute",
                    resume_warning_minutes=30.0,
                )
            self.assertEqual(result, 0)
            stale_confirmation.assert_called_once_with(31.0, 30.0)

    def test_recent_configured_resume_skips_stale_acknowledgement(self):
        with mock.patch("builtins.input") as input_mock:
            self.assertTrue(cli._confirm_stale_configured_resume(29.9, 30.0))
        input_mock.assert_not_called()

    def test_stale_configured_resume_requires_continue(self):
        with mock.patch("builtins.input", return_value="CANCEL") as input_mock:
            self.assertFalse(cli._confirm_stale_configured_resume(30.1, 30.0))
        input_mock.assert_called_once_with(
            "Type CONTINUE to acknowledge this risk, or CANCEL to stop: "
        )

    def test_manifest_classifier_preserves_legacy_compatibility(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest = _write(
                Path(temporary_directory) / "experiment_manifest.json",
                json.dumps({"selected_components": ["moku"]}),
            )
            path, document, family = root_runner.classify_resume_manifest(manifest)
            self.assertEqual(path, manifest.resolve())
            self.assertEqual(document["selected_components"], ["moku"])
            self.assertEqual(family, "legacy")


if __name__ == "__main__":
    unittest.main()
