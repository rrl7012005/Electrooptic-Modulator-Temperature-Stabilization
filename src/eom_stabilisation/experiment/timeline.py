"""Reproducible actual-event timeline derived from the append-only run log."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Mapping


TIMELINE_FIELDS = (
    "timestamp_utc",
    "timestamp_local",
    "elapsed_s",
    "event",
    "source",
    "command",
    "stage_index",
    "stage_name",
    "temperature_phase",
    "action_index",
    "action_name",
    "waveform_name",
    "waveform_run_id",
    "waveform_session_id",
    "moku_phase",
    "phase_continuity",
    "detail",
    "fields_json",
)


def _read_event_records(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    records: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            is_incomplete_final = index == len(lines) and not raw.endswith("\n")
            if is_incomplete_final:
                break
            raise ValueError(
                f"Malformed experiment event at {path}:{index}."
            ) from error
        if not isinstance(record, dict):
            raise ValueError(f"Experiment event at {path}:{index} is not an object.")
        records.append(record)
    return records


def _timeline_row(record: Mapping[str, Any]) -> dict[str, Any]:
    elapsed = record.get("elapsed_s")
    if isinstance(elapsed, bool):
        raise ValueError("Experiment event elapsed_s must be finite and nonnegative.")
    try:
        elapsed_s = float(elapsed)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "Experiment event elapsed_s must be finite and nonnegative."
        ) from error
    if not math.isfinite(elapsed_s) or elapsed_s < 0:
        raise ValueError("Experiment event elapsed_s must be finite and nonnegative.")

    known = set(TIMELINE_FIELDS) | {"supervisor_elapsed_s"}
    extra = {
        key: value
        for key, value in record.items()
        if key not in known
    }
    return {
        field: (
            elapsed_s
            if field == "elapsed_s"
            else json.dumps(extra, sort_keys=True, allow_nan=False)
            if field == "fields_json"
            else record.get(field)
        )
        for field in TIMELINE_FIELDS
    }


def write_actual_timeline(
    events_path: str | Path,
    csv_path: str | Path,
    png_path: str | Path,
) -> int:
    """Atomically replace plan previews with the actual timestamped timeline."""

    source = Path(events_path)
    rows = [_timeline_row(record) for record in _read_event_records(source)]
    destination_csv = Path(csv_path)
    temporary_csv = destination_csv.with_suffix(destination_csv.suffix + ".tmp")
    with temporary_csv.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=TIMELINE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        output.flush()
    temporary_csv.replace(destination_csv)

    import matplotlib.pyplot as plt

    sources = tuple(
        dict.fromkeys(str(row.get("source") or "runtime") for row in rows)
    ) or ("runtime",)
    lane = {name: index for index, name in enumerate(sources)}
    figure, axis = plt.subplots(figsize=(10, max(2.8, 0.7 * len(sources) + 1.8)))
    for row in rows:
        source_name = str(row.get("source") or "runtime")
        axis.scatter(
            [float(row["elapsed_s"])],
            [lane[source_name]],
            marker="|",
            s=180,
            linewidths=1.5,
        )
    axis.set_yticks(range(len(sources)), sources)
    axis.set_xlabel("Elapsed experiment time (s)")
    axis.set_title("Actual experiment event timeline")
    axis.grid(True, axis="x", alpha=0.25)
    figure.tight_layout()
    destination_png = Path(png_path)
    temporary_png = destination_png.with_name(
        destination_png.stem + ".tmp" + destination_png.suffix
    )
    figure.savefig(temporary_png, dpi=160)
    plt.close(figure)
    temporary_png.replace(destination_png)
    return len(rows)


__all__ = ["TIMELINE_FIELDS", "write_actual_timeline"]
