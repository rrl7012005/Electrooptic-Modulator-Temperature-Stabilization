"""Plot Linien lock voltage without modifying the source CSV.

The script can be run while ``linien_logger.py`` is still appending rows.  It
only opens the CSV for reading, ignores an incomplete final row, and writes the
PNG via a temporary file so readers never see a half-written image.
"""

import argparse
from pathlib import Path
import time

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
DATA_FOLDER = SCRIPT_DIRECTORY / "Experiment Results"
AMPLIFIER_GAIN = 10.0
ROLLING_SECONDS = 60
MAX_RAW_POINTS = 100_000
CSV_READ_ATTEMPTS = 3


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Plot Linien lock voltage against UK wall time."
    )
    parser.add_argument(
        "csv_path",
        nargs="?",
        type=Path,
        help="CSV to plot; default: newest RP control log",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="folder for the PNG; default: RP_logs/plots/final",
    )
    parser.add_argument(
        "--no-show",
        action="store_true",
        help="save without opening an interactive plot window",
    )
    parser.add_argument(
        "--in-progress",
        action="store_true",
        help="clearly mark the plot as an unfinished live snapshot",
    )
    return parser.parse_args()


def find_latest_log(folder: Path = DATA_FOLDER) -> Path:
    """Return the most recently modified Linien control CSV."""
    files = list(folder.rglob("*RP*.csv"))
    if not files:
        raise FileNotFoundError(
            f"No RP control CSV was found under:\n{folder}"
        )
    return max(files, key=lambda path: path.stat().st_mtime)


def load_control_data(csv_path: Path) -> pd.DataFrame:
    """Load a stable usable prefix of a possibly growing control log."""
    last_error = None
    for attempt in range(CSV_READ_ATTEMPTS):
        try:
            data = pd.read_csv(
                csv_path,
                usecols=["wall_time", "RP_lock_voltage"],
                on_bad_lines="skip",
            )
            break
        except (OSError, pd.errors.ParserError, ValueError) as error:
            last_error = error
            if attempt + 1 == CSV_READ_ATTEMPTS:
                raise ValueError(
                    f"Could not read control CSV {csv_path}: {error}"
                ) from error
            time.sleep(0.1)
    else:  # pragma: no cover - loop always breaks or raises
        raise ValueError(f"Could not read control CSV: {last_error}")

    data["wall_time"] = pd.to_numeric(data["wall_time"], errors="coerce")
    data["RP_lock_voltage"] = pd.to_numeric(
        data["RP_lock_voltage"], errors="coerce"
    )
    data = data.dropna(subset=["wall_time", "RP_lock_voltage"]).copy()
    if data.empty:
        raise ValueError("No complete, valid control-log rows were found.")

    data["Local Time"] = (
        pd.to_datetime(data["wall_time"], unit="s", utc=True)
        .dt.tz_convert("Europe/London")
    )
    data["DC_Lock_Voltage"] = data["RP_lock_voltage"] * AMPLIFIER_GAIN
    data = (
        data.sort_values("Local Time")
        .drop_duplicates(subset="Local Time")
        .set_index("Local Time")
    )
    data["rolling_average"] = (
        data["DC_Lock_Voltage"]
        .rolling(f"{ROLLING_SECONDS}s", min_periods=1)
        .mean()
    )
    return data


def save_figure_atomic(fig, output_path: Path) -> None:
    """Replace a PNG only after its new contents have been fully written."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    fig.savefig(
        temporary_path,
        format="png",
        dpi=300,
        bbox_inches="tight",
    )
    temporary_path.replace(output_path)


def create_control_plot(
    csv_path: Path,
    output_path: Path,
    *,
    in_progress: bool = False,
):
    """Create one control-voltage plot and return the figure and data."""
    data = load_control_data(csv_path)
    raw_step = max(1, int(np.ceil(len(data) / MAX_RAW_POINTS)))
    raw_data = data.iloc[::raw_step]

    fig, ax = plt.subplots(figsize=(13, 6))
    ax.plot(
        raw_data.index,
        raw_data["DC_Lock_Voltage"],
        linewidth=0.5,
        alpha=0.3,
        label="EOM DC lock voltage",
    )
    ax.plot(
        data.index,
        data["rolling_average"],
        linewidth=1.5,
        label=f"{ROLLING_SECONDS} s rolling mean",
    )

    start_day = data.index[0].strftime("%d %B %Y")
    ax.set_xlabel(f"Local time from {start_day}")
    ax.set_ylabel("EOM lock voltage (V)")
    title = "EOM lock voltage against time"
    if in_progress:
        through = data.index[-1].strftime("%Y-%m-%d %H:%M:%S %Z")
        title = f"IN PROGRESS — {title}\nData through {through}"
        ax.set_title(title, color="#A33A2B")
    else:
        ax.set_title(title)

    locator = mdates.AutoDateLocator()
    formatter = mdates.ConciseDateFormatter(
        locator,
        tz="Europe/London",
    )
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    save_figure_atomic(fig, output_path)
    return fig, data


def main():
    args = parse_arguments()
    csv_path = (
        args.csv_path.expanduser().resolve()
        if args.csv_path is not None
        else find_latest_log()
    )
    if not csv_path.is_file():
        raise FileNotFoundError(f"Control CSV does not exist:\n{csv_path}")

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else csv_path.parent
        / "plots"
        / ("in_progress" if args.in_progress else "final")
    )
    output_name = (
        "control_voltage_in_progress.png"
        if args.in_progress
        else f"{csv_path.stem}_control_voltage_vs_time.png"
    )
    output_path = output_dir / output_name

    print(f"Plotting control log:\n{csv_path}")
    fig, data = create_control_plot(
        csv_path,
        output_path,
        in_progress=args.in_progress,
    )
    print(f"Rows plotted: {len(data):,}")
    print(f"Start: {data.index[0].strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Finish: {data.index[-1].strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Plot saved to: {output_path}")

    if not args.no_show:
        plt.show()
    plt.close(fig)


if __name__ == "__main__":
    main()
