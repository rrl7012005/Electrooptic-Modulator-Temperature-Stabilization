"""Atomic output/provenance tests for configuration-driven acquisition."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np


SRC_DIRECTORY = Path(__file__).resolve().parent / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.experiment.data_writer import (  # noqa: E402
    ConfiguredMokuDataWriter,
    TecDataWriter,
)
from eom_stabilisation.tec import TecSnapshot  # noqa: E402


CLOCK = {
    "timestamp_utc": "2026-08-11T12:00:00Z",
    "timestamp_local": "2026-08-11T13:00:00+01:00",
    "elapsed_s": 2.5,
}
STATE = {
    "temperature_stage_index": 1,
    "temperature_stage_name": "hold_30",
    "temperature_phase": "holding",
    "moku_action_index": 2,
    "moku_action_name": "measure",
    "waveform_name": "square",
    "waveform_run_id": "run-id",
    "waveform_session_id": 3,
    "first_sample_after_waveform_change": True,
    "waveform_phase_continuity": "continuous",
    "measurement_profile": "low,high",
    "measurement_plan_sha256": "b" * 64,
    "waveform_timing_sha256": "a" * 64,
}


class ConfiguredDataWriterTests(unittest.TestCase):
    def test_canonical_measurement_and_full_provenance_stay_aligned(self):
        with tempfile.TemporaryDirectory() as temporary:
            writer = ConfiguredMokuDataWriter(
                temporary, {"minimum", "high_level", "probe"}
            )
            writer.record_measurement(
                clock=CLOCK,
                values_by_role={
                    "minimum": 0.1,
                    "high_level": 0.9,
                    "probe": 0.5,
                },
                point_counts_by_role={"minimum": 4, "high_level": 4, "probe": 2},
                state=STATE,
                runtime_session_id=7,
            )
            writer.flush()

            with writer.samples_path.open(encoding="utf-8", newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(rows[0]["minimum_voltage"], "0.1")
            self.assertEqual(rows[0]["high_level_voltage"], "0.9")
            self.assertNotIn("maximum_voltage", rows[0])

            with writer.provenance_path.open(encoding="utf-8", newline="") as source:
                provenance = list(csv.DictReader(source))
            self.assertEqual(len(provenance), len(rows))
            self.assertEqual(provenance[0]["temperature_stage_name"], "hold_30")
            self.assertEqual(provenance[0]["waveform_run_id"], "run-id")
            self.assertEqual(provenance[0]["runtime_session_id"], "7")
            self.assertTrue(rows[0]["sample_id"])
            self.assertEqual(provenance[0]["sample_id"], rows[0]["sample_id"])

            resumed = ConfiguredMokuDataWriter(
                temporary, {"minimum", "high_level", "probe"}, resume=True
            )
            self.assertEqual(len(resumed.samples), 1)

    def test_role_free_waveform_writes_immutable_raw_trace_and_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            writer = ConfiguredMokuDataWriter(temporary, set())
            trace = writer.record_raw_trace(
                clock=CLOCK,
                frame={
                    "time": [0.0, 1e-6],
                    "photodiode_v": [0.1, 0.2],
                    "waveform_reference_v": [0.0, 1.0],
                },
                state=STATE,
                runtime_session_id=1,
            )
            writer.flush()
            self.assertTrue(trace.is_file())
            with np.load(trace, allow_pickle=False) as content:
                np.testing.assert_allclose(content["photodiode_v"], [0.1, 0.2])
                metadata = json.loads(str(content["metadata_json"]))
            self.assertEqual(metadata["measurement_plan_sha256"], "b" * 64)
            self.assertEqual(writer.raw_index[0]["frame_id"], metadata["frame_id"])
            self.assertEqual(writer.raw_index[0]["sample_id"], metadata["sample_id"])
            self.assertTrue(writer.raw_index_path.is_file())
            with self.assertRaises(FileExistsError):
                ConfiguredMokuDataWriter(temporary, set())

    def test_resume_rejects_unindexed_raw_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            writer = ConfiguredMokuDataWriter(temporary, set())
            trace = writer.record_raw_trace(
                clock=CLOCK,
                frame={"time": [0.0], "photodiode_v": [0.1]},
                state=STATE,
                runtime_session_id=1,
            )
            self.assertTrue(trace.is_file())

            with self.assertRaisesRegex(FileExistsError, "Unindexed raw trace"):
                ConfiguredMokuDataWriter(temporary, set(), resume=True)

    def test_resume_position_must_match_persisted_last_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            writer = ConfiguredMokuDataWriter(temporary, {"minimum"})
            writer.record_measurement(
                clock=CLOCK,
                values_by_role={"minimum": 0.1},
                point_counts_by_role={"minimum": 4},
                state=STATE,
                runtime_session_id=1,
            )
            writer.flush()
            resumed = ConfiguredMokuDataWriter(
                temporary,
                {"minimum"},
                resume=True,
            )
            resumed.validate_resume_position(
                checkpoint_timestamp_utc=CLOCK["timestamp_utc"],
                checkpoint_elapsed_s=CLOCK["elapsed_s"],
            )
            with self.assertRaisesRegex(ValueError, "timestamp mismatch"):
                resumed.validate_resume_position(
                    checkpoint_timestamp_utc="2026-08-11T12:01:00Z",
                    checkpoint_elapsed_s=CLOCK["elapsed_s"],
                )

    def test_tec_failed_read_is_visible_in_atomic_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            writer = TecDataWriter(temporary)
            writer.record(
                clock=CLOCK,
                state={
                    "stage_index": 0,
                    "stage_name": "hold",
                    "phase": "holding",
                    "requested_target_c": 25.0,
                },
                snapshot=TecSnapshot(
                    object_temperature_c=25.0,
                    sink_temperature_c=22.0,
                    temperature_stable=True,
                ),
            )
            writer.record(
                clock=CLOCK,
                state={
                    "stage_index": 0,
                    "stage_name": "hold",
                    "phase": "holding",
                    "requested_target_c": 25.0,
                },
                snapshot=None,
                error=OSError("serial timeout"),
            )
            writer.flush()
            with writer.path.open(encoding="utf-8", newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 2)
            self.assertIn("serial timeout", rows[1]["error_message"])


if __name__ == "__main__":
    unittest.main()
