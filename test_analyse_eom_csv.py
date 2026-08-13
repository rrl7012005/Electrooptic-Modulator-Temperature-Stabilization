"""Tests for typed, profile-aware Moku CSV analysis."""

from __future__ import annotations

from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import matplotlib

matplotlib.use("Agg")
import numpy as np
import pandas as pd

import analyse_eom_csv as analysis


def voltage_frame(rows):
    return pd.DataFrame(
        rows,
        columns=["wall_time", "minimum_voltage", "high_level_voltage"],
    )


class AnalysisProfileTests(unittest.TestCase):
    def test_strict_run_profile_is_discovered_and_cli_overrides_it(self):
        with tempfile.TemporaryDirectory() as directory:
            run_directory = Path(directory) / "run"
            moku_directory = run_directory / "moku"
            moku_directory.mkdir(parents=True)
            csv_path = moku_directory / "moku_samples.csv"
            csv_path.write_text(
                "wall_time,minimum_voltage,high_level_voltage\n",
                encoding="utf-8",
            )
            profile_path = run_directory / analysis.ANALYSIS_PROFILE_FILENAME
            profile_path.write_text(
                json.dumps(
                    {
                        "dark_offset_v": 0.02,
                        "minimum_high_level_v": None,
                        "maximum_minimum_v": 0.4,
                        "minimum_sample_count": 7,
                    }
                ),
                encoding="utf-8",
            )

            discovered = analysis.discover_analysis_profile(csv_path)
            self.assertEqual(discovered, profile_path)
            base = analysis.load_analysis_profile(discovered)
            args = analysis.parse_arguments(
                [str(csv_path), "--dark-offset-v", "0.03", "--no-show"]
            )
            effective = analysis.analysis_profile_from_arguments(
                args,
                base_profile=base,
            )
            self.assertEqual(effective.dark_offset_v, 0.03)
            self.assertIsNone(effective.minimum_high_level_v)
            self.assertEqual(effective.maximum_minimum_v, 0.4)
            self.assertEqual(effective.minimum_sample_count, 7)

    def test_run_profile_rejects_partial_and_unknown_json(self):
        with tempfile.TemporaryDirectory() as directory:
            profile_path = Path(directory) / "analysis_profile.json"
            profile_path.write_text(
                json.dumps({"dark_offset_v": 0.0, "unexpected": 1}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "missing fields.*unknown fields"):
                analysis.load_analysis_profile(profile_path)

    def test_latest_csv_considers_historical_and_configured_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            historical = root / "old" / "raw_photovoltage_tracking.csv"
            configured = root / "new" / "moku_samples.csv"
            historical.parent.mkdir()
            configured.parent.mkdir()
            historical.write_text("old", encoding="utf-8")
            configured.write_text("new", encoding="utf-8")
            os.utime(historical, (1_000, 1_000))
            os.utime(configured, (2_000, 2_000))
            self.assertEqual(analysis.find_latest_csv(root), configured)

    def test_default_discovery_searches_historical_and_configured_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            historical_root = root / "Experiment Results"
            configured_root = root / "runs"
            historical = historical_root / "old" / "raw_photovoltage_tracking.csv"
            configured = configured_root / "new" / "moku" / "moku_samples.csv"
            historical.parent.mkdir(parents=True)
            configured.parent.mkdir(parents=True)
            historical.write_text("old", encoding="utf-8")
            configured.write_text("new", encoding="utf-8")
            os.utime(historical, (1_000, 1_000))
            os.utime(configured, (2_000, 2_000))
            with (
                patch.object(analysis, "MOKU_OUTPUT_ROOT", historical_root),
                patch.object(analysis, "CONFIGURED_RUN_ROOT", configured_root),
            ):
                self.assertEqual(analysis.find_latest_csv(), configured)

    def test_historical_defaults_are_preserved(self):
        profile = analysis.historical_analysis_profile()
        self.assertEqual(profile.dark_offset_v, analysis.DARK_OFFSET_V)
        self.assertEqual(
            profile.minimum_high_level_v,
            analysis.MINIMUM_ALLOWED_HIGH_LEVEL_V,
        )
        self.assertEqual(
            profile.maximum_minimum_v,
            analysis.MAXIMUM_ALLOWED_MINIMUM_V,
        )
        self.assertEqual(
            profile.minimum_sample_count,
            analysis.MINIMUM_SAMPLE_THRESHOLD,
        )

    def test_conflicting_historical_and_canonical_headers_are_rejected(self):
        frame = pd.DataFrame(
            {
                "wall_time": [1.0, 2.0],
                "minimum_voltage": [0.1, 0.1],
                "high_level_voltage": [1.0, 1.1],
                "maximum_voltage": [1.0, 1.2],
            }
        )
        with self.assertRaisesRegex(ValueError, "conflicting"):
            analysis.canonicalise_photovoltage_columns(frame)

    def test_equal_dual_headers_collapse_to_canonical_name(self):
        frame = pd.DataFrame(
            {
                "wall_time": [1.0],
                "minimum_voltage": [0.1],
                "high_level_voltage": [1.0],
                "maximum_voltage": [1.0],
            }
        )
        canonical = analysis.canonicalise_photovoltage_columns(frame)
        self.assertIn("high_level_voltage", canonical)
        self.assertNotIn("maximum_voltage", canonical)

    def test_dark_offset_corrected_formula_uses_injected_profile(self):
        profile = analysis.MeasurementAnalysisProfile(
            dark_offset_v=0.2,
            minimum_high_level_v=None,
            maximum_minimum_v=None,
            minimum_sample_count=1,
        )
        clean, _ = analysis.prepare_data(
            voltage_frame(
                [
                    (1.0, 0.3, 1.3),
                    (2.0, 0.3, 1.3),
                ]
            ),
            profile,
        )
        self.assertTrue(np.allclose(clean["minimum_corrected_voltage"], 0.1))
        self.assertTrue(np.allclose(clean["high_level_corrected_voltage"], 1.1))
        self.assertTrue(np.allclose(clean["extinction_ratio_linear"], 11.0))
        self.assertTrue(np.allclose(clean["normalised_extinction_ratio"], 1.0 / 1.2))

    def test_optional_filters_can_be_disabled_independently(self):
        raw = voltage_frame(
            [
                (1.0, 0.2, 0.5),  # rejected only by the legacy high filter
                (2.0, 0.7, 0.8),  # rejected only by the legacy minimum filter
                (3.0, 0.2, 0.8),
            ]
        )
        legacy_thresholds = analysis.MeasurementAnalysisProfile(
            minimum_sample_count=1,
        )
        clean, _ = analysis.prepare_data(raw, legacy_thresholds)
        self.assertEqual(len(clean), 1)

        no_high_filter = analysis.MeasurementAnalysisProfile(
            minimum_high_level_v=None,
            minimum_sample_count=1,
        )
        clean, _ = analysis.prepare_data(raw, no_high_filter)
        self.assertEqual(len(clean), 2)

        no_optional_filters = analysis.MeasurementAnalysisProfile(
            minimum_high_level_v=None,
            maximum_minimum_v=None,
            minimum_sample_count=3,
        )
        clean, _ = analysis.prepare_data(raw, no_optional_filters)
        self.assertEqual(len(clean), 3)
        self.assertEqual(
            clean.attrs["analysis_profile"],
            no_optional_filters.summary_dict(),
        )

    def test_profile_minimum_sample_count_is_enforced_after_filters(self):
        raw = voltage_frame([(1.0, 0.2, 0.8)])
        with self.assertRaisesRegex(ValueError, "need 2"):
            analysis.prepare_data(
                raw,
                analysis.MeasurementAnalysisProfile(minimum_sample_count=2),
            )

    def test_cli_builds_profile_and_rejects_conflicting_filter_flags(self):
        args = analysis.parse_arguments(
            [
                "--dark-offset-v",
                "0.025",
                "--no-high-level-filter",
                "--max-minimum-v",
                "0.4",
                "--min-sample-count",
                "4",
                "--no-show",
            ]
        )
        profile = analysis.analysis_profile_from_arguments(args)
        self.assertEqual(profile.dark_offset_v, 0.025)
        self.assertIsNone(profile.minimum_high_level_v)
        self.assertEqual(profile.maximum_minimum_v, 0.4)
        self.assertEqual(profile.minimum_sample_count, 4)

        conflicting = analysis.parse_arguments(
            ["--no-high-level-filter", "--min-high-level-v", "0.2"]
        )
        with self.assertRaisesRegex(ValueError, "conflicts"):
            analysis.analysis_profile_from_arguments(conflicting)

    def test_summary_reports_injected_offset_and_disabled_filters(self):
        profile = analysis.MeasurementAnalysisProfile(
            dark_offset_v=0.1,
            minimum_high_level_v=None,
            maximum_minimum_v=None,
            minimum_sample_count=1,
        )
        raw = voltage_frame([(1.0, 0.2, 1.0), (61.0, 0.2, 1.0)])
        clean, minute = analysis.prepare_data(raw, profile)
        events = pd.DataFrame(columns=["timestamp", "timestamp_utc", "event"])
        diagnostics = {
            "path": None,
            "present": False,
            "lines_read": 0,
            "invalid_lines": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            summary = analysis.create_summary(
                raw,
                clean,
                minute,
                events,
                diagnostics,
                Path(directory) / "input.csv",
                Path(directory),
                False,
                profile,
            )
        self.assertIn("Configured dark offset: 0.1 V", summary)
        self.assertIn("Optional high/offset-level filter: disabled", summary)
        self.assertIn("Optional minimum-level filter: disabled", summary)

    def test_only_an_unterminated_final_record_may_be_ignored(self):
        profile = analysis.MeasurementAnalysisProfile(minimum_sample_count=1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "growing.csv"
            path.write_text(
                "wall_time,minimum_voltage,high_level_voltage\n" "1,0.2,0.8\n" "2,0.3",
                encoding="utf-8",
            )
            with patch.object(analysis, "CSV_READ_ATTEMPTS", 1):
                data = analysis.read_growing_csv(path, profile)
        self.assertEqual(len(data), 1)
        self.assertEqual(data.attrs["rows_read"], 2)
        self.assertEqual(data.attrs["incomplete_or_non_numeric_rows"], 1)

    def test_malformed_interior_record_is_not_silently_skipped(self):
        profile = analysis.MeasurementAnalysisProfile(minimum_sample_count=1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malformed.csv"
            path.write_text(
                "wall_time,minimum_voltage,high_level_voltage\n"
                "1,0.2,0.8\n"
                "2,missing,0.9\n"
                "3,0.2,0.8\n",
                encoding="utf-8",
            )
            with patch.object(analysis, "CSV_READ_ATTEMPTS", 1):
                with self.assertRaisesRegex(ValueError, "interior data row"):
                    analysis.read_growing_csv(path, profile)


class RegimeProvenanceTests(unittest.TestCase):
    @staticmethod
    def _frames():
        wall = np.arange(7, dtype=float) + 1_786_446_000.0
        timestamps = pd.to_datetime(wall, unit="s", utc=True).astype(str)
        samples = pd.DataFrame(
            {
                "sample_id": [f"sample-{index}" for index in range(7)],
                "wall_time": wall,
                "timestamp_utc": timestamps,
                "minimum_voltage": np.full(7, 0.1),
                "high_level_voltage": np.full(7, 0.9),
            }
        )
        provenance = pd.DataFrame(
            {
                "sample_id": samples["sample_id"],
                "wall_time": wall,
                "timestamp_utc": timestamps,
                "temperature_stage_index": [0, 0, 1, 1, 1, 1, 0],
                "temperature_stage_name": [
                    "Temp A",
                    "Temp A",
                    "Temp B",
                    "Temp B",
                    "Temp B",
                    "Temp B",
                    "Temp A",
                ],
                "temperature_phase": ["holding"] * 7,
                "moku_action_index": [0, 0, 0, 1, 1, 1, 1],
                "moku_action_name": ["WF 1"] * 3 + ["WF 2"] * 4,
                "waveform_name": ["square"] * 3 + ["staircase"] * 4,
                "waveform_session_id": [1, 1, 1, 2, 3, 4, 4],
                "runtime_session_id": [1, 1, 1, 1, 2, 1, 1],
                "runtime_process_id": ["process-a"] * 5 + ["process-b"] * 2,
                "first_sample_after_waveform_change": [
                    True,
                    False,
                    False,
                    True,
                    True,
                    True,
                    False,
                ],
            }
        )
        return samples, provenance

    def test_exact_join_and_independent_boundary_extraction(self):
        samples, provenance = self._frames()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "moku_sample_provenance.csv"
            provenance.to_csv(path, index=False)
            joined, diagnostics = analysis.join_regime_provenance(samples, path)

        self.assertTrue(diagnostics["present"])
        self.assertEqual(list(joined["temperature_stage_index"]), [0, 0, 1, 1, 1, 1, 0])
        self.assertIn("analysis_regime_id", joined)
        boundaries = analysis.extract_regime_boundaries(joined)
        self.assertEqual(
            [item["index"] for item in boundaries["temperature"]],
            [0, 1, 0],
        )
        self.assertEqual(
            [item["index"] for item in boundaries["waveform"]],
            [0, 1],
        )
        # Session provenance remains independent even where its line coincides
        # with the action change at row 3. Rows 4 and 5 are reconnect/resume
        # boundaries inside the same action.
        self.assertEqual(
            [item["timestamp"] for item in boundaries["session"]][1:],
            [
                pd.to_datetime(
                    samples.loc[3, "wall_time"], unit="s", utc=True
                ).tz_convert(analysis.TIMEZONE),
                pd.to_datetime(
                    samples.loc[4, "wall_time"], unit="s", utc=True
                ).tz_convert(analysis.TIMEZONE),
                pd.to_datetime(
                    samples.loc[5, "wall_time"], unit="s", utc=True
                ).tz_convert(analysis.TIMEZONE),
            ],
        )
        clean, _ = analysis.prepare_data(
            joined,
            analysis.MeasurementAnalysisProfile(
                minimum_high_level_v=None,
                maximum_minimum_v=None,
                minimum_sample_count=1,
            ),
        )
        self.assertIn("temperature_stage_index", clean)
        self.assertIn("moku_action_index", clean)
        self.assertIn("waveform_session_id", clean)

    def test_timestamp_mismatch_is_rejected_instead_of_shifted(self):
        samples, provenance = self._frames()
        provenance.loc[3, "wall_time"] += 0.25
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "moku_sample_provenance.csv"
            provenance.to_csv(path, index=False)
            with self.assertRaisesRegex(ValueError, "wall_time"):
                analysis.join_regime_provenance(samples, path)

    def test_boundary_layers_have_one_legend_entry_per_type(self):
        samples, provenance = self._frames()
        joined = samples.copy()
        for column in analysis.REGIME_COLUMNS:
            if column in provenance:
                joined[column] = provenance[column]
        boundaries = analysis.extract_regime_boundaries(joined)
        figure, axis = analysis.plt.subplots()
        try:
            analysis.apply_regime_boundaries(
                axis,
                boundaries,
                analysis.RegimePlotOptions(
                    show_session_boundaries=True,
                    annotate_temperature_labels=False,
                ),
            )
            labels = [line.get_label() for line in axis.lines]
            self.assertEqual(labels.count("Temperature stage"), 1)
            self.assertEqual(labels.count("Moku action"), 1)
            self.assertEqual(labels.count("Moku reconnect/session"), 1)
            self.assertEqual(
                len([line for line in axis.lines if line.get_linestyle() == "-"]),
                2,
            )
            self.assertEqual(
                len([line for line in axis.lines if line.get_linestyle() == ":"]),
                1,
            )
            self.assertEqual(
                len([line for line in axis.lines if line.get_linestyle() == "--"]),
                3,
            )
        finally:
            analysis.plt.close(figure)

    def test_every_time_series_plot_receives_regime_boundaries(self):
        samples, provenance = self._frames()
        joined = samples.copy()
        for column in analysis.REGIME_COLUMNS:
            if column in provenance:
                joined[column] = provenance[column]
        clean, _ = analysis.prepare_data(
            joined,
            analysis.MeasurementAnalysisProfile(
                minimum_high_level_v=None,
                maximum_minimum_v=None,
                minimum_sample_count=1,
            ),
        )
        boundaries = analysis.extract_regime_boundaries(joined)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            analysis, "save_figure_atomic"
        ):
            figures = analysis.create_plots(
                clean,
                Path(directory),
                regime_boundaries=boundaries,
                regime_options=analysis.RegimePlotOptions(
                    show_session_boundaries=True,
                    annotate_temperature_labels=False,
                ),
            )
        try:
            self.assertEqual(len(figures), 7)
            for figure, _path in figures[:6]:
                labels = [line.get_label() for line in figure.axes[0].lines]
                self.assertIn("Temperature stage", labels)
                self.assertIn("Moku action", labels)
                self.assertIn("Moku reconnect/session", labels)
            scatter_labels = [line.get_label() for line in figures[-1][0].axes[0].lines]
            self.assertNotIn("Temperature stage", scatter_labels)
        finally:
            for figure, _path in figures:
                analysis.plt.close(figure)

    def test_extra_field_interior_record_surfaces_parser_failure(self):
        profile = analysis.MeasurementAnalysisProfile(minimum_sample_count=1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "extra_field.csv"
            path.write_text(
                "wall_time,minimum_voltage,high_level_voltage\n"
                "1,0.2,0.8\n"
                "2,0.2,0.9,unexpected\n"
                "3,0.2,0.8\n",
                encoding="utf-8",
            )
            with patch.object(analysis, "CSV_READ_ATTEMPTS", 1):
                with self.assertRaisesRegex(ValueError, "Could not read a usable"):
                    analysis.read_growing_csv(path, profile)

    def test_terminated_malformed_final_record_is_not_treated_as_growing(self):
        profile = analysis.MeasurementAnalysisProfile(minimum_sample_count=1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malformed_final.csv"
            path.write_text(
                "wall_time,minimum_voltage,high_level_voltage\n"
                "1,0.2,0.8\n"
                "2,0.3\n",
                encoding="utf-8",
            )
            with patch.object(analysis, "CSV_READ_ATTEMPTS", 1):
                with self.assertRaisesRegex(ValueError, "interior data row"):
                    analysis.read_growing_csv(path, profile)


if __name__ == "__main__":
    unittest.main()
