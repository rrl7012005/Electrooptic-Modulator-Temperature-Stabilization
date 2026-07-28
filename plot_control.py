from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


DATA_FOLDER = Path(r"C:\Users\lab7\Documents\Code\Logging")

# Search this folder and all subfolders
linien_files = list(DATA_FOLDER.rglob("*linien*.csv"))

if not linien_files:
    raise FileNotFoundError(
        f"No CSV containing 'linien' was found in:\n{DATA_FOLDER}"
    )

# Select the most recently modified Linien CSV
FILE_PATH = max(linien_files, key=lambda path: path.stat().st_mtime)

print(f"Using most recent Linien file:\n{FILE_PATH}")

OUTPUT_PATH = FILE_PATH.with_name(
    FILE_PATH.stem + "_control_voltage_vs_time.png"
)


# Read only required columns
df = pd.read_csv(
    FILE_PATH,
    usecols=["wall_time", "lock_output_voltage_V"]
)

df["wall_time"] = pd.to_numeric(df["wall_time"], errors="coerce")
df["lock_output_voltage_V"] = pd.to_numeric(
    df["lock_output_voltage_V"],
    errors="coerce"
)

df = df.dropna(
    subset=["wall_time", "lock_output_voltage_V"]
).copy()

if df.empty:
    raise ValueError("No valid rows were found.")


# Unix wall_time represents an absolute instant.
# First interpret it as UTC, then convert to UK local time.
# Europe/London automatically uses BST during summer and GMT in winter.
df["uk_time"] = (
    pd.to_datetime(df["wall_time"], unit="s", utc=True)
    .dt.tz_convert("Europe/London")
)

df = (
    df.sort_values("uk_time")
    .drop_duplicates(subset="uk_time")
    .set_index("uk_time")
)


# 60-second time-based rolling mean
df["rolling_60s"] = (
    df["lock_output_voltage_V"]
    .rolling("60s", min_periods=1)
    .mean()
)


# Reduce only the raw trace if there are extremely many points
max_raw_points = 100_000
raw_step = max(
    1,
    int(np.ceil(len(df) / max_raw_points))
)
raw_df = df.iloc[::raw_step]


fig, ax = plt.subplots(figsize=(13, 6))

ax.plot(
    raw_df.index,
    raw_df["lock_output_voltage_V"],
    linewidth=0.5,
    alpha=0.3,
    label="Raw lock output"
)

ax.plot(
    df.index,
    df["rolling_60s"],
    linewidth=1.5,
    label="60 s rolling mean"
)

start_day = df.index[0].strftime("%d %B %Y")
ax.set_xlabel(f"UK wall time from {start_day}")
ax.set_ylabel("Linien lock output voltage (V)")
ax.set_title("EOM lock control voltage against wall time")

# Choose readable time/date tick labels automatically
locator = mdates.AutoDateLocator()
formatter = mdates.ConciseDateFormatter(
    locator,
    tz="Europe/London"
)

ax.xaxis.set_major_locator(locator)
ax.xaxis.set_major_formatter(formatter)

ax.grid(True, alpha=0.3)
ax.legend()
fig.tight_layout()

fig.savefig(OUTPUT_PATH, dpi=300)
plt.show()


print(f"Rows plotted: {len(df):,}")
print(f"Start: {df.index[0].strftime('%Y-%m-%d %H:%M:%S %Z')}")
print(f"Finish: {df.index[-1].strftime('%Y-%m-%d %H:%M:%S %Z')}")
print(f"Plot saved to: {OUTPUT_PATH}")