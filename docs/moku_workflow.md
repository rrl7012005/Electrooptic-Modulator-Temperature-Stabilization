# Moku generation and acquisition workflow

This page follows one configured Moku experiment from validation to saved data.
It also explains which physical or Moku-software details still need a
real-hardware smoke test.

The waveform sent to the Moku is stored as a **lookup table (LUT)**. This is an
ordered list of requested connector-voltage values for one complete waveform
cycle. The Moku moves through the list at the selected sample rate and repeats
it when required. A compiled LUT describes requested digital timing, not the
analogue voltage measured at the connector or EOM.

The **Moku Python SDK** is the manufacturer's software package used by Python to
control the instrument. A **Moku control connection** is the communication link
between that SDK and the instrument. A **session** is one period during which
the program owns and controls the Moku. On this apparatus, the USB-C control
link appears to Windows as a network connection; it is separate from the
photodiode input, waveform output, and optical path.

In the recommended workflow, the main Python program starts a separate helper
program, called a **worker process**, to own the Moku session. The worker handles
both waveform generation and Oscilloscope acquisition. If an SDK call becomes
stuck, the main program can terminate the whole worker instead of waiting
forever. This also prevents two parts of the experiment from silently competing
for the same Moku.

The original root commands remain compatibility entry points while the shared
runtime is being verified:

| Goal | Command |
| --- | --- |
| Validate a complete experiment without hardware | `python run_experiment.py --config configs/experiment.yaml --dry-run` |
| Save or inspect previews without hardware | `python run_experiment.py --config configs/experiment.yaml --preview` |
| Execute after plan review | `python run_experiment.py --config configs/experiment.yaml --execute` |
| Validate a configured resume snapshot | `python run_experiment.py --resume "path\to\experiment_manifest.json"` |
| Save the exact configured resume plan | `python run_experiment.py --resume "path\to\experiment_manifest.json" --preview` |
| Resume after snapshot review | `python run_experiment.py --resume "path\to\experiment_manifest.json" --execute` |
| Run the historical fixed-pulse collector | `python collect_data.py` |
| Preview a standalone legacy pulse programme | `python pulse_control.py --dry-run` |
| Analyse a completed or growing Moku CSV | `python analyse_eom_csv.py [csv_path]` |

Dry-run and preview must not import the Moku SDK, claim a device, or enable an
output. Real execution is never implied by editing a YAML file.

If you only want to check a configuration, use `--dry-run`. If you also want
saved plots and expanded files to inspect, use `--preview`.

## Preflight pipeline

Before hardware access, the master performs these steps in order:

1. Load the master YAML and every referenced file using paths relative to the
   file containing each reference.
2. Reject duplicate keys, unknown fields, missing files, cycles, ambiguous
   units, and component/schedule conflicts.
3. Expand generated temperature schedules and waveform actions.
4. Compile every waveform LUT and measurement plan.
5. Validate requested connector voltage, frequency, LUT memory, sample rate,
   point spacing, timing quantisation, and repeat limits.
6. Create waveform and schedule previews.
7. Print the fully effective plan, including requested and achieved timing.
8. Create a unique run directory, copy the original inputs, write the expanded
   `effective_experiment.yaml`, and save configuration and LUT hashes.
9. For `--execute`, ask for explicit operator confirmation before opening any
   hardware connection.

Resume rebuilds from a copied tree that preserves every relative YAML and CSV
reference, then verifies that tree, the saved effective plan, compiled LUT
files, program identity, and runtime checkpoint. It never reloads later edits
from the source `configs` directory. Bare configured `--resume` is a dry run;
`--execute` and a final `RESUME` confirmation are both required for hardware.

## Runtime ownership

The main Python program does not directly hold the live Moku connection. The
separate worker process accepts only the specific Moku commands required by the
experiment. The main program sets a maximum wait for every command. If the
worker does not reply in time, the main program terminates it and confirms that
it has stopped before allowing a new worker to take control of the Moku.

Multi-Instrument Mode (MIM) divides one physical Moku into separate instrument
slots. The intended configured Moku:Go session contains:

