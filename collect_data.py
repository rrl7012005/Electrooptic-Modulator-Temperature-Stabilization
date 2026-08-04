import csv
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import matplotlib.pyplot as plt
import numpy as np

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
    OscilloscopeConfiguration,
    RecoveryPolicy,
    apply_oscilloscope_configuration,
    verify_oscilloscope_connection,
)
from eom_stabilisation.moku.process_worker import (
    ProcessIsolatedOscilloscope,
    WorkerTimeouts,
)
from eom_stabilisation.output_layout import (
    append_output_requested,
    component_output_file,
    resolve_component_directory,
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
GET_DATA_HARD_TIMEOUT_S = float(
    os.environ.get("EOM_MOKU_GET_DATA_HARD_TIMEOUT_SECONDS", "15")
)
if not math.isfinite(GET_DATA_HARD_TIMEOUT_S) or GET_DATA_HARD_TIMEOUT_S <= 0:
    raise ValueError("EOM_MOKU_GET_DATA_HARD_TIMEOUT_SECONDS must be positive")
RECOVERY_MODE = os.environ.get(
    "EOM_MOKU_RECOVERY_MODE", "indefinite"
).strip().lower()
MAXIMUM_RECOVERY_OUTAGE_TEXT = os.environ.get(
    "EOM_MOKU_MAX_RECOVERY_OUTAGE_SECONDS", ""
).strip()
MAXIMUM_RECOVERY_OUTAGE_S = (
    float(MAXIMUM_RECOVERY_OUTAGE_TEXT)
    if MAXIMUM_RECOVERY_OUTAGE_TEXT
    else None
)
RECOVERY_POLICY = RecoveryPolicy(
    trigger_timeout_retry_delay_s=0.1,
    trigger_timeout_report_interval_s=60.0,
    transport_errors_before_reconnect=2,
    malformed_frames_before_reconnect=5,
    reconnect_backoff_s=(1.0, 2.0, 5.0, 10.0, 20.0, 30.0),
    recovery_mode=RECOVERY_MODE,
    maximum_recovery_outage_s=MAXIMUM_RECOVERY_OUTAGE_S,
    recovery_status_report_interval_s=60.0,
    max_recovery_cycles_without_valid_frame=None,
)
WORKER_TIMEOUTS = WorkerTimeouts(
    get_data_s=GET_DATA_HARD_TIMEOUT_S,
    rpc_s=15.0,
    startup_s=30.0,
    cleanup_s=5.0,
    terminate_grace_s=3.0,
    kill_grace_s=3.0,
    poll_interval_s=0.1,
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
    connection_interface_type="usb_virtual_ethernet",
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


def save_sample_provenance(
    output_file: str | Path,
    records: Sequence[Mapping[str, Any]],
) -> None:
    """Atomically save one provenance row for every primary CSV row."""

    field_names = (
        "wall_time",
        "timestamp_utc",
        "acquisition_source",
        "run_session_id",
        "waveform_session_id",
        "first_sample_after_reconnect",
        "waveform_timing_may_have_restarted",
    )
    output_path = Path(output_file)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=field_names)
        writer.writeheader()
        writer.writerows(records)
    temporary_path.replace(output_path)


def load_existing_voltage_samples(output_file: str | Path) -> np.ndarray:
    """Load previously saved Moku samples before extending a resumed run."""

    rows = []
    with Path(output_file).open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        required = {"wall_time", "minimum_voltage", "maximum_voltage"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                "Existing Moku CSV is missing columns: "
                + ", ".join(sorted(missing))
            )
        for row in reader:
            try:
                values = [
                    float(row["wall_time"]),
                    float(row["minimum_voltage"]),
                    float(row["maximum_voltage"]),
                ]
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "Existing Moku CSV contains an invalid row."
                ) from error
            if not np.all(np.isfinite(values)):
                raise ValueError(
                    "Existing Moku CSV contains non-finite values."
                )
            rows.append(values)

    if not rows:
        return np.empty((0, 3), dtype=float)
    return np.asarray(rows, dtype=float)


