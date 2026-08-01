"""Analyse and plot a Moku photovoltage log without modifying raw data."""

import argparse
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
MOKU_OUTPUT_ROOT = SCRIPT_DIRECTORY / "Experiment Results" / "moku_pulse_runs"

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


def parse_arguments():
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
        help="output folder; default: an analysis folder beside the CSV",
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
    return parser.parse_args()


def find_latest_csv(folder: Path = MOKU_OUTPUT_ROOT) -> Path:
    files = list(folder.glob("run_*/raw_photovoltage_tracking.csv"))
    if not files:
        raise FileNotFoundError(
            "Could not find any Moku raw photovoltage CSV under:\n"
            f"{folder}"
        )
    return max(files, key=lambda path: path.stat().st_mtime)


def read_growing_csv(csv_path: Path) -> pd.DataFrame:
    """Read a usable snapshot even if Moku is currently rewriting the file."""
    last_error = None
    for attempt in range(CSV_READ_ATTEMPTS):
        try:
            data = pd.read_csv(csv_path, on_bad_lines="skip")
            required = {"wall_time", "minimum_voltage", "maximum_voltage"}
            missing = required - set(data.columns)
            if missing:
                raise ValueError(f"CSV is missing required columns: {sorted(missing)}")
            for column in required:
                data[column] = pd.to_numeric(data[column], errors="coerce")
            data = data.dropna(subset=sorted(required)).copy()
            if len(data) < MINIMUM_SAMPLE_THRESHOLD:
                raise ValueError(
                    "Too few complete samples are currently available "
                    f"({len(data)} found; {MINIMUM_SAMPLE_THRESHOLD} required)."
                )
            return data
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError, ValueError) as error:
            last_error = error
            if attempt + 1 < CSV_READ_ATTEMPTS:
                time.sleep(0.2)

    raise ValueError(f"Could not read a usable Moku CSV snapshot: {last_error}")


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