- an Arbitrary Waveform Generator (AWG) slot for traditional and LUT waveforms;
- an Oscilloscope slot for the physical photodiode input;
- an internal waveform reference for triggering and alignment;
- explicit internal routes; and
- explicit configuration of the physical analogue-to-digital converter (ADC)
  input and digital-to-analogue converter (DAC) output.

The configured internal route sends a copy of the AWG waveform to the second
input of the Oscilloscope slot (`Slot2InB`). The Oscilloscope calls this signal
`ChannelB` and watches it to decide when a trace starts. Physical Input 1, which
receives the photodiode signal, is routed to `Slot2InA` and appears as
`ChannelA`. ChannelA and ChannelB are internal Oscilloscope signal names, not
extra physical connectors. ChannelA may be selected deliberately for the older
photodiode-trigger behavior, but it is not the configured default.
Exact platform behavior, physical input settings, and output-enable behavior
depend on the installed SDK and Moku firmware. Tests replace the Moku with a
fake device, so these details still require the controlled real-hardware check
described in [Safety and operator review](safety.md). A successful dry run is
not hardware verification.

Data returned by the Moku may label the two traces `ch1` and `ch2`, even though
the setup commands call them `ChannelA` and `ChannelB`. The program keeps the
original labels but adds clearer names: `photodiode_v` for ChannelA/`ch1` and
`waveform_reference_v` for ChannelB/`ch2`. Calculations explicitly use
`photodiode_v`. The internal waveform reference is kept for timing records and
is never mistaken for photodiode data.

The physical output remains disabled while the session, instruments, routing,
frontend, Oscilloscope, and LUT are prepared. It is enabled only after the
whole active configuration is ready.

## Waveform and acquisition state

Temperature, Moku waveform scheduling, and acquisition use the same monotonic
experiment clock but remain independent state machines. A temperature-stage
change does not restart the waveform. A waveform change does not advance or
alter the temperature target. They interact only through an explicit named or
temperature-linked condition.

Each accepted sample records both states:

- temperature stage index and name;
- temperature phase;
- Moku action index and name;
- waveform name;
- waveform run identifier;
- waveform session identifier;
- whether it is the first sample after a switch or reconnect;
- waveform phase-continuity status;
- measurement profile; and
- requested and achieved timing identifiers.

The waveform run identifier describes one scheduled action. The waveform
session identifier increments when the waveform must be loaded into a new Moku
session after the computer loses software control of the instrument. A
continuous action can therefore retain one run identifier while having several
sessions.

Frames acquired while waveform, routing, trigger, timebase, or measurement-plan
settings are changing are discarded and logged. A partially accumulated sample
is also discarded if it spans two waveform sessions.

## Measurement plans

Each Oscilloscope frame is a voltage-versus-time trace. A **measurement window**
is a selected time interval in that trace. Averaging the photodiode points in a
named window produces a **reduced measurement**, such as `minimum` or
`high_level`. **Raw-only** means keeping the captured trace without calculating
those summaries.

Measurement windows are compiled from achieved waveform timing, not from a
global pulse-width constant. Segment `measurement_role` values such as
`minimum` and `high_level`, or explicit windows, determine which trace samples
are averaged. Programmed edge intervals are excluded.

Those windows are first expressed in achieved LUT phase. With the configured
ChannelB internal reference, the planner locates and interpolates the unique
configured threshold crossing, makes that phase oscilloscope `t = 0`, and
converts every window to trigger-relative time. Reduced measurement is rejected
when the reference never crosses the threshold, has repeated matching crossings,
or uses ChannelA without deterministic LUT phase. Repeated-crossing waveforms
remain valid only in an intentional raw-only workflow with no reduced roles or
windows.

`high_level` means a measured high or offset optical level. It must not be
described as the true transfer-curve maximum unless an independent measurement
establishes that interpretation. A waveform without meaningful roles requires
an explicit measurement plan or raw-trace-only acquisition; the runtime does
not invent minimum or high-level values.

The configured detector dark offset is saved with the run so later analysis
knows which value was used. Where both roles exist, the analysis retains the
dark-offset-corrected normalised extinction ratio:

```text
(H' - L') / (H' + L')
```

where `H'` and `L'` are the high/offset and minimum readings after subtracting
that offset.

