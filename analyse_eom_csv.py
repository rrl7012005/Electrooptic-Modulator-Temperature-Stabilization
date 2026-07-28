from pathlib import Path
import sys
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

try:
    from scipy.stats import linregress, ttest_ind
    HAVE_SCIPY = True
except Exception:
    HAVE_SCIPY = False


# =========================
# User settings
# =========================

CSV_PATH = None
# If CSV_PATH = None, script automatically finds the newest:
# moku_overnight_runs/run_*/macro_voltage_tracking.csv

ROLLING_SECONDS = 60
FIRST_LAST_MINUTES = 5

# Your wall_time values come from time.time(), i.e. Unix seconds.
# This converts them to UK clock time for plotting.
TIMEZONE = "Europe/London"

# Leave this as 0 unless you have measured photodiode electronic dark offset.
DARK_OFFSET_V = 0.0

# Basic validity filters
MINIMUM_ALLOWED_MAX_VOLTAGE = 0.5
MAXIMUM_ALLOWED_MIN_VOLTAGE = 0.5


# =========================
# Helper functions
# =========================

def find_latest_csv():
    files = list(Path("moku_overnight_runs").glob("run_*/macro_voltage_tracking.csv"))
    if not files:
        raise FileNotFoundError(
            "Could not find any moku_overnight_runs/run_*/macro_voltage_tracking.csv files."
        )
    return max(files, key=lambda p: p.stat().st_mtime)


def save_plot(path):
    plt.tight_layout()
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close()