def prepare_data(raw_data: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Map historical columns to canonical names and calculate metrics."""
    data = raw_data.rename(columns={"maximum_voltage": "high_level_voltage"})
    valid = (
        (data["high_level_voltage"] > data["minimum_voltage"])
        & (data["high_level_voltage"] > MINIMUM_ALLOWED_HIGH_LEVEL_V)
        & (data["minimum_voltage"] < MAXIMUM_ALLOWED_MINIMUM_V)
        & (data["minimum_voltage"] > DARK_OFFSET_V)
    )
    clean = data[valid].copy()
    if len(clean) < MINIMUM_SAMPLE_THRESHOLD:
        raise ValueError(
            "Too few valid samples remain after voltage checks. "
            f"Found {len(clean)}; need {MINIMUM_SAMPLE_THRESHOLD}."
        )

    clean = clean.sort_values("wall_time").drop_duplicates("wall_time")
    clean["elapsed_s"] = clean["wall_time"] - clean["wall_time"].iloc[0]
    clean["elapsed_min"] = clean["elapsed_s"] / 60.0
    clean["timestamp"] = (
        pd.to_datetime(clean["wall_time"], unit="s", utc=True)
        .dt.tz_convert(TIMEZONE)
    )

    clean["minimum_corrected_voltage"] = (
        clean["minimum_voltage"] - DARK_OFFSET_V
    )
    clean["high_level_corrected_voltage"] = (
        clean["high_level_voltage"] - DARK_OFFSET_V
    )
    clean["high_minus_minimum_voltage"] = (
        clean["high_level_corrected_voltage"]
        - clean["minimum_corrected_voltage"]
    )
    clean["extinction_ratio_linear"] = (
        clean["high_level_corrected_voltage"]
        / clean["minimum_corrected_voltage"]
    )
    clean["extinction_ratio_dB"] = 10.0 * np.log10(
        clean["extinction_ratio_linear"]
    )

    clean = clean.set_index("timestamp")
    rolling_columns = [
        "minimum_voltage",
        "high_level_voltage",
        "extinction_ratio_dB",
        "high_minus_minimum_voltage",
    ]
    rolling = clean[rolling_columns].rolling(
        f"{ROLLING_SECONDS}s",
        min_periods=MINIMUM_SAMPLE_THRESHOLD,
    ).mean()
    clean["minimum_rolling_voltage"] = rolling["minimum_voltage"]
    clean["high_level_rolling_voltage"] = rolling["high_level_voltage"]
    clean["rolling_extinction_ratio_dB"] = rolling["extinction_ratio_dB"]
    clean["rolling_high_minus_minimum_voltage"] = rolling[
        "high_minus_minimum_voltage"
    ]
    clean = clean.reset_index()

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
    ax.xaxis.set_major_formatter(
        mdates.ConciseDateFormatter(locator, tz=TIMEZONE)
    )
    ax.set_xlabel(f"UK local time | run start: {start_label}")


def add_gap_markers(ax, data):
    gaps = data[
        data["wall_time"].diff() > THRESHOLD_MARK_MISSING_SAMPLE_S
    ]
    for timestamp in gaps["timestamp"]:
        ax.axvline(timestamp, color="0.5", alpha=0.25, linewidth=0.8)


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


def create_plots(clean, output_dir: Path, *, in_progress=False):
    """Create the standard Moku plots and return their figures and paths."""
    start_label = clean["timestamp"].iloc[0].strftime("%Y-%m-%d %H:%M:%S %Z")
    figures_and_paths = []

    def finish(fig, ax, name):
        format_time_axis(ax, start_label)
        ax.grid(True, alpha=0.3)
        ax.legend()
        add_gap_markers(ax, clean)
        mark_in_progress(fig, clean, in_progress)
        output_path = output_dir / name
        save_figure_atomic(fig, output_path)
        figures_and_paths.append((fig, output_path))

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(clean["timestamp"], clean["minimum_voltage"], label="Raw minimum")
    ax.plot(
        clean["timestamp"],
        clean["minimum_rolling_voltage"],
        linewidth=2,
        label=f"{ROLLING_SECONDS} s rolling mean",
    )
    ax.set_ylabel("Minimum photodiode voltage (V)")
    ax.set_title("Minimum optical level vs time")
    finish(fig, ax, "01_minimum_voltage_vs_time.png")

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(
        clean["timestamp"],
        clean["high_level_voltage"],
        label="Raw measured high/offset level",
    )
    ax.plot(
        clean["timestamp"],
        clean["high_level_rolling_voltage"],
        linewidth=2,
        label=f"{ROLLING_SECONDS} s rolling mean",
    )
    ax.set_ylabel("High/offset photodiode voltage (V)")
    ax.set_title("Measured high/offset optical level vs time")
    finish(fig, ax, "02_high_level_voltage_vs_time.png")

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(
        clean["timestamp"],
        clean["extinction_ratio_dB"],
        label="Raw apparent extinction ratio",
    )
    ax.plot(
        clean["timestamp"],
        clean["rolling_extinction_ratio_dB"],
        linewidth=2,
        label=f"{ROLLING_SECONDS} s rolling mean",
    )
    ax.set_ylabel("Apparent extinction ratio (dB)")
    ax.set_title("Apparent extinction ratio vs time")
    finish(fig, ax, "03_extinction_ratio_vs_time.png")

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(
        clean["timestamp"],
        clean["high_minus_minimum_voltage"],
        label="Raw high minus minimum",
    )
    ax.plot(
        clean["timestamp"],
        clean["rolling_high_minus_minimum_voltage"],
        linewidth=2,
        label=f"{ROLLING_SECONDS} s rolling mean",
    )
    ax.set_ylabel("Voltage difference (V)")
    ax.set_title("Measured high-minus-minimum range vs time")
    finish(fig, ax, "04_high_minus_minimum_vs_time.png")

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
    finish(fig, ax, "05_normalised_levels_vs_time.png")

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.scatter(
        clean["minimum_voltage"],
        clean["high_level_voltage"],
        s=8,
    )
    ax.set_xlabel("Minimum photodiode voltage (V)")
    ax.set_ylabel("High/offset photodiode voltage (V)")
    ax.set_title("Measured high/offset level vs minimum level")
    ax.grid(True, alpha=0.3)
    mark_in_progress(fig, clean, in_progress)
    output_path = output_dir / "06_high_level_vs_minimum_scatter.png"
    save_figure_atomic(fig, output_path)
    figures_and_paths.append((fig, output_path))
    return figures_and_paths


def _ttest_p(first, last):
    if not HAVE_SCIPY or len(first) < 2 or len(last) < 2:
        return np.nan
    return ttest_ind(first, last, equal_var=False).pvalue


def create_summary(raw_data, clean, minute, csv_path, output_dir, in_progress):
    duration_min = clean["elapsed_min"].iloc[-1]
    sample_interval = np.median(np.diff(clean["wall_time"]))
    min_trend = trend_stats(minute["elapsed_min"], minute["minimum_voltage"])
    high_trend = trend_stats(
        minute["elapsed_min"], minute["high_level_voltage"]
    )
    er_trend = trend_stats(
        minute["elapsed_min"], minute["extinction_ratio_dB"]
    )

    first_cut = clean["elapsed_min"].min() + FIRST_LAST_MINUTES
    last_cut = clean["elapsed_min"].max() - FIRST_LAST_MINUTES
    first = clean[clean["elapsed_min"] <= first_cut]
    last = clean[clean["elapsed_min"] >= last_cut]
    large_gaps = int(
        (clean["wall_time"].diff() > THRESHOLD_MARK_MISSING_SAMPLE_S).sum()
    )

    status = "IN PROGRESS — UNFINISHED SNAPSHOT\n\n" if in_progress else ""
    summary = f"""{status}EOM / Moku CSV analysis
=======================

Input file: {csv_path}
Output folder: {output_dir}

Rows
----
Raw rows: {len(raw_data)}
Valid rows used: {len(clean)}
Rejected rows: {len(raw_data) - len(clean)}

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

Trend using one-minute block averages
-------------------------------------
Minimum slope: {min_trend['slope'] * 1000:.4f} mV/min (p={format_p(min_trend['p'])})
High/offset-level slope: {high_trend['slope'] * 1000:.4f} mV/min (p={format_p(high_trend['p'])})
Apparent ER slope: {er_trend['slope']:.5f} dB/min (p={format_p(er_trend['p'])})

First/last {FIRST_LAST_MINUTES:g}-minute windows
-----------------------------------
Minimum change: {(last['minimum_voltage'].mean() - first['minimum_voltage'].mean()) * 1000:.3f} mV (p={format_p(_ttest_p(first['minimum_voltage'], last['minimum_voltage']))})
High/offset-level change: {(last['high_level_voltage'].mean() - first['high_level_voltage'].mean()) * 1000:.3f} mV (p={format_p(_ttest_p(first['high_level_voltage'], last['high_level_voltage']))})
Apparent ER change: {last['extinction_ratio_dB'].mean() - first['extinction_ratio_dB'].mean():.3f} dB (p={format_p(_ttest_p(first['extinction_ratio_dB'], last['extinction_ratio_dB']))})

Notes
-----
The historical source column named maximum_voltage is treated here as a
measured high/offset level; it is not assumed to be the transfer-curve maximum.
The apparent extinction ratio uses 10 log10(high_level / minimum). It is not
corrected unless DARK_OFFSET_V is set from an independent calibration.
Correlation or coincident drift does not establish causation.
""".strip()

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "summary.txt"
    temporary_path = summary_path.with_suffix(".txt.tmp")
    temporary_path.write_text(summary + "\n", encoding="utf-8")
    temporary_path.replace(summary_path)
    return summary


def run_analysis(csv_path: Path, output_dir: Path, *, in_progress=False):
    raw_data = read_growing_csv(csv_path)
    clean, minute = prepare_data(raw_data)
    output_dir.mkdir(parents=True, exist_ok=True)

    cleaned_path = output_dir / "cleaned_photodiode_voltages.csv"
    temporary_path = cleaned_path.with_suffix(".csv.tmp")
    clean.to_csv(temporary_path, index=False)
    temporary_path.replace(cleaned_path)

    figures_and_paths = create_plots(
        clean,
        output_dir,
        in_progress=in_progress,
    )
    summary = create_summary(
        raw_data,
        clean,
        minute,
        csv_path,
        output_dir,
        in_progress,
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

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else csv_path.parent / "analysis"
    )
    print(f"Analysing: {csv_path}")
    figures_and_paths, summary = run_analysis(
        csv_path,
        output_dir,
        in_progress=args.in_progress,
    )
    print(summary)
    print(f"\nSaved analysis to: {output_dir}")

    if not args.no_show:
        plt.show()
    for figure, _ in figures_and_paths:
        plt.close(figure)


if __name__ == "__main__":
    main()
