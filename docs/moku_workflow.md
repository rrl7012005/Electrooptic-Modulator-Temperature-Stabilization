# Moku script and data workflow

This guide explains the distinct roles of `collect_data.py`,
`analyse_eom_csv.py`, and `pulse_control.py`. The scripts are related, but they
are not interchangeable.

## Quick decision guide

| Goal | Use |
| --- | --- |
| Run the standard pulsed EOM drift measurement | `run_experiment.py` or `collect_data.py` |
| Inspect or analyse an existing Moku photovoltage CSV | `analyse_eom_csv.py` |
| Preview a traditional or custom pulse without connecting to hardware | `pulse_control.py --dry-run` |
| Generate pulses without acquiring the photodiode | `pulse_control.py` |
| Generate arbitrary custom pulses while simultaneously acquiring the photodiode | Not currently supported; this needs a separately validated Moku Multi-Instrument Mode implementation |

## `collect_data.py`: standard pulse generation and acquisition

`collect_data.py` is the Moku component used by the normal drift experiment.
It performs two jobs through the Moku Oscilloscope instrument:

1. It generates the standard repeating pulse on physical Output 2.
2. It acquires the photodiode waveform from physical Input 1 and reduces each
   valid triggered frame to a baseline level and a pulse high level.

The present configuration uses a Normal rising-edge Input 1 trigger at 0.6 V.
The Input 1 threshold crossing is therefore `t = 0` in each acquired trace.
The analysis windows assume one 10 µs pulse beginning at that trigger. Changing
the trigger, pulse width, or timebase changes the meaning of the extracted
levels and must be reviewed together.

The collector averages up to five valid frames into approximately one sample
per second. It writes the primary acquisition file:

```text
Experiment Results/run_2026-08-04_15-30-00_BST/Moku_logs/
├── raw_photovoltage_tracking.csv
├── raw_photovoltage_provenance.csv
├── acquisition_events.jsonl
├── traces/
└── plots/
```

The raw CSV retains the historical columns:

| Column | Meaning |
| --- | --- |
| `wall_time` | Unix timestamp in seconds at the end of the sample period |
| `minimum_voltage` | Mean Input 1 photodiode baseline outside the pulse |
| `maximum_voltage` | Historical name for the measured pulse high/offset level; it is not assumed to be the true EOM transfer-curve maximum |

`acquisition_events.jsonl` records timezone-aware connection, timeout,
recovery, interruption, and cleanup events. Expected absence of an Input 1
trigger is reported separately from network or ownership failures. A recovery
can interrupt or restart the Output 2 waveform phase, so it represents an
experimental timing discontinuity.

`raw_photovoltage_provenance.csv` has exactly one row for every row in the
historical three-column raw CSV. It records UTC time, acquisition source, run
ID, waveform-session ID, and whether that sample is the first after a reconnect.
The collector discards a partially accumulated one-second average if a
waveform restart occurs inside it; it never averages frames across that
boundary and never creates placeholder rows for an outage.

The collector first tries the address in `EOM_MOKU_ADDRESS`, which defaults to
`MokuGo-008058`. It uses `EOM_MOKU_FALLBACK_ADDRESS` only when that fallback was
explicitly configured. A USB link-local IPv6 literal must include its scope and
the square brackets required by the Moku API.

```powershell
$env:EOM_MOKU_ADDRESS = "MokuGo-008058"
$env:EOM_MOKU_FALLBACK_ADDRESS = "[fe80::...%<interface-index>]"
python collect_data.py
```

Replace the fallback placeholder with the address verified for the intended
physical Moku. Environment variables must be set before Python starts. The
legacy collectors do not read these variables.

Useful nonstandard commands are:

```powershell
# Print the configured experiment duration without importing the Moku SDK.
python collect_data.py --print-experiment-length

# Connect to the configured Moku and request that Output 2 be disabled.
# This is a real hardware action.
python collect_data.py --disable-output
```

Starting `collect_data.py` normally is also a real hardware action: it claims
the Moku, configures the Oscilloscope, and enables Output 2. On normal
completion or `Ctrl+C`, it saves buffered samples, attempts to switch Output 2
off, and relinquishes ownership. If communication is unavailable, software
cannot confirm that the physical output was disabled.

### Watchdog and long-outage recovery

The Moku SDK runs in a Windows `spawn` child process. A live SDK object is never
pickled or shared with the parent. Only one command can be in flight. If
`get_data()` has not returned in 15 seconds, the parent saves the raw CSV and
provenance sidecar, terminates and joins that child, and refuses to reconnect
unless the old process is confirmed dead. A new child then claims the device,
receives the full configuration, and is checked using `summary()`.

The normal policy retries connection failures indefinitely at 1, 2, 5, 10,
20, then 30-second intervals. It reports continuing recovery once per minute.
Expected optical trigger timeouts continue separately and never consume this
recovery schedule.

```powershell
# Optional watchdog override (seconds).
$env:EOM_MOKU_GET_DATA_HARD_TIMEOUT_SECONDS = "20"

# Optional bounded recovery. Without the second variable, bounded mode makes
# one pass through the retry schedule.
$env:EOM_MOKU_RECOVERY_MODE = "bounded"
$env:EOM_MOKU_MAX_RECOVERY_OUTAGE_SECONDS = "1800"
python collect_data.py
```

The default indefinite mode is suitable for a temporary outage in a 48-hour
run, but it does not recover data that was never acquired. Pressing `Ctrl+C`
interrupts acquisition or retry sleep, preserves current files, retires the
worker, and starts a separately bounded attempt to switch Output 2 off. An
unconfirmed shutdown is logged explicitly.

