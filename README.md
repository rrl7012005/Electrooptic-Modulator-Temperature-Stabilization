# EOM Drift Stabilisation

Software and documentation for running and analysing temperature stabilised
electrooptic modulator drift experiments.

## Master experiment runner

Use `run_experiment.py` to start and supervise the hardware programs from one
terminal. Run it without arguments for a menu, or select a preset directly:

```powershell
python run_experiment.py full
python run_experiment.py temperature
python run_experiment.py temperature-lock
python run_experiment.py drift
python run_experiment.py temperature-log
```

The `full` preset runs Linien lock-point logging, Moku pulse/photovoltage
collection, and scheduled TEC control together. The `temperature-lock` preset
runs scheduled TEC control and Linien lock-point logging without Moku. The
`drift` preset replaces active TEC control with passive temperature logging.
For a custom combination:

```powershell
python run_experiment.py --components lock moku
```

Preview a plan and validate the configured temperature schedule without opening
any hardware connection:

```powershell
python run_experiment.py full --dry-run
```

Continue the latest interrupted or failed master run with:

```powershell
python run_experiment.py --resume-latest
```

The interactive menu also provides this as option 6. To resume a particular
run, pass either its run folder or manifest:

```powershell
python run_experiment.py --resume "Experiment Results/master_runs/run_YYYYMMDD_HHMMSS"
```

A resume creates new CSV files and links the new master manifest to the old
one, preserving both segments. Temperature control continues from the last
logged step and remaining hold time; time spent stopped does not count toward
the hold. A Moku-led drift run continues only for the unrecorded part of its
configured duration.

The runner reports the time since the previous segment's last file activity.
It warns after 30 minutes by default, or use a different threshold:

```powershell
python run_experiment.py --resume-latest --resume-warning-minutes 15
```

When that threshold is exceeded, the operator must type `CONTINUE` to
acknowledge that the lock and thermal state may have changed, or `CANCEL` to
stop without creating a new run. The normal hardware gate also keeps prompting
until the operator types `START`/`RESUME` or `CANCEL`.

Older manifests may not contain a saved Moku target duration. The runner warns
when it must use the current duration and therefore cannot verify that this
setting is unchanged from the original run.

Resume is refused if the previous run completed normally or if its recorded
child processes still appear to be running. Every component must report a
valid output before the next starts. If any component fails, all others are
stopped; forced hardware termination triggers a separate TEC/Moku emergency
output-off attempt and marks the run failed.

When scheduled temperature control is selected, its completion ends the master
run. Otherwise Moku's configured experiment length is used when Moku is
selected, or the run continues until Ctrl+C. Each child keeps its existing data
folder, and the master writes an experiment manifest under
`Experiment Results/master_runs`.

### Automatic and in-progress plots

`run_experiment.py` automatically creates plots for every selected component:

- Linien lock data are handled by `plot_control.py`;
- passive or controlled TEC data are handled by `plot_temp_log.py`; and
- Moku minimum, measured high/offset level, and apparent extinction data are
  handled by `analyse_eom_csv.py`.

Edit this setting near the top of `run_experiment.py` to choose how often live
snapshots are generated:

```python
AUTO_PLOT_INTERVAL_MINUTES = 10.0
```

Set it to `None` to disable only the live snapshots. Automatic end-of-run plots
are controlled separately by `AUTO_PLOT_AT_END`. The runner prints both choices
in its experiment plan before any hardware is opened.

Unfinished snapshots are replaced in the master run's
`in_progress_plots` folder and are prominently labelled `IN PROGRESS`, including
the newest plotted timestamp. Once all experiment processes have stopped and
closed their logs, a fresh set is written to `final_plots`. Plotting commands,
exit codes, and log-file locations are recorded in `experiment_manifest.json`.
A plotting error produces a warning but does not stop or change the experiment.

You can also inspect a running experiment from another terminal. With no CSV
argument, each command selects the newest corresponding log and opens the plot:

```powershell
python plot_control.py
python plot_temp_log.py
python analyse_eom_csv.py
```

To remove any ambiguity, pass the exact CSV shown by the experiment runner:

