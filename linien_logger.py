"""Log Linien signals while recovering temporary Red Pitaya disconnections."""

from __future__ import annotations

import csv
from importlib import metadata as importlib_metadata
import logging
import os
from pathlib import Path
import pickle
import sys
import time
from typing import Any, Callable

import numpy as np


SRC_DIRECTORY = Path(__file__).resolve().parent / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.linien.recovery import (  # noqa: E402
    JsonlLinienEventWriter,
    LinienConnectionRecovery,
    LinienLockNotRestoredError,
    LinienRecoveryPolicy,
)
from eom_stabilisation.output_layout import (  # noqa: E402
    append_output_requested,
    component_output_file,
    resolve_component_directory,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger(__name__)


# =========================
# User settings
# =========================

RP_HOST = "169.254.207.199"
RP_USER = "root"
RP_PASSWORD = os.environ.get("LINIEN_PASSWORD")

SAMPLE_PERIOD = 1.0          # seconds
PRINT_TO_CONSOLE = True

# A temporary transport outage is retried indefinitely. After a replacement
# connection is established, Linien must report that it is locked within this
# time or the logger exits non-zero so the master runner stops the experiment.
RECONNECT_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)
POST_RECONNECT_LOCK_TIMEOUT_SECONDS = 10.0

# Linien GUI scaling:
# error_signal_raw / 8192 -> GUI-scaled error signal, unitless/demodulated units
# control_signal_raw / 8192 -> Red Pitaya fast output voltage in volts
SCALE = 1.0 / 8192.0

# Linien's FPGA signal selector for fast_a_x: FAST IN 1 before demodulation.
FAST_IN_1_SELECTOR = 1


def select_raw_photodiode_signal(client: Any) -> None:
    """Route pre-demodulation FAST IN 1 to Linien's monitor capture channel."""

    client.control.exposed_pause_acquisition()
    try:
        client.control.exposed_set_csr_direct(
            "scopegen_adc_a_q_sel",
            FAST_IN_1_SELECTOR,
        )
    finally:
        client.control.exposed_continue_acquisition()


def signal_master_ready(output_file: Path) -> None:
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


def installed_linien_client_version() -> str:
    """Return the installed Linien client version for run provenance."""

    for distribution_name in ("linien-client", "linien_client"):
        try:
            return importlib_metadata.version(distribution_name)
        except importlib_metadata.PackageNotFoundError:
            continue
    return "unknown"


def load_linien_client_types() -> tuple[type[Any], type[Any], tuple[type[Exception], ...]]:
    """Load hardware dependencies only when the real logger is started."""

    from linien_client.connection import LinienClient
    from linien_client.device import Device
    from linien_client import exceptions as linien_exceptions

    recoverable_names = (
        "GeneralConnectionError",
        "RPYCAuthenticationException",
        "ServerNotRunningException",
    )
    recoverable_types = tuple(
        exception_type
        for name in recoverable_names
        if isinstance(
            exception_type := getattr(linien_exceptions, name, None),
            type,
        )
        and issubclass(exception_type, Exception)
    )
    return Device, LinienClient, recoverable_types


def decode_plot_sample(serialized_plot_data: bytes) -> tuple[float, float, float] | None:
    """Decode the newest error, lock-output and photodiode values."""

    plot_data = pickle.loads(serialized_plot_data)
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
        print("No error/control/photodiode data yet. Keys:", list(plot_data.keys()))
        return None

    return (
        float(error_array[-1]) * SCALE,
        float(control_array[-1]) * SCALE,
        float(photodiode_array[-1]) * SCALE,
    )