See [Moku acquisition reliability](moku_acquisition_reliability.md) for the
reason a process watchdog is required and for local-logging limitations.

## `analyse_eom_csv.py`: read-only analysis and plotting

`analyse_eom_csv.py` does not import the Moku SDK, claim an instrument, or
change hardware state. It reads a Moku CSV and writes derived files to a
separate directory. It can inspect a growing CSV while acquisition continues;
an incomplete final row is excluded and reported.

The loader accepts both:

- historical `maximum_voltage`; and
- canonical `high_level_voltage`.

Internally, both are represented as `high_level_voltage`. If a file contains
both headers with conflicting values, analysis stops instead of choosing one
silently.

Run the newest available Moku log:

```powershell
python analyse_eom_csv.py
```

Run a specific file without opening plot windows:

```powershell
python analyse_eom_csv.py `
  "Experiment Results/run_.../Moku_logs/raw_photovoltage_tracking.csv" `
  --no-show
```

Manual analysis writes the cleaned table and summary beside the source CSV and
places plots in `Moku_logs/plots/final`. Important outputs include:

```text
Moku_logs/
├── moku_eom_cleaned_photovoltage.csv
├── moku_eom_analysis_summary.txt
└── plots/final/
    ├── moku_eom_minimum_photovoltage_vs_time.png
    ├── moku_eom_high_level_photovoltage_vs_time.png
    ├── moku_eom_apparent_extinction_ratio_vs_time.png
    ├── moku_eom_photovoltage_range_vs_time.png
    ├── moku_eom_normalised_levels_vs_time.png
    └── moku_eom_high_level_vs_minimum_scatter.png
```

When `acquisition_events.jsonl` is beside the CSV, the analyzer counts its
events in the summary and marks connection errors, malformed frames, recovery
starts, and reconnection results on time-series plots. Use `--events-path` to
select another event log. Historical runs without event logs remain valid;
their long CSV gaps are still marked.

The script reports minimum-level and measured high/offset-level behaviour
separately. Its extinction ratio is labelled **apparent** because it is
calculated from photodiode voltage and is corrected for detector dark offset
only if `DARK_OFFSET_V` has been independently calibrated and configured.
Correlation and simultaneous drift do not establish causation.

`run_experiment.py` calls this analyzer automatically for periodic unfinished
snapshots and final plots. Manual analysis is read-only with respect to the raw
CSV, but repeated runs replace derived files with the same names atomically.

## `pulse_control.py`: standalone pulse programming

`pulse_control.py` generates output waveforms but does not acquire Input 1 or
produce the drift-measurement CSV.

It supports two modes:

- `traditional` uses the Oscilloscope instrument's built-in repeating Pulse
  waveform. It can run for a configured duration or until `Ctrl+C`.
- `custom` uses the Arbitrary Waveform Generator to compile ordered pulse and
  gap segments. A sequence may have a finite hardware repeat count or run
  continuously.

Always validate and inspect a preview before enabling hardware:

```powershell
python pulse_control.py --mode traditional --dry-run
python pulse_control.py --mode custom --dry-run --save-preview pulse_previews
```

Dry-run mode validates voltage and timing limits, reports quantisation, and can
save waveform previews without importing the Moku SDK or opening the device.
For a real run, omit `--dry-run`; the script displays the requested plan and
normally requires the operator to type `START` before connecting.

Real pulse-program records are written under:

```text
Experiment Results/run_2026-08-04_15-30-00_BST/Moku_logs/
```

They include the effective configuration, event timestamps, previews, and—for
custom sequences—the normalised lookup-table data used by the AWG.

`pulse_control.py` currently uses its own `MOKU_IP` and does not consume
`EOM_MOKU_ADDRESS` or `EOM_MOKU_FALLBACK_ADDRESS`. It is not a component of
`run_experiment.py`.

## Ownership and supported combinations

The following combinations are intentional:

- `collect_data.py` plus `analyse_eom_csv.py`: supported. The analyzer only
  reads files and does not contact the Moku.
- `run_experiment.py` plus its automatically launched analysis processes:
  supported.
- `pulse_control.py --dry-run` while another experiment is running: it does not
  contact the Moku, although saving previews still writes local files.

The following combination is not supported:

- real `pulse_control.py` plus real `collect_data.py` on the same Moku.

Both hardware scripts are standalone instrument owners. Custom AWG generation
and simultaneous Oscilloscope acquisition require explicit Multi-Instrument
Mode slot deployment and signal routing. The existing single-pulse extraction
also assumes one 10 µs pulse, so it is not scientifically valid for an
arbitrary multi-pulse sequence without a new per-segment measurement model.

## Recommended workflows

### Standard drift experiment

1. Verify the optical, electrical, trigger, and output configuration.
2. Set any required Moku address environment variables.
3. Start `run_experiment.py` or `collect_data.py`.
4. Use `analyse_eom_csv.py` for a read-only manual snapshot if required.
5. Stop with `Ctrl+C` and verify the reported Output 2 shutdown.
6. Use the final derived CSV, summary, plots, and acquisition event log together.

### Custom-pulse development without acquisition

1. Edit the custom sequence in `pulse_control.py`.
2. Run `--dry-run --save-preview`.
3. Check requested connector voltage, achieved timing, and EOM loading.
4. Run without `--dry-run` only after the hardware configuration is verified.
5. Confirm that the output-off cleanup message appears at completion.

Do not treat generated previews as measurements of physical connector or EOM
waveforms. Cabling, impedance, bandwidth, and the attached apparatus can change
the physical voltage and edge timing.
