"""Regression tests for automatic and concurrent-safe experiment plotting."""

import csv
from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
import time
import unittest
from zoneinfo import ZoneInfo

os.environ.setdefault("MPLBACKEND", "Agg")

import matplotlib.pyplot as plt
import pandas as pd

import analyse_eom_csv
import plot_control
import plot_temp_log
import run_experiment


class AutomaticPlottingTests(unittest.TestCase):
    def test_control_plot_ignores_incomplete_last_row(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            folder = Path(temporary_directory)
            csv_path = folder / "RP_voltage_tracking.csv"
            csv_path.write_text(
                "wall_time,RP_lock_voltage,error_signal\n"
                "1785500000,0.1,0\n"
                "1785500001,0.2,0\n"
                "1785500002,",
                encoding="utf-8",
            )
            output_path = folder / "plots" / "control.png"

            figure, data = plot_control.create_control_plot(
                csv_path,
                output_path,
                in_progress=True,
            )
            self.addCleanup(plt.close, figure)

            self.assertEqual(len(data), 2)
            self.assertAlmostEqual(data["DC_Lock_Voltage"].iloc[-1], 2.0)
            self.assertTrue(output_path.is_file())
            self.assertIn("IN PROGRESS", figure.axes[0].get_title())

    def test_temperature_plot_reads_growing_control_log(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            folder = Path(temporary_directory)
            csv_path = folder / "tec_temperature_control.csv"
            fieldnames = [
                "wall_time",
                "object_temperature_C",
                "tec_output_current_A",
                "tec_output_voltage_V",
                "read_status",
            ]
            start = datetime(2026, 7, 31, 12, 0, tzinfo=ZoneInfo("Europe/London"))
            with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
                writer.writeheader()
                for index in range(3):
                    writer.writerow(
                        {
                            "wall_time": (start + timedelta(seconds=5 * index)).isoformat(),
                            "object_temperature_C": 25.0 + index / 10,
                            "tec_output_current_A": 0.1 + index / 100,
                            "tec_output_voltage_V": 1.0 + index / 10,
                            "read_status": "OK",
                        }
                    )
                csv_file.write("2026-07-31T12:00:20+01:00,25.4")

            figures_and_paths, data = plot_temp_log.create_temperature_plots(
                csv_path,
                folder / "plots",
                in_progress=True,
            )
            for figure, _ in figures_and_paths:
                self.addCleanup(plt.close, figure)

            self.assertEqual(len(data["temperatures"]), 3)
            self.assertEqual(len(figures_and_paths), 3)
            self.assertTrue(all(path.is_file() for _, path in figures_and_paths))
            self.assertTrue(
                all("IN PROGRESS" in figure.axes[0].get_title() for figure, _ in figures_and_paths)
            )

    def test_moku_analysis_maps_historical_maximum_to_high_level(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            folder = Path(temporary_directory)
            csv_path = folder / "raw_photovoltage_tracking.csv"
            sample_count = 20
            raw = pd.DataFrame(
                {
                    "wall_time": [1785500000 + index for index in range(sample_count)],
                    "minimum_voltage": [0.10 + index * 0.0001 for index in range(sample_count)],
                    "maximum_voltage": [1.00 + index * 0.0002 for index in range(sample_count)],
                }
            )
            raw.to_csv(csv_path, index=False)

            figures_and_paths, summary = analyse_eom_csv.run_analysis(
                csv_path,
                folder / "analysis",
                in_progress=True,
            )
            for figure, _ in figures_and_paths:
                self.addCleanup(plt.close, figure)

            cleaned = pd.read_csv(folder / "analysis" / "cleaned_photodiode_voltages.csv")
            self.assertIn("high_level_voltage", cleaned.columns)
            self.assertNotIn("maximum_voltage", cleaned.columns)
            self.assertIn("IN PROGRESS", summary)
            self.assertEqual(len(figures_and_paths), 6)
            self.assertTrue(all(path.is_file() for _, path in figures_and_paths))

    def test_runner_passes_exact_csv_and_noninteractive_flags(self):
        csv_path = Path("C:/example/run/data.csv")
        output_directory = Path("C:/example/run/in_progress_plots")
        command = run_experiment.plot_command(
            "lock",
            csv_path,
            output_directory,
            in_progress=True,
        )

        self.assertIn(str(csv_path), command)
        self.assertIn("--output-dir", command)
        self.assertIn("--no-show", command)
        self.assertIn("--in-progress", command)

    def test_runner_generates_final_plot_from_manifest_csv(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_folder = Path(temporary_directory)
            csv_path = run_folder / "RP_voltage_tracking.csv"
            csv_path.write_text(
                "wall_time,RP_lock_voltage\n"
                "1785500000,0.1\n"
                "1785500001,0.2\n",
                encoding="utf-8",
            )
            manifest_path = run_folder / "experiment_manifest.json"
            manifest = {
                "processes": {
                    "lock": {
                        "output_file": str(csv_path),
                    }
                },
                "plotting": {
                    "periodic_batches": [],
                    "final_results": [],
                    "finished_at": None,
                },
            }
            run_experiment.write_manifest(manifest_path, manifest)

            run_experiment.run_final_plots(
                ("lock",),
                manifest,
                manifest_path,
            )

            self.assertEqual(
                manifest["plotting"]["final_results"][0]["exit_code"],
                0,
                msg=(run_folder / "final_plots" / "lock_plotting.log").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertTrue(
                (
                    run_folder
                    / "final_plots"
                    / "RP_voltage_tracking_control_voltage_vs_time.png"
                ).is_file()
            )

    def test_runner_generates_live_plot_asynchronously(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_folder = Path(temporary_directory)
            csv_path = run_folder / "RP_voltage_tracking.csv"
            csv_path.write_text(
                "wall_time,RP_lock_voltage\n"
                "1785500000,0.1\n"
                "1785500001,0.2\n",
                encoding="utf-8",
            )
            manifest_path = run_folder / "experiment_manifest.json"
            manifest = {
                "processes": {"lock": {"output_file": str(csv_path)}},
                "plotting": {"periodic_batches": []},
            }
            run_experiment.write_manifest(manifest_path, manifest)

            active_batch = run_experiment.start_periodic_plot_batch(
                ("lock",),
                manifest,
                manifest_path,
            )
            deadline = time.monotonic() + 30.0
            try:
                while active_batch is not None and time.monotonic() < deadline:
                    active_batch = run_experiment.refresh_periodic_plot_batch(
                        active_batch,
                        manifest,
                        manifest_path,
                    )
                    if active_batch is not None:
                        time.sleep(0.05)
            finally:
                run_experiment.stop_periodic_plot_batch(
                    active_batch,
                    manifest,
                    manifest_path,
                )

            self.assertIsNone(active_batch)
            process_record = manifest["plotting"]["periodic_batches"][0][
                "processes"
            ]["lock"]
            self.assertEqual(process_record["exit_code"], 0)
            self.assertTrue(
                (
                    run_folder
                    / "in_progress_plots"
                    / "control_voltage_in_progress.png"
                ).is_file()
            )


if __name__ == "__main__":
    unittest.main()