def run_logging_loop(
    client: Any,
    recovery: LinienConnectionRecovery,
    recoverable_exceptions: tuple[type[Exception], ...],
    writer: Any,
    output_stream: Any,
    output_file: Path,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Log samples forever, replacing a client after transport failures."""

    was_locked = False
    master_ready_signalled = False

    try:
        while True:
            try:
                # Pull the latest parameter and plot data from the Linien server.
                client.parameters.check_for_changed_parameters()
                is_locked = bool(client.parameters.lock.value)

                if is_locked and not was_locked:
                    if client.parameters.dual_channel.value:
                        raise RuntimeError(
                            "Raw FAST IN 1 logging requires Linien dual "
                            "channel mode to be disabled."
                        )

                    select_raw_photodiode_signal(client)
                    was_locked = True
                    print("Raw photodiode logging enabled from FAST IN 1.")
                    sleep(SAMPLE_PERIOD)
                    continue

                if not is_locked:
                    was_locked = False

                # Copy the cached bytes inside the transport-error boundary;
                # decode outside it so malformed data is not mistaken for a
                # recoverable network failure.
                serialized_plot_data = client.parameters.to_plot.value

            except recoverable_exceptions as error:
                output_stream.flush()
                LOGGER.warning(
                    "Linien connection lost; preserving the run and retrying: %s",
                    error,
                )
                client = recovery.recover(client, error)
                was_locked = False
                continue

            sample = decode_plot_sample(serialized_plot_data)
            if sample is None:
                sleep(SAMPLE_PERIOD)
                continue

            error_signal, lock_voltage, photodiode_voltage = sample
            wall_time = time.time()
            writer.writerow(
                [
                    wall_time,
                    error_signal,
                    lock_voltage,
                    photodiode_voltage,
                ]
            )
            output_stream.flush()

            if not master_ready_signalled:
                signal_master_ready(output_file)
                master_ready_signalled = True

            if PRINT_TO_CONSOLE:
                print(
                    f"{time.strftime('%H:%M:%S')}  "
                    f"wall_time={wall_time:.3f}  "
                    f"error={error_signal:.6g}  "
                    f"lock_output={lock_voltage:.6f} V  "
                    f"photodiode_signal={photodiode_voltage:.6f} V"
                )

            sleep(SAMPLE_PERIOD)
    finally:
        recovery.disconnect_safely(client, context="logger_shutdown")


def main() -> int:
    """Connect to Linien and append lock-point measurements until stopped."""

    if not RP_PASSWORD:
        raise RuntimeError(
            "Set the LINIEN_PASSWORD environment variable before running this "
            "script."
        )

    script_directory = Path(__file__).resolve().parent
    _, run_folder = resolve_component_directory(script_directory, "lock")
    filename = component_output_file(run_folder / "RP_voltage_tracking.csv")
    filename.parent.mkdir(parents=True, exist_ok=True)
    append_existing_output = append_output_requested() and filename.is_file()
    event_writer = JsonlLinienEventWriter(
        filename.parent / "linien_connection_events.jsonl"
    )

    Device, LinienClient, linien_connection_errors = load_linien_client_types()
    recoverable_exceptions = tuple(
        dict.fromkeys(
            (
                EOFError,
                ConnectionError,
                TimeoutError,
                OSError,
                *linien_connection_errors,
            )
        )
    )

    device = Device(
        host=RP_HOST,
        username=RP_USER,
        password=RP_PASSWORD,
    )

    def connection_factory() -> Any:
        candidate = LinienClient(device)
        try:
            # Attach only; never start or reconfigure the Linien server.
            candidate.connect(
                autostart_server=False,
                use_parameter_cache=True,
            )
        except Exception:
            try:
                candidate.disconnect()
            except Exception:
                pass
            raise
        return candidate

    def refresh_and_read_lock(candidate: Any) -> bool:
        candidate.parameters.check_for_changed_parameters()
        return bool(candidate.parameters.lock.value)

    def disconnect_client(candidate: Any) -> None:
        candidate.disconnect()

    def is_recoverable_error(error: BaseException) -> bool:
        return isinstance(error, recoverable_exceptions)

    policy = LinienRecoveryPolicy(
        reconnect_backoff_s=RECONNECT_BACKOFF_SECONDS,
        lock_recovery_timeout_s=POST_RECONNECT_LOCK_TIMEOUT_SECONDS,
        lock_poll_interval_s=SAMPLE_PERIOD,
    )
    recovery = LinienConnectionRecovery(
        connection_factory=connection_factory,
        refresh_and_read_lock=refresh_and_read_lock,
        disconnect_client=disconnect_client,
        is_recoverable_error=is_recoverable_error,
        event_writer=event_writer,
        policy=policy,
    )

    event_writer.write(
        "logger_starting",
        red_pitaya_host=RP_HOST,
        linien_client_version=installed_linien_client_version(),
        reconnect_backoff_s=list(RECONNECT_BACKOFF_SECONDS),
        post_reconnect_lock_timeout_s=POST_RECONNECT_LOCK_TIMEOUT_SECONDS,
        autostart_server=False,
    )

    try:
        client = connection_factory()
    except Exception as error:
        event_writer.write(
            "initial_connection_failed",
            error=error,
            include_traceback=True,
        )
        raise

    event_writer.write("initial_connection_succeeded")
    print("Connected to Red Pitaya.")
    print("Logging to:", filename)
    print("Connection events:", event_writer.path)
    print("Press Ctrl+C to stop.")

    logging_loop_owns_client = False
    try:
        try:
            client.parameters.check_for_changed_parameters()
            control_channel = client.parameters.control_channel.value
        except recoverable_exceptions as error:
            client = recovery.recover(client, error)
            control_channel = client.parameters.control_channel.value

        if control_channel == 0:
            print("Linien lock output is configured for FAST OUT 1.")
        else:
            print(
                "WARNING: Linien is configured to send the lock signal to "
                "FAST OUT 2, not FAST OUT 1."
            )

        with filename.open(
            "a" if append_existing_output else "w",
            newline="",
        ) as output_stream:
            writer = csv.writer(output_stream)
            if not append_existing_output or filename.stat().st_size == 0:
                writer.writerow(
                    [
                        "wall_time",
                        "error_signal",
                        "RP_lock_voltage",
                        "photodiode_voltage",
                    ]
                )
                output_stream.flush()

            logging_loop_owns_client = True
            run_logging_loop(
                client,
                recovery,
                recoverable_exceptions,
                writer,
                output_stream,
                filename,
            )

    except KeyboardInterrupt:
        event_writer.write("logger_stopped_by_operator")
        print("Stopped by user.")
        print("Finished.")
        return 130
    except LinienLockNotRestoredError as error:
        LOGGER.error("%s; ending the experiment", error)
        print("Finished.")
        return 1
    finally:
        if not logging_loop_owns_client:
            recovery.disconnect_safely(client, context="logger_shutdown")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
