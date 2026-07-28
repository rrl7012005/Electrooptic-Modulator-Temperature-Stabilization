from datetime import datetime
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd


# All timestamps are converted to naive UK local time before plotting.
PULSING_STOPPED = datetime(2026, 7, 19, 17, 51, 1)

LAB_FILENAME = "Lab_Temp.csv"

# These match both:
# tec_temperature_20260721_....csv
# tec_temperature_peltier_20260721_....csv
TEC_FILE_PATTERNS = (
    "tec_temperature_*.csv",
    "tec_temperature_peltier_*.csv",
)

LAB_RESAMPLE_INTERVAL = "2min"
TEC_RESAMPLE_INTERVAL = "1min"


def find_latest_tec_log(log_folder: Path) -> Path:
    """
    Find the newest TEC logger CSV.

    Both temperature-only and temperature-plus-Peltier files are accepted.
    Current and voltage columns are ignored by this plotting script.
    """

    tec_files = set()

    for pattern in TEC_FILE_PATTERNS:
        tec_files.update(log_folder.glob(pattern))

    if not tec_files:
        raise FileNotFoundError(
            "No TEC temperature logs were found in:\n"
            f"{log_folder}\n\n"
            f"Accepted patterns:\n{TEC_FILE_PATTERNS}"
        )

    # Select the most recently modified log.
    return max(
        tec_files,
        key=lambda path: path.stat().st_mtime,
    )


def load_lab_temperature(csv_path: Path) -> pd.DataFrame:
    """Load the three Grafana laboratory-temperature channels."""

    data = pd.read_csv(csv_path)
    data.columns = data.columns.str.strip()

    if "Time" not in data.columns:
        raise ValueError(
            "Lab CSV has no 'Time' column.\n"
            f"Available columns:\n{list(data.columns)}"
        )

    # The first three columns after Time are the laboratory sensors.
    sensor_columns = list(data.columns[1:4])

    if len(sensor_columns) != 3:
        raise ValueError(
            "Expected exactly three laboratory-temperature columns "
            "after the Time column."
        )

    # Grafana raw Time is Unix time in milliseconds.
    data["Time"] = pd.to_datetime(
        pd.to_numeric(
            data["Time"],
            errors="coerce",
        ),
        unit="ms",
        utc=True,
    ).dt.tz_convert(
        "Europe/London"
    ).dt.tz_localize(
        None
    )

    # This supports either:
    # 22.684735
    # or
    # 22.7 °C
    for column in sensor_columns:
        cleaned_values = (
            data[column]
            .astype(str)
            .str.replace("°C", "", regex=False)
            .str.strip()
        )

        data[column] = pd.to_numeric(
            cleaned_values,
            errors="coerce",
        )

    data = (
        data.dropna(subset=["Time"])
        .sort_values("Time")
        .set_index("Time")
    )

    # Resampling inserts NaNs across missing recording periods.
    # This prevents long artificial diagonal lines across data gaps.
    data = data[sensor_columns].resample(
        LAB_RESAMPLE_INTERVAL
    ).mean()

    data = data.rename(
        columns={
            sensor_columns[0]: "Lab sensor 0",
            sensor_columns[1]: "Lab sensor 1",
            sensor_columns[2]: "Lab sensor 2",
        }
    )

    if data.dropna(how="all").empty:
        raise ValueError(
            "No valid laboratory-temperature measurements were found."
        )

    return data


def load_tec_temperature(csv_path: Path) -> pd.DataFrame:
    """
    Load only object temperature from either TEC logger format.

    Extra Peltier current and voltage columns are deliberately ignored.
    """

    data = pd.read_csv(csv_path)
    data.columns = data.columns.str.strip()

    required_columns = {
        "wall_time",
        "object_temperature_C",
        "read_status",
    }

    missing_columns = required_columns - set(data.columns)

    if missing_columns:
        raise ValueError(
            f"TEC CSV is missing columns: {sorted(missing_columns)}\n"
            f"Available columns:\n{list(data.columns)}"
        )

    peltier_columns = {
        "tec_output_current_A",
        "tec_output_voltage_V",
        "output_current_A",
        "output_voltage_V",
    }

    detected_peltier_columns = (
        peltier_columns & set(data.columns)
    )

    if detected_peltier_columns:
        print(
            "Temperature-and-Peltier log detected.\n"
            "The following Peltier columns will be ignored by this plot:\n"
            f"{sorted(detected_peltier_columns)}\n"
        )
    else:
        print("Temperature-only TEC log detected.\n")

    # Keep only successful controller reads.
    data = data[
        data["read_status"].astype(str).str.strip() == "OK"
    ].copy()

    # Logger timestamps look like:
    # 2026-07-19T17:51:01.123+01:00
    #
    # Convert to UTC first, then back into UK local time, then remove
    # timezone information so it matches the lab-temperature timestamps.
    data["Time"] = pd.to_datetime(
        data["wall_time"],
        errors="coerce",
        utc=True,
    ).dt.tz_convert(
        "Europe/London"
    ).dt.tz_localize(
        None
    )

    data["TEC object temperature"] = pd.to_numeric(
        data["object_temperature_C"],
        errors="coerce",
    )

    data = (
        data.dropna(
            subset=[
                "Time",
                "TEC object temperature",
            ]
        )
        .sort_values("Time")
        .set_index("Time")
    )

    # Average the five-second TEC samples into one-minute values.
    data = data[
        ["TEC object temperature"]
    ].resample(
        TEC_RESAMPLE_INTERVAL
    ).mean()

    if data.dropna(how="all").empty:
        raise ValueError(
            "No successful TEC-temperature measurements were found."
        )

    return data


