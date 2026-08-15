"""Analyse and plot a Moku photovoltage log without modifying raw data."""

import argparse
from collections import Counter
from dataclasses import dataclass, replace
import io
import json
import math
from pathlib import Path
import time

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

try:
    from scipy.stats import linregress, ttest_ind

    HAVE_SCIPY = True
except ImportError:
    HAVE_SCIPY = False


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
MOKU_OUTPUT_ROOT = SCRIPT_DIRECTORY / "Experiment Results"
CONFIGURED_RUN_ROOT = SCRIPT_DIRECTORY / "runs"

# User-adjustable analysis settings.
CSV_PATH = None
ROLLING_SECONDS = 60
FIRST_LAST_MINUTES = 5
TIMEZONE = "Europe/London"
MINIMUM_ALLOWED_HIGH_LEVEL_V = 0.6
MAXIMUM_ALLOWED_MINIMUM_V = 0.6
MINIMUM_SAMPLE_THRESHOLD = 10
THRESHOLD_MARK_MISSING_SAMPLE_S = 15
DARK_OFFSET_V = 0.0
CSV_READ_ATTEMPTS = 3
ACQUISITION_EVENTS_FILENAME = "acquisition_events.jsonl"
PROVENANCE_FILENAME = "moku_sample_provenance.csv"
ANALYSIS_PROFILE_FILENAME = "analysis_profile.json"
ANALYSIS_INPUT_FILENAMES = (
    "raw_photovoltage_tracking.csv",
    "moku_samples.csv",
)

OUTPUT_FILENAMES = {
    "cleaned_csv": "moku_eom_cleaned_photovoltage.csv",
    "summary": "moku_eom_analysis_summary.txt",
    "minimum_plot": "moku_eom_minimum_photovoltage_vs_time.png",
    "high_level_plot": "moku_eom_high_level_photovoltage_vs_time.png",
    "extinction_ratio_plot": "moku_eom_apparent_extinction_ratio_vs_time.png",
    "normalised_extinction_ratio_plot": (
        "moku_eom_normalised_extinction_ratio_vs_time.png"
    ),
    "level_range_plot": "moku_eom_photovoltage_range_vs_time.png",
    "normalised_plot": "moku_eom_normalised_levels_vs_time.png",
    "scatter_plot": "moku_eom_high_level_vs_minimum_scatter.png",
}


@dataclass(frozen=True)
class MeasurementAnalysisProfile:
    """Explicit voltage corrections, optional filters, and sample policy.

    Thresholds are requested photodiode volts.  Setting either optional
    threshold to ``None`` disables only that threshold; the physically required
    checks ``high_level > minimum > dark_offset`` remain active so logarithmic
    and normalised extinction metrics stay finite and meaningful.
    """

    dark_offset_v: float = DARK_OFFSET_V
    minimum_high_level_v: float | None = MINIMUM_ALLOWED_HIGH_LEVEL_V
    maximum_minimum_v: float | None = MAXIMUM_ALLOWED_MINIMUM_V
    minimum_sample_count: int = MINIMUM_SAMPLE_THRESHOLD

    def __post_init__(self) -> None:
        if isinstance(self.dark_offset_v, bool):
            raise ValueError("dark_offset_v must be finite")
        try:
            dark_is_finite = math.isfinite(self.dark_offset_v)
        except TypeError as error:
            raise ValueError("dark_offset_v must be finite") from error
        if not dark_is_finite:
            raise ValueError("dark_offset_v must be finite")
        for name, value in (
            ("minimum_high_level_v", self.minimum_high_level_v),
            ("maximum_minimum_v", self.maximum_minimum_v),
        ):
            if value is None:
                continue
            if isinstance(value, bool):
                raise ValueError(f"{name} must be finite or None")
            try:
                is_finite = math.isfinite(value)
            except TypeError as error:
                raise ValueError(f"{name} must be finite or None") from error
            if not is_finite:
                raise ValueError(f"{name} must be finite or None")
        if (
            isinstance(self.minimum_sample_count, bool)
            or not isinstance(self.minimum_sample_count, int)
            or self.minimum_sample_count < 1
        ):
            raise ValueError("minimum_sample_count must be a positive integer")

    def summary_dict(self) -> dict[str, object]:
        """Return JSON-compatible settings for summaries and tests."""

        return {
            "dark_offset_v": self.dark_offset_v,
            "minimum_high_level_v": self.minimum_high_level_v,
            "maximum_minimum_v": self.maximum_minimum_v,
            "minimum_sample_count": self.minimum_sample_count,
        }


@dataclass(frozen=True)
class RegimePlotOptions:
    """Independent provenance layers shown on scientific time-series plots."""

    show_temperature_boundaries: bool = True
    show_waveform_boundaries: bool = True
    show_session_boundaries: bool = False
    annotate_temperature_labels: bool = True
    annotate_waveform_labels: bool = False
    maximum_labels_per_type: int = 12

    def __post_init__(self) -> None:
        if (
            isinstance(self.maximum_labels_per_type, bool)
            or not isinstance(self.maximum_labels_per_type, int)
            or self.maximum_labels_per_type < 1
        ):
            raise ValueError("maximum_labels_per_type must be a positive integer")


def historical_analysis_profile() -> MeasurementAnalysisProfile:
    """Build the legacy analysis defaults from the user-editable constants."""

    return MeasurementAnalysisProfile(
        dark_offset_v=DARK_OFFSET_V,
        minimum_high_level_v=MINIMUM_ALLOWED_HIGH_LEVEL_V,
        maximum_minimum_v=MAXIMUM_ALLOWED_MINIMUM_V,
        minimum_sample_count=MINIMUM_SAMPLE_THRESHOLD,
    )


