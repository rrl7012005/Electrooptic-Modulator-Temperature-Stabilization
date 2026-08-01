import matplotlib.pyplot as plt
import numpy as np
import math
import os
import sys
import time
from pathlib import Path

SRC_DIRECTORY = Path(__file__).resolve().parent / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.moku.pulse_sequences import (
    TraditionalPulse,
    validate_traditional_pulse,
)

MOKU_IP = "MokuGo-008058"

#Pulse parameters
TRIGGER_LEVEL = 0.6 #V
PULSE_AMPLITUDE = 5 #V, Note: the EOM receives PULSE_AMPLITUDE / 5 V due to impedance mismatch
PULSE_FREQUENCY = 1e2
DUTY_CYCLE = 0.1 #%
PULSE_EDGE_TIME = 100e-9
PULSE_CONFIGURATION = validate_traditional_pulse(
    TraditionalPulse(
        low_level_v=0.0,
        high_level_v=PULSE_AMPLITUDE,
        frequency_hz=PULSE_FREQUENCY,
        duty_cycle_percent=DUTY_CYCLE,
        edge_time_s=PULSE_EDGE_TIME,
    )
)
PULSE_WIDTH = PULSE_CONFIGURATION.pulse_width_s

#Experiment Parameters
DEFAULT_EXPERIMENT_LENGTH = 48 * 3600
EXPERIMENT_LENGTH = float(
    os.environ.get(
        "EOM_MOKU_EXPERIMENT_LENGTH_SECONDS",
        DEFAULT_EXPERIMENT_LENGTH,
    )
)
if not math.isfinite(EXPERIMENT_LENGTH) or EXPERIMENT_LENGTH <= 0:
    raise ValueError("EXPERIMENT_LENGTH must be a finite value above zero")
SAVE_PERIOD = 5 * 60 #Save every five minutes
SAMPLE_PERIOD = 1.0 #Sample max and min voltage every second
FRAMES_PER_SECOND = 5 #Average over 5 frames every second to get sample
PRINT_EVERY_K_SAMPLES = 60
MAX_SAMPLES = int(EXPERIMENT_LENGTH / SAMPLE_PERIOD * 1.25)
MAX_CONSECUTIVE_EMPTY_SAMPLE_PERIODS = 30

def configure_moku(trigger_level):
    osc.set_frontend(1, "1MOhm", "DC", "10Vpp")

    osc.set_sources([
        {"channel": 1, "source": "Input1"},
        {"channel": 2, "source": "Output2"}
    ])

    #Examine 90us window around pulse
    osc.set_timebase(-45e-6, 45e-6, max_length=16384)
    osc.set_trigger(mode="Normal", type="Edge", source="Input1", level=trigger_level, edge="Rising")

def generate_pulse(amplitude, frequency, pulse_width):
    osc.generate_waveform(
        channel=2,
        type="Pulse",
        amplitude=amplitude,
        offset=amplitude / 2,
        frequency=frequency,
        pulse_width=pulse_width,
        edge_time=PULSE_EDGE_TIME,
        strict=True,
    )

def signal_master_ready(output_file):
    """Tell run_experiment.py that Moku and the output file are ready."""
    ready_file = os.environ.get("EOM_READY_FILE")
    if ready_file:
        ready_path = Path(ready_file)
        temporary_path = ready_path.with_suffix(ready_path.suffix + ".tmp")
        temporary_path.write_text(
            str(Path(output_file).resolve()),
            encoding="utf-8",
        )
        temporary_path.replace(ready_path)