The run snapshot also records `minimum_high_level_v`, `maximum_minimum_v`, and
`minimum_sample_count`. The first two are optional historical plausibility
filters: a null value disables that threshold without weakening the invariant
`high_level_voltage > minimum_voltage > dark_offset_v`. Threshold exclusions
and the number of samples remaining after filtering are recorded with the
analysis; they are not evidence that the excluded measurements were physically
zero.

## Trigger timeout and recovery

An absent photodiode trigger is an expected acquisition state. It is reported
at a bounded rate and does not by itself rebuild the Moku session. Transport
errors, stale ownership, a completely blocked SDK call, malformed frames, and
ambiguous output state use the recovery path.

When the old Moku session is lost, the program creates a replacement session
and sends the active settings again in this order. Output 2 remains disabled
during these steps:

1. create and verify the Multi-Instrument session;
2. deploy slots;
3. establish internal routing;
4. apply physical input and output converter settings;
5. confirm the physical output is disabled;
6. configure Oscilloscope sources, timebase, and trigger;
7. upload the active LUT;
8. configure modulation or repeat behavior;
9. restart only when the action's recovery policy permits it; and
10. require one valid acquisition frame before recovery is complete.

The default outage policy holds the current TEC target and pauses the
temperature-stage, valid-data, and waveform-duration timers. After the
replacement is ready, a continuous waveform starts again from the first sample
of its LUT. If a finite burst was active, the program stops because it cannot
know how many cycles reached Output 2. Recovery gives up after the configured
maximum outage time.

Uploading a LUT or enabling an output may fail after the device acted but before
the client received confirmation. In that case the output state is unknown.
The worker is retired, bounded cleanup is attempted, and no output is enabled
on a replacement until the complete configuration is restored.

See [Moku acquisition reliability](moku_acquisition_reliability.md) for the
reason a process watchdog is required and the limits of on-device logging.

## Output and provenance

**Provenance** is the information needed to trace a result back to the exact
configuration, compiled waveform, device state, and time that produced it. A
file **hash** is a digital fingerprint used to detect later changes. A runtime
**checkpoint** records the current schedule positions and confirmed states for
resume; it is not raw measurement data.

A configuration-driven run records, where applicable:

```text
run_.../
|-- effective_experiment.yaml
|-- configuration_hashes.json
|-- analysis_profile.json
|-- experiment_manifest.json
|-- experiment_events.jsonl
|-- runtime_checkpoint.json
|-- waveform_timeline.csv
|-- waveform_timeline.png
|-- Moku_logs/
|   |-- waveform_program.json
|   |-- acquisition_events.jsonl
|   |-- raw_photovoltage_tracking.csv
|   |-- raw_photovoltage_provenance.csv
|   |-- compiled_luts/
|   |-- previews/
|   `-- plots/
|       |-- in_progress/
|       `-- final/
`-- TEC_logs/
```

The exact source YAML files and imported LUT assets are copied without
modification. Hash records identify original bytes, the expanded effective
configuration, and every compiled LUT. Writes to manifests and checkpoints are
atomic. Raw acquisition files are never overwritten by default.

Primary Moku time-series plots contain only measured data and their 60-second
mean. Reconnect, waveform-switch, and temperature-stage information belongs in
the separate timeline and event logs, not as dense vertical markers on the
scientific plots.

## Historical compatibility

Historical acquisition CSVs contain:

| Column | Meaning |
| --- | --- |
| `wall_time` | Unix timestamp in seconds |
| `minimum_voltage` | measured photodiode minimum/baseline |
| `maximum_voltage` | historical name for the measured high/offset level |

Readers accept either `maximum_voltage` or the canonical
`high_level_voltage`. If both are present and disagree, loading stops rather
than choosing silently. A resume appends using the existing file's schema; it
does not rename or rewrite historical raw data. Older runs without an event log
or provenance sidecar remain analysable, with the missing provenance reported.

`collect_data.py` and `pulse_control.py` remain available as compatibility
entry points until the shared runtime has passed the explicit hardware smoke
test. Do not run two real Moku owners at the same time. Their dry-run and
analysis paths are safe because they do not claim the device.