def _profile_or_historical(
    profile: MeasurementAnalysisProfile | None,
) -> MeasurementAnalysisProfile:
    if profile is None:
        return historical_analysis_profile()
    if not isinstance(profile, MeasurementAnalysisProfile):
        raise TypeError("profile must be a MeasurementAnalysisProfile")
    return profile


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Analyse Moku minimum and measured high-level voltages."
    )
    parser.add_argument(
        "csv_path",
        nargs="?",
        type=Path,
        help="CSV to analyse; default: newest Moku pulse-run CSV",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="plot folder; default: plots/final beside the Moku CSV",
    )
    parser.add_argument(
        "--analysis-dir",
        type=Path,
        help="folder for cleaned CSV and summary; default: beside the Moku CSV",
    )
    parser.add_argument(
        "--events-path",
        type=Path,
        help=(
            "Moku acquisition JSONL event log; default: acquisition_events.jsonl "
            "beside the input CSV when present"
        ),
    )
    parser.add_argument(
        "--provenance-path",
        type=Path,
        help=(
            "sample provenance CSV; default: moku_sample_provenance.csv beside "
            "the measurement CSV when present"
        ),
    )
    parser.add_argument(
        "--show-temperature-boundaries",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show or hide temperature-stage boundary lines",
    )
    parser.add_argument(
        "--show-waveform-boundaries",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show or hide Moku action/waveform boundary lines",
    )
    parser.add_argument(
        "--show-session-boundaries",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="show or hide reconnect/resume session boundaries",
    )
    parser.add_argument(
        "--annotate-temperature-labels",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="annotate a bounded subset of temperature regimes",
    )
    parser.add_argument(
        "--annotate-waveform-labels",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="annotate a bounded subset of waveform/action regimes",
    )
    parser.add_argument(
        "--analysis-profile",
        type=Path,
        help=(
            "strict analysis-profile JSON; default: auto-discover "
            "analysis_profile.json beside the CSV or its run directory, then "
            "fall back to historical defaults"
        ),
    )
    parser.add_argument(
        "--no-show",
        action="store_true",
        help="save plots without opening interactive windows",
    )
    parser.add_argument(
        "--in-progress",
        action="store_true",
        help="clearly mark plots and summary as unfinished snapshots",
    )
    parser.add_argument(
        "--dark-offset-v",
        type=float,
        help="independently calibrated detector dark offset in volts; default: 0",
    )
    parser.add_argument(
        "--min-high-level-v",
        "--minimum-high-level-v",
        dest="minimum_high_level_v",
        type=float,
        help="reject high/offset readings at or below this voltage",
    )
    parser.add_argument(
        "--no-high-level-filter",
        "--disable-high-level-filter",
        dest="disable_high_level_filter",
        action="store_true",
        help="disable the optional minimum-high/offset-level threshold",
    )
    parser.add_argument(
        "--max-minimum-v",
        "--maximum-minimum-v",
        dest="maximum_minimum_v",
        type=float,
        help="reject minimum readings at or above this voltage",
    )
    parser.add_argument(
        "--no-minimum-filter",
        "--disable-minimum-filter",
        dest="disable_minimum_filter",
        action="store_true",
        help="disable the optional maximum-minimum threshold",
    )
    parser.add_argument(
        "--min-sample-count",
        "--minimum-sample-count",
        dest="minimum_sample_count",
        type=int,
        help="minimum complete and post-filter sample count; default: 10",
    )
    return parser.parse_args(argv)


def analysis_profile_from_arguments(
    args,
    *,
    base_profile: MeasurementAnalysisProfile | None = None,
) -> MeasurementAnalysisProfile:
    """Create one validated profile from parsed CLI arguments."""

    profile = _profile_or_historical(base_profile)
    if args.disable_high_level_filter and args.minimum_high_level_v is not None:
        raise ValueError("--no-high-level-filter conflicts with --min-high-level-v")
    if args.disable_minimum_filter and args.maximum_minimum_v is not None:
        raise ValueError("--no-minimum-filter conflicts with --max-minimum-v")
    return replace(
        profile,
        dark_offset_v=(
            profile.dark_offset_v if args.dark_offset_v is None else args.dark_offset_v
        ),
        minimum_high_level_v=(
            None
            if args.disable_high_level_filter
            else profile.minimum_high_level_v
            if args.minimum_high_level_v is None
            else args.minimum_high_level_v
        ),
        maximum_minimum_v=(
            None
            if args.disable_minimum_filter
            else profile.maximum_minimum_v
            if args.maximum_minimum_v is None
            else args.maximum_minimum_v
        ),
        minimum_sample_count=(
            profile.minimum_sample_count
            if args.minimum_sample_count is None
            else args.minimum_sample_count
        ),
    )


def load_analysis_profile(path: Path) -> MeasurementAnalysisProfile:
    """Load one complete, strict JSON analysis profile."""

    profile_path = Path(path).expanduser().resolve()
    try:
        document = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Could not load analysis profile {profile_path}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise ValueError(f"Analysis profile {profile_path} must be a JSON object")
    expected = {
        "dark_offset_v",
        "minimum_high_level_v",
        "maximum_minimum_v",
        "minimum_sample_count",
    }
    actual = set(document)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown fields: {', '.join(unknown)}")
        raise ValueError(
            f"Invalid analysis profile {profile_path} ({'; '.join(details)})"
        )
    try:
        return MeasurementAnalysisProfile(**document)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid analysis profile {profile_path}: {error}") from error


