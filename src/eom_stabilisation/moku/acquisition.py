"""Fault classification and bounded recovery for Moku frame acquisition.

The helpers in this module do not import the Moku SDK and never connect to
hardware by themselves.  Hardware objects and clocks are injected so the
recovery policy can be tested entirely with fake instruments.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import logging
from pathlib import Path
import socket
import time
import traceback
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo


LOGGER = logging.getLogger(__name__)
LONDON_TIMEZONE = ZoneInfo("Europe/London")


class AcquisitionFailureKind(str, Enum):
    """Scientifically and operationally distinct acquisition failures."""

    EXPECTED_TRIGGER_TIMEOUT = "expected_trigger_timeout"
    MALFORMED_FRAME = "malformed_frame"
    TRANSIENT_TRANSPORT = "transient_transport"
    STALE_API_CONNECTION = "stale_api_connection"
    OWNERSHIP_LOSS = "ownership_loss"
    UNRECOVERABLE = "unrecoverable"


class AcquisitionRecoveryError(RuntimeError):
    """Raised after the configured bounded recovery attempts are exhausted."""


class MokuConnectionAddressesExhausted(ConnectionError):
    """Raised when neither configured Moku address can create a session."""


@dataclass(frozen=True)
class OscilloscopeConfiguration:
    """Complete Moku configuration that must be identical after recovery."""

    address: str
    force_connect: bool
    frontend_channel: int
    frontend_impedance: str
    frontend_coupling: str
    frontend_range: str
    channel_sources: tuple[tuple[int, str], ...]
    timebase_start_s: float
    timebase_end_s: float
    timebase_max_length: int
    trigger_mode: str
    trigger_type: str
    trigger_source: str
    trigger_level_v: float
    trigger_edge: str
    waveform_channel: int
    waveform_type: str
    waveform_amplitude_vpp: float
    waveform_offset_v: float
    waveform_frequency_hz: float
    waveform_pulse_width_s: float
    waveform_edge_time_s: float
    fallback_address: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.address, str) or not self.address.strip():
            raise ValueError("primary Moku address must not be empty")
        if self.fallback_address is not None and not self.fallback_address.strip():
            raise ValueError("fallback Moku address must not be empty")
        if (
            self.fallback_address is not None
            and self.fallback_address.strip().casefold()
            == self.address.strip().casefold()
        ):
            raise ValueError("fallback Moku address must differ from primary")

    def connection_addresses(self) -> tuple[tuple[str, str], ...]:
        """Return the primary and optional distinct fallback addresses."""

        primary = self.address.strip()
        addresses = [("primary", primary)]
        if self.fallback_address is not None:
            fallback = self.fallback_address.strip()
            addresses.append(("fallback", fallback))
        return tuple(addresses)

    def metadata(self) -> dict[str, Any]:
        """Return JSON-compatible configuration metadata."""

        result = asdict(self)
        result["channel_sources"] = [
            {"channel": channel, "source": source}
            for channel, source in self.channel_sources
        ]
        return result


def resolve_connection_address(address: str) -> tuple[str, ...]:
    """Resolve an address using the same operating-system service as HTTP.

    This performs name resolution only. It does not open a connection to the
    Moku or change any device state. The Moku SDK requires brackets around an
    IPv6 literal, while ``socket.getaddrinfo`` requires the literal without
    those URL brackets.
    """

    resolution_address = address
    if address.startswith("[") and address.endswith("]"):
        resolution_address = address[1:-1]

    address_info = socket.getaddrinfo(
        resolution_address,
        80,
        type=socket.SOCK_STREAM,
    )
    resolved_addresses = tuple(
        dict.fromkeys(str(sockaddr[0]) for *_, sockaddr in address_info)
    )
    if not resolved_addresses:
        raise OSError(f"No network address was returned for {address!r}")
    return resolved_addresses


class MokuConnectionFactory:
    """Create a Moku session using primary then explicitly set fallback.

    The Moku constructor is injected so this class can be tested without the
    SDK or laboratory network. A fallback is never discovered or guessed.
    """

    def __init__(
        self,
        instrument_constructor: Callable[..., Any],
        configuration: OscilloscopeConfiguration,
        *,
        event_writer: Any | None = None,
        address_resolver: Callable[[str], Sequence[str]] = (
            resolve_connection_address
        ),
    ) -> None:
        self.instrument_constructor = instrument_constructor
        self.configuration = configuration
        self.event_writer = event_writer
        self.address_resolver = address_resolver
        self.selected_address: str | None = None
        self.selected_address_role: str | None = None
        self.selected_resolved_addresses: tuple[str, ...] = ()

    def _write_event(
        self,
        event: str,
        *,
        error: BaseException | None = None,
        **fields: Any,
    ) -> None:
        if self.event_writer is None:
            return
        try:
            self.event_writer.write(
                event,
                error=error,
                include_traceback=error is not None,
                **fields,
            )
        except Exception:
            LOGGER.exception("Could not record Moku connection event %s", event)

    def __call__(self) -> Any:
        """Return a session, trying each configured address once in order."""

        failures: list[tuple[str, str, BaseException]] = []
        self.selected_address = None
        self.selected_address_role = None
        self.selected_resolved_addresses = ()

        for address_role, address in self.configuration.connection_addresses():
            try:
                resolved_addresses = tuple(self.address_resolver(address))
                if not resolved_addresses:
                    raise OSError(
                        f"No network address was returned for {address!r}"
                    )
            except Exception as error:
                failures.append((address_role, address, error))
                self._write_event(
                    "connection_address_resolution_failed",
                    error=error,
                    connection_address=address,
                    address_role=address_role,
                )
                LOGGER.warning(
                    "Could not resolve %s Moku address %s: %r",
                    address_role,
                    address,
                    error,
                )
                continue

            self._write_event(
                "connection_address_resolved",
                connection_address=address,
                address_role=address_role,
                resolved_addresses=list(resolved_addresses),
            )
            try:
                instrument = self.instrument_constructor(
                    address,
                    force_connect=self.configuration.force_connect,
                )
            except Exception as error:
                failures.append((address_role, address, error))
                self._write_event(
                    "connection_address_attempt_failed",
                    error=error,
                    connection_address=address,
                    address_role=address_role,
                    resolved_addresses=list(resolved_addresses),
                )
                LOGGER.warning(
                    "Could not connect using %s Moku address %s: %r",
                    address_role,
                    address,
                    error,
                )
                continue

            self.selected_address = address
            self.selected_address_role = address_role
            self.selected_resolved_addresses = resolved_addresses
            self._write_event(
                "connection_address_selected",
                connection_address=address,
                address_role=address_role,
                resolved_addresses=list(resolved_addresses),
                fallback_used=address_role == "fallback",
            )
            if address_role == "fallback":
                LOGGER.warning("Using configured fallback Moku address %s", address)
            else:
                LOGGER.info("Using primary Moku address %s", address)
            return instrument

        failure_summary = "; ".join(
            f"{role} {address!r}: {type(error).__name__}: {error}"
            for role, address, error in failures
        )
        connection_error = MokuConnectionAddressesExhausted(
            "Could not create a Moku session using any configured address"
            + (f" ({failure_summary})" if failure_summary else "")
        )
        if failures:
            raise connection_error from failures[-1][2]
        raise connection_error


@dataclass(frozen=True)
class RecoveryPolicy:
    """Finite retry limits; expected trigger timeouts do not consume them."""

    trigger_timeout_retry_delay_s: float = 0.1
    trigger_timeout_report_interval_s: float = 60.0
    transport_errors_before_reconnect: int = 2
    malformed_frames_before_reconnect: int = 5
    reconnect_backoff_s: tuple[float, ...] = (1.0, 2.0, 5.0)
    max_recovery_cycles_without_valid_frame: int = 3

    def __post_init__(self) -> None:
        if self.trigger_timeout_retry_delay_s < 0:
            raise ValueError("trigger timeout retry delay cannot be negative")
        if self.trigger_timeout_report_interval_s <= 0:
            raise ValueError("trigger timeout report interval must be positive")
        if self.transport_errors_before_reconnect <= 0:
            raise ValueError("transport error limit must be positive")
        if self.malformed_frames_before_reconnect <= 0:
            raise ValueError("malformed frame limit must be positive")
        if not self.reconnect_backoff_s or any(
            delay < 0 for delay in self.reconnect_backoff_s
        ):
            raise ValueError("reconnect backoff must contain non-negative delays")
        if self.max_recovery_cycles_without_valid_frame <= 0:
            raise ValueError("recovery cycle limit must be positive")


@dataclass
class AcquisitionHealth:
    """Counters and timestamps retained across acquisition attempts."""

    consecutive_trigger_timeouts: int = 0
    total_trigger_timeouts: int = 0
    consecutive_connection_errors: int = 0
    total_connection_errors: int = 0
    consecutive_malformed_frames: int = 0
    total_malformed_frames: int = 0
    recovery_cycles_since_valid_frame: int = 0
    total_reconnection_attempts: int = 0
    last_valid_frame_monotonic: float | None = None
    last_valid_frame_timestamp_utc: str | None = None
    trigger_timeout_streak_started_monotonic: float | None = None
    last_trigger_timeout_report_monotonic: float | None = None


def _exception_names(error: BaseException) -> set[str]:
    names: set[str] = set()
    for error_type in type(error).__mro__:
        names.add(error_type.__name__)
        names.add(f"{error_type.__module__}.{error_type.__name__}")
    return names


def classify_acquisition_exception(error: BaseException) -> AcquisitionFailureKind:
    """Classify one exact acquisition exception without importing the SDK.

    Only the Moku device's documented new-frame timeout wording is considered
    an expected optical-trigger timeout.  Generic timeout wording remains a
    transport error so a network failure cannot be silently misclassified.
    """

    message = str(error).lower()
    names = _exception_names(error)

    if "api connection already exists" in message:
        return AcquisitionFailureKind.STALE_API_CONNECTION

    expected_trigger_timeout_messages = (
        "timeout before fetching the new frame",
        "timeout before fetching new frame",
    )
    if any(text in message for text in expected_trigger_timeout_messages):
        return AcquisitionFailureKind.EXPECTED_TRIGGER_TIMEOUT

    if (
        "client key" in message
        or "not owner" in message
        or "ownership lost" in message
        or "no longer owns" in message
    ):
        return AcquisitionFailureKind.OWNERSHIP_LOSS

    transport_type_names = {
        "ConnectTimeout",
        "ConnectionError",
        "MokuNotFound",
        "NameResolutionError",
        "NetworkError",
        "NewConnectionError",
        "ReadTimeout",
        "Timeout",
        "TimeoutError",
        "gaierror",
    }
    if names.intersection(transport_type_names) or any(
        name.startswith("requests.exceptions.")
        and name.rsplit(".", 1)[-1] in transport_type_names
        for name in names
    ):
        return AcquisitionFailureKind.TRANSIENT_TRANSPORT

    transport_message_fragments = (
        "can't connect to the api server",
        "connection aborted",
        "connection reset",
        "failed to resolve",
        "getaddrinfo failed",
        "max retries exceeded",
        "name resolution",
        "remote end closed connection",
        "timeout before receiving response",
        "timeout while waiting for a response",
    )
    if any(fragment in message for fragment in transport_message_fragments):
        return AcquisitionFailureKind.TRANSIENT_TRANSPORT

    return AcquisitionFailureKind.UNRECOVERABLE


class JsonlAcquisitionEventWriter:
    """Append machine-readable, timezone-aware acquisition events."""

    def __init__(
        self,
        path: Path,
        *,
        sdk_version: str,
        configuration: OscilloscopeConfiguration,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.sdk_version = sdk_version
        self.configuration = configuration.metadata()
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))

    def write(
        self,
        event: str,
        *,
        error: BaseException | None = None,
        include_traceback: bool = False,
        **fields: Any,
    ) -> None:
        """Append and flush one event without overwriting earlier records."""

        timestamp_utc = self._utc_now()
        if timestamp_utc.tzinfo is None:
            raise ValueError("event timestamps must be timezone-aware")
        timestamp_utc = timestamp_utc.astimezone(timezone.utc)
        payload: dict[str, Any] = {
            "timestamp_utc": timestamp_utc.isoformat().replace("+00:00", "Z"),
            "timestamp_local": timestamp_utc.astimezone(LONDON_TIMEZONE).isoformat(),
            "event": event,
            "moku_sdk_version": self.sdk_version,
            "configured_primary_address": self.configuration["address"],
            "configured_fallback_address": self.configuration["fallback_address"],
            "trigger_source": self.configuration["trigger_source"],
            "trigger_level_v": self.configuration["trigger_level_v"],
            "waveform_settings": {
                key: value
                for key, value in self.configuration.items()
                if key.startswith("waveform_")
            },
            **fields,
        }
        if error is not None:
            payload["exception_class"] = (
                f"{type(error).__module__}.{type(error).__name__}"
            )
            payload["exception_repr"] = repr(error)
            if include_traceback:
                payload["traceback"] = "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as event_file:
            event_file.write(json.dumps(payload, sort_keys=True, allow_nan=False))
            event_file.write("\n")
            event_file.flush()


class MokuAcquisitionManager:
    """Acquire frames and recover boundedly from non-trigger failures."""

    def __init__(
        self,
        instrument: Any,
        *,
        instrument_factory: Callable[[], Any],
        apply_configuration: Callable[[Any], None],
        verify_connection: Callable[[Any], None],
        event_writer: JsonlAcquisitionEventWriter,
        before_reconnect: Callable[[str], None],
        cleanup_failed_instrument: Callable[[Any], None],
        policy: RecoveryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.instrument = instrument
        self.instrument_factory = instrument_factory
        self.apply_configuration = apply_configuration
        self.verify_connection = verify_connection
        self.event_writer = event_writer
        self.before_reconnect = before_reconnect
        self.cleanup_failed_instrument = cleanup_failed_instrument
        self.policy = policy or RecoveryPolicy()
        self.sleep = sleep
        self.monotonic = monotonic
        self.health = AcquisitionHealth()

    def _time_since_last_valid_frame_s(self, now: float) -> float | None:
        if self.health.last_valid_frame_monotonic is None:
            return None
        return max(0.0, now - self.health.last_valid_frame_monotonic)

    def _health_fields(self, now: float) -> dict[str, Any]:
        return {
            "consecutive_timeout_count": self.health.consecutive_trigger_timeouts,
            "total_timeout_count": self.health.total_trigger_timeouts,
            "consecutive_connection_error_count": (
                self.health.consecutive_connection_errors
            ),
            "total_connection_error_count": self.health.total_connection_errors,
            "consecutive_malformed_frame_count": (
                self.health.consecutive_malformed_frames
            ),
            "total_reconnection_attempts": self.health.total_reconnection_attempts,
            "last_valid_frame_timestamp_utc": (
                self.health.last_valid_frame_timestamp_utc
            ),
            "time_since_last_valid_frame_s": (
                self._time_since_last_valid_frame_s(now)
            ),
        }

    def acquire_frame(
        self,
        *,
        timeout: float,
        wait_reacquire: bool,
        wait_complete: bool,
    ) -> Mapping[str, Sequence[Any]] | None:
        """Return a frame, or ``None`` after a handled retryable failure."""

        try:
            frame = self.instrument.get_data(
                timeout=timeout,
                wait_reacquire=wait_reacquire,
                wait_complete=wait_complete,
            )
        except Exception as error:
            failure_kind = classify_acquisition_exception(error)
            if failure_kind is AcquisitionFailureKind.EXPECTED_TRIGGER_TIMEOUT:
                self._record_trigger_timeout(error)
                self.sleep(self.policy.trigger_timeout_retry_delay_s)
                return None
            if failure_kind is AcquisitionFailureKind.TRANSIENT_TRANSPORT:
                self._record_connection_error(failure_kind, error)
                if (
                    self.health.consecutive_connection_errors
                    >= self.policy.transport_errors_before_reconnect
                ):
                    self._recover_connection(failure_kind.value, error)
                else:
                    self.sleep(self.policy.reconnect_backoff_s[0])
                return None
            if failure_kind in (
                AcquisitionFailureKind.STALE_API_CONNECTION,
                AcquisitionFailureKind.OWNERSHIP_LOSS,
            ):
                self._record_connection_error(failure_kind, error)
                self._recover_connection(failure_kind.value, error)
                return None

            now = self.monotonic()
            self.event_writer.write(
                "unrecoverable_acquisition_error",
                error=error,
                include_traceback=True,
                failure_kind=failure_kind.value,
                **self._health_fields(now),
            )
            LOGGER.error("Unrecoverable Moku acquisition error: %r", error)
            raise

        self.health.consecutive_connection_errors = 0
        return frame

    def _record_trigger_timeout(self, error: BaseException) -> None:
        now = self.monotonic()
        health = self.health
        health.consecutive_connection_errors = 0
        health.consecutive_trigger_timeouts += 1
        health.total_trigger_timeouts += 1
        if health.trigger_timeout_streak_started_monotonic is None:
            health.trigger_timeout_streak_started_monotonic = now

        last_report = health.last_trigger_timeout_report_monotonic
        should_report = (
            health.consecutive_trigger_timeouts == 1
            or last_report is None
            or now - last_report >= self.policy.trigger_timeout_report_interval_s
        )
        if not should_report:
            return

        health.last_trigger_timeout_report_monotonic = now
        duration = max(0.0, now - health.trigger_timeout_streak_started_monotonic)
        self.event_writer.write(
            "expected_trigger_timeout",
            error=error,
            include_traceback=health.consecutive_trigger_timeouts == 1,
            trigger_timeout_streak_duration_s=duration,
            **self._health_fields(now),
        )
        LOGGER.warning(
            "No Input trigger yet: %d consecutive timeout(s), %.1f s in streak",
            health.consecutive_trigger_timeouts,
            duration,
        )

    def _record_connection_error(
        self,
        failure_kind: AcquisitionFailureKind,
        error: BaseException,
    ) -> None:
        now = self.monotonic()
        self.health.consecutive_connection_errors += 1
        self.health.total_connection_errors += 1
        self.event_writer.write(
            "connection_error",
            error=error,
            include_traceback=True,
            failure_kind=failure_kind.value,
            **self._health_fields(now),
        )
        LOGGER.error("Moku %s: %r", failure_kind.value, error)

    def record_malformed_frame(self, error: BaseException) -> None:
        """Record an invalid frame and recover after a bounded threshold."""

        now = self.monotonic()
        self.health.consecutive_malformed_frames += 1
        self.health.total_malformed_frames += 1
        self.event_writer.write(
            "malformed_frame",
            error=error,
            include_traceback=True,
            failure_kind=AcquisitionFailureKind.MALFORMED_FRAME.value,
            **self._health_fields(now),
        )
        LOGGER.warning("Discarding malformed Moku frame: %r", error)
        if (
            self.health.consecutive_malformed_frames
            >= self.policy.malformed_frames_before_reconnect
        ):
            self._recover_connection(
                AcquisitionFailureKind.MALFORMED_FRAME.value,
                error,
            )

    def record_valid_frame(self) -> None:
        """Mark acquisition healthy and close any preceding failure streak."""

        now = self.monotonic()
        timestamp_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        had_failure_streak = any(
            (
                self.health.consecutive_trigger_timeouts,
                self.health.consecutive_connection_errors,
                self.health.consecutive_malformed_frames,
                self.health.recovery_cycles_since_valid_frame,
            )
        )
        timeout_count = self.health.consecutive_trigger_timeouts
        connection_error_count = self.health.consecutive_connection_errors
        malformed_count = self.health.consecutive_malformed_frames
        recovery_cycles = self.health.recovery_cycles_since_valid_frame

        self.health.last_valid_frame_monotonic = now
        self.health.last_valid_frame_timestamp_utc = timestamp_utc
        self.health.consecutive_trigger_timeouts = 0
        self.health.consecutive_connection_errors = 0
        self.health.consecutive_malformed_frames = 0
        self.health.recovery_cycles_since_valid_frame = 0
        self.health.trigger_timeout_streak_started_monotonic = None
        self.health.last_trigger_timeout_report_monotonic = None

        if had_failure_streak:
            self.event_writer.write(
                "valid_frame_resumed",
                preceding_timeout_count=timeout_count,
                preceding_connection_error_count=connection_error_count,
                preceding_malformed_frame_count=malformed_count,
                preceding_recovery_cycles=recovery_cycles,
                **self._health_fields(now),
            )
            LOGGER.info("Valid Moku frame acquisition resumed")

    def _recover_connection(self, reason: str, cause: BaseException) -> None:
        health = self.health
        if (
            health.recovery_cycles_since_valid_frame
            >= self.policy.max_recovery_cycles_without_valid_frame
        ):
            now = self.monotonic()
            self.event_writer.write(
                "recovery_cycle_limit_reached",
                error=cause,
                include_traceback=True,
                recovery_reason=reason,
                **self._health_fields(now),
            )
            raise AcquisitionRecoveryError(
                "Moku recovery cycle limit reached without a subsequent valid frame"
            ) from cause

        health.recovery_cycles_since_valid_frame += 1
        self.before_reconnect(reason)
        now = self.monotonic()
        self.event_writer.write(
            "recovery_started",
            error=cause,
            recovery_reason=reason,
            recovery_cycle=health.recovery_cycles_since_valid_frame,
            waveform_may_restart=True,
            **self._health_fields(now),
        )

        old_instrument = self.instrument
        try:
            old_instrument.relinquish_ownership()
        except Exception as error:
            self.event_writer.write(
                "old_session_relinquish_failed",
                error=error,
                include_traceback=True,
                recovery_reason=reason,
                **self._health_fields(self.monotonic()),
            )

        last_error: BaseException = cause
        for attempt, delay_s in enumerate(self.policy.reconnect_backoff_s, start=1):
            self.sleep(delay_s)
            health.total_reconnection_attempts += 1
            candidate = None
            try:
                candidate = self.instrument_factory()
                self.apply_configuration(candidate)
                self.verify_connection(candidate)
            except Exception as error:
                last_error = error
                self.event_writer.write(
                    "reconnection_attempt_failed",
                    error=error,
                    include_traceback=True,
                    recovery_reason=reason,
                    reconnection_attempt_number=attempt,
                    reconnection_delay_s=delay_s,
                    **self._health_fields(self.monotonic()),
                )
                if candidate is not None:
                    try:
                        self.cleanup_failed_instrument(candidate)
                    except Exception as cleanup_error:
                        self.event_writer.write(
                            "failed_candidate_cleanup_failed",
                            error=cleanup_error,
                            include_traceback=True,
                            recovery_reason=reason,
                            reconnection_attempt_number=attempt,
                            **self._health_fields(self.monotonic()),
                        )
                continue

            self.instrument = candidate
            health.consecutive_connection_errors = 0
            health.consecutive_malformed_frames = 0
            self.event_writer.write(
                "reconnection_succeeded",
                recovery_reason=reason,
                reconnection_attempt_number=attempt,
                recovery_cycle=health.recovery_cycles_since_valid_frame,
                waveform_restarted=True,
                **self._health_fields(self.monotonic()),
            )
            LOGGER.warning(
                "Moku connection reconstructed after %s; waveform timing may have restarted",
                reason,
            )
            return

        self.event_writer.write(
            "reconnection_exhausted",
            error=last_error,
            include_traceback=True,
            recovery_reason=reason,
            reconnection_attempt_number=len(self.policy.reconnect_backoff_s),
            **self._health_fields(self.monotonic()),
        )
        raise AcquisitionRecoveryError(
            "Moku reconnection failed after "
            f"{len(self.policy.reconnect_backoff_s)} attempts"
        ) from last_error


def apply_oscilloscope_configuration(
    instrument: Any,
    configuration: OscilloscopeConfiguration,
) -> None:
    """Apply the complete acquisition and pulse configuration in a fixed order."""

    instrument.set_frontend(
        configuration.frontend_channel,
        configuration.frontend_impedance,
        configuration.frontend_coupling,
        configuration.frontend_range,
    )
    instrument.set_sources(
        [
            {"channel": channel, "source": source}
            for channel, source in configuration.channel_sources
        ]
    )
    instrument.set_timebase(
        configuration.timebase_start_s,
        configuration.timebase_end_s,
        max_length=configuration.timebase_max_length,
    )
    instrument.set_trigger(
        mode=configuration.trigger_mode,
        type=configuration.trigger_type,
        source=configuration.trigger_source,
        level=configuration.trigger_level_v,
        edge=configuration.trigger_edge,
    )
    instrument.generate_waveform(
        channel=configuration.waveform_channel,
        type=configuration.waveform_type,
        amplitude=configuration.waveform_amplitude_vpp,
        offset=configuration.waveform_offset_v,
        frequency=configuration.waveform_frequency_hz,
        pulse_width=configuration.waveform_pulse_width_s,
        edge_time=configuration.waveform_edge_time_s,
        strict=True,
    )


def verify_oscilloscope_connection(instrument: Any) -> None:
    """Verify that the reconstructed API session accepts a read-only request."""

    instrument.summary()
