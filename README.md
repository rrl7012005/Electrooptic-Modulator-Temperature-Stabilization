# EOM temperature stabilisation

This repository contains software for running and analysing electro-optic
modulator (EOM) drift experiments. The main question is whether active
temperature stabilisation improves optical stability over time.

The software keeps two effects separate:

- **Bias-point drift** is movement of the voltage needed to keep the EOM at its
  optical dark point.
- **Extinction-floor drift** is a change in the minimum optical power that can
  be reached at that point.

These are different measurements and must not be combined. The measured
`high_level` is an offset or comparison level; it is not automatically the true
maximum of the EOM transfer curve.

The analysis can also compare extinction ratio, pulsed optical-output
stability, settling after temperature changes, drift rates, and repeatability
after different thermal or electrical histories. Correlation with temperature
is not, by itself, evidence that temperature caused the change.

Common terms used in the documentation:

| Term | Plain-language meaning |
| --- | --- |
| EOM | Electro-optic modulator being studied. |
| TEC | Thermoelectric temperature-control system. |
| YAML | A human-readable configuration-file format that uses indentation to group settings. |
| CSV | A comma-separated table that can be opened in a text editor or spreadsheet program. |
| JSONL | An event-log format with one JSON record on each line. |
| UTC | Coordinated Universal Time, used as the unambiguous reference timestamp. |
| LUT | **Lookup table:** the ordered list of voltage values that describes one complete waveform cycle. The Moku steps through this list at the selected sample rate, then repeats it when required. |
| Moku | The instrument used for waveform output and photodiode acquisition. |
| Linien | The Red Pitaya-based dark-point locking system. |
| SDK | The manufacturer's Python software package used to send commands to an instrument and read data from it. |
| API | The set of software commands provided by an instrument or library. The SDK calls the Moku API on behalf of this program. |
| Moku control connection | The communication link between the Python program on the laboratory computer and the Moku. On this apparatus, USB-C appears to Windows as a network connection. This is separate from the optical beam and the physical waveform cables. |
| Moku session | One period during which a Python worker owns and controls the Moku. A replacement session is a new control connection created after the old one fails. |
| Worker process | A separate helper Python program that owns the live Moku connection. The main program can terminate this helper if a Moku command becomes permanently stuck. |
| AWG | Arbitrary Waveform Generator: the Moku function that sends the programmed waveform to a physical output connector. |
| Trigger | The event the Oscilloscope uses as time zero when it records a trace. |
| Dry run | Complete software validation with no hardware connection. |
| Preview | A dry run that also saves expanded files and plots for inspection. |

## How the experiment fits together

The intended configured arrangement is shown below. The exact cables, routing,
loading, and installed SDK behavior still require apparatus-specific review.

```text
Laboratory computer
|
|-- run_experiment.py
|   |
|   |-- Moku software-control connection (Moku Python SDK)
|   |   `-- Moku:Go in Multi-Instrument Mode
|   |       |-- AWG -> physical Output 2 -> requested electrical waveform path
|   |       |-- physical Input 1 <- photodiode <- EOM optical output
|   |       `-- internal waveform copy -> ChannelB trigger reference
|   |
|   |-- Meerstetter control connection -> TEC and temperature sensors
|   `-- Linien connection -> Red Pitaya dark-point lock
|
`-- saved configuration, raw data, events, checkpoints, and plots
```

Inside the Moku Oscilloscope slot, `ChannelA` is the routed photodiode signal
from physical Input 1. `ChannelB` is an internal copy of the generated waveform
used to decide when a trace starts. These names describe signals inside the
Oscilloscope; they are not additional physical connectors.

When this documentation says **the Moku control connection failed**, it means
the Python program can no longer send commands to, or receive replies from, the
Moku through the SDK. It does not mean that the optical fibre, photodiode cable,
Output 2 cable, TEC connection, or Linien connection was physically unplugged.

## Start here

The recommended workflow uses YAML configuration files and starts in dry-run
mode. A dry run validates the complete experiment without importing a hardware
SDK or connecting to the Moku, TEC controller, or Linien.

