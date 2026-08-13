"""Log Linien signals while recovering temporary Red Pitaya disconnections."""

from __future__ import annotations

import csv
from copy import deepcopy
from importlib import metadata as importlib_metadata
import logging
import math
import os
from pathlib import Path
import pickle
import sys
import time
from typing import Any, Callable, Mapping

import numpy as np


SRC_DIRECTORY = Path(__file__).resolve().parent / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.linien.recovery import (  # noqa: E402
    attach_or_start_linien_server,
    JsonlLinienEventWriter,
    LinienConfigurationError,
    LinienConnectionResult,
    LinienConnectionRecovery,
    LinienLockObservation,
    LinienLockNotRestoredError,
    LinienRelockManager,
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

# Configured v2 runs inject ``LINIEN_HOST`` after validating the experiment
# configuration. Direct legacy use retains the original apparatus default.
RP_HOST = os.environ.get("LINIEN_HOST", "169.254.207.199")
RP_USER = "root"
RP_PASSWORD = os.environ.get("LINIEN_PASSWORD")

SAMPLE_PERIOD = 1.0          # seconds
PRINT_TO_CONSOLE = True

# A temporary transport outage is retried indefinitely. After reconnecting,
# Linien gets this long to preserve its existing lock before one guarded
# mid-run relock attempt is made from the last-known-good state.
RECONNECT_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)
POST_RECONNECT_LOCK_TIMEOUT_SECONDS = 10.0
RELOCK_REFERENCE_WINDOW_SECONDS = 30.0
RELOCK_REFERENCE_GUARD_SECONDS = 2.0
RELOCK_MINIMUM_REFERENCE_SAMPLES = 10
RELOCK_ACQUISITION_TIMEOUT_SECONDS = 10.0
RELOCK_SETTLE_SECONDS = 5.0
RELOCK_VALIDATION_SECONDS = 10.0
RELOCK_MINIMUM_VALIDATION_SAMPLES = 5
RELOCK_QUALITY_RATIO_LIMIT = 3.0
FAST_OUTPUT_RAIL_VOLTS = 0.98

# Linien GUI scaling:
# error_signal_raw / 8192 -> GUI-scaled error signal, unitless/demodulated units
# control_signal_raw / 8192 -> Red Pitaya fast output voltage in volts
SCALE = 1.0 / 8192.0

# Linien's FPGA signal selector for fast_a_x: FAST IN 1 before demodulation.
FAST_IN_1_SELECTOR = 1

# Parameters that define the signal path and normal PID lock.  sweep_center is
# deliberately excluded: after a disconnect it is replaced by the robust
# pre-loss control-voltage estimate.  Transient task/acquisition/lock state and
# unrelated manual analog outputs are also deliberately excluded.
LOCK_CONFIGURATION_PARAMETER_NAMES = (
    "mod_channel",
    "sweep_channel",
    "control_channel",
    "slow_control_channel",
    "polarity_fast_out1",
    "polarity_fast_out2",
    "polarity_analog_out0",
    "sweep_amplitude",
    "sweep_speed",
    "modulation_amplitude",
    "modulation_frequency",
    "pid_only_mode",
    "dual_channel",
    "channel_mixing",
    "demodulation_phase_a",
    "demodulation_phase_b",
    "demodulation_multiplier_a",
    "demodulation_multiplier_b",
    "offset_a",
    "offset_b",
    "invert_a",
    "invert_b",
    "filter_automatic_a",
    "filter_automatic_b",
    "filter_1_enabled_a",
    "filter_2_enabled_a",
    "filter_1_enabled_b",
    "filter_2_enabled_b",
    "filter_1_frequency_a",
    "filter_2_frequency_a",
    "filter_1_frequency_b",
    "filter_2_frequency_b",
    "filter_1_type_a",
    "filter_2_type_a",
    "filter_1_type_b",
    "filter_2_type_b",
    "combined_offset",
    "p",
    "i",
    "d",
    "target_slope_rising",
    "pid_on_slow_enabled",
    "pid_on_slow_strength",
    "check_lock",
    "watch_lock",
    "watch_lock_threshold",
    "autolock_mode_preference",
    "autolock_determine_offset",
)

