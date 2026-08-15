"""Plot a TEC log safely, including while the logger is still running."""

import argparse
import csv
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_LOG_FOLDER = SCRIPT_DIRECTORY / "Experiment Results"
UK_TIME = ZoneInfo("Europe/London")

PULSING_STOPPED = datetime(
    2026,
    7,
    19,
    17,
    51,
    1,
    tzinfo=UK_TIME,
)

# True: combine dense five-second measurements into one mean per minute.
# False: plot every raw measurement.
USE_AVERAGING = True

CURRENT_COLUMN_CANDIDATES = (
    "tec_output_current_A",
    "output_current_A",
    "output_current_a",
)
VOLTAGE_COLUMN_CANDIDATES = (
    "tec_output_voltage_V",
    "output_voltage_V",
    "output_voltage_v",
)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Plot object temperature and available TEC output data."
    )
    parser.add_argument(
        "csv_path",
        nargs="?",
        type=Path,
        help="CSV to plot; default: newest TEC log",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="folder for PNG files; default: TEC_logs/plots/final",
    )
    parser.add_argument(
        "--no-show",
        action="store_true",
        help="save without opening interactive plot windows",
    )
    parser.add_argument(
        "--in-progress",
        action="store_true",
        help="clearly mark plots as unfinished live snapshots",
    )
    return parser.parse_args()


def find_latest_log(folder: Path = DEFAULT_LOG_FOLDER) -> Path:
    """Find the newest passive or scheduled-control TEC CSV."""
    log_files = list(folder.rglob("tec_temperature*.csv"))
    if not log_files:
        raise FileNotFoundError(
            f"No TEC logger CSV files were found under:\n{folder}"
        )
    return max(log_files, key=lambda file: file.stat().st_mtime)


def find_first_existing_column(fieldnames, candidates):
    """Return the first candidate column present in the CSV header."""
    for candidate in candidates:
        if candidate in fieldnames:
            return candidate
    return None


def _text(row, column):
    """Return stripped cell text, including for an incomplete final row."""
    value = row.get(column)
    return "" if value is None else str(value).strip()


def load_log_data(csv_path: Path):
    """Load complete valid measurements from a possibly growing TEC CSV."""
    temperature_times = []
    temperatures = []
    current_times = []
    currents = []
    voltage_times = []
    voltages = []

    with csv_path.open(
        mode="r",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        reader = csv.DictReader(file)
        fieldnames = reader.fieldnames or []
        configured_v2 = "timestamp_utc" in fieldnames
        required_columns = (
            {"timestamp_utc", "object_temperature_c", "controller_status"}
            if configured_v2
            else {"wall_time", "object_temperature_C", "read_status"}
        )
        missing_columns = required_columns - set(fieldnames)
        if missing_columns:
            raise ValueError(
                f"Missing required columns: {sorted(missing_columns)}\n"
                f"Available columns: {fieldnames}"
            )

        current_column = find_first_existing_column(
            fieldnames,
            CURRENT_COLUMN_CANDIDATES,
        )
        voltage_column = find_first_existing_column(
            fieldnames,
            VOLTAGE_COLUMN_CANDIDATES,
        )

        try:
            for row_number, row in enumerate(reader, start=2):
                try:
                    if configured_v2:
                        if _text(row, "error_message"):
                            continue
                        wall_time_text = _text(row, "timestamp_utc")
                        temperature_text = _text(row, "object_temperature_c")
                    else:
                        if _text(row, "read_status") != "OK":
                            continue
                        wall_time_text = _text(row, "wall_time")
                        temperature_text = _text(row, "object_temperature_C")
                    if not wall_time_text:
                        continue
                    timestamp = datetime.fromisoformat(
                        wall_time_text[:-1] + "+00:00"
                        if wall_time_text.endswith("Z")
                        else wall_time_text
                    )
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=UK_TIME)
                    else:
                        timestamp = timestamp.astimezone(UK_TIME)

                    if temperature_text:
                        temperature = float(temperature_text)
                        if math.isfinite(temperature):
                            temperature_times.append(timestamp)
                            temperatures.append(temperature)

                    if current_column is not None:
                        current_text = _text(row, current_column)
                        if current_text:
                            current = float(current_text)
                            if math.isfinite(current):
                                current_times.append(timestamp)
                                currents.append(current)

                    if voltage_column is not None:
                        voltage_text = _text(row, voltage_column)
                        if voltage_text:
                            voltage = float(voltage_text)
                            if math.isfinite(voltage):
                                voltage_times.append(timestamp)
                                voltages.append(voltage)
                except (ValueError, TypeError, KeyError) as error:
                    print(f"Skipping unreadable row {row_number}: {error}")
        except csv.Error as error:
            # A writer can momentarily leave its last record incomplete.  All
            # complete rows already yielded by DictReader remain usable.
            print(f"Ignoring incomplete final CSV record: {error}")

    if not temperature_times:
        raise ValueError("No successful temperature measurements were found.")

    return {
        "configured_v2": configured_v2,
        "temperature_times": temperature_times,
        "temperatures": temperatures,
        "current_times": current_times,
        "currents": currents,
        "voltage_times": voltage_times,
        "voltages": voltages,
        "has_current": bool(currents),
        "has_voltage": bool(voltages),
        "current_column": current_column,
        "voltage_column": voltage_column,
    }