def trend_stats(x, y):
    """
    Fits y = slope*x + intercept.
    x should be in minutes.
    Returns slope per minute.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)

    mask = np.isfinite(x) & np.isfinite(y)
    x = x[mask]
    y = y[mask]

    if len(x) < 3:
        return {
            "slope": np.nan,
            "intercept": np.nan,
            "r": np.nan,
            "p": np.nan,
            "stderr": np.nan,
        }

    if HAVE_SCIPY:
        result = linregress(x, y)
        return {
            "slope": result.slope,
            "intercept": result.intercept,
            "r": result.rvalue,
            "p": result.pvalue,
            "stderr": result.stderr,
        }

    # Fallback: no p-value if scipy is unavailable
    slope, intercept = np.polyfit(x, y, 1)
    r = np.corrcoef(x, y)[0, 1]
    return {
        "slope": slope,
        "intercept": intercept,
        "r": r,
        "p": np.nan,
        "stderr": np.nan,
    }


def format_p(p):
    if not np.isfinite(p):
        return "N/A"
    if p < 1e-4:
        return f"{p:.2e}"
    return f"{p:.4f}"


def format_time_axis(ax, start_label):
    """
    Use real clock time on the x-axis. The axis label contains the run start date/time,
    so the tick labels can stay compact as HH:MM.
    """
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_xlabel(f"Time / {TIMEZONE}   |   run start: {start_label}")
    ax.figure.autofmt_xdate()


def add_gap_markers(ax, data, threshold_s=3.0):
    """
    Optional: mark large acquisition gaps on time-domain plots.
    """
    gaps = data[data["wall_time"].diff() > threshold_s]
    for ts in gaps["timestamp"]:
        ax.axvline(ts, color="0.5", alpha=0.25, linewidth=0.8)


# =========================
# Load CSV
# =========================

if len(sys.argv) > 1:
    csv_path = Path(sys.argv[1])
elif CSV_PATH is not None:
    csv_path = Path(CSV_PATH)
else:
    csv_path = find_latest_csv()

print(f"Analysing: {csv_path}")

df = pd.read_csv(csv_path)

required_columns = {"wall_time", "minimum_voltage", "maximum_voltage"}
missing = required_columns - set(df.columns)

if missing:
    raise ValueError(f"CSV is missing required columns: {missing}")

for col in ["wall_time", "minimum_voltage", "maximum_voltage"]:
    df[col] = pd.to_numeric(df[col], errors="coerce")

df = df.dropna(subset=["wall_time", "minimum_voltage", "maximum_voltage"]).copy()

# Remove obvious invalid startup / broken samples
valid = (
    (df["maximum_voltage"] > df["minimum_voltage"])
    & (df["maximum_voltage"] > MINIMUM_ALLOWED_MAX_VOLTAGE)
    & (df["minimum_voltage"] < MAXIMUM_ALLOWED_MIN_VOLTAGE)
    & (df["minimum_voltage"] > DARK_OFFSET_V)
)

clean = df[valid].copy()

if len(clean) < 10:
    raise ValueError("Too few valid samples after cleaning. Check the CSV or validity thresholds.")

# Time axis
clean["elapsed_s"] = clean["wall_time"] - clean["wall_time"].iloc[0]
clean["elapsed_min"] = clean["elapsed_s"] / 60

# Convert Unix wall_time to timezone-aware UK clock time.
# wall_time from time.time() is seconds since Unix epoch, so utc=True is the correct first step.
clean["timestamp"] = (
    pd.to_datetime(clean["wall_time"], unit="s", utc=True)
    .dt.tz_convert(TIMEZONE)
)

start_clock = clean["timestamp"].iloc[0]
finish_clock = clean["timestamp"].iloc[-1]
start_label = start_clock.strftime("%Y-%m-%d %H:%M:%S %Z")
finish_label = finish_clock.strftime("%Y-%m-%d %H:%M:%S %Z")

# Apparent extinction ratio
clean["minimum_corrected"] = clean["minimum_voltage"] - DARK_OFFSET_V
clean["maximum_corrected"] = clean["maximum_voltage"] - DARK_OFFSET_V

clean["extinction_ratio_linear"] = clean["maximum_corrected"] / clean["minimum_corrected"]
clean["extinction_ratio_dB"] = 10 * np.log10(clean["extinction_ratio_linear"])

# Rolling smoothing
clean = clean.set_index("timestamp")

rolling = clean[
    ["minimum_voltage", "maximum_voltage", "extinction_ratio_dB"]
].rolling(f"{ROLLING_SECONDS}s", min_periods=10).mean()

clean["minimum_voltage_roll"] = rolling["minimum_voltage"]
clean["maximum_voltage_roll"] = rolling["maximum_voltage"]
clean["extinction_ratio_dB_roll"] = rolling["extinction_ratio_dB"]

clean = clean.reset_index()

# Minute block averages for statistics and minute-averaged plot
minute = clean.set_index("timestamp").resample("60s").mean(numeric_only=True).dropna().reset_index()
minute["elapsed_min"] = (minute["wall_time"] - minute["wall_time"].iloc[0]) / 60


# =========================
# Output folder
# =========================

out_dir = csv_path.parent / "analysis"
out_dir.mkdir(exist_ok=True)

processed_path = out_dir / "processed_eom_data_with_extinction_ratio.csv"
summary_path = out_dir / "summary.txt"

clean.to_csv(processed_path, index=False)


# =========================
# Plots
# =========================

# 1. Minimum voltage
fig, ax = plt.subplots(figsize=(10, 5))
ax.plot(clean["timestamp"], clean["minimum_voltage"], label="Raw minimum voltage")
ax.plot(clean["timestamp"], clean["minimum_voltage_roll"], linewidth=2, label=f"{ROLLING_SECONDS}s rolling mean")
format_time_axis(ax, start_label)
ax.set_ylabel("Minimum photodiode voltage / V")
ax.set_title("Minimum photodiode level vs time")
ax.grid(True)
ax.legend()
add_gap_markers(ax, clean)
save_plot(out_dir / "01_minimum_voltage_vs_wall_time.png")

# 2. Maximum voltage
fig, ax = plt.subplots(figsize=(10, 5))
ax.plot(clean["timestamp"], clean["maximum_voltage"], label="Raw maximum voltage")
ax.plot(clean["timestamp"], clean["maximum_voltage_roll"], linewidth=2, label=f"{ROLLING_SECONDS}s rolling mean")
format_time_axis(ax, start_label)
ax.set_ylabel("Maximum photodiode voltage / V")
ax.set_title("Bright photodiode level vs time")
ax.grid(True)
ax.legend()
add_gap_markers(ax, clean)
save_plot(out_dir / "02_maximum_voltage_vs_wall_time.png")

# 3. Apparent extinction ratio
fig, ax = plt.subplots(figsize=(10, 5))
ax.plot(clean["timestamp"], clean["extinction_ratio_dB"], label="Raw apparent ER")
ax.plot(clean["timestamp"], clean["extinction_ratio_dB_roll"], linewidth=2, label=f"{ROLLING_SECONDS}s rolling mean")
format_time_axis(ax, start_label)
ax.set_ylabel("Apparent extinction ratio / dB")
ax.set_title("Apparent extinction ratio vs time")
ax.grid(True)
ax.legend()
add_gap_markers(ax, clean)
save_plot(out_dir / "03_extinction_ratio_vs_wall_time.png")

# 4. Minimum and maximum normalized together
fig, ax = plt.subplots(figsize=(10, 5))
ax.plot(
    clean["timestamp"],
    clean["minimum_voltage"] / clean["minimum_voltage"].median(),
    label="Minimum voltage / median",
)
ax.plot(
    clean["timestamp"],
    clean["maximum_voltage"] / clean["maximum_voltage"].median(),
    label="Maximum voltage / median",
)
format_time_axis(ax, start_label)
ax.set_ylabel("Normalized voltage")
ax.set_title("Normalized minimum and maximum levels vs time")
ax.grid(True)
ax.legend()
add_gap_markers(ax, clean)
save_plot(out_dir / "04_normalized_minimum_maximum_wall_time.png")

# 5. Scatter: maximum vs minimum
plt.figure(figsize=(7, 5))
plt.scatter(clean["minimum_voltage"], clean["maximum_voltage"], s=8)
plt.xlabel("Minimum photodiode voltage / V")
plt.ylabel("Maximum photodiode voltage / V")
plt.title("Maximum vs minimum photodiode level")
plt.grid(True)
save_plot(out_dir / "05_maximum_vs_minimum_scatter.png")

# 6. Minute-averaged ER
fig, ax = plt.subplots(figsize=(10, 5))
ax.plot(minute["timestamp"], minute["extinction_ratio_dB"], marker="o", label="1-minute block average")
format_time_axis(ax, start_label)
ax.set_ylabel("Apparent extinction ratio / dB")
ax.set_title("Minute averaged apparent extinction ratio vs time")
ax.grid(True)
ax.legend()
save_plot(out_dir / "06_minute_averaged_extinction_ratio_wall_time.png")

# 7. Histogram of extinction ratio
plt.figure(figsize=(8, 5))
plt.hist(clean["extinction_ratio_dB"], bins=40)
plt.xlabel("Apparent extinction ratio / dB")
plt.ylabel("Count")
plt.title("Distribution of apparent extinction ratio")
plt.grid(True)
save_plot(out_dir / "07_extinction_ratio_histogram.png")


# =========================
# Statistics
# =========================

duration_min = clean["elapsed_min"].iloc[-1]
sample_interval = np.median(np.diff(clean["wall_time"]))

min_trend = trend_stats(minute["elapsed_min"], minute["minimum_voltage"])
max_trend = trend_stats(minute["elapsed_min"], minute["maximum_voltage"])
er_trend = trend_stats(minute["elapsed_min"], minute["extinction_ratio_dB"])

first_cut = clean["elapsed_min"].min() + FIRST_LAST_MINUTES
last_cut = clean["elapsed_min"].max() - FIRST_LAST_MINUTES

first = clean[clean["elapsed_min"] <= first_cut]
last = clean[clean["elapsed_min"] >= last_cut]

first_min_mean = first["minimum_voltage"].mean()
last_min_mean = last["minimum_voltage"].mean()

first_max_mean = first["maximum_voltage"].mean()
last_max_mean = last["maximum_voltage"].mean()

first_er_mean = first["extinction_ratio_dB"].mean()
last_er_mean = last["extinction_ratio_dB"].mean()

if HAVE_SCIPY:
    min_ttest = ttest_ind(first["minimum_voltage"], last["minimum_voltage"], equal_var=False)
    max_ttest = ttest_ind(first["maximum_voltage"], last["maximum_voltage"], equal_var=False)
    er_ttest = ttest_ind(first["extinction_ratio_dB"], last["extinction_ratio_dB"], equal_var=False)

    min_ttest_p = min_ttest.pvalue
    max_ttest_p = max_ttest.pvalue
    er_ttest_p = er_ttest.pvalue
else:
    min_ttest_p = np.nan
    max_ttest_p = np.nan
    er_ttest_p = np.nan

corr_min_max = clean["minimum_voltage"].corr(clean["maximum_voltage"])
corr_min_er = clean["minimum_voltage"].corr(clean["extinction_ratio_dB"])

large_gaps = clean["wall_time"].diff() > 3
num_large_gaps = int(large_gaps.sum())

summary = f"""
EOM / Moku CSV analysis
=======================

