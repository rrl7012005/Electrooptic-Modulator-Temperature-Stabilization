import csv
import os
import sys
import time
import pickle
from pathlib import Path

import numpy as np

from linien_client.device import Device
from linien_client.connection import LinienClient

SRC_DIRECTORY = Path(__file__).resolve().parent / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.output_layout import (
    append_output_requested,
    component_output_file,
    resolve_component_directory,
)


# =========================
# User settings
# =========================

RP_HOST = "169.254.207.199"
RP_USER = "root"
RP_PASSWORD = os.environ.get("LINIEN_PASSWORD")

SAMPLE_PERIOD = 1.0          # seconds
PRINT_TO_CONSOLE = True

# Linien GUI scaling:
# error_signal_raw / 8192 -> GUI-scaled error signal, unitless/demodulated units
# control_signal_raw / 8192 -> Red Pitaya fast output voltage in volts
SCALE = 1.0 / 8192.0

# Linien's FPGA signal selector for fast_a_x: FAST IN 1 before demodulation.
FAST_IN_1_SELECTOR = 1


# =========================
# Output file
# =========================

_, run_folder = resolve_component_directory(
    Path(__file__).resolve().parent,
    "lock",
)
filename = component_output_file(run_folder / "RP_voltage_tracking.csv")
filename.parent.mkdir(parents=True, exist_ok=True)
append_existing_output = append_output_requested() and filename.is_file()


# =========================
# Connect to Linien
# =========================

if not RP_PASSWORD:
    raise RuntimeError(
        "Set the LINIEN_PASSWORD environment variable before running this script."
    )

dev = Device(
    host=RP_HOST,
    username=RP_USER,
    password=RP_PASSWORD,
)

client = LinienClient(dev)
# GUI/server should already be running, so only attach to existing server.
client.connect(autostart_server=False, use_parameter_cache=True)

print("Connected to Red Pitaya.")
print("Logging to:", filename)
print("Press Ctrl+C to stop.")

if client.parameters.control_channel.value == 0:
    print("Linien lock output is configured for FAST OUT 1.")
else:
    print(
        "WARNING: Linien is configured to send the lock signal to FAST OUT 2, "
        "not FAST OUT 1."
    )


def select_raw_photodiode_signal():
    """Route pre-demodulation FAST IN 1 to Linien's monitor capture channel."""
    client.control.exposed_pause_acquisition()
    try:
        client.control.exposed_set_csr_direct(
            "scopegen_adc_a_q_sel",
            FAST_IN_1_SELECTOR,
        )
    finally:
        client.control.exposed_continue_acquisition()


def signal_master_ready(output_file):
    """Tell run_experiment.py that Linien and the output file are ready."""
    ready_file = os.environ.get("EOM_READY_FILE")
    if ready_file:
        ready_path = Path(ready_file)
        temporary_path = ready_path.with_suffix(ready_path.suffix + ".tmp")
        temporary_path.write_text(
            str(Path(output_file).resolve()),
            encoding="utf-8",
        )
        temporary_path.replace(ready_path)


# =========================
# Logging loop
# =========================

interrupted = False

with open(filename, "a" if append_existing_output else "w", newline="") as f:
    writer = csv.writer(f)

    if not append_existing_output or filename.stat().st_size == 0:
        writer.writerow([
            "wall_time",
            "error_signal",
            "RP_lock_voltage",
            "photodiode_voltage",
        ])
        f.flush()

    was_locked = False
    master_ready_signalled = False

    try:
        while True:
            # Pull latest Linien parameter/plot data from the Red Pitaya server
            client.parameters.check_for_changed_parameters()

            is_locked = bool(client.parameters.lock.value)

            if is_locked and not was_locked:
                if client.parameters.dual_channel.value:
                    raise RuntimeError(
                        "Raw FAST IN 1 logging requires Linien dual channel mode "
                        "to be disabled."
                    )

                select_raw_photodiode_signal()
                was_locked = True
                print("Raw photodiode logging enabled from FAST IN 1.")

                time.sleep(SAMPLE_PERIOD)
                continue

            if not is_locked:
                was_locked = False

            # Decode Linien GUI plot buffer
            plot_data = pickle.loads(client.parameters.to_plot.value)

            # These are the same plotted arrays used by the GUI
            error_array = np.asarray(plot_data.get("error_signal", []), dtype=float)
            control_array = np.asarray(plot_data.get("control_signal", []), dtype=float)
            photodiode_array = np.asarray(
                plot_data.get("monitor_signal", []),
                dtype=float,
            )

            if (
                len(error_array) == 0
                or len(control_array) == 0
                or len(photodiode_array) == 0
            ):
                print(
                    "No error/control/photodiode data yet. Keys:",
                    list(plot_data.keys()),
                )
                time.sleep(SAMPLE_PERIOD)
                continue

            # Take latest point from each GUI buffer
            error_signal = float(error_array[-1]) * SCALE
            RP_lock_voltage = float(control_array[-1]) * SCALE

            RP_photodiode_voltage = float(photodiode_array[-1]) * SCALE

            wall_time = time.time()

            # Save one row
            writer.writerow([
                wall_time,
                error_signal,
                RP_lock_voltage,
                RP_photodiode_voltage,
            ])
            f.flush()

            if not master_ready_signalled:
                signal_master_ready(filename)
                master_ready_signalled = True

            # Optional live display
            if PRINT_TO_CONSOLE:
                print(
                    f"{time.strftime('%H:%M:%S')}  "
                    f"wall_time={wall_time:.3f}  "
                    f"error={error_signal:.6g}  "
                    f"lock_output={RP_lock_voltage:.6f} V  "
                    f"photodiode_signal={RP_photodiode_voltage:.6f} V"
                )

            time.sleep(SAMPLE_PERIOD)

    except KeyboardInterrupt:
        interrupted = True
        print("Stopped by user.")

print("Finished.")

if interrupted:
    raise SystemExit(130)
