import csv
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mecom import MeComSerial


# ---------------- USER SETTINGS ----------------

COM_PORT = "COM10"

SAMPLE_INTERVAL_S = 5.0
RECONNECT_DELAY_S = 5.0

OUTPUT_DIRECTORY = Path("tec_temperature_logs")

# Choose:
# "ask"                       -> ask when the program starts
# "temperature_only"          -> log only object temperature
# "temperature_and_peltier"   -> log temperature, current and voltage
LOG_MODE = "ask"

# Meerstetter monitoring parameter IDs
OBJECT_TEMPERATURE_ID = 1000
OUTPUT_CURRENT_ID = 1020
OUTPUT_VOLTAGE_ID = 1021

# TEC-1091 has one TEC control channel.
TEC_CHANNEL = 1

UK_TIME = ZoneInfo("Europe/London")

# ------------------------------------------------


def choose_log_mode() -> str:
    """Return the selected logging mode."""

    valid_modes = {
        "temperature_only",
        "temperature_and_peltier",
    }

    if LOG_MODE in valid_modes:
        return LOG_MODE

    if LOG_MODE != "ask":
        raise ValueError(
            "LOG_MODE must be 'ask', 'temperature_only', "
            "or 'temperature_and_peltier'."
        )

    while True:
        print("\nChoose logging mode:")
        print("1: Temperature only")
        print("2: Temperature + Peltier current and voltage")

        choice = input("Enter 1 or 2: ").strip()

        if choice == "1":
            return "temperature_only"

        if choice == "2":
            return "temperature_and_peltier"

        print("Invalid choice. Enter 1 or 2.")


def close_session(session):
    """Close the TEC serial connection if one exists."""

    if session is not None:
        try:
            session.stop()
        except Exception:
            pass


def read_parameter(session, address, parameter_id):
    """Read one parameter from the TEC-1091's only channel."""

    return session.get_parameter(
        parameter_id=parameter_id,
        address=address,
        parameter_instance=TEC_CHANNEL,
    )


def main():
    mode = choose_log_mode()

    include_peltier_data = (
        mode == "temperature_and_peltier"
    )

    OUTPUT_DIRECTORY.mkdir(
        parents=True,
        exist_ok=True,
    )

    start_wall_time = datetime.now(UK_TIME)
    start_monotonic_time = time.monotonic()

    if include_peltier_data:
        filename_prefix = "tec_temperature_peltier"
    else:
        filename_prefix = "tec_temperature"

    filename = OUTPUT_DIRECTORY / (
        f"{filename_prefix}_"
        f"{start_wall_time.strftime('%Y%m%d_%H%M%S')}.csv"
    )

    columns = [
        "wall_time",
        "elapsed_s",
        "object_temperature_C",
    ]

    if include_peltier_data:
        columns.extend([
            "tec_output_current_A",
            "tec_output_voltage_V",
        ])

    columns.append("read_status")

    session = None
    address = None

    print(f"\nLogging mode: {mode}")
    print(f"Logging to:\n{filename.resolve()}")
    print("Press Ctrl+C to stop.\n")

    try:
        with filename.open(
            mode="w",
            newline="",
            encoding="utf-8",
            buffering=1,
        ) as csv_file:

            writer = csv.DictWriter(
                csv_file,
                fieldnames=columns,
            )

            writer.writeheader()
            csv_file.flush()

            while True:
                cycle_start = time.monotonic()

                try:
                    # Connect or reconnect when necessary.
                    if session is None:
                        print(f"Connecting on {COM_PORT}...")

                        session = MeComSerial(
                            serialport=COM_PORT
                        )

                        address = session.identify()

                        print(
                            f"Connected to MeCom address "
                            f"{address}."
                        )

                    # Parameter 1000:
                    # measured temperature of the object NTC.
                    object_temperature = read_parameter(
                        session,
                        address,
                        OBJECT_TEMPERATURE_ID,
                    )

                    output_current = None
                    output_voltage = None

                    if include_peltier_data:
                        # Parameter 1020:
                        # actual TEC output current.
                        output_current = read_parameter(
                            session,
                            address,
                            OUTPUT_CURRENT_ID,
                        )

                        # Parameter 1021:
                        # actual TEC output voltage.
                        output_voltage = read_parameter(
                            session,
                            address,
                            OUTPUT_VOLTAGE_ID,
                        )

                    wall_time = datetime.now(
                        UK_TIME
                    ).isoformat(
                        timespec="milliseconds"
                    )

                    elapsed_s = (
                        time.monotonic()
                        - start_monotonic_time
                    )

                    row = {
                        "wall_time": wall_time,
                        "elapsed_s": f"{elapsed_s:.3f}",
                        "object_temperature_C": (
                            f"{object_temperature:.6f}"
                        ),
                        "read_status": "OK",
                    }

                    if include_peltier_data:
                        row.update({
                            "tec_output_current_A": (
                                f"{output_current:.6f}"
                            ),
                            "tec_output_voltage_V": (
                                f"{output_voltage:.6f}"
                            ),
                        })

                    writer.writerow(row)
                    csv_file.flush()

                    if include_peltier_data:
                        print(
                            f"{wall_time}  "
                            f"T = {object_temperature:.4f} °C  "
                            f"I = {output_current:+.4f} A  "
                            f"V = {output_voltage:.4f} V"
                        )
                    else:
                        print(
                            f"{wall_time}  "
                            f"T = {object_temperature:.4f} °C"
                        )

                except Exception as error:
                    wall_time = datetime.now(
                        UK_TIME
                    ).isoformat(
                        timespec="milliseconds"
                    )

                    elapsed_s = (
                        time.monotonic()
                        - start_monotonic_time
                    )

                    error_message = (
                        f"{type(error).__name__}: {error}"
                    ).replace("\n", " ")

                    # Create a blank row with the correct columns.
                    error_row = {
                        column: ""
                        for column in columns
                    }

                    error_row.update({
                        "wall_time": wall_time,
                        "elapsed_s": f"{elapsed_s:.3f}",
                        "read_status": error_message,
                    })

                    writer.writerow(error_row)
                    csv_file.flush()

                    print(
                        f"{wall_time}  "
                        f"Read failed: {error_message}"
                    )

                    close_session(session)

                    session = None
                    address = None

                    time.sleep(RECONNECT_DELAY_S)
                    continue

                # Keep measurements approximately five seconds apart,
                # including the time spent communicating with the TEC.
                cycle_duration = (
                    time.monotonic() - cycle_start
                )

                remaining_time = (
                    SAMPLE_INTERVAL_S - cycle_duration
                )

                if remaining_time > 0:
                    time.sleep(remaining_time)

    except KeyboardInterrupt:
        print("\nLogging stopped by user.")

    finally:
        close_session(session)

        print(
            f"CSV saved to:\n{filename.resolve()}"
        )


if __name__ == "__main__":
    main()