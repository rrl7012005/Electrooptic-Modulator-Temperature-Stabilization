"""Measure the blocked-photodiode DC offset using a Moku oscilloscope.

Block all light reaching the photodiode before running this script.  The
reported DARK_OFFSET is intended for ``analyse_eom_csv.py`` when that script
uses the same Moku frontend settings.
"""

from pathlib import Path
import time

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from moku.instruments import Oscilloscope


# =========================
# User settings
# =========================

MOKU_IP = "MokuGo-008058"

# Keep these identical to the settings used for the EOM measurement.
INPUT_IMPEDANCE = "1MOhm"
INPUT_COUPLING = "DC"
INPUT_RANGE = "10Vpp"

# A 0.2 s trace contains exactly 10 cycles of 50 Hz and 12 cycles of 60 Hz,
# which helps the trace mean reject mains pickup.
TRACE_START_S = -0.1
TRACE_END_S = 0.1
TRACE_LENGTH = 16384

NUMBER_OF_FRAMES = 100
GET_DATA_TIMEOUT_S = 3.0
MAX_CONSECUTIVE_FAILURES = 10
PRINT_EVERY_FRAMES = 10

# Reject frames whose mean is unusually far from the typical frame mean.
CLIP_SIGMA = 4.0
MAX_CLIP_ITERATIONS = 10

# Uncertainty is calculated from groups of consecutive frames rather than
# treating every oscilloscope point as an independent measurement.
FRAMES_PER_BLOCK = 10

OUTPUT_ROOT = Path("Experiment Results") / "dark_offset_measurements"


# =========================
# Helper functions
# =========================

def sigma_clip_mask(values, sigma=4.0, max_iterations=10):
    """Return a mask that robustly rejects outliers using median/MAD."""
    values = np.asarray(values, dtype=float)
    mask = np.isfinite(values)

    for _ in range(max_iterations):
        current = values[mask]
        if len(current) < 3:
            break

        centre = np.median(current)
        mad = np.median(np.abs(current - centre))
        scale = 1.4826 * mad

        if not np.isfinite(scale) or scale == 0:
            scale = np.std(current, ddof=1)
        if not np.isfinite(scale) or scale == 0:
            break

        new_mask = np.isfinite(values) & (np.abs(values - centre) <= sigma * scale)
        if np.array_equal(new_mask, mask):
            break
        mask = new_mask

    return mask


def save_plot(path):
    plt.tight_layout()
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close()


def configure_moku(osc):
    osc.set_frontend(1, INPUT_IMPEDANCE, INPUT_COUPLING, INPUT_RANGE)
    osc.set_sources([
        {"channel": 1, "source": "Input1"},
        {"channel": 2, "source": "Output2"},
    ])
    osc.set_timebase(TRACE_START_S, TRACE_END_S, max_length=TRACE_LENGTH)

    # Auto triggering is important here: with the detector blocked there may
    # be no real edge on which a Normal trigger can reliably acquire.
    osc.set_trigger(
        mode="Auto",
        type="Edge",
        source="Input1",
        level=0.0,
        edge="Rising",
    )


def acquire_frames(osc):
    rows = []
    time_arrays = []
    voltage_arrays = []
    consecutive_failures = 0
    acquisition_start = time.monotonic()

    while len(rows) < NUMBER_OF_FRAMES:
        try:
            data = osc.get_data(
                wait_reacquire=True,
                wait_complete=True,
                timeout=GET_DATA_TIMEOUT_S,
            )
        except Exception as exc:
            consecutive_failures += 1
            print(
                f"Frame acquisition failed ({consecutive_failures}/"
                f"{MAX_CONSECUTIVE_FAILURES}): {exc}"
            )
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise RuntimeError("Too many consecutive acquisition failures") from exc
            continue

        trace_time = np.asarray(data["time"], dtype=float)
        voltage = np.asarray(data["ch1"], dtype=float)
        finite = np.isfinite(trace_time) & np.isfinite(voltage)
        trace_time = trace_time[finite]
        voltage = voltage[finite]

        if len(voltage) < 2:
            consecutive_failures += 1
            print("Discarding a frame containing fewer than two finite samples")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise RuntimeError("Too many consecutive invalid frames")
            continue

        consecutive_failures = 0
        frame_number = len(rows)
        rows.append({
            "frame": frame_number,
            "wall_time": time.time(),
            "elapsed_s": time.monotonic() - acquisition_start,
            "sample_count": len(voltage),
            "mean_voltage": np.mean(voltage),
            "median_voltage": np.median(voltage),
            "std_voltage": np.std(voltage, ddof=1),
            "percentile_1_voltage": np.percentile(voltage, 1),
            "percentile_99_voltage": np.percentile(voltage, 99),
        })
        time_arrays.append(trace_time)
        voltage_arrays.append(voltage)

        if len(rows) % PRINT_EVERY_FRAMES == 0:
            recent = [row["mean_voltage"] for row in rows[-PRINT_EVERY_FRAMES:]]
            print(
                f"Acquired {len(rows)}/{NUMBER_OF_FRAMES} frames; "
                f"recent mean = {np.mean(recent):.6f} V"
            )

    return pd.DataFrame(rows), time_arrays, voltage_arrays


