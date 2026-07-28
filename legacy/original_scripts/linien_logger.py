import csv
import time
import pickle
from pathlib import Path

import numpy as np

from linien_client.device import Device
from linien_client.connection import LinienClient


# =========================
# User settings
# =========================

RP_HOST = "169.254.207.199"
RP_USER = "root"
RP_PASSWORD = "root"

SAMPLE_PERIOD = 1.0          # seconds
PRINT_TO_CONSOLE = True      # set False for overnight if you do not want printing

# Linien GUI scaling:
# error_signal_raw / 8192 -> GUI-scaled error signal, unitless/demodulated units
# control_signal_raw / 8192 -> Red Pitaya fast output voltage in volts
SCALE = 1.0 / 8192.0


# =========================
# Output file
# =========================

run_folder = Path("linien_control_logs") / time.strftime("run_%Y%m%d_%H%M%S")
run_folder.mkdir(parents=True, exist_ok=True)

filename = run_folder / "linien_error_lock_voltage_tracking.csv"


# =========================
# Connect to Linien
# =========================

dev = Device(
    host=RP_HOST,
    username=RP_USER,
    password=RP_PASSWORD,
)

client = LinienClient(dev)

# Safer for real experiment:
# GUI/server should already be running, so only attach to existing server.
client.connect(autostart_server=False, use_parameter_cache=True)

print("Connected to Linien.")
print("Logging to:", filename)
print("Press Ctrl+C to stop.")


# =========================
# Logging loop
# =========================

with open(filename, "w", newline="") as f:
    writer = csv.writer(f)

    writer.writerow([
        "wall_time",
        "error_signal",
        "lock_output_voltage_V",
    ])

    try:
        while True:
            # Pull latest Linien parameter/plot data from the Red Pitaya server
            client.parameters.check_for_changed_parameters()

            # Decode Linien GUI plot buffer
            plot_data = pickle.loads(client.parameters.to_plot.value)

            # These are the same plotted arrays used by the GUI
            error_array = np.asarray(plot_data.get("error_signal", []), dtype=float)
            control_array = np.asarray(plot_data.get("control_signal", []), dtype=float)

            if len(error_array) == 0 or len(control_array) == 0:
                print("No error/control data yet. Keys:", list(plot_data.keys()))
                time.sleep(SAMPLE_PERIOD)
                continue

            # Take latest point from each GUI buffer
            error_signal = float(error_array[-1]) * SCALE
            lock_output_voltage_V = float(control_array[-1]) * SCALE

            wall_time = time.time()

            # Save one row
            writer.writerow([
                wall_time,
                error_signal,
                lock_output_voltage_V,
            ])
            f.flush()

            # Optional live display
            if PRINT_TO_CONSOLE:
                print(
                    f"{time.strftime('%H:%M:%S')}  "
                    f"wall_time={wall_time:.3f}  "
                    f"error={error_signal:.6g}  "
                    f"lock_output={lock_output_voltage_V:.6f} V"
                )

            time.sleep(SAMPLE_PERIOD)

    except KeyboardInterrupt:
        print("Stopped by user.")

print("Finished.")