def calculate_average(times, values):
    """Combine all measurements within each minute into one mean value."""
    grouped_values = defaultdict(list)
    for timestamp, value in zip(times, values):
        time_group = timestamp.replace(second=0, microsecond=0)
        grouped_values[time_group].append(value)

    averaged_times = sorted(grouped_values)
    averaged_values = np.array(
        [np.mean(grouped_values[timestamp]) for timestamp in averaged_times]
    )
    return averaged_times, averaged_values


def prepare_plot_data(times, values):
    """Return either raw data or one-minute averages."""
    if USE_AVERAGING:
        return calculate_average(times, values)
    return times, np.asarray(values)


def add_pulsing_marker(ax, plot_times):
    """Add the historical pulsing-stop event when it is in range."""
    if plot_times[0] <= PULSING_STOPPED <= plot_times[-1]:
        ax.axvline(
            PULSING_STOPPED,
            linestyle="--",
            linewidth=1.4,
            color="#444444",
            label="Pulsing stopped: 19 Jul 17:51:01",
        )
        ax.annotate(
            "Pulsing stopped\n19 Jul 17:51:01",
            xy=(PULSING_STOPPED, 1.0),
            xycoords=("data", "axes fraction"),
            xytext=(7, -8),
            textcoords="offset points",
            ha="left",
            va="top",
            fontsize=9,
        )


def save_figure_atomic(fig, output_path: Path) -> None:
    """Replace a PNG only after its new contents have been fully written."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    fig.savefig(
        temporary_path,
        format="png",
        dpi=180,
        bbox_inches="tight",
    )
    temporary_path.replace(output_path)


def create_plot(
    times,
    values,
    title,
    ylabel,
    line_label,
    color,
    output_path,
    *,
    in_progress=False,
    historical_markers=True,
):
    """Create and atomically save one graph."""
    plot_times, plot_values = prepare_plot_data(times, values)
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(
        plot_times,
        plot_values,
        linewidth=1.4,
        color=color,
        label=line_label,
    )
    if historical_markers:
        add_pulsing_marker(ax, plot_times)

    if in_progress:
        through = plot_times[-1].strftime("%Y-%m-%d %H:%M:%S %Z")
        title = f"IN PROGRESS — {title}\nData through {through}"
        ax.set_title(title, color="#A33A2B")
    else:
        ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.set_xlabel("UK local time")
    ax.xaxis.set_major_formatter(
        mdates.DateFormatter("%d %b\n%H:%M", tz=UK_TIME)
    )
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    save_figure_atomic(fig, output_path)
    print(f"Plot saved to:\n{output_path}\n")
    return fig


def create_temperature_plots(
    csv_path: Path,
    output_dir: Path,
    *,
    in_progress: bool = False,
):
    """Create every applicable TEC plot and return figures and paths."""
    data = load_log_data(csv_path)
    prefix = "tec_in_progress" if in_progress else csv_path.stem
    figures_and_paths = []

    plot_specs = [
        (
            data["temperature_times"],
            data["temperatures"],
            "EOM temperature",
            "Temperature (°C)",
            "EOM temperature",
            "#2369A1",
            output_dir / f"{prefix}_temperature.png",
        )
    ]
    if data["has_current"]:
        plot_specs.append(
            (
                data["current_times"],
                data["currents"],
                "TEC output current",
                "Current (A)",
                "TEC output current",
                "#D2691E",
                output_dir / f"{prefix}_current.png",
            )
        )
    if data["has_voltage"]:
        plot_specs.append(
            (
                data["voltage_times"],
                data["voltages"],
                "TEC output voltage",
                "Voltage (V)",
                "TEC output voltage",
                "#27864A",
                output_dir / f"{prefix}_voltage.png",
            )
        )

    for spec in plot_specs:
        output_path = spec[-1]
        figure = create_plot(
            *spec,
            in_progress=in_progress,
            historical_markers=not data["configured_v2"],
        )
        figures_and_paths.append((figure, output_path))

    return figures_and_paths, data


def main():
    args = parse_arguments()
    csv_path = (
        args.csv_path.expanduser().resolve()
        if args.csv_path is not None
        else find_latest_log()
    )
    if not csv_path.is_file():
        raise FileNotFoundError(f"Temperature CSV does not exist:\n{csv_path}")

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else csv_path.parent
        / "plots"
        / ("in_progress" if args.in_progress else "final")
    )
    print(f"Plotting TEC log:\n{csv_path}\n")
    figures_and_paths, data = create_temperature_plots(
        csv_path,
        output_dir,
        in_progress=args.in_progress,
    )
    print(
        f"Log begins: {data['temperature_times'][0]}\n"
        f"Log ends:   {data['temperature_times'][-1]}"
    )

    if not args.no_show:
        plt.show()
    for figure, _ in figures_and_paths:
        plt.close(figure)


if __name__ == "__main__":
    main()
