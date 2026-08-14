"""Regression tests for configured-v2 automatic plotting orchestration."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

import numpy as np


SRC_DIRECTORY = Path(__file__).resolve().parent / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.experiment.plotting import ConfiguredPlotManager  # noqa: E402
import plot_moku_trace  # noqa: E402


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class CompletedProcess:
    pid = 4321

    def poll(self):
        return 0


class ConfiguredPlottingTests(unittest.TestCase):
    def test_periodic_and_final_jobs_are_noninteractive_and_recorded(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = Path(temporary_directory) / "run"
            moku = run_directory / "moku"
            moku.mkdir(parents=True)
            (run_directory / "experiment_manifest.json").write_text(
                json.dumps({"format": "eom_configured_experiment"}),
                encoding="utf-8",
            )
            (moku / "moku_samples.csv").write_text(
                "wall_time,minimum_voltage,high_level_voltage\n1,0.1,0.9\n",
                encoding="utf-8",
            )
            trace_directory = moku / "raw_traces"
            trace_directory.mkdir()
            np.savez_compressed(
                trace_directory / "frame_00000001.npz",
                time_s=np.asarray([0.0, 1.0]),
                photodiode_v=np.asarray([0.1, 0.2]),
                waveform_reference_v=np.asarray([0.0, 1.0]),
                metadata_json=np.asarray("{}"),
            )
            with (moku / "raw_trace_index.csv").open(
                "w", encoding="utf-8", newline=""
            ) as output:
                writer = csv.DictWriter(output, fieldnames=["frame_id", "raw_trace_file"])
                writer.writeheader()
                writer.writerow(
                    {
                        "frame_id": "frame-id",
                        "raw_trace_file": "raw_traces/frame_00000001.npz",
                    }
                )

            clock = FakeMonotonic()
            popen_commands = []
            run_commands = []

            def popen(command, **_kwargs):
                popen_commands.append(command)
                return CompletedProcess()

            def run(command, **_kwargs):
                run_commands.append(command)
                return SimpleNamespace(returncode=0)

            manager = ConfiguredPlotManager(
                run_directory,
                ("moku",),
                interval_s=10.0,
                final_plots=True,
                monotonic=clock,
                popen=popen,
                run=run,
            )
            manager.start()
            clock.now = 10.0
            manager.poll()
            manager.poll()
            manager.finish()

            self.assertEqual(len(popen_commands), 2)
            self.assertEqual(len(run_commands), 2)
            self.assertTrue(all("--no-show" in command for command in popen_commands))
            self.assertTrue(all("--in-progress" in command for command in popen_commands))
            manifest = json.loads(
                (run_directory / "experiment_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            plotting = manifest["plotting"]
            self.assertEqual(
                set(plotting["periodic_batches"][0]["processes"]),
                {"moku", "moku-trace"},
            )
            self.assertEqual(
                {record["component"] for record in plotting["final_results"]},
                {"moku", "moku-trace"},
            )
            self.assertTrue(
                all(record["exit_code"] == 0 for record in plotting["final_results"])
            )

    def test_raw_trace_renderer_uses_newest_indexed_trace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            traces = root / "raw_traces"
            traces.mkdir()
            for index in (1, 2):
                np.savez_compressed(
                    traces / f"frame_{index:08d}.npz",
                    time_s=np.asarray([0.0, 1.0]),
                    photodiode_v=np.asarray([index, index + 1.0]),
                    waveform_reference_v=np.asarray([0.0, 1.0]),
                    metadata_json=np.asarray("{}"),
                )
            index_path = root / "raw_trace_index.csv"
            index_path.write_text(
                "frame_id,raw_trace_file\n"
                "one,raw_traces/frame_00000001.npz\n"
                "two,raw_traces/frame_00000002.npz\n",
                encoding="utf-8",
            )

            selected, row = plot_moku_trace.newest_trace(index_path)
            output = root / "plots" / "trace.png"
            figure = plot_moku_trace.create_trace_plot(
                selected, output, in_progress=True
            )
            try:
                self.assertEqual(row["frame_id"], "two")
                self.assertTrue(output.is_file())
            finally:
                import matplotlib.pyplot as plt

                plt.close(figure)


if __name__ == "__main__":
    unittest.main()