def align_traces(time_arrays, voltage_arrays):
    """Interpolate traces onto their common overlapping time interval."""
    prepared_traces = []
    for trace_time, voltage in zip(time_arrays, voltage_arrays):
        order = np.argsort(trace_time)
        trace_time = trace_time[order]
        voltage = voltage[order]
        unique_time, unique_indices = np.unique(trace_time, return_index=True)
        voltage = voltage[unique_indices]

        if len(unique_time) < 2:
            raise ValueError("A trace has fewer than two unique time points")

        prepared_traces.append((unique_time, voltage))

    # Acquisitions can differ by a sample or by tiny floating-point endpoint
    # shifts, especially after a communication timeout.  Use only the time
    # interval that every successfully acquired trace actually covers.
    common_start = max(trace_time[0] for trace_time, _ in prepared_traces)
    common_end = min(trace_time[-1] for trace_time, _ in prepared_traces)

    if common_start >= common_end:
        raise ValueError("Acquired traces have no overlapping time interval")

    first_time = prepared_traces[0][0]
    reference_time = first_time[
        (first_time >= common_start) & (first_time <= common_end)
    ]

    # This fallback is unlikely, but permits alignment when the first trace's
    # grid has no interior samples despite the traces having a valid overlap.
    if len(reference_time) < 2:
        common_length = min(len(trace_time) for trace_time, _ in prepared_traces)
        reference_time = np.linspace(common_start, common_end, common_length)

    aligned = []
    for trace_time, voltage in prepared_traces:
        aligned.append(np.interp(reference_time, trace_time, voltage))

    return reference_time, np.vstack(aligned)


# =========================
# Acquire dark traces
# =========================

run_folder = OUTPUT_ROOT / time.strftime("run_%Y%m%d_%H%M%S")
run_folder.mkdir(parents=True, exist_ok=False)

print("Photodiode must be fully blocked for this measurement.")
print(f"Connecting to {MOKU_IP} ...")

osc = None
try:
    osc = Oscilloscope(MOKU_IP, force_connect=True)
    configure_moku(osc)
    frame_data, trace_times, traces = acquire_frames(osc)
finally:
    if osc is not None:
        try:
            osc.relinquish_ownership()
            print("Moku ownership released")
        except Exception as exc:
            print(f"Could not relinquish Moku ownership: {exc}")


# =========================
# Robust offset estimate
# =========================

accepted = sigma_clip_mask(
    frame_data["mean_voltage"].to_numpy(),
    sigma=CLIP_SIGMA,
    max_iterations=MAX_CLIP_ITERATIONS,
)
frame_data["accepted"] = accepted

if accepted.sum() < 2:
    raise ValueError("Fewer than two usable frames remain after outlier rejection")

accepted_frame_means = frame_data.loc[accepted, "mean_voltage"].to_numpy()
dark_offset = np.mean(accepted_frame_means)

# Preserve original frame grouping so gaps caused by rejected frames do not
# make the uncertainty look artificially small.
frame_data["block"] = frame_data["frame"] // FRAMES_PER_BLOCK
block_means = (
    frame_data.loc[accepted]
    .groupby("block", sort=True)["mean_voltage"]
    .mean()
    .to_numpy()
)

if len(block_means) >= 2:
    standard_error = np.std(block_means, ddof=1) / np.sqrt(len(block_means))
    confidence_95 = 1.96 * standard_error
else:
    standard_error = np.nan
    confidence_95 = np.nan

accepted_time_arrays = [
    trace_time for trace_time, keep in zip(trace_times, accepted) if keep
]
accepted_voltage_arrays = [
    voltage for voltage, keep in zip(traces, accepted) if keep
]
reference_time, accepted_traces = align_traces(
    accepted_time_arrays,
    accepted_voltage_arrays,
)
average_trace = np.mean(accepted_traces, axis=0)
trace_point_std = np.std(accepted_traces, axis=0, ddof=1)