def main():
    script_folder = Path(__file__).resolve().parent
    log_folder = script_folder / "tec_temperature_logs"

    if not log_folder.exists():
        raise FileNotFoundError(
            f"Log folder does not exist:\n{log_folder}"
        )

    lab_path = log_folder / LAB_FILENAME

    if not lab_path.exists():
        raise FileNotFoundError(
            "Could not find the laboratory-temperature CSV:\n"
            f"{lab_path}"
        )

    latest_tec_path = find_latest_tec_log(log_folder)

    print(f"Laboratory log:\n{lab_path}\n")
    print(f"Latest TEC log:\n{latest_tec_path}\n")

    lab_data = load_lab_temperature(lab_path)
    tec_data = load_tec_temperature(latest_tec_path)

    fig, ax = plt.subplots(figsize=(13, 6))

    # TEC temperature is darker and thicker so it remains easy to identify.
    plot_styles = {
        "TEC object temperature": {
            "color": "#9C2F2F",
            "linewidth": 2.2,
            "alpha": 1.0,
            "zorder": 4,
        },
        "Lab sensor 0": {
            "color": "#2A9D8F",
            "linewidth": 1.2,
            "alpha": 0.9,
            "zorder": 2,
        },
        "Lab sensor 1": {
            "color": "#E9A23B",
            "linewidth": 1.2,
            "alpha": 0.9,
            "zorder": 2,
        },
        "Lab sensor 2": {
            "color": "#457B9D",
            "linewidth": 1.2,
            "alpha": 0.9,
            "zorder": 2,
        },
    }

    for column in lab_data.columns:
        ax.plot(
            lab_data.index,
            lab_data[column],
            label=column,
            **plot_styles[column],
        )

    ax.plot(
        tec_data.index,
        tec_data["TEC object temperature"],
        label="TEC object temperature",
        **plot_styles["TEC object temperature"],
    )

    valid_lab_data = lab_data.dropna(how="all")
    valid_tec_data = tec_data.dropna(how="all")

    first_time = min(
        valid_lab_data.index.min(),
        valid_tec_data.index.min(),
    )

    last_time = max(
        valid_lab_data.index.max(),
        valid_tec_data.index.max(),
    )

    if first_time <= PULSING_STOPPED <= last_time:
        ax.axvline(
            PULSING_STOPPED,
            color="#333333",
            linestyle="--",
            linewidth=1.5,
            zorder=5,
            label="Pulsing stopped: 19 Jul 17:51:01",
        )

        ax.annotate(
            "Pulsing stopped\n19 Jul 17:51:01",
            xy=(PULSING_STOPPED, 1),
            xycoords=("data", "axes fraction"),
            xytext=(7, -8),
            textcoords="offset points",
            ha="left",
            va="top",
            fontsize=9,
        )
    else:
        print(
            "Warning: pulsing stopped on 19 July 2026 at "
            "17:51:01 BST, but that time is outside the "
            "combined plotted range."
        )

    ax.set_title("EOM and laboratory temperature")
    ax.set_xlabel("Time")
    ax.set_ylabel("Temperature (°C)")

    ax.xaxis.set_major_formatter(
        mdates.DateFormatter("%d %b\n%H:%M")
    )

    ax.grid(True, alpha=0.25)

    ax.legend(
        loc="best",
        ncol=2,
        frameon=True,
    )

    fig.tight_layout()

    output_path = (
        log_folder / "combined_lab_and_tec_temperature.png"
    )

    fig.savefig(
        output_path,
        dpi=200,
        bbox_inches="tight",
    )

    print(f"Lab data start: {valid_lab_data.index.min()}")
    print(f"Lab data end:   {valid_lab_data.index.max()}")
    print(f"TEC data start: {valid_tec_data.index.min()}")
    print(f"TEC data end:   {valid_tec_data.index.max()}")
    print("Pulsing stopped: 19 July 2026 at 17:51:01 BST")
    print(f"Plot saved to:\n{output_path}")

    plt.show()


if __name__ == "__main__":
    main()