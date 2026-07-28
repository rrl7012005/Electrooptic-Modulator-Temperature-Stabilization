from moku.instruments import Oscilloscope
import matplotlib.pyplot as plt

MOKU_IP = "MokuGo-008058"

print("CONNECTING\n")
osc = Oscilloscope(MOKU_IP, force_connect=True)
print("CONNECTED")

try:
    osc.set_frontend(1, "1MOhm", "DC", "10Vpp")

    osc.set_sources([
        {"channel": 1, "source": "Input1"},
        {"channel": 2, "source": "Output2"}
    ])

    osc.generate_waveform(
        channel=2,
        type="Pulse",
        amplitude=2.5,
        offset=1.25,
        frequency=1e2,
        pulse_width=10e-6,
        edge_time=100e-9
    )

    osc.set_timebase(-10e-6, 11000e-6, max_length=4096)

    osc.set_trigger(
        mode="Normal",
        type="Edge",
        source="Input1",
        level=0.5,
        edge="Rising"
    )

    data = osc.get_data(
        wait_reacquire=True,
        wait_complete=True,
        timeout=2.0
    )

    plt.plot(data["time"], data["ch1"], label="Input1 physical measurement")
    plt.plot(data["time"], data["ch2"], label="Output2 internal reference")
    plt.xlabel("Time / s")
    plt.ylabel("Voltage / V")
    plt.grid(True)
    plt.legend()
    plt.show()

    input("Pulse still running. Press Enter to stop...")

finally:
    osc.generate_waveform(channel=2, type="Off")
    osc.relinquish_ownership()