def load_existing_sample_provenance(
    output_file: str | Path,
) -> list[dict[str, str]]:
    """Load the provenance sidecar before extending a resumed run."""

    with Path(output_file).open(newline="", encoding="utf-8-sig") as csv_file:
        return list(csv.DictReader(csv_file))


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
    try:
        configured_addresses = ", ".join(
            address
            for _, address in MOKU_CONFIGURATION.connection_addresses()
        )
        print(
            "Connecting for emergency Output 2 shutdown using: "
            f"{configured_addresses}"
        )
        cleanup_osc = ProcessIsolatedOscilloscope(
            MOKU_CONFIGURATION,
            event_writer=None,
            timeouts=WORKER_TIMEOUTS,
        )
        cleanup_osc.generate_waveform(channel=2, type="Off")
        print(
            "Moku Output 2 is OFF via "
            f"{cleanup_osc.selected_address}."
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


def run_acquisition_experiment() -> int:
    """Run one acquisition with all live SDK objects isolated in children."""

    osc = None
    acquisition_manager = None
    event_writer = None
    filename = None
    provenance_filename = None
    voltage_samples = None
    sample_provenance = []
    sample_count = 0
    interrupted = False
    create_oscilloscope = None

    try:
        experiment_run_directory, run_folder = resolve_component_directory(
            Path(__file__).resolve().parent,
            "moku",
        )
        filename = component_output_file(
            run_folder / "raw_photovoltage_tracking.csv"
        )
        run_folder = filename.parent
        run_folder.mkdir(parents=True, exist_ok=True)
        provenance_filename = run_folder / "raw_photovoltage_provenance.csv"
        resume_existing_output = append_output_requested() and filename.is_file()
        event_writer = JsonlAcquisitionEventWriter(
            run_folder / "acquisition_events.jsonl",
            sdk_version=get_moku_sdk_version(),
            configuration=MOKU_CONFIGURATION,
        )

        start_time = time.monotonic()
        last_save_time = time.monotonic()
        existing_samples = (
            load_existing_voltage_samples(filename)
            if resume_existing_output
            else np.empty((0, 3), dtype=float)
        )
        if resume_existing_output:
            if not provenance_filename.is_file():
                raise FileNotFoundError(
                    "Existing Moku provenance CSV does not exist: "
                    f"{provenance_filename}"
                )
            sample_provenance = load_existing_sample_provenance(
                provenance_filename
            )
            if len(sample_provenance) != len(existing_samples):
                raise ValueError(
                    "Existing Moku samples and provenance rows are not "
                    "one-to-one."
                )
        next_waveform_session_id = 1
        if sample_provenance:
            try:
                next_waveform_session_id = 1 + max(
                    int(record["waveform_session_id"])
                    for record in sample_provenance
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    "Existing Moku provenance contains an invalid waveform "
                    "session ID."
                ) from error
        sample_count = len(existing_samples)
        initial_sample_count = sample_count
        voltage_samples = np.zeros((sample_count + MAX_SAMPLES, 3))
        voltage_samples[:sample_count] = existing_samples
        if not resume_existing_output:
            save_voltage_samples(filename, voltage_samples, 0)
            save_sample_provenance(provenance_filename, sample_provenance)
        write_event_safely(
            event_writer,
            "experiment_initialising",
            effective_configuration=MOKU_CONFIGURATION.metadata(),
            recovery_policy={
                "mode": RECOVERY_POLICY.recovery_mode,
                "maximum_recovery_outage_s": (
                    RECOVERY_POLICY.maximum_recovery_outage_s
                ),
                "reconnect_backoff_s": list(
                    RECOVERY_POLICY.reconnect_backoff_s
                ),
            },
            worker_timeouts=vars(WORKER_TIMEOUTS),
        )

        def create_isolated_oscilloscope():
            return ProcessIsolatedOscilloscope(
                MOKU_CONFIGURATION,
                event_writer=event_writer,
                timeouts=WORKER_TIMEOUTS,
            )

        create_oscilloscope = create_isolated_oscilloscope

        def apply_recorded_configuration(candidate_osc):
            apply_oscilloscope_configuration(candidate_osc, MOKU_CONFIGURATION)

        def save_before_reconnect(reason):
            save_voltage_samples(filename, voltage_samples, sample_count)
            save_sample_provenance(
                provenance_filename,
                sample_provenance,
            )
            write_event_safely(
                event_writer,
                "buffer_saved_before_reconnect",
                recovery_reason=reason,
                valid_sample_count=sample_count,
            )

        osc = create_oscilloscope()
        apply_recorded_configuration(osc)
        verify_oscilloscope_connection(osc)
        write_event_safely(
            event_writer,
            "initial_connection_verified",
            connection_address=osc.selected_address,
            address_role=osc.selected_address_role,
            resolved_addresses=list(osc.selected_resolved_addresses),
            worker_pid=osc.worker_pid,
            valid_sample_count=sample_count,
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
        acquisition_manager.waveform_session_id = next_waveform_session_id

        print(
            "Pulse still running. Press Ctrl+C to stop... PLEASE CONSULT "
            "EXPERIMENT RUNNER BEFORE SHUTTING DOWN. DO NOT TURN ON THE "
            "LIGHTS. DO NOT OPEN CURTAINS. NO LAMP"
        )

        while time.monotonic() - start_time < EXPERIMENT_LENGTH:
            previous_sample_time = time.monotonic()
            per_pulse_voltage_tracking = np.zeros((FRAMES_PER_SECOND, 2))
            frames_collected = 0
            data = None
            aggregation_waveform_session_id = (
                acquisition_manager.waveform_session_id
            )

            while time.monotonic() - previous_sample_time < SAMPLE_PERIOD:
                if frames_collected >= FRAMES_PER_SECOND:
                    break

                data = acquisition_manager.acquire_frame(
                    wait_reacquire=True,
                    wait_complete=True,
                    timeout=1.0,
                )
                if (
                    acquisition_manager.waveform_session_id
                    != aggregation_waveform_session_id
                ):
                    if frames_collected:
                        write_event_safely(
                            event_writer,
                            "partial_sample_discarded_after_waveform_restart",
                            discarded_valid_frame_count=frames_collected,
                            previous_waveform_session_id=(
                                aggregation_waveform_session_id
                            ),
                            waveform_session_id=(
                                acquisition_manager.waveform_session_id
                            ),
                        )
                    frames_collected = 0
                    aggregation_waveform_session_id = (
                        acquisition_manager.waveform_session_id
                    )
                if data is None:
                    continue

                try:
                    baseline_voltage, peak_voltage = measure_frame_levels(data)
                except (KeyError, TypeError, ValueError) as error:
                    acquisition_manager.record_malformed_frame(error)
                    continue

                acquisition_manager.record_valid_frame()
                per_pulse_voltage_tracking[frames_collected] = np.array(
                    [baseline_voltage, peak_voltage]
                )
                frames_collected += 1

            if frames_collected == 0:
                continue

            remaining_sample_time = SAMPLE_PERIOD - (
                time.monotonic() - previous_sample_time
            )
            if remaining_sample_time > 0:
                time.sleep(remaining_sample_time)

            average_levels = np.mean(
                per_pulse_voltage_tracking[:frames_collected], axis=0
            )
            minimum_voltage_sample = average_levels[0]
            maximum_voltage_sample = average_levels[1]

            if sample_count >= len(voltage_samples):
                print("Sample array full; stopping experiment.")
                break

            sample_wall_time = time.time()
            voltage_samples[sample_count] = np.array(
                [sample_wall_time, minimum_voltage_sample, maximum_voltage_sample]
            )
            previous_sample_waveform_session_id = (
                int(sample_provenance[-1]["waveform_session_id"])
                if sample_provenance
                else 1
            )
            sample_provenance.append(
                {
                    "wall_time": f"{sample_wall_time:.9f}",
                    "timestamp_utc": datetime.fromtimestamp(
                        sample_wall_time, timezone.utc
                    ).isoformat().replace("+00:00", "Z"),
                    "acquisition_source": "live_oscilloscope",
                    "run_session_id": experiment_run_directory.name,
                    "waveform_session_id": aggregation_waveform_session_id,
                    "first_sample_after_reconnect": (
                        aggregation_waveform_session_id
                        != previous_sample_waveform_session_id
                    ),
                    "waveform_timing_may_have_restarted": (
                        aggregation_waveform_session_id > 1
                    ),
                }
            )
            sample_count += 1

            if sample_count == initial_sample_count + 1:
                save_voltage_samples(filename, voltage_samples, sample_count)
                save_sample_provenance(
                    provenance_filename,
                    sample_provenance,
                )
                signal_master_ready(filename)

            if sample_count % PRINT_EVERY_K_SAMPLES == 0:
                print(
                    time.strftime("%H:%M:%S"),
                    "samples =", sample_count,
                    "min =", minimum_voltage_sample,
                    "max =", maximum_voltage_sample,
                    "frames =", frames_collected,
                )

            if time.monotonic() - last_save_time >= SAVE_PERIOD:
                save_voltage_samples(filename, voltage_samples, sample_count)
                save_sample_provenance(
                    provenance_filename,
                    sample_provenance,
                )
                last_save_time = time.monotonic()
                print("Saved", sample_count, "samples to", filename)

                plt.plot(
                    data["time"],
                    data["ch1"],
                    label="Input1 physical measurement",
                )
                plt.plot(
                    data["time"],
                    data["ch2"],
                    label="Output2 internal reference",
                )
                plt.xlabel("Time / s")
                plt.ylabel("Voltage / V")
                plt.grid(True)
                plt.legend()
                trace_directory = run_folder / "traces"
                trace_directory.mkdir(exist_ok=True)
                trace_filename = trace_directory / (
                    f"trace_time_{voltage_samples[sample_count - 1][0]:.0f}.png"
                )
                plt.savefig(trace_filename, dpi=300, bbox_inches="tight")
                plt.close()

        save_voltage_samples(filename, voltage_samples, sample_count)
        save_sample_provenance(provenance_filename, sample_provenance)
        write_event_safely(
            event_writer,
            "experiment_completed",
            valid_sample_count=sample_count,
        )

    except KeyboardInterrupt:
        interrupted = True
        print("Experiment stop requested; saving final Moku data.")
        write_event_safely(
            event_writer,
            "experiment_interrupted",
            valid_sample_count=sample_count,
        )

    except Exception as error:
        write_event_safely(
            event_writer,
            "experiment_failed",
            error=error,
            include_traceback=True,
            valid_sample_count=sample_count,
        )
        raise

    finally:
        print("Experiment Finished")
        final_save_error = None
        if voltage_samples is not None and filename is not None:
            try:
                save_voltage_samples(
                    filename, voltage_samples, sample_count
                )
                if provenance_filename is not None:
                    save_sample_provenance(
                        provenance_filename,
                        sample_provenance,
                    )
                print("Final saved", sample_count, "samples to", filename)
            except Exception as error:
                final_save_error = error
                print("Could not save final Moku data:", error)
                write_event_safely(
                    event_writer,
                    "final_data_save_failed",
                    error=error,
                    include_traceback=True,
                    valid_sample_count=sample_count,
                )

        active_osc = (
            acquisition_manager.instrument
            if acquisition_manager is not None
            else osc
        )
        output_disabled = False
        if active_osc is not None:
            try:
                active_osc.generate_waveform(
                    channel=MOKU_CONFIGURATION.waveform_channel,
                    type="Off",
                )
                output_disabled = True
                print("Output2 turned off")
                write_event_safely(event_writer, "output_disabled")
            except Exception as error:
                print("Could not turn Output2 off via active session:", error)
                write_event_safely(
                    event_writer,
                    "output_disable_unconfirmed",
                    error=error,
                    include_traceback=True,
                    cleanup_connection_attempt_pending=True,
                )

            try:
                active_osc.relinquish_ownership()
                print("Ownership released or SDK worker retired")
                write_event_safely(event_writer, "ownership_relinquished")
            except Exception as error:
                print("Could not relinquish ownership:", error)
                write_event_safely(
                    event_writer,
                    "ownership_relinquish_failed",
                    error=error,
                    include_traceback=True,
                )

        if not output_disabled and create_oscilloscope is not None:
            cleanup_osc = None
            try:
                cleanup_osc = create_oscilloscope()
                cleanup_osc.generate_waveform(
                    channel=MOKU_CONFIGURATION.waveform_channel,
                    type="Off",
                )
                output_disabled = True
                print("Output2 turned off using a bounded cleanup session")
                write_event_safely(
                    event_writer,
                    "output_disabled_via_cleanup_session",
                )
            except Exception as error:
                print("Cleanup session could not confirm Output2 off:", error)
                write_event_safely(
                    event_writer,
                    "output_disable_final_unconfirmed",
                    error=error,
                    include_traceback=True,
                )
            finally:
                if cleanup_osc is not None:
                    try:
                        cleanup_osc.relinquish_ownership()
                    except Exception as error:
                        write_event_safely(
                            event_writer,
                            "cleanup_session_release_failed",
                            error=error,
                            include_traceback=True,
                        )

        if final_save_error is not None and sys.exc_info()[0] is None:
            raise final_save_error

    return 130 if interrupted else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the small command surface without side effects on import."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--print-experiment-length"]:
        print(f"{EXPERIMENT_LENGTH:.9f}")
        return 0
    if arguments == ["--disable-output"]:
        disable_moku_output_now()
        return 0
    if arguments:
        raise SystemExit(
            "Supported options are --disable-output and "
            "--print-experiment-length"
        )
    return run_acquisition_experiment()


if __name__ == "__main__":
    raise SystemExit(main())
