import csv
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np


# ---------------- SETTINGS ----------------

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

# True: combine the dense five-second measurements into one mean per minute.
# False: plot every raw measurement.
USE_MINUTE_AVERAGE = True

# Support both the current logger names and older possible names.
CURRENT_COLUMN_CANDIDATES = (
    "tec_output_current_A",
    "output_current_A",
)

VOLTAGE_COLUMN_CANDIDATES = (
    "tec_output_voltage_V",
    "output_voltage_V",
)

# ------------------------------------------


def find_latest_log(folder: Path) -> Path:
    """
    Find the newest temperature-only or temperature-and-Peltier CSV.
    """

    # Matches:
    # tec_temperature_20260721_....
    # tec_temperature_peltier_20260721_....
    log_files = list(folder.glob("tec_temperature*.csv"))

    if not log_files:
        raise FileNotFoundError(
            "No TEC logger CSV files were found in:\n"
            f"{folder}"
        )

    return max(
        log_files,
        key=lambda file: file.stat().st_mtime,
    )


def find_first_existing_column(fieldnames, candidates):
    """Return the first candidate column found in the CSV header."""

    for candidate in candidates:
        if candidate in fieldnames:
            return candidate

    return None


def load_log_data(csv_path: Path):
    """
    Load temperature and automatically detect whether Peltier current
    and voltage are also present.
    """

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

        required_columns = {
            "wall_time",
            "object_temperature_C",
            "read_status",
        }

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

        has_current = current_column is not None
        has_voltage = voltage_column is not None

        for row_number, row in enumerate(reader, start=2):
            try:
                # Ignore rows where the TEC read failed.
                if row["read_status"].strip() != "OK":
                    continue

                wall_time_text = row["wall_time"].strip()
                temperature_text = row["object_temperature_C"].strip()

                if not wall_time_text or not temperature_text:
                    continue

                timestamp = datetime.fromisoformat(wall_time_text)

                # Ensure every timestamp is represented in UK local time.
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=UK_TIME)
                else:
                    timestamp = timestamp.astimezone(UK_TIME)

                temperature = float(temperature_text)

                if math.isfinite(temperature):
                    temperature_times.append(timestamp)
                    temperatures.append(temperature)

                if has_current:
                    current_text = row[current_column].strip()

                    if current_text:
                        current = float(current_text)

                        if math.isfinite(current):
                            current_times.append(timestamp)
                            currents.append(current)

                if has_voltage:
                    voltage_text = row[voltage_column].strip()

                    if voltage_text:
                        voltage = float(voltage_text)

                        if math.isfinite(voltage):
                            voltage_times.append(timestamp)
                            voltages.append(voltage)

            except (ValueError, TypeError, KeyError) as error:
                print(
                    f"Skipping unreadable row {row_number}: {error}"
                )

    if not temperature_times:
        raise ValueError(
            "No successful temperature measurements were found."
        )

    return {
        "temperature_times": temperature_times,
        "temperatures": temperatures,
        "current_times": current_times,
        "currents": currents,
        "voltage_times": voltage_times,
        "voltages": voltages,
        "has_current": has_current and bool(currents),
        "has_voltage": has_voltage and bool(voltages),
        "current_column": current_column,
        "voltage_column": voltage_column,
    }


def calculate_minute_average(times, values):
    """Combine all measurements within each minute into one mean value."""

    minute_values = defaultdict(list)

    for timestamp, value in zip(times, values):
        minute = timestamp.replace(
            second=0,
            microsecond=0,
        )

        minute_values[minute].append(value)

    averaged_times = sorted(minute_values)

    averaged_values = np.array([
        np.mean(minute_values[timestamp])
        for timestamp in averaged_times
    ])

    return averaged_times, averaged_values


def prepare_plot_data(times, values):
    """Return either raw data or one-minute averages."""

    if USE_MINUTE_AVERAGE:
        return calculate_minute_average(times, values)

    return times, np.asarray(values)


def add_pulsing_marker(ax, plot_times):
    """Add the pulsing-stop event when it lies inside the data range."""

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


def format_time_axis(ax):
    """Apply the common time-axis formatting."""

    ax.set_xlabel("Time")

    ax.xaxis.set_major_formatter(
        mdates.DateFormatter(
            "%d %b\n%H:%M",
            tz=UK_TIME,
        )
    )

    ax.grid(True, alpha=0.3)
    ax.legend()

    ax.figure.tight_layout()


def create_plot(
    times,
    values,
    title,
    ylabel,
    line_label,
    color,
    output_path,
):
    """Create and save one separate graph."""

    plot_times, plot_values = prepare_plot_data(
        times,
        values,
    )

    fig, ax = plt.subplots(figsize=(11, 5))

    ax.plot(
        plot_times,
        plot_values,
        linewidth=1.4,
        color=color,
        label=line_label,
    )

    add_pulsing_marker(ax, plot_times)

    ax.set_title(title)
    ax.set_ylabel(ylabel)

    format_time_axis(ax)

    fig.savefig(
        output_path,
        dpi=180,
        bbox_inches="tight",
    )

    print(f"Plot saved to:\n{output_path}\n")


def main():
    script_folder = Path(__file__).resolve().parent
    log_folder = script_folder / "tec_temperature_logs"

    if not log_folder.exists():
        raise FileNotFoundError(
            "Temperature-log folder does not exist:\n"
            f"{log_folder}"
        )

    latest_log = find_latest_log(log_folder)

    print(f"Latest log selected:\n{latest_log}\n")

    data = load_log_data(latest_log)

    # Temperature is always plotted.
    create_plot(
        times=data["temperature_times"],
        values=data["temperatures"],
        title="TEC CH1 object temperature",
        ylabel="Temperature (°C)",
        line_label="Object temperature",
        color="#2369A1",
        output_path=(
            log_folder / "latest_tec_temperature_plot.png"
        ),
    )

    peltier_detected = (
        data["has_current"] or data["has_voltage"]
    )

    if peltier_detected:
        print(
            "Peltier monitoring columns detected. "
            "Creating separate Peltier graphs.\n"
        )

        if data["has_current"]:
            create_plot(
                times=data["current_times"],
                values=data["currents"],
                title="TEC output current",
                ylabel="Current (A)",
                line_label="TEC output current",
                color="#D2691E",
                output_path=(
                    log_folder / "latest_tec_current_plot.png"
                ),
            )
        else:
            print(
                "No usable TEC output-current data were found.\n"
            )

        if data["has_voltage"]:
            create_plot(
                times=data["voltage_times"],
                values=data["voltages"],
                title="TEC output voltage",
                ylabel="Voltage (V)",
                line_label="TEC output voltage",
                color="#27864A",
                output_path=(
                    log_folder / "latest_tec_voltage_plot.png"
                ),
            )
        else:
            print(
                "No usable TEC output-voltage data were found.\n"
            )

    else:
        print(
            "Temperature-only log detected. "
            "No Peltier graphs were created.\n"
        )

    print(
        f"Log begins: {data['temperature_times'][0]}\n"
        f"Log ends:   {data['temperature_times'][-1]}\n"
        "Pulsing stopped: 19 July 2026 at 17:51:01 BST"
    )

    # Display all figures together.
    plt.show()


if __name__ == "__main__":
    main()