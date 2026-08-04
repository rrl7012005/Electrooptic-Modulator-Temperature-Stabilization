"""Regression tests for the unified experiment-results directory layout."""

from datetime import datetime
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np

SRC_DIRECTORY = Path(__file__).resolve().parent / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.output_layout import (
    COMPONENT_DIRECTORY_NAMES,
    RUN_DIRECTORY_ENVIRONMENT_VARIABLE,
    component_plot_directory,
    create_experiment_run_directory,
    resolve_component_directory,
)


class OutputLayoutTests(unittest.TestCase):
    def test_run_directory_name_is_readable_and_timezone_explicit(self):
        timestamp = datetime(
            2026,
            8,
            4,
            15,
            30,
            0,
            tzinfo=ZoneInfo("Europe/London"),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = create_experiment_run_directory(
                Path(temporary_directory) / "Experiment Results",
                timestamp=timestamp,
            )

        self.assertEqual(
            run_directory.name,
            "run_2026-08-04_15-30-00_BST",
        )

    def test_master_run_directory_is_shared_by_every_component(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            script_directory = Path(temporary_directory)
            run_directory = script_directory / "Experiment Results" / "run_test"
            with patch.dict(
                os.environ,
                {RUN_DIRECTORY_ENVIRONMENT_VARIABLE: str(run_directory)},
                clear=False,
            ):
                resolved = {
                    component: resolve_component_directory(
                        script_directory,
                        component,
                    )[1]
                    for component in COMPONENT_DIRECTORY_NAMES
                }

            self.assertEqual(resolved["lock"], run_directory / "RP_logs")
            self.assertEqual(resolved["moku"], run_directory / "Moku_logs")
            self.assertEqual(
                resolved["temp-control"],
                run_directory / "TEC_logs",
            )
            self.assertEqual(resolved["temp-log"], run_directory / "TEC_logs")

    def test_plots_stay_inside_their_respective_component_folder(self):
        run_directory = Path("C:/example/Experiment Results/run_test")

        self.assertEqual(
            component_plot_directory(
                run_directory,
                "lock",
                in_progress=True,
            ),
            run_directory / "RP_logs" / "plots" / "in_progress",
        )
        self.assertEqual(
            component_plot_directory(
                run_directory,
                "moku",
                in_progress=False,
            ),
            run_directory / "Moku_logs" / "plots" / "final",
        )
        self.assertEqual(
            component_plot_directory(
                run_directory,
                "temp-control",
                in_progress=False,
            ),
            run_directory / "TEC_logs" / "plots" / "final",
        )

    def test_resumed_moku_data_can_extend_existing_component_files(self):
        from collect_data import (
            load_existing_sample_provenance,
            load_existing_voltage_samples,
            save_sample_provenance,
            save_voltage_samples,
        )

        records = [
            {
                "wall_time": "1.0",
                "timestamp_utc": "1970-01-01T00:00:01Z",
                "acquisition_source": "live_oscilloscope",
                "run_session_id": "run_test",
                "waveform_session_id": 1,
                "first_sample_after_reconnect": False,
                "waveform_timing_may_have_restarted": False,
            }
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            moku_directory = Path(temporary_directory) / "Moku_logs"
            moku_directory.mkdir()
            primary_path = moku_directory / "raw_photovoltage_tracking.csv"
            provenance_path = (
                moku_directory / "raw_photovoltage_provenance.csv"
            )
            samples = np.asarray([[1.0, 0.1, 1.0]])
            save_voltage_samples(primary_path, samples, 1)
            save_sample_provenance(provenance_path, records)

            loaded_samples = load_existing_voltage_samples(primary_path)
            loaded_provenance = load_existing_sample_provenance(
                provenance_path
            )

        np.testing.assert_array_equal(loaded_samples, samples)
        self.assertEqual(len(loaded_provenance), 1)
        self.assertEqual(loaded_provenance[0]["waveform_session_id"], "1")


if __name__ == "__main__":
    unittest.main()
