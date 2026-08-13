"""Shared monotonic and timezone-aware experiment clock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import time
from typing import Callable
from zoneinfo import ZoneInfo


LONDON_TIMEZONE = ZoneInfo("Europe/London")


def format_utc(timestamp: datetime) -> str:
    """Format an aware datetime as a machine-readable UTC timestamp."""

    if timestamp.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware.")
    return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ClockReading:
    """One coherent monotonic/UTC/London timestamp set."""

    elapsed_s: float
    timestamp_utc: datetime
    timestamp_local: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "elapsed_s": self.elapsed_s,
            "timestamp_utc": format_utc(self.timestamp_utc),
            "timestamp_local": self.timestamp_local.isoformat(),
        }


class ExperimentClock:
    """Clock injectable with deterministic monotonic and UTC sources."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self._monotonic = monotonic
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self.started_monotonic = float(self._monotonic())
        if not math.isfinite(self.started_monotonic):
            raise ValueError("Initial monotonic time must be finite.")
        self.started_utc = self._aware_utc(self._utc_now())
        self._last_monotonic = self.started_monotonic

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("UTC clock must return a timezone-aware datetime.")
        return value.astimezone(timezone.utc)

    def read(self) -> ClockReading:
        monotonic_now = float(self._monotonic())
        if not math.isfinite(monotonic_now):
            raise ValueError("Monotonic time must be finite.")
        if monotonic_now < self._last_monotonic:
            raise ValueError("Monotonic time moved backwards.")
        self._last_monotonic = monotonic_now
        utc_now = self._aware_utc(self._utc_now())
        return ClockReading(
            elapsed_s=monotonic_now - self.started_monotonic,
            timestamp_utc=utc_now,
            timestamp_local=utc_now.astimezone(LONDON_TIMEZONE),
        )

    def elapsed_s(self) -> float:
        return self.read().elapsed_s