Input file:
{csv_path}

Output folder:
{out_dir}

Rows
----
Raw rows:                        {len(df)}
Valid rows used:                 {len(clean)}
Rejected rows:                   {len(df) - len(clean)}

Time
----
Start wall time:                  {start_label}
Finish wall time:                 {finish_label}
Duration:                         {duration_min:.2f} min
Median sample interval:            {sample_interval:.3f} s
Large gaps > 3 s:                  {num_large_gaps}

Voltage levels
--------------
Mean minimum voltage:             {clean["minimum_voltage"].mean():.6f} V
Std minimum voltage:              {clean["minimum_voltage"].std():.6f} V
Mean maximum voltage:             {clean["maximum_voltage"].mean():.6f} V
Std maximum voltage:              {clean["maximum_voltage"].std():.6f} V

Apparent extinction ratio
-------------------------
Mean ER:                          {clean["extinction_ratio_linear"].mean():.3f} linear
Mean ER:                          {clean["extinction_ratio_dB"].mean():.3f} dB
Std ER:                           {clean["extinction_ratio_dB"].std():.3f} dB
Min ER:                           {clean["extinction_ratio_dB"].min():.3f} dB
Max ER:                           {clean["extinction_ratio_dB"].max():.3f} dB

Trend using 1-minute block averages
-----------------------------------
Minimum voltage slope:             {min_trend["slope"] * 1000:.4f} mV/min
Minimum voltage p-value:           {format_p(min_trend["p"])}

