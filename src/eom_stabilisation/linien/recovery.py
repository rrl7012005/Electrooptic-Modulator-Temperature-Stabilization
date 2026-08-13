"""Hardware-independent recovery support for a Linien client connection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
import traceback
from typing import Any, Callable, Protocol
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
    """Timing policy for indefinite Linien reconnection attempts."""

    reconnect_backoff_s: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)
    lock_recovery_timeout_s: float = 10.0
    lock_poll_interval_s: float = 1.0

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


class LinienLockNotRestoredError(RuntimeError):
    """Raised when a reconnected Linien server remains unlocked."""


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
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.connection_factory = connection_factory
        self.refresh_and_read_lock = refresh_and_read_lock
        self.disconnect_client = disconnect_client
        self.is_recoverable_error = is_recoverable_error
        self.event_writer = event_writer
        self.policy = policy or LinienRecoveryPolicy()
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

            try:
                candidate = self.connection_factory()
                self.event_writer.write(
                    "reconnection_transport_established",
                    reconnection_attempt_number=attempt,
                    recovery_outage_duration_s=max(
                        0.0,
                        self.monotonic() - recovery_started,
                    ),
                )
                self._wait_for_lock(candidate, attempt, recovery_started)
            except LinienLockNotRestoredError:
                self.disconnect_safely(
                    candidate,
                    context="reconnected_but_unlocked_client",
                )
                raise
            except Exception as error:
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
