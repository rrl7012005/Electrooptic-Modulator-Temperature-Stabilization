from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd


def main():
    # Folder containing this Python script.
    script_folder = Path(__file__).resolve().parent

    # Lab_Temp.csv is inside the same subfolder as the TEC logs.
    log_folder = script_folder / "tec_temperature_logs"
    csv_path = log_folder / "Lab_Temp.csv"

    if not csv_path.exists():
        raise FileNotFoundError(
            f"Could not find the laboratory-temperature file:\n{csv_path}"
        )

    # Read the CSV.
    data = pd.read_csv(csv_path)

    if "Time" not in data.columns:
        raise ValueError(
            f"Could not find the 'Time' column.\n"
            f"Available columns:\n{list(data.columns)}"
        )

    # The three columns after Time are the three lab-temperature sensors.
    sensor_columns = list(data.columns[1:4])

    if len(sensor_columns) < 3:
        raise ValueError(
            "The file does not contain three temperature-sensor columns."
        )

    # Convert the Time text into actual datetime values.
    # Convert raw Unix wall time in seconds into UK local time.
    data["Time"] = pd.to_datetime(
        pd.to_numeric(data["Time"], errors="coerce"),
        unit="ms",
        utc=True,
    ).dt.tz_convert("Europe/London").dt.tz_localize(None)

    # Convert entries such as "22.7 °C" into numeric values such as 22.7.
    for column in sensor_columns:

        data[column] = pd.to_numeric(
            data[column],
            errors="coerce",
        )

    # Remove rows whose timestamp could not be read.
    data = data.dropna(subset=["Time"])

    if data.empty:
        raise ValueError("No valid laboratory-temperature rows were found.")

    # Sort chronologically in case the CSV is not already ordered.
    data = data.sort_values("Time")

    # Simpler legend names.
    sensor_labels = [
        "Lab sensor 0",
        "Lab sensor 1",
        "Lab sensor 2",
    ]

    fig, ax = plt.subplots(figsize=(11, 5))

    for column, label in zip(sensor_columns, sensor_labels):
        ax.plot(
            data["Time"],
            data[column],
            linewidth=1.2,
            label=label,
        )

    ax.set_title("Laboratory temperature")
    ax.set_xlabel("Time")
    ax.set_ylabel("Temperature (°C)")

    # Include both date and clock time because the log may span several days.
    ax.xaxis.set_major_formatter(
        mdates.DateFormatter("%d %b\n%H:%M")
    )

    ax.grid(True, alpha=0.3)
    ax.legend()

    fig.tight_layout()

    output_path = log_folder / "lab_temperature_plot.png"
    fig.savefig(output_path, dpi=180)

    print(f"Loaded:\n{csv_path}")
    print(f"Start time: {data['Time'].min()}")
    print(f"End time:   {data['Time'].max()}")
    print(f"Plot saved to:\n{output_path}")

    plt.show()


if __name__ == "__main__":
    main()