"""Hardware-independent recovery support for a Linien client connection."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import time
import traceback
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo


UK_TIME = ZoneInfo("Europe/London")


class LinienEventWriter(Protocol):
    """Structural interface used by the recovery loop for provenance events."""

    def write(
        self,
        event: str,
        *,
        error: BaseException | None = None,
        include_traceback: bool = False,
        **fields: Any,
    ) -> None:
        """Write one connection or recovery event."""


@dataclass(frozen=True)
class LinienRecoveryPolicy:
    """Timing and validation policy for Linien reconnection and relocking."""

    reconnect_backoff_s: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)
    lock_recovery_timeout_s: float = 10.0
    lock_poll_interval_s: float = 1.0
    relock_reference_window_s: float = 30.0
    relock_reference_guard_s: float = 2.0
    relock_minimum_reference_samples: int = 10
    relock_acquisition_timeout_s: float = 10.0
    relock_settle_s: float = 5.0
    relock_validation_s: float = 10.0
    relock_minimum_validation_samples: int = 5
    relock_quality_ratio_limit: float = 3.0
    relock_noise_floor: float = 1.0 / 8192.0
    fast_output_rail_v: float = 0.98

    def __post_init__(self) -> None:
        if not self.reconnect_backoff_s:
            raise ValueError("reconnect_backoff_s must contain at least one delay")
        if any(
            not math.isfinite(delay) or delay <= 0
            for delay in self.reconnect_backoff_s
        ):
            raise ValueError("all Linien reconnect delays must be finite and positive")
        if (
            not math.isfinite(self.lock_recovery_timeout_s)
            or self.lock_recovery_timeout_s <= 0
        ):
            raise ValueError(
                "lock_recovery_timeout_s must be finite and positive"
            )
        if (
            not math.isfinite(self.lock_poll_interval_s)
            or self.lock_poll_interval_s <= 0
        ):
            raise ValueError("lock_poll_interval_s must be finite and positive")
        positive_floats = {
            "relock_reference_window_s": self.relock_reference_window_s,
            "relock_acquisition_timeout_s": self.relock_acquisition_timeout_s,
            "relock_validation_s": self.relock_validation_s,
            "relock_quality_ratio_limit": self.relock_quality_ratio_limit,
            "relock_noise_floor": self.relock_noise_floor,
            "fast_output_rail_v": self.fast_output_rail_v,
        }
        for name, value in positive_floats.items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        nonnegative_floats = {
            "relock_reference_guard_s": self.relock_reference_guard_s,
            "relock_settle_s": self.relock_settle_s,
        }
        for name, value in nonnegative_floats.items():
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.relock_minimum_reference_samples < 2:
            raise ValueError("relock_minimum_reference_samples must be at least 2")
        if self.relock_minimum_validation_samples < 2:
            raise ValueError("relock_minimum_validation_samples must be at least 2")
        if self.fast_output_rail_v > 1.0:
            raise ValueError("fast_output_rail_v cannot exceed the +/-1 V range")


class LinienLockNotRestoredError(RuntimeError):
    """Raised when a reconnected Linien server remains unlocked."""


class LinienConfigurationError(LinienLockNotRestoredError):
    """Raised when the last known locking configuration cannot be preserved."""


@dataclass(frozen=True)
class LinienConnectionResult:
    """A newly connected client and whether this call started its server."""

    client: Any
    server_started: bool = False


def attach_or_start_linien_server(
    connect_candidate: Callable[[bool], Any],
    server_not_running_exception: type[Exception],
    event_writer: LinienEventWriter,
    *,
    status_reporter: Callable[[str], None] | None = None,
) -> LinienConnectionResult:
    """Attach to an existing server, starting one only for its explicit absence."""

    try:
        return LinienConnectionResult(
            connect_candidate(False),
            server_started=False,
        )
    except server_not_running_exception as error:
        message = (
            "No Linien server is listening; asking Linien to start its server "
            "before reconnecting."
        )
        if status_reporter is not None:
            status_reporter(message)
        event_writer.write("linien_server_missing_starting", error=error)
        client = connect_candidate(True)
        event_writer.write("linien_server_started")
        return LinienConnectionResult(client, server_started=True)


@dataclass(frozen=True)
class LinienLockObservation:
    """One scaled lock observation from Linien's plotted signals."""

    locked: bool
    error_signal: float | None = None
    control_voltage_v: float | None = None