def discover_analysis_profile(csv_path: Path) -> Path | None:
    """Find the run profile for historical or configured CSV layouts."""

    resolved_csv = Path(csv_path).expanduser().resolve()
    candidates = (
        resolved_csv.parent / ANALYSIS_PROFILE_FILENAME,
        resolved_csv.parent.parent / ANALYSIS_PROFILE_FILENAME,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def find_latest_csv(folder: Path | None = None) -> Path:
    roots = (
        (MOKU_OUTPUT_ROOT, CONFIGURED_RUN_ROOT) if folder is None else (Path(folder),)
    )
    files = [
        path
        for root in roots
        if root.is_dir()
        for filename in ANALYSIS_INPUT_FILENAMES
        for path in root.rglob(filename)
    ]
    if not files:
        raise FileNotFoundError(
            "Could not find a historical or configured Moku sample CSV under:\n"
            + "\n".join(str(root) for root in roots)
        )
    return max(files, key=lambda path: path.stat().st_mtime)


def canonicalise_photovoltage_columns(data: pd.DataFrame) -> pd.DataFrame:
    """Map historical input headers to the current internal schema."""

    required_base = {"wall_time", "minimum_voltage"}
    missing_base = required_base - set(data.columns)
    if missing_base:
        raise ValueError(f"CSV is missing required columns: {sorted(missing_base)}")

    historical_column = "maximum_voltage"
    canonical_column = "high_level_voltage"
    if not ({historical_column, canonical_column} & set(data.columns)):
        raise ValueError(
            "CSV must contain high_level_voltage or the historical "
            "maximum_voltage column"
        )

    result = data.copy()
    numeric_columns = ["wall_time", "minimum_voltage"]
    numeric_columns.extend(
        column
        for column in (canonical_column, historical_column)
        if column in result.columns
    )
    for column in numeric_columns:
        result[column] = pd.to_numeric(result[column], errors="coerce")

    if canonical_column in result.columns and historical_column in result.columns:
        comparable = result[[canonical_column, historical_column]].dropna()
        if not comparable.empty and not np.allclose(
            comparable[canonical_column],
            comparable[historical_column],
            rtol=1e-9,
            atol=1e-12,
        ):
            raise ValueError(
                "CSV contains conflicting high_level_voltage and "
                "maximum_voltage columns"
            )
        result[canonical_column] = result[canonical_column].combine_first(
            result[historical_column]
        )
        result = result.drop(columns=[historical_column])
    elif historical_column in result.columns:
        result = result.rename(columns={historical_column: canonical_column})

    return result


def read_growing_csv(
    csv_path: Path,
    profile: MeasurementAnalysisProfile | None = None,
) -> pd.DataFrame:
    """Read a usable snapshot even if Moku is currently rewriting the file."""
    profile = _profile_or_historical(profile)
    last_error = None
    for attempt in range(CSV_READ_ATTEMPTS):
        try:
            snapshot = csv_path.read_text(encoding="utf-8-sig")
            final_fragment_is_unterminated = bool(snapshot) and not snapshot.endswith(
                ("\n", "\r")
            )
            parser_skipped_final_row = 0
            try:
                data = pd.read_csv(io.StringIO(snapshot), on_bad_lines="error")
            except pd.errors.ParserError:
                if not final_fragment_is_unterminated:
                    raise
                last_newline = max(snapshot.rfind("\n"), snapshot.rfind("\r"))
                if last_newline < 0:
                    raise
                data = pd.read_csv(
                    io.StringIO(snapshot[: last_newline + 1]),
                    on_bad_lines="error",
                )
                parser_skipped_final_row = 1

            rows_read = len(data) + parser_skipped_final_row
            data = canonicalise_photovoltage_columns(data)
            required = {"wall_time", "minimum_voltage", "high_level_voltage"}
            numeric = data[sorted(required)].to_numpy(dtype=float)
            complete = pd.Series(
                np.all(np.isfinite(numeric), axis=1),
                index=data.index,
            )
            incomplete_indices = list(data.index[~complete])
            incomplete_final_row = 0
            if incomplete_indices:
                final_index = data.index[-1] if len(data) else None
                if (
                    len(incomplete_indices) == 1
                    and incomplete_indices[0] == final_index
                    and final_fragment_is_unterminated
                ):
                    incomplete_final_row = 1
                    data = data[complete].copy()
                else:
                    raise ValueError(
                        "CSV contains a malformed, missing, or non-finite "
                        "interior data row; only an unterminated final row may "
                        "be ignored while a file is growing"
                    )
            data.attrs["rows_read"] = rows_read
            data.attrs["incomplete_or_non_numeric_rows"] = (
                parser_skipped_final_row + incomplete_final_row
            )
            if len(data) < profile.minimum_sample_count:
                raise ValueError(
                    "Too few complete samples are currently available "
                    f"({len(data)} found; {profile.minimum_sample_count} required)."
                )
            return data
        except (
            OSError,
            pd.errors.EmptyDataError,
            pd.errors.ParserError,
            ValueError,
        ) as error:
            last_error = error
            if attempt + 1 < CSV_READ_ATTEMPTS:
                time.sleep(0.2)

    raise ValueError(f"Could not read a usable Moku CSV snapshot: {last_error}")


REGIME_COLUMNS = (
    "temperature_stage_index",
    "temperature_stage_name",
    "temperature_phase",
    "moku_action_index",
    "moku_action_name",
    "waveform_name",
    "waveform_session_id",
    "runtime_session_id",
    "runtime_process_id",
    "first_sample_after_waveform_change",
    "first_sample_after_action_change",
    "first_sample_after_session_change",
)


def join_regime_provenance(
    samples: pd.DataFrame,
    provenance_path: Path | None,
) -> tuple[pd.DataFrame, dict[str, object]]:
    """Join configured-run provenance with exact row and timestamp validation."""

    diagnostics: dict[str, object] = {
        "path": None if provenance_path is None else str(provenance_path),
        "present": False,
        "rows": 0,
    }
    if provenance_path is None or not provenance_path.is_file():
        return samples.copy(), diagnostics
    provenance = pd.read_csv(provenance_path, dtype={"sample_id": "string"})
    diagnostics["present"] = True
    diagnostics["rows"] = len(provenance)
    if len(provenance) != len(samples):
        raise ValueError(
            "Moku sample/provenance row counts differ; refusing a shifted regime join"
        )
    required_clock = {"wall_time", "timestamp_utc"}
    missing_clock = required_clock - set(provenance.columns)
    if missing_clock:
        raise ValueError(
            f"provenance CSV is missing clock columns: {sorted(missing_clock)}"
        )
    if "sample_id" in samples.columns and "sample_id" in provenance.columns:
        sample_ids = samples["sample_id"].astype("string")
        provenance_ids = provenance["sample_id"].astype("string")
        if sample_ids.isna().any() or provenance_ids.isna().any():
            raise ValueError("sample/provenance sample_id contains missing values")
        if sample_ids.duplicated().any() or provenance_ids.duplicated().any():
            raise ValueError("sample/provenance sample_id must be unique")
        if not sample_ids.reset_index(drop=True).equals(
            provenance_ids.reset_index(drop=True)
        ):
            raise ValueError("sample/provenance sample_id order does not match")
    sample_wall = pd.to_numeric(samples["wall_time"], errors="raise").to_numpy()
    provenance_wall = pd.to_numeric(provenance["wall_time"], errors="raise").to_numpy()
    if not np.allclose(sample_wall, provenance_wall, rtol=0.0, atol=1e-9):
        raise ValueError("sample/provenance wall_time values do not align exactly")
    if "timestamp_utc" in samples.columns and not samples["timestamp_utc"].astype(
        str
    ).reset_index(drop=True).equals(
        provenance["timestamp_utc"].astype(str).reset_index(drop=True)
    ):
        raise ValueError("sample/provenance timestamp_utc values do not align exactly")

    joined = samples.reset_index(drop=True).copy()
    for column in REGIME_COLUMNS:
        if column in provenance.columns:
            if column in joined.columns:
                left = joined[column].astype("string")
                right = provenance[column].astype("string")
                if not left.equals(right):
                    raise ValueError(f"sample/provenance column {column!r} conflicts")
            else:
                joined[column] = provenance[column].to_numpy()
    regime_key_columns = [
        column
        for column in (
            "temperature_stage_index",
            "moku_action_index",
            "waveform_session_id",
            "runtime_process_id",
        )
        if column in joined.columns
    ]
    if regime_key_columns:
        normalized = joined[regime_key_columns].astype("string").fillna("<none>")
        changed = normalized.ne(normalized.shift()).any(axis=1)
        if len(changed):
            changed.iloc[0] = True
        joined["analysis_regime_id"] = changed.cumsum().astype(int) - 1
    return joined, diagnostics


def extract_regime_boundaries(data: pd.DataFrame) -> dict[str, list[dict[str, object]]]:
    """Extract independent stage, action, and reconnect boundaries from provenance."""

    result: dict[str, list[dict[str, object]]] = {
        "temperature": [],
        "waveform": [],
        "session": [],
    }
    if data.empty:
        return result
    timestamps = (
        pd.to_datetime(data["wall_time"], unit="s", utc=True)
        .dt.tz_convert(TIMEZONE)
        .reset_index(drop=True)
    )
    has_temperature = "temperature_stage_index" in data.columns
    has_waveform = "moku_action_index" in data.columns
    has_session = "waveform_session_id" in data.columns

    def value(row: pd.Series, column: str) -> object:
        item = row.get(column)
        return None if pd.isna(item) else item

    previous_temperature: tuple[object, object] | None = None
    previous_action: tuple[object, object, object] | None = None
    previous_session: tuple[object, object] | None = None
    for position, (_, row) in enumerate(data.reset_index(drop=True).iterrows()):
        temperature = (
            value(row, "temperature_stage_index"),
            value(row, "temperature_stage_name"),
        )
        action = (
            value(row, "moku_action_index"),
            value(row, "moku_action_name"),
            value(row, "waveform_name"),
        )
        session = (
            value(row, "runtime_process_id"),
            value(row, "waveform_session_id"),
        )
        timestamp = timestamps.iloc[position]
        if has_temperature and (position == 0 or temperature != previous_temperature):
            result["temperature"].append(
                {
                    "timestamp": timestamp,
                    "index": temperature[0],
                    "name": temperature[1],
                    "initial": position == 0,
                }
            )
        action_changed = position == 0 or action != previous_action
        if has_waveform and action_changed:
            result["waveform"].append(
                {
                    "timestamp": timestamp,
                    "index": action[0],
                    "name": action[1],
                    "waveform_name": action[2],
                    "initial": position == 0,
                }
            )
        if has_session and (position == 0 or session != previous_session):
            # Session/reconnect provenance is a separate layer. Preserve it
            # even when it happens at the same sample as an action boundary;
            # showing that layer is optional, but the fact must not be lost or
            # converted into a waveform transition.
            result["session"].append(
                {
                    "timestamp": timestamp,
                    "runtime_process_id": session[0],
                    "waveform_session_id": session[1],
                    "initial": position == 0,
                }
            )
        previous_temperature = temperature
        previous_action = action
        previous_session = session
    return result


def read_acquisition_events(
    events_path: Path | None,
) -> tuple[pd.DataFrame, dict[str, object]]:
    """Read a JSONL acquisition log without failing analysis of older runs."""

    diagnostics: dict[str, object] = {
        "path": str(events_path) if events_path is not None else None,
        "present": False,
        "lines_read": 0,
        "invalid_lines": 0,
    }
    columns = ["timestamp", "timestamp_utc", "event"]
    if events_path is None or not events_path.is_file():
        return pd.DataFrame(columns=columns), diagnostics

    diagnostics["present"] = True
    records = []
    invalid_lines = 0
    with events_path.open("r", encoding="utf-8") as event_file:
        for line_number, line in enumerate(event_file, start=1):
            if not line.strip():
                continue
            diagnostics["lines_read"] = int(diagnostics["lines_read"]) + 1
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("event is not a JSON object")
                event = record.get("event")
                timestamp_utc = record.get("timestamp_utc")
                if not isinstance(timestamp_utc, str) or not timestamp_utc:
                    raise ValueError("event timestamp_utc is missing")
                timestamp = pd.to_datetime(timestamp_utc, utc=True, errors="raise")
                if not isinstance(event, str) or not event:
                    raise ValueError("event name is missing")
                timestamp_local = timestamp.tz_convert(TIMEZONE)
            except (json.JSONDecodeError, TypeError, ValueError):
                invalid_lines += 1
                continue
            records.append(
                {
                    **record,
                    "timestamp": timestamp_local,
                    "source_line": line_number,
                }
            )

    diagnostics["invalid_lines"] = invalid_lines
    if not records:
        return pd.DataFrame(columns=columns), diagnostics
    return pd.DataFrame.from_records(records).sort_values("timestamp"), diagnostics


def trend_stats(x, y):
    """Fit y against elapsed minutes and return slope and fit statistics."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    x = x[mask]
    y = y[mask]
    if len(x) < 3:
        return {
            "slope": np.nan,
            "intercept": np.nan,
            "r": np.nan,
            "p": np.nan,
            "stderr": np.nan,
        }

    if HAVE_SCIPY:
        result = linregress(x, y)
        return {
            "slope": result.slope,
            "intercept": result.intercept,
            "r": result.rvalue,
            "p": result.pvalue,
            "stderr": result.stderr,
        }

    slope, intercept = np.polyfit(x, y, 1)
    return {
        "slope": slope,
        "intercept": intercept,
        "r": np.corrcoef(x, y)[0, 1],
        "p": np.nan,
        "stderr": np.nan,
    }


def format_p(value):
    if not np.isfinite(value):
        return "N/A"
    if value < 1e-4:
        return f"{value:.2e}"
    return f"{value:.4f}"


def prepare_data(
    raw_data: pd.DataFrame,
    profile: MeasurementAnalysisProfile | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Map historical columns to canonical names and calculate metrics."""
    profile = _profile_or_historical(profile)
    data = canonicalise_photovoltage_columns(raw_data)
    valid = (data["high_level_voltage"] > data["minimum_voltage"]) & (
        data["minimum_voltage"] > profile.dark_offset_v
    )
    if profile.minimum_high_level_v is not None:
        valid &= data["high_level_voltage"] > profile.minimum_high_level_v
    if profile.maximum_minimum_v is not None:
        valid &= data["minimum_voltage"] < profile.maximum_minimum_v
    voltage_rejected_rows = int((~valid).sum())
    clean = data[valid].copy()
    if len(clean) < profile.minimum_sample_count:
        raise ValueError(
            "Too few valid samples remain after voltage checks. "
            f"Found {len(clean)}; need {profile.minimum_sample_count}."
        )

    clean = clean.sort_values("wall_time")
    duplicate_timestamp_rows = int(clean["wall_time"].duplicated().sum())
    clean = clean.drop_duplicates("wall_time")
    clean["elapsed_s"] = clean["wall_time"] - clean["wall_time"].iloc[0]
    clean["elapsed_min"] = clean["elapsed_s"] / 60.0
    clean["timestamp"] = pd.to_datetime(
        clean["wall_time"], unit="s", utc=True
    ).dt.tz_convert(TIMEZONE)

    clean["minimum_corrected_voltage"] = (
        clean["minimum_voltage"] - profile.dark_offset_v
    )
    clean["high_level_corrected_voltage"] = (
        clean["high_level_voltage"] - profile.dark_offset_v
    )
    clean["high_minus_minimum_voltage"] = (
        clean["high_level_corrected_voltage"] - clean["minimum_corrected_voltage"]
    )
    clean["extinction_ratio_linear"] = (
        clean["high_level_corrected_voltage"] / clean["minimum_corrected_voltage"]
    )
    clean["extinction_ratio_dB"] = 10.0 * np.log10(clean["extinction_ratio_linear"])
    clean["normalised_extinction_ratio"] = (
        clean["high_level_corrected_voltage"] - clean["minimum_corrected_voltage"]
    ) / (clean["high_level_corrected_voltage"] + clean["minimum_corrected_voltage"])

    clean = clean.set_index("timestamp")
    rolling_columns = [
        "minimum_voltage",
        "high_level_voltage",
        "extinction_ratio_dB",
        "normalised_extinction_ratio",
        "high_minus_minimum_voltage",
    ]
    if "analysis_regime_id" in clean.columns:
        rolling = (
            clean.groupby("analysis_regime_id", sort=False, dropna=False)[
                rolling_columns
            ]
            .rolling(
                f"{ROLLING_SECONDS}s",
                min_periods=profile.minimum_sample_count,
            )
            .mean()
            .reset_index(level=0, drop=True)
        )
    else:
        rolling = (
            clean[rolling_columns]
            .rolling(
                f"{ROLLING_SECONDS}s",
                min_periods=profile.minimum_sample_count,
            )
            .mean()
        )
    clean["minimum_rolling_voltage"] = rolling["minimum_voltage"]
    clean["high_level_rolling_voltage"] = rolling["high_level_voltage"]
    clean["rolling_extinction_ratio_dB"] = rolling["extinction_ratio_dB"]
    clean["rolling_normalised_extinction_ratio"] = rolling[
        "normalised_extinction_ratio"
    ]
    clean["rolling_high_minus_minimum_voltage"] = rolling["high_minus_minimum_voltage"]
    clean = clean.reset_index()
    clean.attrs["voltage_rejected_rows"] = voltage_rejected_rows
    clean.attrs["duplicate_timestamp_rows"] = duplicate_timestamp_rows
    clean.attrs["analysis_profile"] = profile.summary_dict()

    minute = (
        clean.set_index("timestamp")
        .resample("60s")
        .mean(numeric_only=True)
        .dropna(subset=["minimum_voltage", "high_level_voltage"])
        .reset_index()
    )
    if not minute.empty:
        minute["elapsed_min"] = (
            minute["wall_time"] - minute["wall_time"].iloc[0]
        ) / 60.0
    return clean, minute


def format_time_axis(ax, start_label):
    locator = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=TIMEZONE))
    ax.set_xlabel(f"UK local time | run start: {start_label}")