def measure_frame_levels(data):
    """Return baseline and pulse voltages from one valid Moku frame."""
    t = np.asarray(data["time"], dtype=float)
    voltage = np.asarray(data["ch1"], dtype=float)

    if t.ndim != 1 or voltage.ndim != 1 or len(t) != len(voltage):
        raise ValueError("time and ch1 must be equal-length 1D arrays")
    if len(t) < 2 or not np.all(np.isfinite(t)) or not np.all(np.isfinite(voltage)):
        raise ValueError("trace contains too few points or non-finite values")

    time_steps = np.diff(t)
    if not np.all(time_steps > 0):
        raise ValueError("trace time values are not strictly increasing")

    dt = float(np.median(time_steps))
    zero_point_index = int(np.argmin(np.abs(t)))
    pulse_end_index = int(np.argmin(np.abs(t - PULSE_WIDTH)))
    num_samples_in_edge = max(1, int(np.ceil(PULSE_EDGE_TIME / dt)))

    baseline_before = voltage[:zero_point_index - num_samples_in_edge]
    baseline_after = voltage[pulse_end_index + num_samples_in_edge:]
    peak_voltage_points = voltage[
        zero_point_index + num_samples_in_edge:
        pulse_end_index - num_samples_in_edge
    ]

    baseline_count = len(baseline_before) + len(baseline_after)
    if baseline_count == 0 or len(peak_voltage_points) == 0:
        raise ValueError("trace window does not contain usable baseline and pulse regions")

    baseline_voltage = (
        np.sum(baseline_before) + np.sum(baseline_after)
    ) / baseline_count
    peak_voltage = np.mean(peak_voltage_points)

    return float(baseline_voltage), float(peak_voltage)


def disable_moku_output_now():
    """Connect solely to switch Moku Output 2 off, then release ownership."""
    cleanup_osc = None
    try:
        print(f"Connecting to {MOKU_IP} for emergency Output 2 shutdown...")
        cleanup_osc = Oscilloscope(MOKU_IP, force_connect=True)
        cleanup_osc.generate_waveform(channel=2, type="Off")
        print("Moku Output 2 is OFF.")
    finally:
        if cleanup_osc is not None:
            cleanup_osc.relinquish_ownership()


if sys.argv[1:] == ["--print-experiment-length"]:
    print(f"{EXPERIMENT_LENGTH:.9f}")
    raise SystemExit(0)
if sys.argv[1:] not in ([], ["--disable-output"]):
    raise SystemExit(
        "Supported options are --disable-output and --print-experiment-length"
    )

try:
    from moku.instruments import Oscilloscope
except ModuleNotFoundError as error:
    raise ModuleNotFoundError(
        "The Moku Python package is not installed in this environment."
    ) from error

if sys.argv[1:] == ["--disable-output"]:
    disable_moku_output_now()
    raise SystemExit(0)


osc = None
filename = None
voltage_samples = None
i = 0
interrupted = False

