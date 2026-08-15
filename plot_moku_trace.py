"""Render the newest configured raw Moku trace without changing raw data."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def parse_arguments():
    parser = argparse.ArgumentParser(description="Plot a configured Moku raw trace.")
    parser.add_argument("index_path", type=Path, help="raw_trace_index.csv path")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--no-show", action="store_true")
    parser.add_argument("--in-progress", action="store_true")
    return parser.parse_args()


def newest_trace(index_path: Path) -> tuple[Path, dict[str, str]]:
    with index_path.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    if not rows:
        raise ValueError("Configured raw-trace index contains no rows.")
    row = rows[-1]
    relative = Path(row["raw_trace_file"])
    trace_path = (index_path.parent / relative).resolve()
    if relative.is_absolute() or not trace_path.is_relative_to(index_path.parent.resolve()):
        raise ValueError("Raw-trace index path escapes the Moku output directory.")
    if not trace_path.is_file():
        raise FileNotFoundError(f"Indexed raw trace does not exist: {trace_path}")
    return trace_path, row


def create_trace_plot(
    trace_path: Path,
    output_path: Path,
    *,
    in_progress: bool = False,
):
    with np.load(trace_path, allow_pickle=False) as trace:
        time_s = np.asarray(trace["time_s"], dtype=float)
        photodiode_v = np.asarray(trace["photodiode_v"], dtype=float)
        reference_v = np.asarray(trace["waveform_reference_v"], dtype=float)
    if time_s.ndim != 1 or photodiode_v.shape != time_s.shape:
        raise ValueError("Raw trace has invalid time/photodiode vectors.")
    if reference_v.size and reference_v.shape != time_s.shape:
        raise ValueError("Raw trace has an invalid waveform-reference vector.")
    figure, axis = plt.subplots(figsize=(10, 5))
    axis.plot(time_s, photodiode_v, label="Photodiode (Input 1)")
    if reference_v.size:
        axis.plot(time_s, reference_v, label="Output 2 reference")
    axis.set_xlabel("Time (s)")
    axis.set_ylabel("Voltage (V)")
    title = "Newest configured Moku trace"
    if in_progress:
        title = "IN PROGRESS — " + title
    axis.set_title(title)
    axis.grid(True, alpha=0.3)
    axis.legend()
    figure.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    figure.savefig(temporary, format="png", dpi=200, bbox_inches="tight")
    temporary.replace(output_path)
    return figure


def main() -> None:
    args = parse_arguments()
    index_path = args.index_path.expanduser().resolve()
    trace_path, row = newest_trace(index_path)
    output_name = (
        f"trace_{row.get('frame_id') or trace_path.stem}_in_progress.png"
        if args.in_progress
        else f"trace_{row.get('frame_id') or trace_path.stem}.png"
    )
    output_path = args.output_dir.expanduser().resolve() / output_name
    figure = create_trace_plot(trace_path, output_path, in_progress=args.in_progress)
    print(f"Trace plotted: {trace_path}")
    print(f"Plot saved to: {output_path}")
    if not args.no_show:
        plt.show()
    plt.close(figure)


if __name__ == "__main__":
    main()
