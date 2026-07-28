from moku.instruments import Oscilloscope
import matplotlib.pyplot as plt
import numpy as np
import time
from pathlib import Path

MOKU_IP = "MokuGo-008058"

osc = Oscilloscope(MOKU_IP, force_connect=True)

try:
    osc.set_frontend(1, "1MOhm", "DC", "10Vpp")

    osc.set_sources([
        {"channel": 1, "source": "Input1"},
        {"channel": 2, "source": "Output2"}
    ])

    osc.generate_waveform(
        channel=2,
        type="Pulse",
        amplitude=5,
        offset=2.5,
        frequency=1e2,
        pulse_width=10e-6,
        edge_time=100e-9
    )

    osc.set_timebase(-45e-6, 45e-6, max_length=16384)
    osc.set_trigger(mode="Normal", type="Edge", source="Input1", level=0.6, edge="Rising")

    #Set Timer
    experiment_length = 48 #hours
    experiment_length *= 3600 #seconds

    save_period = 5 * 60  # seconds
    last_save_time = time.monotonic()
    run_folder = Path("moku_overnight_runs") / time.strftime("run_%Y%m%d_%H%M%S")
    run_folder.mkdir(parents=True, exist_ok=True)

    filename = run_folder / "macro_voltage_tracking.csv"

    #Every second
    sample_period = 1.0
    start_time = time.time()
    macro_voltage_tracking = np.zeros((350000, 3))

    print("Pulse still running. Press Enter to stop... PLEASE CONSULT EXPERIMENT RUNNER BEFORE SHUTTING DOWN. DO NOT TURN ON THE LIGHTS. DO NOT OPEN CURTAINS. NO LAMP")

    i = 0
    frames_per_second = 5
    while (time.time() - start_time < experiment_length):
        previous_sample_time = time.monotonic()
        pulse_level_voltage_tracking = np.zeros((frames_per_second, 2))
        j = 0
        while (time.monotonic() - previous_sample_time < sample_period):
            #Each pulse
            if j >= frames_per_second:
                break
            
            try:
                data = osc.get_data(
                    wait_reacquire=True,
                    wait_complete=True,
                    timeout=1.0
                )
            except Exception as e:
                print("No new triggered frame:", e)
                time.sleep(0.1)
                continue

            t = np.array(data["time"])
            voltage = np.array(data["ch1"])
            dt = t[1] - t[0]
            data_length = len(t)
            zero_point_index = np.argmin(np.abs(t - 0.0))
            pulse_end = np.argmin(np.abs(t - 10e-6))
            num_samples_in_edge = int(100e-9/dt) + 1

            baseline_before = voltage[:zero_point_index - num_samples_in_edge]
            baseline_after = voltage[pulse_end + num_samples_in_edge:]
            baseline_voltage = (np.sum(baseline_before) + np.sum(baseline_after)) / (len(baseline_before) + len(baseline_after))
            peak_voltage = np.mean(voltage[zero_point_index + num_samples_in_edge:pulse_end - num_samples_in_edge])
            pulse_level_voltage_tracking[j] = np.array([baseline_voltage, peak_voltage])

            j += 1
        
        if j == 0:
            continue

        average_levels = np.mean(pulse_level_voltage_tracking[:j], axis=0)
        minimum_voltage_sample = average_levels[0]
        maximum_voltage_sample = average_levels[1]

        if i >= len(macro_voltage_tracking):
            print("Sample array full; stopping safely.")
            break

        macro_voltage_tracking[i] = np.array([time.time(), minimum_voltage_sample, maximum_voltage_sample])
        i += 1

        if i % 60 == 0:
            print(
                time.strftime("%H:%M:%S"),
                "samples =", i,
                "min =", minimum_voltage_sample,
                "max =", maximum_voltage_sample,
                "frames =", j
            )

        if time.monotonic() - last_save_time >= save_period:
            np.savetxt(
                filename,
                macro_voltage_tracking[:i],
                delimiter=",",
                header="wall_time,minimum_voltage,maximum_voltage",
                comments=""
            )
            last_save_time = time.monotonic()
            print("Saved", i, "samples to", filename)

            plt.plot(data["time"], data["ch1"], label="Input1 physical measurement")
            plt.plot(data["time"], data["ch2"], label="Output2 internal reference")
            plt.xlabel("Time / s")
            plt.ylabel("Voltage / V")
            plt.grid(True)
            plt.legend()
            trace_filename = run_folder / f"trace_time_{macro_voltage_tracking[i-1][0]:.0f}.png"
            plt.savefig(trace_filename, dpi=300, bbox_inches="tight")
            plt.close()

    np.savetxt(
        filename,
        macro_voltage_tracking[:i],
        delimiter=",",
        header="wall_time,minimum_voltage,maximum_voltage",
        comments=""
    )

#   input("Pulse still running. Press Enter to stop... PLEASE CONSULT EXPERIMENT RUNNER BEFORE SHUTTING DOWN. DO NOT TURN ON THE LIGHTS. DO NOT OPEN CURTAINS. NO LAMP")

finally:
    print("Experiment Finished")

    if "macro_voltage_tracking" in locals() and "filename" in locals() and i > 0:
        np.savetxt(
            filename,
            macro_voltage_tracking[:i],
            delimiter=",",
            header="wall_time,minimum_voltage,maximum_voltage",
            comments=""
        )
        print("Final saved", i, "samples to", filename)

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