Maximum voltage slope:             {max_trend["slope"] * 1000:.4f} mV/min
Maximum voltage p-value:           {format_p(max_trend["p"])}

ER slope:                          {er_trend["slope"]:.5f} dB/min
ER p-value:                        {format_p(er_trend["p"])}

First {FIRST_LAST_MINUTES} min vs last {FIRST_LAST_MINUTES} min
---------------------------------------------------------------
Minimum voltage first mean:        {first_min_mean:.6f} V
Minimum voltage last mean:         {last_min_mean:.6f} V
Minimum voltage change:            {(last_min_mean - first_min_mean) * 1000:.3f} mV
Minimum voltage t-test p-value:    {format_p(min_ttest_p)}

Maximum voltage first mean:        {first_max_mean:.6f} V
Maximum voltage last mean:         {last_max_mean:.6f} V
Maximum voltage change:            {(last_max_mean - first_max_mean) * 1000:.3f} mV
Maximum voltage t-test p-value:    {format_p(max_ttest_p)}

ER first mean:                     {first_er_mean:.3f} dB
ER last mean:                      {last_er_mean:.3f} dB
ER change:                         {last_er_mean - first_er_mean:.3f} dB
ER t-test p-value:                 {format_p(er_ttest_p)}

Correlations
------------
corr(minimum voltage, maximum voltage): {corr_min_max:.4f}
corr(minimum voltage, ER_dB):           {corr_min_er:.4f}

Interpretation guide
--------------------
- If minimum voltage rises while maximum voltage stays roughly constant:
  dark floor is drifting upward, so extinction gets worse.

- If maximum and minimum both move together:
  likely optical power / detector gain / coupling drift.

- If ER_dB is strongly anti-correlated with minimum voltage:
  extinction is mainly controlled by the dark level.

- This ER is apparent ER from photodiode voltage:
  ER_dB = 10 log10(maximum_voltage / minimum_voltage)

- It is not corrected for detector dark offset unless DARK_OFFSET_V is set.
"""

summary = summary.strip()

with open(summary_path, "w", encoding="utf-8") as f:
    f.write(summary)

print()
print(summary)
print()
print("Saved processed CSV to:")
print(processed_path)
print()
print("Saved plots to:")
for p in sorted(out_dir.glob("*.png")):
    print(p)