```powershell
python plot_control.py "RP_control_logs/run_.../RP_voltage_tracking.csv"
python plot_temp_log.py "tec_temperature_logs/run_.../tec_temperature_control_....csv"
python analyse_eom_csv.py "Experiment Results/moku_pulse_runs/run_.../raw_photovoltage_tracking.csv"
```

These scripts open experiment CSVs read-only and tolerate an incomplete final
row while a logger is appending. Saved PNGs are replaced atomically, so neither
manual nor automatic plotting writes to or locks the raw experiment log. The
historical Moku column `maximum_voltage` is preserved in the raw CSV but mapped
to `high_level_voltage` in derived output because it is not assumed to be the
true transfer-curve maximum.

### Moku acquisition timeouts and recovery

`collect_data.py` keeps the optical signal on Input 1 as its Normal rising-edge
trigger at 0.6 V. The Input 1 threshold crossing therefore remains at `t = 0`,
and the existing baseline and pulse-level windows retain their meaning.

An absent Input 1 crossing is an expected experimental state when the optical
floor moves above the threshold or Linien is searching for lock. Trigger
timeouts are counted and reported at most once per minute, but they do not stop
the run or cause a Moku reconnection. Acquisition resumes when the Input 1 edge
returns.

Transport failures, lost or stale ownership, and the device response
`API Connection already exists` are handled separately. Before recovery the
collector saves its buffered measurements. It then makes at most three attempts
to reconstruct the connection, reapplies the same recorded frontend,
source, timebase, Input 1 trigger, and Output 2 pulse settings, and verifies the
new API session using a read-only summary request. Reconnection is not verified
by demanding an optical trigger because the optical edge may still legitimately
be absent.

The primary connection address defaults to `MokuGo-008058` and can be changed
with `EOM_MOKU_ADDRESS`. An optional fallback is used only when it has been
explicitly supplied through `EOM_MOKU_FALLBACK_ADDRESS`. For example, in the
same PowerShell session used to start the collector:

```powershell
$env:EOM_MOKU_FALLBACK_ADDRESS = "<verified stable IP of this Moku>"
python collect_data.py
```

Replace the placeholder with an address verified for this physical device.
For a USB connection, Moku reports a scoped link-local IPv6 address. Copy the
current address from the Moku Desktop App or `mokucli list` and retain its scope
identifier, enclosing the complete address in square brackets as required by
the Moku Python API, for example
`[fe80::...%<Windows-interface-index>]`.
For Ethernet or Wi-Fi IPv4, prefer a DHCP reservation or an otherwise
documented stable assignment; an old DHCP address could later identify a
different instrument. The collector never
guesses or discovers a fallback. During each connection round it resolves and
tries the primary address first, then the configured fallback. Resolution
failures, resolved addresses, connection failures, and the selected address are
written to `acquisition_events.jsonl`. If neither address works, the existing
bounded recovery and shutdown behaviour applies.

Recovery can restart the Output 2 waveform phase or cause a brief output
interruption. Each attempt and result is therefore written to
`acquisition_events.jsonl` in the run directory, together with timestamps, Moku
SDK version, exception details, counters, last-valid-frame time, trigger
configuration, and pulse settings. Treat a recorded waveform restart as an
experimental timing discontinuity rather than continuous pulse history.

Repeated malformed frames trigger the same bounded recovery path. If recovery
is exhausted, the buffered CSV is saved, Output 2 shutdown is attempted, and the
collector exits with an error. If the API remains unavailable, software cannot
guarantee that Output 2 was disabled; the event log and console report this
explicitly.

## Moku:Go pulse control

`pulse_control.py` provides two separately validated pulse modes on a selected
Moku:Go output:

- `traditional` uses the Oscilloscope's built-in repeating Pulse waveform with
  frequency, duty cycle, voltage levels, edge time, and an optional run time;
- `custom` uses the Arbitrary Waveform Generator (AWG) for any ordered list of
  pulse and gap durations. Each sequence can run for an exact hardware repeat
  count or continuously.

Edit `PULSE_MODE` and the settings near the top of `pulse_control.py`. A custom
sequence has this form:

```python
CUSTOM_SEQUENCES = [
    {
        "name": "example_two_pulse_sequence",
        "segments": [
            {"type": "pulse", "duration_s": 10e-6, "edge_time_s": 100e-9},
            {"type": "gap", "duration_s": 5e-6},
            {"type": "pulse", "duration_s": 20e-6, "edge_time_s": 100e-9},
            {"type": "gap", "duration_s": 50e-6},
            {"type": "gap", "duration_s": 250e-6},
        ],
        "repeat_count": 10,
    },
]
```

A pulse duration includes its rising edge, high-level plateau, and falling
edge. A gap stays at `LOW_LEVEL_V`. Set `repeat_count` to an integer for a Moku
hardware `NCycle` burst, or to `None` to continue until `Ctrl+C`. Multiple
finite sequences run in list order. Timing within a sequence and its repeat
count are hardware-generated; the changeover delay between different list
entries includes Python/API upload latency and is therefore not deterministic.

Always inspect a dry run first. It does not import the Moku package or connect
to a device, and it reports every requested and achieved quantised duration:

```powershell
python pulse_control.py --mode custom --dry-run --save-preview pulse_previews
python pulse_control.py --mode traditional --dry-run
```

For a real run, omit `--dry-run`. The script displays the complete validated
plan and requires the operator to type `START` before opening the Moku. Run
records, waveform previews, exact normalised LUT values, and UTC/local event
timestamps are saved under `Experiment Results/pulse_programs`. The selected
output remains disabled during custom setup and is switched off in cleanup
after normal completion, `Ctrl+C`, or an API error where communication still
permits it.
Moku's manual trigger is device-wide, so custom mode disables both physical
AWG outputs before setup and again during cleanup; only `OUTPUT_CHANNEL` is
enabled for the requested burst.

The validator enforces conservative Moku:Go limits before connection:

- requested connector levels must remain between -5 V and +5 V and cannot
  exceed 10 Vpp;
- traditional Pulse amplitude is at least 2 mVpp, frequency is 1 mHz to
  20 MHz, and edge/pulse width is at least 16 ns;
- custom AWG amplitude is at least 4 mVpp and sequence frequency is 1 mHz to
  10 MHz;
- a custom pulse edge is at least 16 ns and must occupy at least two LUT points
  at the finest safe resolution for the whole sequence;
- the conservative AWG memory table is 8,192 points at 125 MSa/s, 16,384 at
  62.5 MSa/s, 32,768 at 31.25 MSa/s, and 65,536 at 15.625 MSa/s; and
- a finite repeat count is between 1 and 1,000,000 cycles.

The compiler also checks that `point_count * sequence_frequency` does not
exceed the selected sample rate, preventing skipped LUT points. It rejects an
unrepresentable sequence instead of silently shortening a gap or edge. The
real API calls use strict mode to prevent Moku-side coercion.

Reported custom edge times are the programmed LUT ramp durations. They are not
a measurement of the analogue connector rise/fall time; Moku output bandwidth,
the connected load, cabling, and the rest of the apparatus can make the
physical edge different. Verify critical edge timing on an oscilloscope before
using it as an experimental calibration.

Custom mode owns the Moku as a standalone AWG. It cannot run at the same time
as the existing standalone Oscilloscope-based `collect_data.py`, and it is not
currently a `run_experiment.py` component. Simultaneous custom generation and
Moku acquisition would require a separately designed and verified
Multi-Instrument Mode signal route; do not run the two standalone scripts
against the same Moku.

## Project objective

The project investigates whether active temperature stabilisation reduces
drift in an electrooptic modulator during normal operation.

## Timed TEC temperature control

Edit `TEMPERATURE_SCHEDULE` near the top of `tec_temperature_controller.py`
to define any sequence of `(temperature_C, duration_minutes)` steps. Use
`(None, duration_minutes)` for a period with the TEC output switched off.

Preview the schedule without connecting to the controller:

```powershell
python tec_temperature_controller.py --dry-run
```

Then close the Meerstetter software and `tec_temp_logger.py` before starting
the controller:

```powershell
python tec_temperature_controller.py
```

The script records object and sink temperatures, current, voltage, setpoint,
and programme step in a timestamped folder under `tec_temperature_logs`. Before
the first scheduled target is written, it requires finite object/sink readings
and checks that the controller is not already reporting its error state.