REQUIRED_LOCK_CONFIGURATION_PARAMETER_NAMES = (
    "mod_channel",
    "sweep_channel",
    "control_channel",
    "modulation_amplitude",
    "modulation_frequency",
    "pid_only_mode",
    "dual_channel",
    "demodulation_phase_a",
    "demodulation_multiplier_a",
    "offset_a",
    "combined_offset",
    "p",
    "i",
    "d",
    "target_slope_rising",
)


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


def load_linien_client_types() -> tuple[Any, ...]:
    """Load hardware dependencies only when the real logger is started."""

    from linien_common.common import AutolockMode
    from linien_common.communication import unpack
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
    server_not_running = getattr(
        linien_exceptions,
        "ServerNotRunningException",
        None,
    )
    if not isinstance(server_not_running, type) or not issubclass(
        server_not_running,
        Exception,
    ):
        raise RuntimeError(
            "This Linien client does not expose ServerNotRunningException; "
            "safe attach-before-autostart recovery is unavailable."
        )
    return (
        Device,
        LinienClient,
        recoverable_types,
        server_not_running,
        unpack,
        AutolockMode.SIMPLE,
    )


def read_remote_parameter_value(
    client: Any,
    name: str,
    unpack_value: Callable[[Any], Any],
) -> Any:
    """Read a parameter directly from the server, bypassing the client cache."""

    packed_value = client.parameters.remote.exposed_get_param(name)
    return unpack_value(packed_value)