# RMS here describes actual dark-trace variation around the estimated DC
# level; it is noise amplitude, not uncertainty in the averaged offset.
noise_rms = np.sqrt(np.mean((accepted_traces - dark_offset) ** 2))
all_accepted_points = accepted_traces.ravel()
robust_peak_to_peak = (
    np.percentile(all_accepted_points, 99.5)
    - np.percentile(all_accepted_points, 0.5)
)


# =========================
# Save results
# =========================

frame_statistics_path = run_folder / "frame_statistics.csv"
average_trace_path = run_folder / "average_dark_trace.csv"
summary_path = run_folder / "summary.txt"

frame_data.to_csv(frame_statistics_path, index=False)
pd.DataFrame({
    "time_s": reference_time,
    "mean_voltage": average_trace,
    "frame_to_frame_std_voltage": trace_point_std,
}).to_csv(average_trace_path, index=False)

fig, ax = plt.subplots(figsize=(10, 5))
ax.plot(
    frame_data["elapsed_s"],
    frame_data["mean_voltage"],
    ".",
    label="Accepted frame mean",
)
if (~accepted).any():
    ax.plot(
        frame_data.loc[~accepted, "elapsed_s"],
        frame_data.loc[~accepted, "mean_voltage"],
        "x",
        color="tab:red",
        markersize=8,
        label="Rejected frame",
    )
ax.axhline(dark_offset, color="black", linewidth=2, label="DARK_OFFSET")
ax.set_xlabel("Elapsed acquisition time / s")
ax.set_ylabel("Frame-mean voltage / V")
ax.set_title("Blocked-photodiode frame means")
ax.grid(True)
ax.legend()
save_plot(run_folder / "01_frame_means.png")

fig, ax = plt.subplots(figsize=(9, 5))
ax.hist(accepted_frame_means, bins="auto", alpha=0.8)
ax.axvline(dark_offset, color="black", linewidth=2, label="DARK_OFFSET")
ax.set_xlabel("Frame-mean voltage / V")
ax.set_ylabel("Count")
ax.set_title("Distribution of accepted dark frame means")
ax.grid(True)
ax.legend()
save_plot(run_folder / "02_frame_mean_histogram.png")

fig, ax = plt.subplots(figsize=(10, 5))
ax.plot(reference_time, average_trace, label="Mean of accepted traces")
ax.fill_between(
    reference_time,
    average_trace - trace_point_std,
    average_trace + trace_point_std,
    alpha=0.25,
    label="Frame-to-frame standard deviation",
)
ax.axhline(dark_offset, color="black", linewidth=1.5, label="DARK_OFFSET")
ax.set_xlabel("Time within oscilloscope frame / s")
ax.set_ylabel("Photodiode voltage / V")
ax.set_title("Averaged blocked-photodiode trace")
ax.grid(True)
ax.legend()
save_plot(run_folder / "03_average_dark_trace.png")

confidence_text = (
    f"{confidence_95:.9f} V" if np.isfinite(confidence_95) else "not available"
)

summary = f"""
Blocked photodiode dark-offset measurement
===========================================

Use this value in analyse_eom_csv.py:
DARK_OFFSET = {dark_offset:.9f}

Measurement quality
-------------------
Accepted frames:                   {accepted.sum()} / {len(accepted)}
Rejected frames:                   {(~accepted).sum()}
Blocks used for uncertainty:       {len(block_means)}
Standard error from block means:   {standard_error:.9f} V
Approximate 95% confidence:        +/- {confidence_text}
Dark trace RMS noise:              {noise_rms:.9f} V
Robust 99% peak-to-peak noise:     {robust_peak_to_peak:.9f} V

Acquisition settings
--------------------
Moku:                              {MOKU_IP}
Frontend:                          {INPUT_IMPEDANCE}, {INPUT_COUPLING}, {INPUT_RANGE}
Trace window:                      {TRACE_START_S:.3f} to {TRACE_END_S:.3f} s
Requested frames:                  {NUMBER_OF_FRAMES}
Outlier threshold:                 {CLIP_SIGMA:.1f} robust sigma
Frames per uncertainty block:      {FRAMES_PER_BLOCK}

Interpretation
--------------
- DARK_OFFSET is the robust average DC voltage with the photodiode blocked.
- RMS noise describes the noisy trace; it is not the error in DARK_OFFSET.
- Repeat the measurement if the frame-mean plot drifts rather than settling.
- Use the same frontend settings when collecting the EOM data.
""".strip()

summary_path.write_text(summary, encoding="utf-8")

print()
print(summary)
print()
print(f"Saved results to: {run_folder}")