If this is your first time using the repository, begin with the
[complete user tutorial](docs/complete_user_tutorial.md). It explains the
apparatus model, installation, every experiment feature, configuration,
execution, recovery, resume, outputs, analysis, and the older compatibility
commands as one guided workflow.

Install the Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

The configured workflow adds PyYAML for parsing YAML. Analysis also uses
NumPy, pandas, Matplotlib, and optionally SciPy. Hardware SDKs are installed and
verified separately; dry-run, preview, and automated tests do not require them.

Validate the example experiment:

```powershell
python run_experiment.py --config configs/experiment.yaml --dry-run
```

Create and inspect waveform and schedule previews:

```powershell
python run_experiment.py --config configs/experiment.yaml --preview
```

Both commands are hardware-free. Real execution requires a separate command
and a final operator confirmation:

```powershell
python run_experiment.py --config configs/experiment.yaml --execute
```

Do not use `--execute` with the public example values. They deliberately contain
placeholder device addresses and null apparatus-specific safety bounds.

## How the configuration files fit together

One master file selects the experiment components and points to three files
with separate responsibilities:

```text
configs/
|-- experiment.yaml             # components and overall completion rule
|-- run_settings.yaml           # device settings, recovery, and analysis
|-- temperature_schedule.yaml   # temperature stages
`-- pulse_schedule.yaml         # waveforms and Moku actions
```

Files under [configs/examples](configs/examples) are complete worked examples.
Their shared supporting files are under
[configs/examples/includes](configs/examples/includes). An example such as
`moku_only_duty_cycle.yaml` refers to the appropriate file in `includes/`; you
normally open the top-level example first.

Paths are resolved relative to the YAML file that contains the reference.
Settings are not loosely merged. Duplicate keys, unknown fields, missing files,
ambiguous units, and conflicting definitions are rejected.

The available components are:

| Component | Purpose | Required settings |
| --- | --- | --- |
| `moku` | Generate waveforms and acquire the photodiode signal. | `run_settings.moku`; normally a pulse schedule. |
| `temp-control` | Write and monitor scheduled TEC targets. | `run_settings.temperature` and a temperature schedule. |
| `temp-log` | Record temperatures without advancing an active target schedule. | `run_settings.temperature`. |
| `lock` | Run the retained Linien lock logger. | `run_settings.linien.host` before real execution; a placeholder is allowed in dry-run. |

## Choose the right guide

| If you want to... | Read... |
| --- | --- |
| Learn the entire program from setup to analysis | [Complete user tutorial](docs/complete_user_tutorial.md) |
| Understand every YAML field | [Experiment configuration](docs/experiment_configuration.md) |
| Build a temperature schedule and understand exactly when each timer starts | [Temperature schedules](docs/temperature_schedules.md) |
| Define square pulses, pulse trains, staircases, segments, Python functions, or CSV LUTs | [Waveform modes](docs/waveform_modes.md) |
| Look up every root command and command-line argument | [Command reference](docs/command_reference.md) |
| Understand independent temperature and waveform timing | [Scheduling, recovery, and resumption](docs/scheduling_and_resumption.md) |
| Understand Moku routing, triggering, measurement windows, and output files | [Moku generation and acquisition](docs/moku_workflow.md) |
| Review equipment risks before a real run | [Safety and operator review](docs/safety.md) |
| Diagnose a failure | [Troubleshooting](docs/troubleshooting.md) |
| Understand the historical Moku freeze and watchdog design | [Moku acquisition reliability](docs/moku_acquisition_reliability.md) |

## Main commands

| Command | Hardware access | Purpose |
| --- | --- | --- |
| `python run_experiment.py --config ... --dry-run` | None | Validate and print an effective experiment plan. |
| `python run_experiment.py --config ... --preview` | None | Validate and save previews. |
| `python run_experiment.py --config ... --execute` | Possible after confirmation | Run the configured experiment. |
| `python analyse_eom_csv.py [csv_path]` | None | Analyse completed or growing Moku data. |
| `python collect_data.py` | Yes | Older fixed-pulse acquisition workflow. |
| `python pulse_control.py --dry-run` | None | Preview the older standalone pulse workflow. |

For new experiments, use the configuration-driven `run_experiment.py` path.
The root-level acquisition and pulse scripts remain available for compatibility
until the shared Multi-Instrument runtime has completed a controlled hardware
smoke test.

Never run two programs that both try to own the same Moku.

## What happens before hardware access

The configured workflow completes all of the following first:

1. Load every referenced YAML or CSV file.
2. Validate the complete experiment and all component combinations.
3. Expand generated temperature and waveform schedules.
4. Compile every waveform LUT and measurement plan.
5. Check voltage, timing, sample-rate, memory, and repeat limits.
6. Create previews and print the effective plan.
7. Copy the source files and save configuration and LUT hashes.
8. Block real execution if a public placeholder or required safety bound remains.
9. Ask the operator for confirmation immediately before hardware modules load.

Waveform voltages always mean requested volts at the selected Moku connector.
They do not describe the voltage that reaches the EOM after cables, loading,
termination, bias networks, or other apparatus.

## Measurement and analysis

Each Oscilloscope capture is a voltage-versus-time trace. A **measurement
window** is a chosen time interval within that trace. The program averages the
photodiode points inside named windows to produce smaller summary values such as
`minimum` and `high_level`; these summaries are called **reduced measurements**.

Measurement windows use achieved LUT timing. Every reduced frame independently
checks ChannelB: the configured-direction threshold crossing must be near
Oscilloscope `t = 0` and the complete observed threshold-transition pattern
must identify the compiled trigger candidate. ChannelA is then aligned by a
sustained per-frame response edge or by an explicitly calibrated fixed delay,
and a configured settling guard is removed before reduction. Missing,
inconsistent, or geometrically incomplete frames never produce reduced values.
Non-finite ChannelA points are removed only after role selection.

For an unambiguous two-level square wave, the `minimum` uses stable low samples
both before and after the delayed optical pulse. Multi-level and multi-pulse
waveforms still require explicit roles; unlabelled regions are never called a
minimum.

A waveform with repeated matching trigger crossings can produce reduced
measurements only when the full cyclic transition patterns distinguish those
crossings. An indistinguishable reference may still be used intentionally in
**raw-only mode**, which keeps the captured trace but does not calculate
minimum, high-level, or extinction values.

The semantic Moku channels are:

- `photodiode_v`: the physical photodiode signal routed through ChannelA;
- `waveform_reference_v`: the internal waveform reference on ChannelB.

Historical three-column CSV files remain supported. The old column
`maximum_voltage` is read as the measured high/offset level and maps to the
canonical name `high_level_voltage`. It is not treated as a proven optical
maximum.

The analysis preserves the dark-offset-corrected normalised extinction ratio:

```text
(H' - L') / (H' + L')
```

`H'` and `L'` are the measured high/offset and minimum values after subtracting
the configured detector dark offset.

Run analysis with:

```powershell
python analyse_eom_csv.py
python analyse_eom_csv.py "path\to\raw_photovoltage_tracking.csv"
```

The analysis reads raw CSV files without modifying them. Cleaned data,
summaries, and plots are written as separate derived files. Scientific
time-series plots use the exactly aligned provenance sidecar: subtle solid lines
mark temperature stages, dotted lines independently mark Moku actions/waveforms,
and optional dashed lines mark reconnect/resume sessions. Labels are bounded so
long schedules remain readable. The cleaned export retains the joined regime
columns, and rolling means restart at regime changes.

## Output and provenance

**Provenance** means the information needed to understand how, when, and with
which settings a result was produced. A **hash** is a digital fingerprint of a
file; if the file changes, its hash changes. A **checkpoint** is a small state
file recording the current temperature stage, waveform action, timers, and last
confirmed output state so a stopped run can be assessed safely.

A configured run creates a unique directory containing, where applicable:

- copies of every source configuration and imported LUT;
- `effective_experiment.yaml`;
- configuration and LUT hashes;
- compiled LUTs and waveform previews;
- raw Moku, TEC, and Linien logs;
- `experiment_events.jsonl`;
- `moku/moku_recovery.log` and `moku/measurement_alignment.csv`;
- `runtime_checkpoint.json`;
- `waveform_timeline.csv` and `waveform_timeline.png`;
- analysis settings and derived plots; and
- UTC, `Europe/London`, and monotonic elapsed timestamps.

Raw experimental data is never overwritten by default. The final timeline is
derived from events that actually occurred; it does not claim that every
planned transition happened.

## Resuming a configured experiment

Start with a hardware-free validation of the copied run snapshot:

```powershell
python run_experiment.py --resume "path\to\experiment_manifest.json"
python run_experiment.py --resume "path\to\experiment_manifest.json" --preview
```

Only this form may reconnect to hardware:

```powershell
python run_experiment.py --resume "path\to\experiment_manifest.json" --execute
```

It still requires the operator to type `RESUME`. Resume verifies the copied
configuration, effective plan, imported assets, LUTs, logs, and checkpoint. It
does not silently use later edits from the original `configs` directory.

If communication fails during a finite burst, the software cannot know how many
cycles reached Output 2. The checkpoint records that count as unknown, and the
burst is never triggered again automatically.

## Recovery in plain language

- If the photodiode signal does not produce the expected Oscilloscope trigger,
  the program reports a trigger timeout. That does not mean the computer has
  lost its software-control connection to the Moku.
- The Moku connection runs in a helper process. The main program limits how long
  it will wait for each Moku command and can terminate the helper if that command
  becomes permanently stuck.
- Valid buffered data is saved before a failed worker is replaced.
- Output 2 stays disabled while a replacement session is configured.
- A continuous waveform may start again from the first sample of its LUT when
  its policy allows this. Its timing relationship to the earlier output is then
  lost, so the break is logged.
- With `continuous_waveform: abort`, recovery never activates or restarts the
  waveform.
- A `duration` action is continuous output stopped by its monotonic wall-clock
  timer. Outage time always counts. If it expires while disconnected, recovery
  confirms output disabled and does not restart it.
- Strict `count` remains indeterminate after a disconnect. Optional
  `bounded_uncertainty` count recovery allocates NCycle chunks in advance,
  never replays an interrupted chunk, and reports an honest delivered interval.
- Reconnect attempts are polled while TEC sampling/scheduling and Linien
  supervision continue. Backoff is capped at 30 seconds; a null maximum outage
  retries until operator interruption.
- If software cannot confirm the physical output state, it reports the state as
  unknown. It does not claim that the output is off.
- The Linien logger first tries to attach to the existing server. It starts a
  server only after Linien explicitly reports that no server is running.
- After a mid-run disconnect, the logger preserves or restores the previous
  locking configuration and makes at most one checked PID relock attempt. A
  failed relock ends the configured experiment and starts normal cleanup.

### Linien connection and relock recovery

Configured v2 runs pass the validated `run_settings.linien.host` to
`linien_logger.py` through `LINIEN_HOST`; legacy direct use retains the original
apparatus default. Every connection first uses `autostart_server=False`. Only
Linien's specific `ServerNotRunningException` causes a second connection with
`autostart_server=True`. Authentication, version, timeout, and unrelated
network errors do not trigger server startup. The first lock must still be
established in the Linien GUI, and automatic relocking is enabled only after
the current run has recorded enough valid locked data.

While locked, the logger retains an in-memory last-known-good copy of the
modulation, demodulation, filters, offsets, channel selection, output polarity,
PID gains and slope, slow-PID, and lock-watch settings. Transient task,
acquisition, and lock state and unrelated manual analog outputs are excluded.
If a reconnected server remains locked, a fresh read-back must match the saved
configuration.

If a mid-run connection remains unlocked for 10 seconds, one automatic relock
attempt is allowed. Sweep and PID control must use the same FAST OUT channel.
The logger restores changed parameters while unlocked, writes them to the FPGA,
verifies their read-back, and positions `sweep_center` at the median of the
preceding 30-second stable control-voltage window, excluding the final 2
seconds. It then invokes Linien's normal simple/manual PID lock. The starting
value is Red Pitaya FAST OUT voltage (`control_signal / 8192`), not FPGA counts
or externally amplified EOM voltage.

The console and the JSONL event log report `ATTEMPTING RELOCK`,
`RELOCK SUCCESSFUL`, or `RELOCK FAILED OR ABORTED`. Following acquisition and
settling grace periods, a 10-second validation window must contain valid locked
data, stay below 0.98 V magnitude, and keep error RMS, robust error variation,
and robust control variation within three times their pre-loss baselines. A
configuration/read-back failure, insufficient history, channel mismatch,
second interrupted attempt, lost lock, output rail, or failed quality check
ends the logger with code 1. Both the configured v2 runner and the legacy
runner then stop their other components using their existing cleanup paths.

The raw CSV retains a real wall-clock gap during the outage; measurements are
not invented or interpolated. Recovery provenance is stored in
`linien_connection_events.jsonl` beside the Linien CSV.

## Older compatibility workflows

The sections below describe retained scripts, not the recommended configured
workflow.

### Legacy master presets

Running `run_experiment.py` without `--config` uses the older preset interface:

```powershell
python run_experiment.py full
python run_experiment.py temperature
python run_experiment.py temperature-lock
python run_experiment.py drift
python run_experiment.py temperature-log
python run_experiment.py --components lock moku
```

Use `--dry-run` to inspect a legacy plan without starting hardware. Legacy
resume uses `--resume-latest` or an older run directory/manifest. It retains its
historical `START`, `RESUME`, and stale-run warning behavior and is separate
from hash-verified configured resume.

The legacy master duration and automatic plotting controls remain near the top
of `run_experiment.py`:

```python
MASTER_EXPERIMENT_LENGTH_SECONDS = None
AUTO_PLOT_INTERVAL_MINUTES = 10.0
```

`None` leaves duration component-led. Set the plot interval to `None` to disable
only in-progress snapshots; final plotting is controlled separately by
`AUTO_PLOT_AT_END`.

### Fixed-pulse collection

`collect_data.py` is the older Oscilloscope workflow. It triggers from the
photodiode on Input 1 and drives the fixed Output 2 pulse. Its recovery settings
can be adjusted with these environment variables:

- `EOM_MOKU_ADDRESS`;
- `EOM_MOKU_RECOVERY_MODE`;
- `EOM_MOKU_MAX_RECOVERY_OUTAGE_SECONDS`; and
- `EOM_MOKU_GET_DATA_HARD_TIMEOUT_SECONDS`.

This path uses the historical three-column raw CSV plus a matching provenance
sidecar. See [Moku acquisition reliability](docs/moku_acquisition_reliability.md)
for its detailed behavior and remaining uncertainties.

### Standalone pulse control

`pulse_control.py` retains traditional built-in pulses and standalone custom AWG
sequences. Preview it before any real run:

```powershell
python pulse_control.py --mode traditional --dry-run
python pulse_control.py --mode custom --dry-run --save-preview pulse_previews
```

A finite custom sequence uses the Moku's `NCycle` mode, meaning the instrument
is asked to produce an exact number of waveform cycles. A continuous sequence
runs until `Ctrl+C`. Switching between separate uploaded sequences includes
Python and SDK delay, so only timing within one LUT is deterministic.

### Standalone TEC schedule

The older `tec_temperature_controller.py` supports its original manual and
generated schedules. Preview it without contacting the controller:

```powershell
python tec_temperature_controller.py --dry-run
```

Do not run it alongside another program that owns the same TEC controller.
Closing Python is not the same as disabling TEC output; follow the configured
completion behavior and verify the apparatus.

## Hardware verification status

Automated tests use fake devices. They do not prove the installed Moku SDK,
Multi-Instrument routing, ChannelA/ChannelB behavior, physical output state,
finite-burst behavior, MeCom types or readback, TEC safety bounds, or Linien
connectivity on the laboratory apparatus.

A controlled smoke test requires separate explicit authorization. Read
[Safety and operator review](docs/safety.md) before any real connection.