def read_lock_configuration(
    client: Any,
    *,
    fresh: bool = False,
    unpack_value: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """Read the supported signal-path and PID settings for guarded recovery."""

    if fresh and unpack_value is None:
        raise ValueError("unpack_value is required for a fresh server read")

    available: dict[str, Any] = {}
    for name in LOCK_CONFIGURATION_PARAMETER_NAMES:
        if not hasattr(client.parameters, name):
            continue
        value = (
            read_remote_parameter_value(client, name, unpack_value)
            if fresh
            else getattr(client.parameters, name).value
        )
        available[name] = deepcopy(value)

    missing = [
        name
        for name in REQUIRED_LOCK_CONFIGURATION_PARAMETER_NAMES
        if name not in available
    ]
    if missing:
        raise LinienConfigurationError(
            "The installed Linien version is missing required lock parameters: "
            + ", ".join(missing)
        )
    return available


def lock_configuration_differences(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> list[str]:
    """Return parameter names whose exact read-back differs from the snapshot."""

    return [
        name
        for name, expected_value in expected.items()
        if name not in actual or actual[name] != expected_value
    ]


def restore_configuration_and_start_manual_lock(
    client: Any,
    configuration: Mapping[str, Any],
    starting_voltage_v: float,
    *,
    unpack_value: Callable[[Any], Any],
    simple_autolock_mode: Any,
) -> None:
    """Restore a verified lock configuration and invoke Linien's normal PID lock."""

    if not math.isfinite(starting_voltage_v) or abs(starting_voltage_v) > 1.0:
        raise LinienConfigurationError(
            "Refusing an invalid FAST OUT relock starting voltage: "
            f"{starting_voltage_v!r}"
        )
    if bool(read_remote_parameter_value(client, "lock", unpack_value)):
        raise LinienConfigurationError(
            "Refusing to rewrite Linien parameters while it reports lock=True"
        )

    current = read_lock_configuration(
        client,
        fresh=True,
        unpack_value=unpack_value,
    )
    changed_names = lock_configuration_differences(configuration, current)

    client.control.exposed_pause_acquisition()
    try:
        for name in changed_names:
            getattr(client.parameters, name).value = deepcopy(configuration[name])

        # This is the automated equivalent of positioning the GUI sweep at the
        # previous dark-point voltage before pressing the normal manual-lock
        # button.  It is RP FAST OUT volts, not amplified EOM volts or ADC counts.
        client.parameters.sweep_center.value = starting_voltage_v
        client.parameters.fetch_additional_signals.value = False
        client.parameters.autolock_mode.value = simple_autolock_mode
        client.parameters.autolock_target_position.value = 0
        client.control.exposed_write_registers()
    finally:
        client.control.exposed_continue_acquisition()

    restored = read_lock_configuration(
        client,
        fresh=True,
        unpack_value=unpack_value,
    )
    failed_names = lock_configuration_differences(configuration, restored)
    read_back_center = float(
        read_remote_parameter_value(client, "sweep_center", unpack_value)
    )
    if not math.isclose(
        read_back_center,
        starting_voltage_v,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        failed_names.append("sweep_center")
    if failed_names:
        raise LinienConfigurationError(
            "Linien configuration read-back failed for: "
            + ", ".join(sorted(set(failed_names)))
        )

    client.control.exposed_start_lock()


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


def decode_lock_observation(
    serialized_plot_data: bytes,
    *,
    locked: bool,
) -> LinienLockObservation:
    """Decode the error/control values needed to assess a recovered lock."""

    if not locked:
        return LinienLockObservation(locked=False)

    plot_data = pickle.loads(serialized_plot_data)
    error_array = np.asarray(plot_data.get("error_signal", []), dtype=float)
    control_array = np.asarray(plot_data.get("control_signal", []), dtype=float)
    if len(error_array) == 0 or len(control_array) == 0:
        return LinienLockObservation(locked=True)
    return LinienLockObservation(
        locked=True,
        error_signal=float(error_array[-1]) * SCALE,
        control_voltage_v=float(control_array[-1]) * SCALE,
    )


def run_logging_loop(
    client: Any,
    recovery: LinienConnectionRecovery,
    recoverable_exceptions: tuple[type[Exception], ...],
    writer: Any,
    output_stream: Any,
    output_file: Path,
    *,
    relock_manager: LinienRelockManager,
    configuration_reader: Callable[[Any], Mapping[str, Any]] = (
        read_lock_configuration
    ),
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
            if is_locked:
                relock_manager.record_locked_sample(
                    error_signal=error_signal,
                    control_voltage_v=lock_voltage,
                    configuration=configuration_reader(client),
                    timestamp_s=time.monotonic(),
                )
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

    (
        Device,
        LinienClient,
        linien_connection_errors,
        ServerNotRunningException,
        unpack_parameter,
        simple_autolock_mode,
    ) = load_linien_client_types()
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

    def connect_candidate(*, autostart_server: bool) -> Any:
        candidate = LinienClient(device)
        try:
            candidate.connect(
                autostart_server=autostart_server,
                use_parameter_cache=True,
            )
        except Exception:
            try:
                candidate.disconnect()
            except Exception:
                pass
            raise
        return candidate

    def connection_factory() -> LinienConnectionResult:
        """Attach first; start Linien only after its specific missing-server error."""

        return attach_or_start_linien_server(
            lambda autostart_server: connect_candidate(
                autostart_server=autostart_server
            ),
            ServerNotRunningException,
            event_writer,
            status_reporter=LOGGER.warning,
        )

    def refresh_and_read_lock(candidate: Any) -> bool:
        candidate.parameters.check_for_changed_parameters()
        return bool(candidate.parameters.lock.value)

    def read_observation(candidate: Any) -> LinienLockObservation:
        candidate.parameters.check_for_changed_parameters()
        locked = bool(candidate.parameters.lock.value)
        if not locked:
            return LinienLockObservation(locked=False)
        return decode_lock_observation(
            candidate.parameters.to_plot.value,
            locked=True,
        )

    def disconnect_client(candidate: Any) -> None:
        candidate.disconnect()

    def is_recoverable_error(error: BaseException) -> bool:
        return isinstance(error, recoverable_exceptions)

    policy = LinienRecoveryPolicy(
        reconnect_backoff_s=RECONNECT_BACKOFF_SECONDS,
        lock_recovery_timeout_s=POST_RECONNECT_LOCK_TIMEOUT_SECONDS,
        lock_poll_interval_s=SAMPLE_PERIOD,
        relock_reference_window_s=RELOCK_REFERENCE_WINDOW_SECONDS,
        relock_reference_guard_s=RELOCK_REFERENCE_GUARD_SECONDS,
        relock_minimum_reference_samples=RELOCK_MINIMUM_REFERENCE_SAMPLES,
        relock_acquisition_timeout_s=RELOCK_ACQUISITION_TIMEOUT_SECONDS,
        relock_settle_s=RELOCK_SETTLE_SECONDS,
        relock_validation_s=RELOCK_VALIDATION_SECONDS,
        relock_minimum_validation_samples=RELOCK_MINIMUM_VALIDATION_SAMPLES,
        relock_quality_ratio_limit=RELOCK_QUALITY_RATIO_LIMIT,
        relock_noise_floor=SCALE,
        fast_output_rail_v=FAST_OUTPUT_RAIL_VOLTS,
    )
    relock_manager = LinienRelockManager(
        read_observation=read_observation,
        read_configuration=lambda candidate, fresh: read_lock_configuration(
            candidate,
            fresh=fresh,
            unpack_value=unpack_parameter if fresh else None,
        ),
        restore_configuration_and_start_lock=(
            lambda candidate, configuration, starting_voltage_v: (
                restore_configuration_and_start_manual_lock(
                    candidate,
                    configuration,
                    starting_voltage_v,
                    unpack_value=unpack_parameter,
                    simple_autolock_mode=simple_autolock_mode,
                )
            )
        ),
        event_writer=event_writer,
        policy=policy,
    )
    recovery = LinienConnectionRecovery(
        connection_factory=connection_factory,
        refresh_and_read_lock=refresh_and_read_lock,
        disconnect_client=disconnect_client,
        is_recoverable_error=is_recoverable_error,
        event_writer=event_writer,
        policy=policy,
        relock_manager=relock_manager,
    )

    event_writer.write(
        "logger_starting",
        red_pitaya_host=RP_HOST,
        linien_client_version=installed_linien_client_version(),
        reconnect_backoff_s=list(RECONNECT_BACKOFF_SECONDS),
        post_reconnect_lock_timeout_s=POST_RECONNECT_LOCK_TIMEOUT_SECONDS,
        attach_before_server_autostart=True,
        automatic_relock_after_mid_run_disconnect=True,
        relock_reference_window_s=RELOCK_REFERENCE_WINDOW_SECONDS,
        relock_reference_guard_s=RELOCK_REFERENCE_GUARD_SECONDS,
        relock_quality_ratio_limit=RELOCK_QUALITY_RATIO_LIMIT,
        fast_output_rail_v=FAST_OUTPUT_RAIL_VOLTS,
    )

    try:
        initial_connection = connection_factory()
        client = initial_connection.client
    except Exception as error:
        event_writer.write(
            "initial_connection_failed",
            error=error,
            include_traceback=True,
        )
        raise

    event_writer.write(
        "initial_connection_succeeded",
        server_started=initial_connection.server_started,
    )
    print("Connected to Red Pitaya.")
    print("Logging to:", filename)
    print("Connection events:", event_writer.path)
    print("Press Ctrl+C to stop.")

    logging_loop_owns_client = False
    try:
        initial_lock_deadline = (
            time.monotonic() + POST_RECONNECT_LOCK_TIMEOUT_SECONDS
        )
        while True:
            try:
                client.parameters.check_for_changed_parameters()
                initial_lock_present = bool(client.parameters.lock.value)
            except recoverable_exceptions as error:
                client = recovery.recover(client, error)
                continue
            if initial_lock_present:
                break
            remaining_s = initial_lock_deadline - time.monotonic()
            if remaining_s <= 0:
                error = LinienLockNotRestoredError(
                    "Initial Linien connection is unlocked. Establish the first "
                    "lock in the Linien GUI before starting the experiment; "
                    "automatic relocking is only allowed after valid locked "
                    "samples have been recorded."
                )
                event_writer.write("initial_lock_missing", error=error)
                raise error
            time.sleep(min(SAMPLE_PERIOD, remaining_s))

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
                relock_manager=relock_manager,
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