def save_figure_atomic(fig, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    fig.tight_layout()
    fig.savefig(
        temporary_path,
        format="png",
        dpi=300,
        bbox_inches="tight",
    )
    temporary_path.replace(output_path)


def mark_in_progress(fig, clean, in_progress):
    if not in_progress:
        return
    through = clean["timestamp"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S %Z")
    fig.suptitle(
        f"IN PROGRESS — unfinished snapshot, data through {through}",
        color="#A33A2B",
        fontsize=11,
    )


def _boundary_label(boundary_type: str, boundary: dict[str, object]) -> str:
    index = boundary.get("index")
    name = boundary.get("name")
    if boundary_type == "temperature":
        return f"T{index}: {name}"
    waveform_name = boundary.get("waveform_name")
    suffix = "" if waveform_name in {None, name} else f" ({waveform_name})"
    return f"A{index}: {name}{suffix}"


def _annotate_boundaries(
    ax,
    boundaries: list[dict[str, object]],
    *,
    boundary_type: str,
    maximum_labels: int,
    y_positions: tuple[float, float],
) -> None:
    if not boundaries:
        return
    step = max(1, math.ceil(len(boundaries) / maximum_labels))
    selected = boundaries[::step]
    if boundaries[-1] not in selected:
        selected.append(boundaries[-1])
    for index, boundary in enumerate(selected):
        ax.annotate(
            _boundary_label(boundary_type, boundary),
            xy=(boundary["timestamp"], y_positions[index % 2]),
            xycoords=("data", "axes fraction"),
            xytext=(2, -2),
            textcoords="offset points",
            rotation=90,
            va="top",
            ha="left",
            fontsize=6.5,
            color="#385170" if boundary_type == "temperature" else "#9A5A00",
            alpha=0.78,
            clip_on=True,
        )


def apply_regime_boundaries(
    ax,
    boundaries: dict[str, list[dict[str, object]]] | None,
    options: RegimePlotOptions,
) -> None:
    """Draw independent, subtle provenance boundaries without legend spam."""

    if not boundaries:
        return
    layers = []
    if options.show_temperature_boundaries:
        layers.append(("temperature", "Temperature stage", "#4C78A8", "-", 0.34))
    if options.show_waveform_boundaries:
        layers.append(("waveform", "Moku action", "#F58518", ":", 0.40))
    if options.show_session_boundaries:
        layers.append(("session", "Moku reconnect/session", "#7A5195", "--", 0.34))
    for boundary_type, legend_label, color, linestyle, alpha in layers:
        visible = [
            boundary
            for boundary in boundaries.get(boundary_type, [])
            if not boundary.get("initial")
        ]
        for index, boundary in enumerate(visible):
            ax.axvline(
                boundary["timestamp"],
                color=color,
                linestyle=linestyle,
                linewidth=0.8,
                alpha=alpha,
                label=legend_label if index == 0 else "_nolegend_",
                zorder=0.5,
            )
    if options.annotate_temperature_labels and options.show_temperature_boundaries:
        _annotate_boundaries(
            ax,
            boundaries.get("temperature", []),
            boundary_type="temperature",
            maximum_labels=options.maximum_labels_per_type,
            y_positions=(0.99, 0.91),
        )
    if options.annotate_waveform_labels and options.show_waveform_boundaries:
        _annotate_boundaries(
            ax,
            boundaries.get("waveform", []),
            boundary_type="waveform",
            maximum_labels=options.maximum_labels_per_type,
            y_positions=(0.82, 0.74),
        )


def create_plots(
    clean,
    output_dir: Path,
    *,
    in_progress=False,
    regime_boundaries: dict[str, list[dict[str, object]]] | None = None,
    regime_options: RegimePlotOptions | None = None,
):
    """Create the standard Moku plots and return their figures and paths."""
    start_label = clean["timestamp"].iloc[0].strftime("%Y-%m-%d %H:%M:%S %Z")
    figures_and_paths = []
    regime_options = regime_options or RegimePlotOptions()

    def finish(fig, ax, name):
        apply_regime_boundaries(ax, regime_boundaries, regime_options)
        format_time_axis(ax, start_label)
        ax.grid(True, axis="y", alpha=0.3)
        ax.legend(frameon=False)
        mark_in_progress(fig, clean, in_progress)
        output_path = output_dir / name
        save_figure_atomic(fig, output_path)
        figures_and_paths.append((fig, output_path))

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(
        clean["timestamp"],
        clean["minimum_voltage"],
        linewidth=0.8,
        alpha=0.65,
        label="Data",
    )
    ax.plot(
        clean["timestamp"],
        clean["minimum_rolling_voltage"],
        linewidth=2.2,
        label=f"{ROLLING_SECONDS} s mean",
    )
    ax.set_ylabel("Minimum photodiode voltage (V)")
    ax.set_title("Minimum optical level vs time")
    finish(fig, ax, OUTPUT_FILENAMES["minimum_plot"])

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(
        clean["timestamp"],
        clean["high_level_voltage"],
        linewidth=0.8,
        alpha=0.65,
        label="Data",
    )
    ax.plot(
        clean["timestamp"],
        clean["high_level_rolling_voltage"],
        linewidth=2.2,
        label=f"{ROLLING_SECONDS} s mean",
    )
    ax.set_ylabel("High/offset photodiode voltage (V)")
    ax.set_title("Measured high/offset optical level vs time")
    finish(fig, ax, OUTPUT_FILENAMES["high_level_plot"])

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(
        clean["timestamp"],
        clean["extinction_ratio_dB"],
        linewidth=0.8,
        alpha=0.65,
        label="Data",
    )
    ax.plot(
        clean["timestamp"],
        clean["rolling_extinction_ratio_dB"],
        linewidth=2.2,
        label=f"{ROLLING_SECONDS} s mean",
    )
    ax.set_ylabel("Apparent extinction ratio (dB)")
    ax.set_title("Apparent extinction ratio vs time")
    finish(fig, ax, OUTPUT_FILENAMES["extinction_ratio_plot"])

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(
        clean["timestamp"],
        clean["normalised_extinction_ratio"],
        linewidth=0.8,
        alpha=0.65,
        label="Data",
    )
    ax.plot(
        clean["timestamp"],
        clean["rolling_normalised_extinction_ratio"],
        linewidth=2.2,
        label=f"{ROLLING_SECONDS} s mean",
    )
    ax.set_ylabel("Normalised extinction ratio, (H' - L') / (H' + L')")
    ax.set_title("Dark-offset-corrected normalised extinction ratio vs time")
    finish(
        fig,
        ax,
        OUTPUT_FILENAMES["normalised_extinction_ratio_plot"],
    )

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(
        clean["timestamp"],
        clean["high_minus_minimum_voltage"],
        linewidth=0.8,
        alpha=0.65,
        label="Data",
    )
    ax.plot(
        clean["timestamp"],
        clean["rolling_high_minus_minimum_voltage"],
        linewidth=2.2,
        label=f"{ROLLING_SECONDS} s mean",
    )
    ax.set_ylabel("Voltage difference (V)")
    ax.set_title("Measured high-minus-minimum range vs time")
    finish(fig, ax, OUTPUT_FILENAMES["level_range_plot"])

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(
        clean["timestamp"],
        clean["minimum_voltage"] / clean["minimum_voltage"].median(),
        label="Minimum / median",
    )
    ax.plot(
        clean["timestamp"],
        clean["high_level_voltage"] / clean["high_level_voltage"].median(),
        label="High/offset level / median",
    )
    ax.set_ylabel("Normalised voltage")
    ax.set_title("Normalised minimum and high/offset levels")
    finish(fig, ax, OUTPUT_FILENAMES["normalised_plot"])

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.scatter(
        clean["minimum_voltage"],
        clean["high_level_voltage"],
        s=8,
    )
    ax.set_xlabel("Minimum photodiode voltage (V)")
    ax.set_ylabel("High/offset photodiode voltage (V)")
    ax.set_title("Measured high/offset level vs minimum level")
    ax.grid(True, axis="y", alpha=0.3)
    mark_in_progress(fig, clean, in_progress)
    output_path = output_dir / OUTPUT_FILENAMES["scatter_plot"]
    save_figure_atomic(fig, output_path)
    figures_and_paths.append((fig, output_path))
    return figures_and_paths


def _ttest_p(first, last):
    if not HAVE_SCIPY or len(first) < 2 or len(last) < 2:
        return np.nan
    return ttest_ind(first, last, equal_var=False).pvalue


def create_summary(
    raw_data,
    clean,
    minute,
    events,
    event_diagnostics,
    csv_path,
    output_dir,
    in_progress,
    profile: MeasurementAnalysisProfile | None = None,
):
    profile = _profile_or_historical(profile)
    duration_min = clean["elapsed_min"].iloc[-1]
    sample_interval = np.median(np.diff(clean["wall_time"]))
    min_trend = trend_stats(minute["elapsed_min"], minute["minimum_voltage"])
    high_trend = trend_stats(minute["elapsed_min"], minute["high_level_voltage"])
    er_trend = trend_stats(minute["elapsed_min"], minute["extinction_ratio_dB"])

    large_gaps = int(
        (clean["wall_time"].diff() > THRESHOLD_MARK_MISSING_SAMPLE_S).sum()
    )

    if duration_min > 2 * FIRST_LAST_MINUTES:
        first_cut = clean["elapsed_min"].min() + FIRST_LAST_MINUTES
        last_cut = clean["elapsed_min"].max() - FIRST_LAST_MINUTES
        first = clean[clean["elapsed_min"] <= first_cut]
        last = clean[clean["elapsed_min"] >= last_cut]
        first_last_section = f"""First/last {FIRST_LAST_MINUTES:g}-minute windows
-----------------------------------
Minimum change: {(last['minimum_voltage'].mean() - first['minimum_voltage'].mean()) * 1000:.3f} mV (p={format_p(_ttest_p(first['minimum_voltage'], last['minimum_voltage']))})
High/offset-level change: {(last['high_level_voltage'].mean() - first['high_level_voltage'].mean()) * 1000:.3f} mV (p={format_p(_ttest_p(first['high_level_voltage'], last['high_level_voltage']))})
Apparent ER change: {last['extinction_ratio_dB'].mean() - first['extinction_ratio_dB'].mean():.3f} dB (p={format_p(_ttest_p(first['extinction_ratio_dB'], last['extinction_ratio_dB']))})"""
    else:
        first_last_section = f"""First/last {FIRST_LAST_MINUTES:g}-minute windows
-----------------------------------
Not calculated: the {duration_min:.2f}-minute run is too short to provide two non-overlapping {FIRST_LAST_MINUTES:g}-minute windows."""

    if bool(event_diagnostics["present"]):
        event_counts = Counter(events["event"])
        event_count_lines = "\n".join(
            f"{event_name}: {count}"
            for event_name, count in sorted(event_counts.items())
        )
        if not event_count_lines:
            event_count_lines = "No valid acquisition events found."
        acquisition_event_section = f"""Acquisition events
------------------
Event log: {event_diagnostics['path']}
JSONL lines read: {event_diagnostics['lines_read']}
Malformed/incomplete event lines ignored: {event_diagnostics['invalid_lines']}
{event_count_lines}"""
    else:
        acquisition_event_section = f"""Acquisition events
------------------
No acquisition event log was found at: {event_diagnostics['path']}
This is expected for historical runs; long data gaps are still counted above."""

    rows_read = int(raw_data.attrs.get("rows_read", len(raw_data)))
    incomplete_rows = int(raw_data.attrs.get("incomplete_or_non_numeric_rows", 0))
    voltage_rejected_rows = int(clean.attrs.get("voltage_rejected_rows", 0))
    duplicate_timestamp_rows = int(clean.attrs.get("duplicate_timestamp_rows", 0))

    high_filter_description = (
        "disabled"
        if profile.minimum_high_level_v is None
        else f"> {profile.minimum_high_level_v:.9g} V"
    )
    minimum_filter_description = (
        "disabled"
        if profile.maximum_minimum_v is None
        else f"< {profile.maximum_minimum_v:.9g} V"
    )
    status = "IN PROGRESS — UNFINISHED SNAPSHOT\n\n" if in_progress else ""
    summary = f"""{status}EOM / Moku CSV analysis
=======================

Input file: {csv_path}
Output folder: {output_dir}

Rows
----
CSV rows read: {rows_read}
Incomplete/non-numeric rows excluded: {incomplete_rows}
Complete numeric rows: {len(raw_data)}
Rows rejected by voltage checks: {voltage_rejected_rows}
Duplicate timestamp rows excluded: {duplicate_timestamp_rows}
Valid rows used: {len(clean)}

Analysis profile
----------------
Dark offset: {profile.dark_offset_v:.9g} V
Optional high/offset-level filter: {high_filter_description}
Optional minimum-level filter: {minimum_filter_description}
Minimum sample count: {profile.minimum_sample_count}
Always-required metric domain: high/offset > minimum > dark offset

Time
----
Start: {clean['timestamp'].iloc[0].strftime('%Y-%m-%d %H:%M:%S %Z')}
Finish: {clean['timestamp'].iloc[-1].strftime('%Y-%m-%d %H:%M:%S %Z')}
Duration: {duration_min:.2f} min
Median sample interval: {sample_interval:.3f} s
Gaps above {THRESHOLD_MARK_MISSING_SAMPLE_S} s: {large_gaps}

Separate optical metrics
------------------------
Minimum mean: {clean['minimum_voltage'].mean():.6f} V
Minimum standard deviation: {clean['minimum_voltage'].std():.6f} V
Measured high/offset-level mean: {clean['high_level_voltage'].mean():.6f} V
Measured high/offset-level standard deviation: {clean['high_level_voltage'].std():.6f} V
Apparent extinction-ratio mean: {clean['extinction_ratio_dB'].mean():.3f} dB
Apparent extinction-ratio standard deviation: {clean['extinction_ratio_dB'].std():.3f} dB
Normalised extinction-ratio mean: {clean['normalised_extinction_ratio'].mean():.6f}
Normalised extinction-ratio standard deviation: {clean['normalised_extinction_ratio'].std():.6f}

Dark-offset correction
----------------------
Configured dark offset: {profile.dark_offset_v:.9g} V
H' = measured high/offset voltage - configured dark offset
L' = measured minimum voltage - configured dark offset
Normalised extinction ratio = (H' - L') / (H' + L')

Trend using one-minute block averages
-------------------------------------
Minimum slope: {min_trend['slope'] * 1000:.4f} mV/min (p={format_p(min_trend['p'])})
High/offset-level slope: {high_trend['slope'] * 1000:.4f} mV/min (p={format_p(high_trend['p'])})
Apparent ER slope: {er_trend['slope']:.5f} dB/min (p={format_p(er_trend['p'])})

{first_last_section}

{acquisition_event_section}

Notes
-----
The historical source column named maximum_voltage is treated here as a
measured high/offset level; it is not assumed to be the transfer-curve maximum.
The apparent extinction ratio uses 10 log10(H' / L'). It is not corrected
unless the profile dark offset is set from an independent calibration.
The normalised extinction-ratio plot uses the dark-offset-corrected H' and L'
values defined above. A configured offset of 0 V applies no correction.
Correlation or coincident drift does not establish causation.
""".strip()

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / OUTPUT_FILENAMES["summary"]
    temporary_path = summary_path.with_suffix(summary_path.suffix + ".tmp")
    temporary_path.write_text(summary + "\n", encoding="utf-8")
    temporary_path.replace(summary_path)
    return summary


def run_analysis(
    csv_path: Path,
    output_dir: Path,
    *,
    analysis_dir: Path | None = None,
    events_path: Path | None = None,
    in_progress=False,
    profile: MeasurementAnalysisProfile | None = None,
    provenance_path: Path | None = None,
    regime_options: RegimePlotOptions | None = None,
):
    profile = _profile_or_historical(profile)
    raw_data = read_growing_csv(csv_path, profile)
    if provenance_path is None:
        provenance_path = csv_path.with_name(PROVENANCE_FILENAME)
    raw_data, provenance_diagnostics = join_regime_provenance(
        raw_data,
        provenance_path,
    )
    regime_boundaries = extract_regime_boundaries(raw_data)
    clean, minute = prepare_data(raw_data, profile)
    clean.attrs["provenance"] = provenance_diagnostics
    if events_path is None:
        events_path = csv_path.with_name(ACQUISITION_EVENTS_FILENAME)
    events, event_diagnostics = read_acquisition_events(events_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    analysis_dir = output_dir if analysis_dir is None else analysis_dir
    analysis_dir.mkdir(parents=True, exist_ok=True)

    cleaned_path = analysis_dir / OUTPUT_FILENAMES["cleaned_csv"]
    temporary_path = cleaned_path.with_suffix(cleaned_path.suffix + ".tmp")
    clean.to_csv(temporary_path, index=False)
    temporary_path.replace(cleaned_path)

    figures_and_paths = create_plots(
        clean,
        output_dir,
        in_progress=in_progress,
        regime_boundaries=regime_boundaries,
        regime_options=regime_options,
    )
    summary = create_summary(
        raw_data,
        clean,
        minute,
        events,
        event_diagnostics,
        csv_path,
        analysis_dir,
        in_progress,
        profile,
    )
    return figures_and_paths, summary


def main():
    args = parse_arguments()
    if args.csv_path is not None:
        csv_path = args.csv_path.expanduser().resolve()
    elif CSV_PATH is not None:
        csv_path = Path(CSV_PATH).expanduser().resolve()
    else:
        csv_path = find_latest_csv()
    if not csv_path.is_file():
        raise FileNotFoundError(f"Moku CSV does not exist:\n{csv_path}")

    profile_path = (
        args.analysis_profile.expanduser().resolve()
        if args.analysis_profile is not None
        else discover_analysis_profile(csv_path)
    )
    try:
        base_profile = (
            historical_analysis_profile()
            if profile_path is None
            else load_analysis_profile(profile_path)
        )
        profile = analysis_profile_from_arguments(
            args,
            base_profile=base_profile,
        )
    except ValueError as error:
        raise SystemExit(f"Invalid analysis profile: {error}") from error

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else csv_path.parent
        / "plots"
        / ("in_progress" if args.in_progress else "final")
    )
    analysis_dir = (
        args.analysis_dir.expanduser().resolve()
        if args.analysis_dir is not None
        else csv_path.parent
    )
    events_path = (
        args.events_path.expanduser().resolve()
        if args.events_path is not None
        else csv_path.with_name(ACQUISITION_EVENTS_FILENAME)
    )
    provenance_path = (
        args.provenance_path.expanduser().resolve()
        if args.provenance_path is not None
        else csv_path.with_name(PROVENANCE_FILENAME)
    )
    regime_options = RegimePlotOptions(
        show_temperature_boundaries=args.show_temperature_boundaries,
        show_waveform_boundaries=args.show_waveform_boundaries,
        show_session_boundaries=args.show_session_boundaries,
        annotate_temperature_labels=args.annotate_temperature_labels,
        annotate_waveform_labels=args.annotate_waveform_labels,
    )
    print(f"Analysing: {csv_path}")
    print(
        "Analysis profile: "
        + ("historical defaults" if profile_path is None else str(profile_path))
    )
    figures_and_paths, summary = run_analysis(
        csv_path,
        output_dir,
        analysis_dir=analysis_dir,
        events_path=events_path,
        in_progress=args.in_progress,
        profile=profile,
        provenance_path=provenance_path,
        regime_options=regime_options,
    )
    print(summary)
    print(f"\nSaved analysis tables and summary to: {analysis_dir}")
    print(f"Saved plots to: {output_dir}")

    if not args.no_show:
        plt.show()
    for figure, _ in figures_and_paths:
        plt.close(figure)


if __name__ == "__main__":
    main()
