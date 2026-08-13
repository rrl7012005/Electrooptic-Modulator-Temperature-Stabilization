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
        self.assertTrue(
            np.allclose(clean["minimum_corrected_voltage"], 0.1)
        )
        self.assertTrue(
            np.allclose(clean["high_level_corrected_voltage"], 1.1)
        )
        self.assertTrue(np.allclose(clean["extinction_ratio_linear"], 11.0))
        self.assertTrue(
            np.allclose(clean["normalised_extinction_ratio"], 1.0 / 1.2)
        )

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
                "wall_time,minimum_voltage,high_level_voltage\n"
                "1,0.2,0.8\n"
                "2,0.3",
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