@dataclass(frozen=True)
class _StoredLockSample:
    timestamp_s: float
    error_signal: float
    control_voltage_v: float


@dataclass(frozen=True)
class _LockQuality:
    error_rms: float
    error_robust_sigma: float
    control_robust_sigma_v: float


def _robust_sigma(values: list[float]) -> float:
    """Return a median-absolute-deviation estimate of standard deviation."""

    center = statistics.median(values)
    median_absolute_deviation = statistics.median(
        abs(value - center) for value in values
    )
    return 1.4826 * median_absolute_deviation


def _quality(samples: list[_StoredLockSample]) -> _LockQuality:
    errors = [sample.error_signal for sample in samples]
    controls = [sample.control_voltage_v for sample in samples]
    return _LockQuality(
        error_rms=math.sqrt(sum(value * value for value in errors) / len(errors)),
        error_robust_sigma=_robust_sigma(errors),
        control_robust_sigma_v=_robust_sigma(controls),
    )


def _configuration_differences(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> list[str]:
    differences: list[str] = []
    for name, expected_value in expected.items():
        if name not in actual or actual[name] != expected_value:
            differences.append(name)
    return differences


class LinienRelockManager:
    """Keep a last-known-good lock state and perform one guarded relock."""

    def __init__(
        self,
        *,
        read_observation: Callable[[Any], LinienLockObservation],
        read_configuration: Callable[[Any, bool], Mapping[str, Any]],
        restore_configuration_and_start_lock: Callable[
            [Any, Mapping[str, Any], float], None
        ],
        event_writer: LinienEventWriter,
        policy: LinienRecoveryPolicy,
        status_reporter: Callable[[str], None] = print,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.read_observation = read_observation
        self.read_configuration = read_configuration
        self.restore_configuration_and_start_lock = (
            restore_configuration_and_start_lock
        )
        self.event_writer = event_writer
        self.policy = policy
        self.status_reporter = status_reporter
        self.monotonic = monotonic
        self.sleep = sleep
        self._samples: deque[_StoredLockSample] = deque()
        self._configuration: dict[str, Any] | None = None
        self.last_call_attempted_relock = False

    @property
    def has_reference_state(self) -> bool:
        """Whether any last-known-good configuration has been captured."""

        return self._configuration is not None

    def record_locked_sample(
        self,
        *,
        error_signal: float,
        control_voltage_v: float,
        configuration: Mapping[str, Any],
        timestamp_s: float | None = None,
    ) -> None:
        """Record one trustworthy locked sample and the contemporaneous settings."""

        if not math.isfinite(error_signal) or not math.isfinite(control_voltage_v):
            return
        if not configuration:
            raise ValueError("the Linien lock configuration snapshot is empty")

        now = self.monotonic() if timestamp_s is None else timestamp_s
        self._samples.append(
            _StoredLockSample(now, error_signal, control_voltage_v)
        )
        history_s = (
            self.policy.relock_reference_window_s
            + self.policy.relock_reference_guard_s
            + 2 * self.policy.lock_poll_interval_s
        )
        oldest_allowed = now - history_s
        while self._samples and self._samples[0].timestamp_s < oldest_allowed:
            self._samples.popleft()

        new_configuration = deepcopy(dict(configuration))
        if self._configuration != new_configuration:
            previous = self._configuration
            self._configuration = new_configuration
            self.event_writer.write(
                "lock_configuration_snapshot_updated",
                configuration_parameter_count=len(new_configuration),
                changed_parameter_names=(
                    sorted(new_configuration)
                    if previous is None
                    else sorted(
                        set(_configuration_differences(previous, new_configuration))
                        | set(_configuration_differences(new_configuration, previous))
                    )
                ),
            )

    def recover_lock(
        self,
        client: Any,
        *,
        server_started: bool,
        allow_relock: bool,
        event_fields: Mapping[str, Any] | None = None,
    ) -> None:
        """Accept a preserved lock or make one controlled manual-lock attempt."""

        self.last_call_attempted_relock = False
        fields = dict(event_fields or {})
        deadline = self.monotonic() + self.policy.lock_recovery_timeout_s
        unlocked_event_written = False

        while True:
            observation = self.read_observation(client)
            if observation.locked:
                self._verify_preserved_configuration(client, fields)
                self.event_writer.write(
                    "reconnection_lock_preserved",
                    server_started=server_started,
                    **fields,
                )
                return

            if not unlocked_event_written:
                self.event_writer.write(
                    "reconnected_but_unlocked",
                    server_started=server_started,
                    lock_recovery_timeout_s=self.policy.lock_recovery_timeout_s,
                    **fields,
                )
                unlocked_event_written = True

            remaining_s = deadline - self.monotonic()
            if remaining_s <= 0:
                break
            self.sleep(min(self.policy.lock_poll_interval_s, remaining_s))

        if not allow_relock:
            self._abort(
                "the only automatic relock attempt was interrupted by another "
                "connection failure",
                fields,
            )
        reference_samples = self._reference_samples()
        if self._configuration is None:
            self._abort(
                "no last-known-good Linien configuration is available",
                fields,
            )
        control_channel = self._configuration.get("control_channel")
        sweep_channel = self._configuration.get("sweep_channel")
        if control_channel not in (0, 1) or sweep_channel != control_channel:
            self._abort(
                "automatic voltage seeding requires the sweep and PID control "
                "to use the same FAST OUT channel; saved sweep_channel="
                f"{sweep_channel!r}, control_channel={control_channel!r}",
                fields,
            )

        reference_quality = _quality(reference_samples)
        starting_voltage_v = statistics.median(
            sample.control_voltage_v for sample in reference_samples
        )
        if abs(starting_voltage_v) >= self.policy.fast_output_rail_v:
            self._abort(
                "the previous control-voltage estimate is at the configured "
                f"FAST OUT rail ({starting_voltage_v:.6f} V)",
                fields,
            )

        message = (
            "ATTEMPTING RELOCK AT RP FAST OUT = "
            f"{starting_voltage_v:.6f} V "
            f"(median of {len(reference_samples)} stable samples)"
        )
        self.status_reporter(message)
        self.event_writer.write(
            "relock_attempting",
            server_started=server_started,
            starting_control_voltage_v=starting_voltage_v,
            reference_sample_count=len(reference_samples),
            reference_error_rms=reference_quality.error_rms,
            reference_error_robust_sigma=reference_quality.error_robust_sigma,
            reference_control_robust_sigma_v=(
                reference_quality.control_robust_sigma_v
            ),
            **fields,
        )
        self.last_call_attempted_relock = True
        try:
            self.restore_configuration_and_start_lock(
                client,
                deepcopy(self._configuration),
                starting_voltage_v,
            )
        except LinienConfigurationError as error:
            self._abort(str(error), fields)

        self._wait_for_valid_locked_observation(client, fields)
        self._settle_relock(client, fields)
        validation_samples = self._collect_validation_samples(client, fields)
        validation_quality = _quality(validation_samples)
        self._validate_quality(
            reference_quality,
            validation_quality,
            validation_samples,
            fields,
        )

        final_voltage_v = statistics.median(
            sample.control_voltage_v for sample in validation_samples
        )
        success_message = f"RELOCK SUCCESSFUL AT {final_voltage_v:.6f} V"
        self.status_reporter(success_message)
        self.event_writer.write(
            "relock_successful",
            starting_control_voltage_v=starting_voltage_v,
            final_median_control_voltage_v=final_voltage_v,
            validation_sample_count=len(validation_samples),
            validation_error_rms=validation_quality.error_rms,
            validation_error_robust_sigma=(
                validation_quality.error_robust_sigma
            ),
            validation_control_robust_sigma_v=(
                validation_quality.control_robust_sigma_v
            ),
            **fields,
        )

    def _verify_preserved_configuration(
        self,
        client: Any,
        fields: Mapping[str, Any],
    ) -> None:
        if self._configuration is None:
            self._abort(
                "the connection returned locked but no last-known-good "
                "configuration is available",
                fields,
            )
        try:
            current = dict(self.read_configuration(client, True))
        except LinienConfigurationError as error:
            self._abort(str(error), fields)
        differences = _configuration_differences(self._configuration, current)
        if differences:
            self._abort(
                "the reconnected locked server does not match the saved "
                "configuration: " + ", ".join(differences),
                fields,
            )

    def _reference_samples(self) -> list[_StoredLockSample]:
        if not self._samples:
            self._abort("no pre-disconnection locked samples are available", {})
        latest = self._samples[-1].timestamp_s
        end = latest - self.policy.relock_reference_guard_s
        start = end - self.policy.relock_reference_window_s
        samples = [
            sample for sample in self._samples if start <= sample.timestamp_s <= end
        ]
        if len(samples) < self.policy.relock_minimum_reference_samples:
            self._abort(
                "only "
                f"{len(samples)} stable pre-disconnection samples are available; "
                f"{self.policy.relock_minimum_reference_samples} are required",
                {},
            )
        return samples

    def _wait_for_valid_locked_observation(
        self,
        client: Any,
        fields: Mapping[str, Any],
    ) -> None:
        deadline = self.monotonic() + self.policy.relock_acquisition_timeout_s
        while True:
            observation = self.read_observation(client)
            if self._observation_is_valid_and_locked(observation):
                return
            remaining_s = deadline - self.monotonic()
            if remaining_s <= 0:
                self._abort(
                    "Linien did not produce valid locked error/control data "
                    f"within {self.policy.relock_acquisition_timeout_s:g} seconds",
                    fields,
                )
            self.sleep(min(self.policy.lock_poll_interval_s, remaining_s))

    def _settle_relock(
        self,
        client: Any,
        fields: Mapping[str, Any],
    ) -> None:
        deadline = self.monotonic() + self.policy.relock_settle_s
        while self.monotonic() < deadline:
            observation = self.read_observation(client)
            if not observation.locked:
                self._abort("Linien lost lock during the relock settling period", fields)
            remaining_s = deadline - self.monotonic()
            if remaining_s > 0:
                self.sleep(min(self.policy.lock_poll_interval_s, remaining_s))

    def _collect_validation_samples(
        self,
        client: Any,
        fields: Mapping[str, Any],
    ) -> list[_StoredLockSample]:
        deadline = self.monotonic() + self.policy.relock_validation_s
        samples: list[_StoredLockSample] = []
        while True:
            observation = self.read_observation(client)
            if not observation.locked:
                self._abort("Linien lost lock during relock validation", fields)
            if self._observation_is_valid_and_locked(observation):
                samples.append(
                    _StoredLockSample(
                        self.monotonic(),
                        float(observation.error_signal),
                        float(observation.control_voltage_v),
                    )
                )
            remaining_s = deadline - self.monotonic()
            if remaining_s <= 0:
                break
            self.sleep(min(self.policy.lock_poll_interval_s, remaining_s))

        if len(samples) < self.policy.relock_minimum_validation_samples:
            self._abort(
                "only "
                f"{len(samples)} valid post-relock samples were received; "
                f"{self.policy.relock_minimum_validation_samples} are required",
                fields,
            )
        return samples

    def _validate_quality(
        self,
        reference: _LockQuality,
        validation: _LockQuality,
        validation_samples: list[_StoredLockSample],
        fields: Mapping[str, Any],
    ) -> None:
        if any(
            abs(sample.control_voltage_v) >= self.policy.fast_output_rail_v
            for sample in validation_samples
        ):
            self._abort("the relocked FAST OUT control signal reached its rail", fields)

        comparisons = (
            (
                "error RMS",
                validation.error_rms,
                reference.error_rms,
            ),
            (
                "error variation",
                validation.error_robust_sigma,
                reference.error_robust_sigma,
            ),
            (
                "control-voltage variation",
                validation.control_robust_sigma_v,
                reference.control_robust_sigma_v,
            ),
        )
        failures: list[str] = []
        for label, observed, baseline in comparisons:
            denominator = max(baseline, self.policy.relock_noise_floor)
            ratio = observed / denominator
            if ratio > self.policy.relock_quality_ratio_limit:
                failures.append(f"{label} is {ratio:.2f}x the pre-loss baseline")
        if failures:
            self._abort("; ".join(failures), fields)

    @staticmethod
    def _observation_is_valid_and_locked(
        observation: LinienLockObservation,
    ) -> bool:
        return (
            observation.locked
            and observation.error_signal is not None
            and observation.control_voltage_v is not None
            and math.isfinite(observation.error_signal)
            and math.isfinite(observation.control_voltage_v)
        )

    def _abort(self, reason: str, fields: Mapping[str, Any]) -> None:
        message = f"RELOCK FAILED OR ABORTED: {reason}"
        self.status_reporter(message)
        error = LinienLockNotRestoredError(message)
        self.event_writer.write(
            "relock_failed_or_aborted",
            error=error,
            reason=reason,
            **fields,
        )
        raise error


class JsonlLinienEventWriter:
    """Append timestamped Linien recovery events to a JSONL file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def write(
        self,
        event: str,
        *,
        error: BaseException | None = None,
        include_traceback: bool = False,
        **fields: Any,
    ) -> None:
        timestamp_utc = datetime.now(timezone.utc)
        record: dict[str, Any] = {
            "timestamp_utc": timestamp_utc.isoformat().replace("+00:00", "Z"),
            "timestamp_local": timestamp_utc.astimezone(UK_TIME).isoformat(),
            "event": event,
            **fields,
        }
        if error is not None:
            record["error_type"] = type(error).__name__
            record["error_message"] = str(error)
            if include_traceback:
                record["traceback"] = "".join(
                    traceback.format_exception(
                        type(error),
                        error,
                        error.__traceback__,
                    )
                )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")


class LinienConnectionRecovery:
    """Replace a failed Linien client and verify that its lock recovers."""

    def __init__(
        self,
        *,
        connection_factory: Callable[[], Any],
        refresh_and_read_lock: Callable[[Any], bool],
        disconnect_client: Callable[[Any], None],
        is_recoverable_error: Callable[[BaseException], bool],
        event_writer: LinienEventWriter,
        policy: LinienRecoveryPolicy | None = None,
        relock_manager: LinienRelockManager | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.connection_factory = connection_factory
        self.refresh_and_read_lock = refresh_and_read_lock
        self.disconnect_client = disconnect_client
        self.is_recoverable_error = is_recoverable_error
        self.event_writer = event_writer
        self.policy = policy or LinienRecoveryPolicy()
        self.relock_manager = relock_manager
        self.monotonic = monotonic
        self.sleep = sleep

    def recover(self, failed_client: Any, cause: BaseException) -> Any:
        """Reconnect indefinitely, then require lock within the grace period."""

        if not self.is_recoverable_error(cause):
            raise TypeError("recover() requires a recoverable transport error")

        recovery_started = self.monotonic()
        self.event_writer.write(
            "connection_lost",
            error=cause,
            include_traceback=True,
        )
        self.disconnect_safely(failed_client, context="failed_client")

        attempt = 0
        relock_already_attempted = False
        while True:
            delay_s = self.policy.reconnect_backoff_s[
                min(attempt, len(self.policy.reconnect_backoff_s) - 1)
            ]
            self.event_writer.write(
                "reconnection_waiting",
                reconnection_attempt_number=attempt + 1,
                reconnection_delay_s=delay_s,
                recovery_outage_duration_s=max(
                    0.0,
                    self.monotonic() - recovery_started,
                ),
            )
            self.sleep(delay_s)
            attempt += 1
            candidate = None
            server_started = False

            try:
                connection_result = self.connection_factory()
                if isinstance(connection_result, LinienConnectionResult):
                    candidate = connection_result.client
                    server_started = connection_result.server_started
                else:
                    candidate = connection_result
                self.event_writer.write(
                    "reconnection_transport_established",
                    reconnection_attempt_number=attempt,
                    server_started=server_started,
                    recovery_outage_duration_s=max(
                        0.0,
                        self.monotonic() - recovery_started,
                    ),
                )
                if (
                    self.relock_manager is None
                    or not self.relock_manager.has_reference_state
                ):
                    self._wait_for_lock(candidate, attempt, recovery_started)
                else:
                    self.relock_manager.recover_lock(
                        candidate,
                        server_started=server_started,
                        allow_relock=not relock_already_attempted,
                        event_fields={
                            "reconnection_attempt_number": attempt,
                            "recovery_outage_duration_s": max(
                                0.0,
                                self.monotonic() - recovery_started,
                            ),
                        },
                    )
            except LinienLockNotRestoredError:
                self.disconnect_safely(
                    candidate,
                    context="reconnected_but_unlocked_client",
                )
                raise
            except Exception as error:
                if (
                    self.relock_manager is not None
                    and self.relock_manager.last_call_attempted_relock
                ):
                    relock_already_attempted = True
                self.disconnect_safely(
                    candidate,
                    context="failed_reconnection_candidate",
                )
                if not self.is_recoverable_error(error):
                    self.event_writer.write(
                        "reconnection_fatal_error",
                        error=error,
                        include_traceback=True,
                        reconnection_attempt_number=attempt,
                        recovery_outage_duration_s=max(
                            0.0,
                            self.monotonic() - recovery_started,
                        ),
                    )
                    raise

                self.event_writer.write(
                    "reconnection_attempt_failed",
                    error=error,
                    include_traceback=True,
                    reconnection_attempt_number=attempt,
                    recovery_outage_duration_s=max(
                        0.0,
                        self.monotonic() - recovery_started,
                    ),
                )
                continue

            self.event_writer.write(
                "reconnection_succeeded",
                reconnection_attempt_number=attempt,
                recovery_outage_duration_s=max(
                    0.0,
                    self.monotonic() - recovery_started,
                ),
                server_started=server_started,
                lock_confirmed=True,
            )
            return candidate

    def _wait_for_lock(
        self,
        client: Any,
        attempt: int,
        recovery_started: float,
    ) -> None:
        lock_check_started = self.monotonic()
        deadline = lock_check_started + self.policy.lock_recovery_timeout_s
        unlocked_event_written = False

        while True:
            if self.refresh_and_read_lock(client):
                return

            if not unlocked_event_written:
                self.event_writer.write(
                    "reconnected_but_unlocked",
                    reconnection_attempt_number=attempt,
                    lock_recovery_timeout_s=(
                        self.policy.lock_recovery_timeout_s
                    ),
                    recovery_outage_duration_s=max(
                        0.0,
                        self.monotonic() - recovery_started,
                    ),
                )
                unlocked_event_written = True

            remaining_s = deadline - self.monotonic()
            if remaining_s <= 0:
                error = LinienLockNotRestoredError(
                    "Linien reconnected but did not report lock=True within "
                    f"{self.policy.lock_recovery_timeout_s:g} seconds"
                )
                self.event_writer.write(
                    "lock_not_restored",
                    error=error,
                    reconnection_attempt_number=attempt,
                    lock_recovery_timeout_s=(
                        self.policy.lock_recovery_timeout_s
                    ),
                    recovery_outage_duration_s=max(
                        0.0,
                        self.monotonic() - recovery_started,
                    ),
                )
                raise error

            self.sleep(min(self.policy.lock_poll_interval_s, remaining_s))

    def disconnect_safely(self, client: Any, *, context: str) -> None:
        """Best-effort close of a client without masking the primary error."""

        if client is None:
            return
        try:
            self.disconnect_client(client)
        except Exception as error:
            self.event_writer.write(
                "client_disconnect_failed",
                error=error,
                include_traceback=True,
                context=context,
            )