try:
    osc = Oscilloscope(MOKU_IP, force_connect=True)

    configure_moku(TRIGGER_LEVEL)

    run_folder = Path("Experiment Results") / "moku_pulse_runs" / time.strftime("run_%Y%m%d_%H%M%S")
    run_folder.mkdir(parents=True, exist_ok=False)
    filename = run_folder / "raw_photovoltage_tracking.csv"

    #Generate RF pulses
    generate_pulse(PULSE_AMPLITUDE, PULSE_FREQUENCY, PULSE_WIDTH)

    #Set timers
    start_time = time.monotonic()
    last_save_time = time.monotonic()

    voltage_samples = np.zeros((MAX_SAMPLES, 3))
    np.savetxt(
        filename,
        voltage_samples[:0],
        delimiter=",",
        header="wall_time,minimum_voltage,maximum_voltage",
        comments=""
    )

    print("Pulse still running. Press Ctrl+C to stop... PLEASE CONSULT EXPERIMENT RUNNER BEFORE SHUTTING DOWN. DO NOT TURN ON THE LIGHTS. DO NOT OPEN CURTAINS. NO LAMP")

    #Entire experiment
    consecutive_empty_sample_periods = 0
    while (time.monotonic() - start_time < EXPERIMENT_LENGTH):
        previous_sample_time = time.monotonic()
        per_pulse_voltage_tracking = np.zeros((FRAMES_PER_SECOND, 2))

        #Each pulse
        j = 0
        while (time.monotonic() - previous_sample_time < SAMPLE_PERIOD):
            if j >= FRAMES_PER_SECOND:
                break
            
            try:
                data = osc.get_data(
                    wait_reacquire=True,
                    wait_complete=True,
                    timeout=1.0 #Wait up to a second
                )
            except Exception as e:
                print("No new triggered frame: ", e)
                time.sleep(0.1)
                continue

            try:
                baseline_voltage, peak_voltage = measure_frame_levels(data)
            except (KeyError, TypeError, ValueError) as e:
                print("Discarding invalid Moku frame:", e)
                continue

            per_pulse_voltage_tracking[j] = np.array([baseline_voltage, peak_voltage])

            j += 1
        
        if j == 0:
            consecutive_empty_sample_periods += 1
            print(
                "No valid Moku frames in sample period "
                f"({consecutive_empty_sample_periods}/"
                f"{MAX_CONSECUTIVE_EMPTY_SAMPLE_PERIODS})."
            )
            if (
                consecutive_empty_sample_periods
                >= MAX_CONSECUTIVE_EMPTY_SAMPLE_PERIODS
            ):
                raise RuntimeError(
                    "Moku produced no valid frames for too many consecutive "
                    "sample periods"
                )
            continue

        consecutive_empty_sample_periods = 0

        remaining_sample_time = (
            SAMPLE_PERIOD
            - (time.monotonic() - previous_sample_time)
        )

        if remaining_sample_time > 0:
            time.sleep(remaining_sample_time)

        average_levels = np.mean(per_pulse_voltage_tracking[:j], axis=0)
        minimum_voltage_sample = average_levels[0]
        maximum_voltage_sample = average_levels[1]

        if i >= len(voltage_samples):
            print("Sample array full; stopping experiment. ")
            break

        #Stores voltage values corresponding to time at the end of sample period
        voltage_samples[i] = np.array([time.time(), minimum_voltage_sample, maximum_voltage_sample])
        i += 1

        if i == 1:
            np.savetxt(
                filename,
                voltage_samples[:i],
                delimiter=",",
                header="wall_time,minimum_voltage,maximum_voltage",
                comments=""
            )
            signal_master_ready(filename)

        if i % PRINT_EVERY_K_SAMPLES == 0:
            print(
                time.strftime("%H:%M:%S"),
                "samples =", i,
                "min =", minimum_voltage_sample,
                "max =", maximum_voltage_sample,
                "frames =", j
            )

        if time.monotonic() - last_save_time >= SAVE_PERIOD:
            np.savetxt(
                filename,
                voltage_samples[:i],
                delimiter=",",
                header="wall_time,minimum_voltage,maximum_voltage",
                comments=""
            )
            last_save_time = time.monotonic()
            print("Saved", i, "samples to", filename)

            #Upload plots of most recent pulse

            plt.plot(data["time"], data["ch1"], label="Input1 physical measurement")
            plt.plot(data["time"], data["ch2"], label="Output2 internal reference")
            plt.xlabel("Time / s")
            plt.ylabel("Voltage / V")
            plt.grid(True)
            plt.legend()
            trace_filename = run_folder / f"trace_time_{voltage_samples[i-1][0]:.0f}.png"
            plt.savefig(trace_filename, dpi=300, bbox_inches="tight")
            plt.close()

    np.savetxt(
        filename,
        voltage_samples[:i],
        delimiter=",",
        header="wall_time,minimum_voltage,maximum_voltage",
        comments=""
    )

except KeyboardInterrupt:
    interrupted = True
    print("Experiment stop requested; saving final Moku data.")

finally:
    print("Experiment Finished")

    if voltage_samples is not None and filename is not None and i > 0:
        np.savetxt(
            filename,
            voltage_samples[:i],
            delimiter=",",
            header="wall_time,minimum_voltage,maximum_voltage",
            comments=""
        )
        print("Final saved ", i, "samples to", filename)

    if osc is not None:
        try:
            osc.generate_waveform(channel=2, type="Off")
            print("Output2 turned off")
        except Exception as e:
            print("Could not turn Output2 off via API:", e)

        try:
            osc.relinquish_ownership()
            print("Ownership released")
        except Exception as e:
            print("Could not relinquish ownership:", e)

if interrupted:
    raise SystemExit(130)
