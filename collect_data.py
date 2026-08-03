import matplotlib.pyplot as plt
import numpy as np
from importlib import metadata as importlib_metadata
import logging
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
from eom_stabilisation.moku.acquisition import (
    JsonlAcquisitionEventWriter,
    MokuAcquisitionManager,
    MokuConnectionFactory,
    OscilloscopeConfiguration,
    RecoveryPolicy,
    apply_oscilloscope_configuration,
    verify_oscilloscope_connection,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger(__name__)

DEFAULT_MOKU_ADDRESS = "MokuGo-008058"
MOKU_ADDRESS = os.environ.get(
    "EOM_MOKU_ADDRESS",
    DEFAULT_MOKU_ADDRESS,
).strip()
MOKU_FALLBACK_ADDRESS = (
    os.environ.get("EOM_MOKU_FALLBACK_ADDRESS", "").strip() or None
)

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
RECOVERY_POLICY = RecoveryPolicy(
    trigger_timeout_retry_delay_s=0.1,
    trigger_timeout_report_interval_s=60.0,
    transport_errors_before_reconnect=2,
    malformed_frames_before_reconnect=5,
    reconnect_backoff_s=(1.0, 2.0, 5.0),
    max_recovery_cycles_without_valid_frame=3,
)

MOKU_CONFIGURATION = OscilloscopeConfiguration(
    address=MOKU_ADDRESS,
    force_connect=True,
    frontend_channel=1,
    frontend_impedance="1MOhm",
    frontend_coupling="DC",
    frontend_range="10Vpp",
    channel_sources=((1, "Input1"), (2, "Output2")),
    timebase_start_s=-45e-6,
    timebase_end_s=45e-6,
    timebase_max_length=16384,
    trigger_mode="Normal",
    trigger_type="Edge",
    trigger_source="Input1",
    trigger_level_v=TRIGGER_LEVEL,
    trigger_edge="Rising",
    waveform_channel=2,
    waveform_type="Pulse",
    waveform_amplitude_vpp=PULSE_AMPLITUDE,
    waveform_offset_v=PULSE_AMPLITUDE / 2,
    waveform_frequency_hz=PULSE_FREQUENCY,
    waveform_pulse_width_s=PULSE_WIDTH,
    waveform_edge_time_s=PULSE_EDGE_TIME,
    fallback_address=MOKU_FALLBACK_ADDRESS,
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


def save_voltage_samples(output_file, samples, sample_count):
    """Atomically save the currently buffered valid measurement samples."""

    output_path = Path(output_file)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    np.savetxt(
        temporary_path,
        samples[:sample_count],
        delimiter=",",
        header="wall_time,minimum_voltage,maximum_voltage",
        comments="",
    )
    temporary_path.replace(output_path)


def get_moku_sdk_version():
    """Return the installed Moku SDK version for run provenance."""

    try:
        return importlib_metadata.version("moku")
    except importlib_metadata.PackageNotFoundError:
        return "unknown"


def write_event_safely(event_writer, event, **fields):
    """Record an event without preventing later hardware cleanup."""

    if event_writer is None:
        return
    try:
        event_writer.write(event, **fields)
    except Exception:
        LOGGER.exception("Could not write Moku acquisition event %s", event)

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
    connection_factory = MokuConnectionFactory(
        Oscilloscope,
        MOKU_CONFIGURATION,
    )
    try:
        configured_addresses = ", ".join(
            address
            for _, address in MOKU_CONFIGURATION.connection_addresses()
        )
        print(
            "Connecting for emergency Output 2 shutdown using: "
            f"{configured_addresses}"
        )
        cleanup_osc = connection_factory()
        cleanup_osc.generate_waveform(channel=2, type="Off")
        print(
            "Moku Output 2 is OFF via "
            f"{connection_factory.selected_address}."
        )
    finally:
        if cleanup_osc is not None:
            cleanup_osc.relinquish_ownership()


def cleanup_failed_reconnection(candidate_osc):
    """Switch off a partially configured replacement object and release it."""

    try:
        candidate_osc.generate_waveform(
            channel=MOKU_CONFIGURATION.waveform_channel,
            type="Off",
        )
    finally:
        candidate_osc.relinquish_ownership()


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
acquisition_manager = None
event_writer = None
filename = None
voltage_samples = None
i = 0
interrupted = False

try:
    run_folder = Path("Experiment Results") / "moku_pulse_runs" / time.strftime("run_%Y%m%d_%H%M%S")
    run_folder.mkdir(parents=True, exist_ok=False)
    filename = run_folder / "raw_photovoltage_tracking.csv"
    event_writer = JsonlAcquisitionEventWriter(
        run_folder / "acquisition_events.jsonl",
        sdk_version=get_moku_sdk_version(),
        configuration=MOKU_CONFIGURATION,
    )

    #Set timers
    start_time = time.monotonic()
    last_save_time = time.monotonic()

    voltage_samples = np.zeros((MAX_SAMPLES, 3))
    save_voltage_samples(filename, voltage_samples, 0)
    write_event_safely(
        event_writer,
        "experiment_initialising",
        effective_configuration=MOKU_CONFIGURATION.metadata(),
    )

    connection_factory = MokuConnectionFactory(
        Oscilloscope,
        MOKU_CONFIGURATION,
        event_writer=event_writer,
    )

    def create_oscilloscope():
        return connection_factory()

    def apply_recorded_configuration(candidate_osc):
        apply_oscilloscope_configuration(candidate_osc, MOKU_CONFIGURATION)

    def save_before_reconnect(reason):
        save_voltage_samples(filename, voltage_samples, i)
        write_event_safely(
            event_writer,
            "buffer_saved_before_reconnect",
            recovery_reason=reason,
            valid_sample_count=i,
        )

    osc = create_oscilloscope()
    apply_recorded_configuration(osc)
    verify_oscilloscope_connection(osc)
    write_event_safely(
        event_writer,
        "initial_connection_verified",
        connection_address=connection_factory.selected_address,
        address_role=connection_factory.selected_address_role,
        resolved_addresses=list(
            connection_factory.selected_resolved_addresses
        ),
        valid_sample_count=i,
    )

    acquisition_manager = MokuAcquisitionManager(
        osc,
        instrument_factory=create_oscilloscope,
        apply_configuration=apply_recorded_configuration,
        verify_connection=verify_oscilloscope_connection,
        event_writer=event_writer,
        before_reconnect=save_before_reconnect,
        cleanup_failed_instrument=cleanup_failed_reconnection,
        policy=RECOVERY_POLICY,
    )

    print("Pulse still running. Press Ctrl+C to stop... PLEASE CONSULT EXPERIMENT RUNNER BEFORE SHUTTING DOWN. DO NOT TURN ON THE LIGHTS. DO NOT OPEN CURTAINS. NO LAMP")

    #Entire experiment
    while (time.monotonic() - start_time < EXPERIMENT_LENGTH):
        previous_sample_time = time.monotonic()
        per_pulse_voltage_tracking = np.zeros((FRAMES_PER_SECOND, 2))

        #Each pulse
        j = 0
        while (time.monotonic() - previous_sample_time < SAMPLE_PERIOD):
            if j >= FRAMES_PER_SECOND:
                break

            data = acquisition_manager.acquire_frame(
                wait_reacquire=True,
                wait_complete=True,
                timeout=1.0,
            )
            if data is None:
                continue

            try:
                baseline_voltage, peak_voltage = measure_frame_levels(data)
            except (KeyError, TypeError, ValueError) as e:
                acquisition_manager.record_malformed_frame(e)
                continue

            acquisition_manager.record_valid_frame()
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

        if i == 1:
            save_voltage_samples(filename, voltage_samples, i)
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
            save_voltage_samples(filename, voltage_samples, i)
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

    save_voltage_samples(filename, voltage_samples, i)
    write_event_safely(
        event_writer,
        "experiment_completed",
        valid_sample_count=i,
    )

except KeyboardInterrupt:
    interrupted = True
    print("Experiment stop requested; saving final Moku data.")
    write_event_safely(
        event_writer,
        "experiment_interrupted",
        valid_sample_count=i,
    )

except Exception as error:
    write_event_safely(
        event_writer,
        "experiment_failed",
        error=error,
        include_traceback=True,
        valid_sample_count=i,
    )
    raise

finally:
    print("Experiment Finished")

    final_save_error = None
    if voltage_samples is not None and filename is not None:
        try:
            save_voltage_samples(filename, voltage_samples, i)
            print("Final saved ", i, "samples to", filename)
        except Exception as error:
            final_save_error = error
            print("Could not save final Moku data:", error)
            write_event_safely(
                event_writer,
                "final_data_save_failed",
                error=error,
                include_traceback=True,
                valid_sample_count=i,
            )

    active_osc = (
        acquisition_manager.instrument
        if acquisition_manager is not None
        else osc
    )
    if active_osc is not None:
        try:
            active_osc.generate_waveform(
                channel=MOKU_CONFIGURATION.waveform_channel,
                type="Off",
            )
            print("Output2 turned off")
            write_event_safely(event_writer, "output_disabled")
        except Exception as e:
            print("Could not turn Output2 off via API:", e)
            write_event_safely(
                event_writer,
                "output_disable_unconfirmed",
                error=e,
                include_traceback=True,
            )

        try:
            active_osc.relinquish_ownership()
            print("Ownership released")
            write_event_safely(event_writer, "ownership_relinquished")
        except Exception as e:
            print("Could not relinquish ownership:", e)
            write_event_safely(
                event_writer,
                "ownership_relinquish_failed",
                error=e,
                include_traceback=True,
            )

    if final_save_error is not None and sys.exc_info()[0] is None:
        raise final_save_error

if interrupted:
    raise SystemExit(130)
