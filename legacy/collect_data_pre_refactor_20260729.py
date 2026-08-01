from moku.instruments import Oscilloscope
import matplotlib.pyplot as plt
import numpy as np
import time
from pathlib import Path

MOKU_IP = "MokuGo-008058"

#Pulse parameters
TRIGGER_LEVEL = 0.6 #V
PULSE_AMPLITUDE = 5 #V, Note: the EOM receives PULSE_AMPLITUDE / 5 V due to impedance mismatch
PULSE_FREQUENCY = 1e2
DUTY_CYCLE = 0.1 #%
PULSE_WIDTH = DUTY_CYCLE / (PULSE_FREQUENCY * 100)

#Experiment Parameters
EXPERIMENT_LENGTH = 48 * 3600
SAVE_PERIOD = 5 * 60 #Save every five minutes
SAMPLE_PERIOD = 1.0 #Sample max and min voltage every second
FRAMES_PER_SECOND = 5 #Average over 5 frames every second to get sample
PRINT_EVERY_K_SAMPLES = 60
MAX_SAMPLES = int(EXPERIMENT_LENGTH / SAMPLE_PERIOD * 1.25)

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
        edge_time=100e-9
    )

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

    print("Pulse still running. Press Ctrl+C to stop... PLEASE CONSULT EXPERIMENT RUNNER BEFORE SHUTTING DOWN. DO NOT TURN ON THE LIGHTS. DO NOT OPEN CURTAINS. NO LAMP")

    #Entire experiment
    i = 0
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

            t = np.array(data["time"])
            voltage = np.array(data["ch1"])

            dt = t[1] - t[0]
            data_length = len(t)

            zero_point_index = np.argmin(np.abs(t - 0.0)) #Index of time array at pulse trigger
            pulse_end_index = np.argmin(np.abs(t - PULSE_WIDTH))
            num_samples_in_edge = int(100e-9/dt) + 1

            #Exclude num_samples_in_edge samples before/after each pulse edges in calculating baseline and peak voltages
            baseline_before = voltage[:zero_point_index - num_samples_in_edge]
            baseline_after = voltage[pulse_end_index + num_samples_in_edge:]

            baseline_voltage = (np.sum(baseline_before) + np.sum(baseline_after)) / (len(baseline_before) + len(baseline_after))
            peak_voltage = np.mean(voltage[zero_point_index + num_samples_in_edge:pulse_end_index - num_samples_in_edge])
            per_pulse_voltage_tracking[j] = np.array([baseline_voltage, peak_voltage])

            j += 1
        
        if j == 0:
            continue

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

finally:
    print("Experiment Finished")

    if "voltage_samples" in locals() and "filename" in locals() and i > 0:
        np.savetxt(
            filename,
            voltage_samples[:i],
            delimiter=",",
            header="wall_time,minimum_voltage,maximum_voltage",
            comments=""
        )
        print("Final saved ", i, "samples to", filename)

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
