"""Tests for clocks, run snapshots, and typed atomic checkpoints."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SRC_DIRECTORY = Path(__file__).resolve().parent / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.config import ResumeConfigurationMismatch  # noqa: E402
from eom_stabilisation.experiment import (  # noqa: E402
    AtomicCheckpointStore,
    ExperimentClock,
    JsonlExperimentEventWriter,
    MokuCheckpoint,
    RuntimeCheckpoint,
    TemperatureCheckpoint,
    validate_resume_checkpoint,
    validate_resume_hashes,
)
from eom_stabilisation.run_store import RunStore, sha256_file  # noqa: E402


class MutableClock:
    def __init__(self) -> None:
        self.monotonic = 10.0
        self.utc = datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc)

    def monotonic_now(self) -> float:
        return self.monotonic

    def utc_now(self) -> datetime:
        return self.utc


def _checkpoint(*, indeterminate: bool = False) -> RuntimeCheckpoint:
    return RuntimeCheckpoint(
        configuration_hash="a" * 64,
        source_hashes={"experiment": "b" * 64},
        lut_hashes={"pulse": "c" * 64},
        experiment_elapsed_s=12.5,
        updated_at_utc="2026-08-11T12:00:00Z",
        temperature=TemperatureCheckpoint(
            stage_index=1,
            stage_name="hold_25",
            phase="holding",
            requested_target_c=25.0,
            completed_hold_s=10.0,
            stable_elapsed_s=0.0,
            phase_elapsed_s=10.0,
            schedule_elapsed_s=12.5,
            safe_resume_boundary=True,
            last_confirmed_output_state="enabled",
        ),
        moku=MokuCheckpoint(
            action_index=0,
            action_name="pulse",
            waveform_name="square",
            waveform_run_id="run-1",
            waveform_session_id=2,
            phase="indeterminate" if indeterminate else "running",
            repeat_mode="duration",
            requested_duration_s=100.0,
            completed_duration_s=20.0,
            requested_count=None,
            completed_count=0,
            safe_resume_boundary=not indeterminate,
            action_indeterminate=indeterminate,
            last_valid_sample_timestamp_utc="2026-08-11T11:59:59Z",
            last_confirmed_output_state="disabled",
            phase_continuity="reset_to_phase_zero",
        ),
    )


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_round_trip_is_atomic_and_typed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "runtime_checkpoint.json"
            store = AtomicCheckpointStore(path)
            store.save(_checkpoint())
            loaded = store.load()
            self.assertEqual(loaded, _checkpoint())
            self.assertFalse(path.with_suffix(".json.tmp").exists())
            self.assertEqual(json.loads(path.read_text())["version"], 2)

    def test_checkpoint_retries_transient_permission_error_from_replace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "runtime_checkpoint.json"
            real_replace = os.replace
            attempts = 0

            def transient_permission_error(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError("simulated Windows file lock")
                return real_replace(source, destination)

            with patch(
                "eom_stabilisation.run_store.os.replace",
                side_effect=transient_permission_error,
            ), patch("eom_stabilisation.experiment.checkpoint.time.sleep"):
                AtomicCheckpointStore(path).save(_checkpoint())

            self.assertEqual(attempts, 3)
            self.assertEqual(AtomicCheckpointStore(path).load(), _checkpoint())

    def test_hash_mismatch_is_rejected(self):
        with self.assertRaises(ResumeConfigurationMismatch):
            validate_resume_hashes(
                _checkpoint(),
                configuration_hash="d" * 64,
                source_hashes={"experiment": "b" * 64},
                lut_hashes={"pulse": "c" * 64},
            )

    def test_indeterminate_finite_action_is_not_silently_resumable(self):
        checkpoint = _checkpoint(indeterminate=True)
        with self.assertRaisesRegex(ValueError, "indeterminate"):
            validate_resume_checkpoint(
                checkpoint,
                configuration_hash=checkpoint.configuration_hash,
                source_hashes=checkpoint.source_hashes,
                lut_hashes=checkpoint.lut_hashes,
            )

    def test_checkpoint_unknown_fields_are_rejected(self):
        document = _checkpoint().to_dict()
        document["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "Unknown checkpoint"):
            RuntimeCheckpoint.from_dict(document)

    def test_pre_schema_checkpoint_missing_resume_fields_is_rejected_clearly(self):
        document = _checkpoint().to_dict()
        del document["temperature"]["phase_elapsed_s"]
        del document["moku"]["phase"]
        with self.assertRaisesRegex(ValueError, "phase_elapsed_s"):
            RuntimeCheckpoint.from_dict(document)

    def test_clock_and_event_writer_include_utc_london_and_elapsed(self):
        source = MutableClock()
        clock = ExperimentClock(
            monotonic=source.monotonic_now,
            utc_now=source.utc_now,
        )
        source.monotonic += 5
        source.utc = datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "events.jsonl"
            event = JsonlExperimentEventWriter(path, clock).write(
                "stage_started", stage_index=2
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(event.clock.elapsed_s, 5)
        self.assertEqual(payload["timestamp_utc"], "2026-03-29T01:30:00Z")
        self.assertEqual(payload["timestamp_local"], "2026-03-29T02:30:00+01:00")
        self.assertEqual(payload["stage_index"], 2)

    def test_run_store_copies_hashed_sources_and_effective_yaml(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source" / "experiment.yaml"
            source.parent.mkdir()
            source.write_text("name: test\n", encoding="utf-8")
            run = RunStore.create(root / "run")
            digest = sha256_file(source)
            manifest = run.snapshot_configuration(
                sources={"experiment": source},
                source_hashes={"experiment": digest},
                effective={"name": "test"},
            )
            snapshot = run.root / manifest["sources"]["experiment"]["snapshot_path"]
            self.assertEqual(snapshot.read_bytes(), source.read_bytes())
            self.assertTrue((run.root / "effective_experiment.yaml").is_file())
            self.assertTrue((run.root / "configuration_hashes.json").is_file())


if __name__ == "__main__":
    unittest.main()
