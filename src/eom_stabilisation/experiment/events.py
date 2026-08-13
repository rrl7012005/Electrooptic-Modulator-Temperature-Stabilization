"""Append-only master experiment event records."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from .clock import ClockReading, ExperimentClock


@dataclass(frozen=True)
class ExperimentEvent:
    """One timestamped supervisor event."""

    name: str
    clock: ClockReading
    fields: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        # Caller fields must not be able to replace authoritative clock/event
        # metadata.
        return {**dict(self.fields), **self.clock.to_dict(), "event": self.name}


class JsonlExperimentEventWriter:
    """Append and flush one JSON object per experiment event."""

    def __init__(self, path: str | Path, clock: ExperimentClock) -> None:
        self.path = Path(path)
        self.clock = clock

    def write(self, event: str, **fields: Any) -> ExperimentEvent:
        if not isinstance(event, str) or not event.strip():
            raise ValueError("Event name must not be empty.")
        record = ExperimentEvent(event.strip(), self.clock.read(), fields)
        line = json.dumps(record.to_dict(), sort_keys=True, allow_nan=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as output:
            output.write(line)
            output.write("\n")
            output.flush()
        return record
