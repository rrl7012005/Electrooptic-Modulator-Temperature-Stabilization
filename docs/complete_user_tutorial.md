# Complete user tutorial

This is the start-to-finish guide for a user who understands the EOM physics
but has not used this repository or this particular apparatus. It explains
what the program controls, how the configuration files fit together, how to
make a hardware-free trial, how to start and stop a real run, what recovery can
and cannot do, and how to interpret the saved files.

The recommended interface is the **configuration-driven v2 workflow**:

```powershell
python run_experiment.py --config path\to\experiment.yaml --dry-run
python run_experiment.py --config path\to\experiment.yaml --preview
python run_experiment.py --config path\to\experiment.yaml --execute
```

The older positional commands such as `python run_experiment.py drift` remain
available for compatibility, but they are a separate workflow. Start with the
YAML workflow unless you specifically need to reproduce an old run.

## 1. What the program is for

The program can coordinate up to four experiment components:

| Component name | Instrument or process | What it does |
| --- | --- | --- |
| `moku` | Moku:Go AWG and Oscilloscope | Generates a voltage waveform, captures photodiode traces, and reduces defined trace windows to measurements such as `minimum` and `high_level`. |
| `temp-control` | Meerstetter TEC controller | Applies a validated sequence of temperature targets, waits for stability when requested, holds each stage, and logs the controller state. |
| `temp-log` | Meerstetter TEC controller | Reads and logs TEC temperatures and electrical output without running a temperature schedule. It cannot be selected together with `temp-control` because both would own the same connection. |
| `lock` | Red Pitaya running Linien | Logs the dark-point lock error, raw fast-output control voltage, and raw photodiode monitor signal. It can recover a lost connection and make one guarded mid-run relock attempt. |

The scientific outputs deliberately keep these quantities distinct:

- **bias-point drift**: movement of the Linien control voltage required to
  maintain the dark point;
- **extinction-floor drift**: movement of the measured minimum optical signal;
- **high-level drift**: movement of an explicitly measured comparison or
  offset level; and
- **extinction ratio**: a derived comparison of high and minimum levels after
  applying the configured detector dark offset.

`high_level` is not automatically the true transfer-curve maximum. Its meaning
comes from the waveform and measurement window used in that experiment.

## 2. The apparatus as the software sees it

The configured v2 signal and control arrangement is:

```text
Windows laboratory computer
|
|-- Python master process: run_experiment.py
|   |
|   |-- Moku SDK control connection
|   |   `-- Moku:Go Multi-Instrument Mode
|   |       |-- Slot 1 AWG -> physical Output 2 -> requested waveform path
|   |       |-- physical Input 1 <- photodiode monitoring EOM optical output
|   |       `-- internal AWG copy -> Slot 2 ChannelB trigger reference
|   |
|   |-- serial/MeCom connection -> Meerstetter TEC controller
|   |
|   `-- network/Linien connection -> Red Pitaya dark-point lock
|
`-- one run directory containing configuration, data, events, and checkpoints
```

Inside the Moku Oscilloscope:

- `ChannelA` is the signal routed from physical Input 1, normally the
  photodiode voltage;
- `ChannelB` is the internal copy of the AWG waveform used as the timing
  reference; and
- Output 2 is a physical connector. ChannelB is not another output connector.

There are three independent software connections: Moku, TEC, and Linien. Loss
of one does not prove that a cable or the other two instruments failed. The
program treats each connection according to its own safety and recovery rules.

Before a real run, the operator still has to establish and verify the actual
optical path, detector gain, electrical loading, attenuation, EOM drive path,
TEC/thermistor/heatsink arrangement, and Red Pitaya wiring. The repository does
not infer those facts.

## 3. Safety boundary

Use `--dry-run` first and `--preview` second. Both are hardware-free. Only
`--execute`, followed by typing the exact confirmation word `EXECUTE`, permits
the configured runtime to import hardware SDKs and open connections.

Important limitations:

- Voltages in waveform YAML are **requested Moku connector voltages**. They
  are not externally amplified EOM voltages.
- Public example addresses and TEC bounds are placeholders. The execution gate
  rejects them.
- Automated tests use fake devices. They do not prove physical routing,
  polarity, load voltage, detector saturation, Moku SDK behavior, TEC safety,
  or Linien behavior on the apparatus.
- `Ctrl+C` asks the program to perform bounded cleanup, but a lost connection
  can prevent software from confirming the final physical output state.
- Stopping Python is not synonymous with disabling TEC output. Temperature
  completion behavior is an explicit schedule setting.
- A real lock must be established and judged acceptable in the Linien GUI
  before the logger's automatic mid-run relock feature becomes eligible.

Read [Safety and operator review](safety.md) before the first real connection.

## 4. Repository tour

The files a normal user interacts with are:

```text
run_experiment.py                 main experiment command
analyse_eom_csv.py                Moku measurement analysis command
configs/
|-- experiment.yaml              example master experiment
|-- run_settings.yaml            example device/recovery/analysis settings
|-- temperature_schedule.yaml    example temperature schedule
|-- pulse_schedule.yaml          example waveform schedule
`-- examples/                     complete worked experiment configurations
docs/                             detailed reference documentation
runs/                             real configured runs, created by --execute
previews/                         hardware-free artifacts, created by --preview
src/eom_stabilisation/            reusable implementation modules
```

Root scripts such as `collect_data.py`, `pulse_control.py`,
`tec_temperature_controller.py`, and the positional `run_experiment.py` modes
are retained compatibility tools. Do not begin by editing source-code constants
when a YAML option exists.

Files under `legacy/original_scripts/` are frozen historical copies and must
not be edited.

## 5. First-time Windows setup

### 5.1 Open PowerShell in the repository

In VS Code, open the repository folder and then open **Terminal -> New
Terminal**. Confirm that the prompt ends in the repository name. You can also
navigate there explicitly:

```powershell
Set-Location "C:\path\to\EOM Temperature Stabilisation"
```

`Set-Location` changes PowerShell's working directory. Quotes are required
because the folder name contains spaces.

### 5.2 Activate the Python environment

Use the environment in which the laboratory's compatible Moku, MeCom, and
Linien clients are installed. For example, if that environment is named
`moku311`:

```powershell
conda activate moku311
python --version
```

Replace `moku311` if the actual environment has a different name. The Python
interpreter used to start the master is also used for the Linien subprocess,
so all required clients must be visible in that same environment.

### 5.3 Install repository dependencies

```powershell
python -m pip install -r requirements.txt
```

The configured parser requires PyYAML. Compilation and plotting use NumPy and
Matplotlib; analysis uses NumPy, pandas, Matplotlib, and optionally SciPy. The
hardware path additionally requires the apparatus-compatible Moku, pyMeCom,
and Linien packages. The repository does not currently pin every laboratory
SDK in `requirements.txt`, so use the versions verified for the apparatus and
vendor API rather than guessing an upgrade immediately before an experiment.

Useful checks are:

```powershell
python run_experiment.py --help
python analyse_eom_csv.py --help
```

Do not use `python linien_logger.py --help`: that script has no CLI help mode
and immediately expects its credential before beginning its logger workflow.
Normally `run_experiment.py` launches it.

### 5.4 Supply the Linien password safely

The Red Pitaya host belongs in YAML, but the password does not. Set it in the
current PowerShell process before an experiment containing `lock`:

```powershell
$env:LINIEN_PASSWORD = "your-password"
```

This assigns a process environment variable inherited by the Linien logger.
Do not write the password into YAML, source code, a command transcript intended
for publication, or Git.

Closing that terminal normally discards the process-scoped value. To remove it
earlier:

```powershell
Remove-Item Env:LINIEN_PASSWORD
```

## 6. Your first hardware-free run

From the repository root, run:

```powershell
python run_experiment.py --config configs\experiment.yaml --dry-run
```

This command:

1. loads the master YAML and its three referenced files;
2. rejects duplicate keys, unknown fields, invalid types, impossible timing,
   unsafe target relationships, and inconsistent component choices;
3. expands generated temperature stages;
4. compiles each waveform to an achieved digital lookup table (LUT);
5. calculates achieved periods, sample counts, run durations, and measurement
   windows;
6. checks that schedule dependencies and the master completion rule can
   terminate; and
7. prints the effective plan.

It does **not** import Moku, MeCom, or Linien hardware packages, create a run
directory, or open a device connection.

Read the printed plan carefully. In particular, check:

- selected components;
- expanded temperature stage names, targets, and durations;
- requested versus achieved waveform timing;
- requested connector voltage range;
- exact hardware burst count or duration rounding policy;
- measurement windows and whether the plan is `raw_only`; and
- the master completion condition.

Next create persistent inspection artifacts:

```powershell
python run_experiment.py --config configs\experiment.yaml --preview
```

Preview remains hardware-free. It creates a unique directory such as:

```text
previews/20260813_143000_example_experiment/
```

Open `effective_experiment.yaml`, `waveform_timeline.png`, and the PNG files in
`waveforms/`. The preview shows the waveform that the compiler can actually
represent, which may differ slightly from the request because the LUT has a
finite sample interval.

## 7. Make a private experiment configuration

Do not turn the public example into your permanent apparatus configuration.
Copy the four starter files into a new folder:

```powershell
New-Item -ItemType Directory -Path configs\my_first_experiment
Copy-Item configs\experiment.yaml, configs\run_settings.yaml, configs\temperature_schedule.yaml, configs\pulse_schedule.yaml -Destination configs\my_first_experiment
```

The first command creates a folder. The second copies the four files into it.
Because the master uses relative filenames, the copied set continues to point
to the copied supporting files.

Edit:

```text
configs/my_first_experiment/experiment.yaml
configs/my_first_experiment/run_settings.yaml
configs/my_first_experiment/temperature_schedule.yaml
configs/my_first_experiment/pulse_schedule.yaml
```

Run a dry run after each small change:

```powershell
python run_experiment.py --config configs\my_first_experiment\experiment.yaml --dry-run
```

YAML indentation is structural. Use spaces, not tabs. Paths in a YAML file are
resolved relative to that file, not relative to the terminal's directory.

## 8. The four YAML files

### 8.1 Master experiment file

The master file answers: **what participates, which files define it, and when
does the whole run end?**

```yaml
name: my_first_experiment
components: [lock, moku, temp-control]
run_settings_file: run_settings.yaml
temperature_schedule_file: temperature_schedule.yaml
pulse_schedule_file: pulse_schedule.yaml
end_when: all_schedules_complete
```

Allowed components are `lock`, `moku`, `temp-control`, and `temp-log`.
`temp-control` and `temp-log` are mutually exclusive.

Common component selections are:

| Goal | Components |
| --- | --- |
| Pulse/acquire only | `[moku]` |
| Controlled temperature only | `[temp-control]` |
| Passive TEC logging only | `[temp-log]` |
| Lock-point logging only | `[lock]` |
| Temperature plus lock, no pulse waveform | `[lock, temp-control]` |
| Pulse acquisition plus passive temperature log | `[moku, temp-log]` |
| Full coordinated experiment | `[lock, moku, temp-control]` |

The master completion choices are:

| `end_when` value | Meaning |
| --- | --- |
| `all_schedules_complete` | End after all selected finite schedules finish. |
| `moku_schedule_complete` | End when the Moku action schedule finishes. |
| `temperature_schedule_complete` | End when the temperature schedule finishes. |
| `operator_ctrl_c` | Keep running until the operator presses `Ctrl+C`. |

A fixed-duration master uses a mapping:

```yaml
end_when:
  mode: fixed_duration
  duration_hours: 2
```

Use exactly one of `duration_s`, `duration_ms`, `duration_minutes`, or
`duration_hours`.

### 8.2 Run settings file

The run-settings file contains apparatus selection, acquisition settings,
recovery policy, and the analysis profile. It does not define the temperature
or waveform timeline.

#### Temperature connection and safety

Replace the placeholder values only with apparatus-verified values:

```yaml
temperature:
  serial_port: COM10
  channel: 1
  min_target_c: 20
  max_target_c: 35
  sampling_interval_s: 1.0
  object_temperature_min_c: 15
  object_temperature_max_c: 40
  sink_temperature_min_c: 15
  sink_temperature_max_c: 45
```

The numbers above are syntax examples, **not recommendations**. The four
sensor plausibility bounds and target limits must be verified for the actual
controller, thermistor, TEC, heatsink, and EOM assembly. Real execution is
blocked when the sensor bounds remain null. The current verified adapter
supports channel 1.

Before a target write, the runtime connects, reads controller identity/status
and current temperatures, checks finite/plausible values and the target range,
writes the volatile target, and verifies readback. It does not silently change
PID values, thermistor coefficients, current limits, voltage limits, polarity,
or persistent protection settings.

#### Linien host

```yaml
linien:
  host: 169.254.x.x
```

Use the verified Red Pitaya hostname or IP address. Do not publish an
unnecessary laboratory address. The password still comes from
`LINIEN_PASSWORD`, not this file.

#### Moku connection, routing, and acquisition

```yaml
moku:
  address: MokuGo-XXXXXX
  fallback_address: null
  force_connect: false
  platform_id: 2
  awg_slot: 1
  oscilloscope_slot: 2
  output_channel: 2
  input_channel: 1
  frontend_impedance: 1MOhm
  frontend_coupling: DC
  frontend_attenuation: 0dB
  trigger_source: ChannelB
  trigger_level_v: 0.6
  trigger_edge: Rising
  trigger_mode: Normal
  trigger_type: Edge
  timebase_start_s: -0.000045
  timebase_end_s: 0.000045
  timebase_max_length: 16384
  sample_period_s: 1.0
  frames_per_sample: 5
  raw_capture:
    reduced_mode: periodic
    interval_minutes: 10
    first_after_action: true
    first_after_session: true
    save_rejected: false
    maximum_rejected_frames_per_sample: 1
    raw_only_window: full_period
```

`address` is the primary software-control address. `fallback_address` is an
optional second *known and verified* address; the program does not discover or
guess devices. `force_connect: false` avoids deliberately taking ownership
from another session.

The current v2 hardware adapter is intentionally narrow: Moku:Go platform 2,
AWG slot 1, Oscilloscope slot 2, physical Output 2, and physical Input 1.
Dry-run validation is not proof of physical routing or analog loading.

`sample_period_s` is the interval between reduced acquisition samples.
`frames_per_sample` is the number of valid frames averaged into one saved
measurement. A transition frame is discarded so an average does not cross a
waveform or recovered-session boundary. An exact finite hardware burst uses
one complete acquired frame rather than triggering another burst merely to
satisfy the averaging preference.

Every non-raw measurement window must fit inside the Oscilloscope timebase.
`trigger_level_v` must give an unambiguous ChannelB reference. A single matching
crossing is simplest; several are allowed only when their complete cyclic
threshold-transition patterns distinguish them. Raw-only automatic acquisition
uses a full achieved period by default. Choose `raw_only_window:
trigger_window` with explicit pre/post durations when a cropped trace is
intended; a full-period cap that is too short is rejected.

`raw_capture` also keeps bounded diagnostic evidence for reduced runs. The
example saves a trace every ten minutes and on the first accepted sample after
an action or real reconnect. `reduced_mode: all` can generate very large run
directories. `save_rejected` is off by default and, when enabled, is bounded by
`maximum_rejected_frames_per_sample`.

#### Analysis profile

```yaml
measurement:
  dark_offset_v: 0.0
  minimum_high_level_v: 0.6
  maximum_minimum_v: 0.6
  minimum_sample_count: 10
  maximum_optical_delay_s: 0.000002
  reference_edge_tolerance_s: 0.000001
  minimum_valid_points_per_role: 10
  minimum_optical_edge_snr: 3.0
  optical_delay_mode: per_frame
  optical_settling_guard_s: 0.0
  maximum_consecutive_invalid_optical_samples: 60
  maximum_invalid_optical_duration_s: 300
```

These public values are illustrative historical filters, not detector
calibration. Set `dark_offset_v` from an independently justified dark reading.
Set either threshold to `null` to disable that filter. The profile is saved
with the run so later analysis does not silently depend on whatever defaults
exist on another computer.

`per_frame` alignment requires a sustained optical transition in every accepted
trace and rejects isolated spikes. `fixed` alignment instead requires a
separately calibrated `fixed_optical_delay_s`; it is useful when timing is known
but the optical trace is flat or changes only in amplitude. The settling guard
trims the start of every shifted role window. Do not copy the zero guard from
the example as if it were a calibration. The two invalid-optical limits stop a
scientifically empty run without disguising that condition as a network fault.

#### Moku recovery policy

```yaml
recovery:
  temperature_hold_during_moku_outage: pause_timer
  waveform_duration_during_outage: pause_timer
  continuous_waveform: restart_from_phase_zero
  finite_burst_interrupted: abort
  maximum_moku_outage_s: 1800
```

With this policy, a Moku outage pauses relevant experiment timing, leaves the
TEC at its existing target, and tries to establish a replacement Moku session.
A continuous waveform restarts from the first LUT sample. It cannot resume at
the exact lost phase. An interrupted exact finite burst is marked
indeterminate and is not replayed automatically because software cannot prove
how many cycles reached the connector.

### 8.3 Temperature schedule file

A target stage normally follows:

```text
target request -> settling -> stable qualification -> hold -> complete
```

The hold timer begins after the requested stable period, not immediately after
the target changes. If stability is not required, holding begins after the
validated target write/readback.

An explicit schedule is easiest to understand:

```yaml
name: my_temperature_schedule
type: explicit
completion_behavior: hold_current_target
stability:
  required: true
  stable_duration_s: 30
  timeout_s: 600
stages:
  - name: baseline_25_c
    target_c: 25
    hold_duration_minutes: 30
    sampling_interval_s: 1
    notes: Baseline measurement.
  - name: measurement_30_c
    target_c: 30
    hold_duration_minutes: 30
    notes: Second measurement stage.
```

An explicit stage with `target_c: null` represents a TEC-off stage. That is
physically different from merely ending a timer.

Read the explicit example literally:

- `sampling_interval_s: 1` means one requested TEC read/log sample each second;
- `stable_duration_s: 30` means 30 uninterrupted seconds satisfying the
  controller stable flag and any configured software tolerance;
- `timeout_s: 600` means stable qualification must finish within ten minutes
  of entering the stage; and
- `hold_duration_minutes: 30` begins only after that qualification.

A stage can override only the stability fields it needs. All omitted values
remain inherited from the schedule. For example:

```yaml
  - name: slower_qualification
    target_c: 30
    hold_duration_hours: 2
    stability:
      stable_duration_minutes: 10
```

This changes only the stable duration. It retains schedule-level `required`,
`tolerance_c`, and `timeout_*` values.

Generated schedule types are also supported:

- `sweep`: build transitions and measurement points from `start_c`,
  `finish_c`, measurement interval, transition increment, hold durations,
  optional reversal, and cycle count;
- `targets`: use an explicit list of target temperatures while generating the
  stage details; and
- `range`: generate targets from a start, stop, and step.

The shortest generated form is a target list:

```yaml
name: three_targets
type: targets
completion_behavior: hold_current_target
targets_c: [20, 22.5, 27]
hold_duration_minutes: 30
reverse: true
cycles: 1
stability:
  required: true
  stable_duration_minutes: 5
  timeout_minutes: 30
```

It expands to `20, 22.5, 27, 22.5, 20`. The 27 °C turn is not duplicated. A
range expresses the same idea with regular spacing:

```yaml
name: descending_range
type: range
completion_behavior: hold_current_target
start_c: 30
finish_c: 24
step_c: 2
hold_duration_minutes: 30
```

This expands to `30, 28, 26, 24`. Temperature `step_c` is always a positive
magnitude; start and finish determine direction. A sweep adds short transition
holds between its longer measurement holds. These three concepts are different,
so choose the form that states the experiment most directly.

Always inspect the expanded stage list printed by dry-run. The plain-language
timing rules, every accepted field, and worked expansions are in
[Temperature schedules](temperature_schedules.md). The compact schema is in
[Experiment configuration](experiment_configuration.md), and a working sweep is in
[`configs/examples/temperature_only_sweep.yaml`](../configs/examples/temperature_only_sweep.yaml).

Completion behavior defines the physical request after the last stage or a
configured settling timeout:

| Behavior | Meaning |
| --- | --- |
| `hold_current_target` | Leave the currently requested target in place. |
| `disable_output` | Explicitly request TEC output disable. |
| `return_to_safe_target` | Request a separately configured safe target; this requires the corresponding verified setting. |
| `revert_to_stored_target` | Restore the target captured during preflight. |

Choose this deliberately. Do not treat these behaviors as interchangeable.

### 8.4 Pulse schedule file

A pulse file has two sections:

```yaml
waveforms:
  square_100khz:
    type: square
    low_level_v: 0.0
    high_level_v: 1.0
    frequency_hz: 100000
    duty_cycle_percent: 10
    edge_time_ns: 100

moku_schedule:
  - name: five_cycles
    waveform: square_100khz
    start: immediately
    run:
      mode: count
      count: 5
```

`waveforms` defines reusable one-cycle shapes. `moku_schedule` defines when
each shape begins and how long it runs.

## 9. Every waveform mode

All waveform voltages are Moku connector voltages. Every mode is compiled to a
finite LUT and reports its achieved timing.

| Mode | Use | Working example |
| --- | --- | --- |
| `square` | A two-level periodic waveform defined by frequency/duty cycle or equivalent timing pairs. | [`moku_only_duty_cycle.yaml`](../configs/examples/moku_only_duty_cycle.yaml) |
| `segments` | A cycle made from named pulse and gap segments, including unequal pulse levels and explicit measurement roles. | [`moku_only_two_pulse_sequence.yaml`](../configs/examples/moku_only_two_pulse_sequence.yaml) |
| `pulse_train` | A generated group of repeated pulses and gaps inside one deterministic LUT. | [`pulse_train.yaml`](../configs/examples/pulse_train.yaml) |
| `staircase` | Explicit or generated levels with dwell times and up/down behavior. `dwell_*` is total time per step, including its starting programmed edge; the settled plateau is approximately dwell minus edge time before quantisation. | [`continuous_staircase.yaml`](../configs/examples/continuous_staircase.yaml) |
| `custom_python` | A function from the safe built-in registry; currently demonstrated by `raised_cosine`. It does not evaluate arbitrary Python from YAML. | [`custom_python_waveform.yaml`](../configs/examples/custom_python_waveform.yaml) |
| `csv_lut` | A waveform imported from a CSV asset with finite, increasing, uniformly spaced `time_s` and `voltage_v` columns. | [`imported_lut_waveform.yaml`](../configs/examples/imported_lut_waveform.yaml) |

Use a single `segments`, `pulse_train`, or imported LUT cycle when relative
timing between pulses must be deterministic. Changeover between separate
actions involves Python and SDK latency and is not deterministic.

The detailed syntax, every field, timing alternatives, staircase endpoint
sequences, dwell arithmetic, edge treatment, and measurement roles are in
[Waveform modes](waveform_modes.md).

## 10. Measurement roles and raw-only acquisition

The Oscilloscope returns a voltage trace. The program can either reduce defined
parts of that trace or save the full trace.

For reduced acquisition, a waveform segment or explicit window has a role such
as:

- `minimum`: optical dark/low region;
- `high_level`: comparison/offset region; or
- another explicitly named role, which gets its own normalized CSV column.

The compiler converts waveform-phase windows into trigger-relative
Oscilloscope time using the achieved LUT and configured ChannelB threshold
crossing. One crossing is straightforward. With several crossings in the same
direction, the compiler and reducer use the complete pattern of ChannelB
threshold transitions to identify which candidate produced `t = 0`; a repeated
pattern that cannot be distinguished is rejected. Every delayed and guarded
role window needs at least
`run_settings.measurement.minimum_valid_points_per_role` finite acquired
samples. The default is 10, not one.

If a waveform has no scientifically meaningful role/window or its reference
has genuinely ambiguous crossings, configure it as raw-only. Raw-only records
compressed `.npz` traces and an index instead of inventing `minimum` or
`high_level` values. The NPZ metadata and index retain stable frame/sample IDs,
timestamps, action/session state, timebase and trigger geometry, and plan/LUT
hashes, so the filename is not the sole provenance record.

Inspect the preview and verify on the real Oscilloscope that:

- ChannelA contains the intended photodiode trace;
- ChannelB contains the intended waveform reference;
- the trigger crossing is stable and either unique or signature-distinguishable;
- neither channel is clipped or saturated; and
- every measurement window covers the intended settled portion of the pulse.

## 11. Starting and stopping waveform actions

An action can start:

- `immediately`;
- at a specified monotonic elapsed time;
- `after_previous_waveform_action`;
- when a named temperature stage starts;
- when a named temperature stage becomes stable;
- when a named temperature stage completes; or
- on a named supervisor event.

A waveform run can use:

| Run mode | Behavior |
| --- | --- |
| `count` | Ask the Moku for an exact hardware cycle count. |
| `duration` | Run continuous output until a monotonic wall-clock deadline. |
| `until_temperature_stage_end` | Continue until one named temperature stage ends. |
| `fill_temperature_stage` | Fill one named temperature stage. |
| `until_experiment_end` or `continuous` | Continue until the master completion policy ends the experiment. |
| `forever` | Continue until operator `Ctrl+C`; valid only as the last reachable action. |

For `duration`, the configured end policy is retained only as provenance for
older files. The runtime does not convert the duration to `NCycle`, and a
partial final cycle is therefore possible when Output 2 is disabled at the
deadline. Use `count` when the scientific requirement is an exact cycle count.

Temperature and Moku schedules otherwise advance independently. To couple
them, use an explicit named-stage condition. Examples:

- independent schedules:
  [`independent_temperature_and_moku.yaml`](../configs/examples/independent_temperature_and_moku.yaml);
- begin after a stage is stable:
  [`waveform_started_when_temperature_stable.yaml`](../configs/examples/waveform_started_when_temperature_stable.yaml);
- run until a stage ends:
  [`waveform_until_temperature_stage_end.yaml`](../configs/examples/waveform_until_temperature_stage_end.yaml);
- a duty-cycle parameter sweep:
  [`duty_cycle_parameter_sweep.yaml`](../configs/examples/duty_cycle_parameter_sweep.yaml); and
- exactly five 100 kHz, 10% cycles in 50 microseconds:
  [`moku_only_100khz_10percent_for_50us.yaml`](../configs/examples/moku_only_100khz_10percent_for_50us.yaml).

## 12. Pre-run apparatus checklist

Before using `--execute`, review the preview and verify all of the following:

### Computer and software

- The intended Conda environment is active.
- `python run_experiment.py ... --dry-run` and `--preview` both succeed.
- The exact YAML files about to be used are saved.
- The Moku, MeCom, and Linien client versions are the apparatus-tested ones.
- `LINIEN_PASSWORD` exists in the current terminal if `lock` is selected.
- Windows will not sleep or apply a disruptive update during the run.
- The destination drive has enough free space, especially for raw-only traces.

### Optical and electrical path

- The laser power, polarisation, fibre/free-space alignment, detector gain,
  attenuation, and photodiode range are recorded and suitable.
- Moku physical Input 1 receives the intended photodiode signal.
- Moku physical Output 2 reaches the intended drive path.
- Connector voltage and external gain produce a safe EOM voltage.
- Grounds, impedance/loading, and any amplifier limits have been checked.
- The configured trigger level produces one intended ChannelB crossing.

### Temperature system

- The correct COM port and channel are selected.
- Thermistor coefficients, sensor selection, TEC polarity, PID, current limit,
  voltage limit, and controller protections have been verified independently.
- Object and sink readings are plausible before control.
- Every target and completion action is safe for this thermal stack.
- Sensor plausibility bounds in YAML are meaningful, not copied blindly.

### Linien

- The configured host is correct and reachable.
- Linien's existing server/settings correspond to this optical setup.
- The EOM is already locked at the desired dark point in the GUI.
- The lock has been observed long enough to provide stable pre-loss history.
- Sweep/control channel, modulation, demodulation, offsets, polarity, PID, and
  output scaling have been checked.

## 13. Start a real configured run

Run:

```powershell
python run_experiment.py --config configs\my_first_experiment\experiment.yaml --execute
```

The program first validates and compiles the complete plan. It then creates a
unique directory under `runs\`, copies the source configurations, writes the
effective configuration, hashes the sources and LUTs, and saves previews.

If placeholders or missing TEC sensor bounds remain, execution stops before
hardware imports. Otherwise the final prompt is:

```text
Review the saved effective plan, connector voltages, routing, TEC limits, and
apparatus. Type EXECUTE to permit hardware connections, or CANCEL to stop:
```

Only uppercase `EXECUTE` proceeds. `--yes` does not bypass this configured-run
confirmation.

After confirmation, the master owns the overall run. It launches the Linien
logger when selected, connects and preflights selected hardware, starts the
independent schedules from a shared monotonic clock, writes data/events, and
updates `runtime_checkpoint.json` throughout the run.

The master ends when the configured `end_when` condition is met, a fatal error
occurs, or the operator presses `Ctrl+C`. Keep the terminal open so warnings
and status remain visible.

## 14. What to expect during a run

The runtime records three kinds of time:

- `timestamp_utc`: unambiguous timezone-aware wall time;
- `timestamp_local`: Europe/London time including daylight-saving offset; and
- `elapsed_s`: monotonic experiment time used for scheduling.

Normal output includes stage/action transitions and data messages. Warnings
should not be dismissed merely because data collection continues. In
particular:

- an optical trigger timeout means the Oscilloscope did not see the expected
  edge; it is not automatically loss of Moku ownership;
- a transport/ownership/watchdog failure can trigger Moku recovery;
- `Linien connection lost` starts attach/reconnect behavior; and
- missing or implausible TEC data is logged as an error rather than converted
  to zero.

Do not edit the active YAML or raw output files during the experiment. The run
uses its immutable saved snapshot and atomically updates structured outputs.

## 15. Linien disconnection and automatic relock

### Initial connection

At initial startup, the logger first tries to attach to an already-running
Linien server. If and only if Linien reports the specific
`ServerNotRunningException`, it retries with server autostart enabled. Other
connection failures are not treated as proof that starting a server is safe.

The logger must receive actual plot data before it reports ready to the master.
An initially unlocked apparatus is not automatically locked by this recovery
feature; establish the initial lock in the GUI.

### Mid-run connection loss

On a recoverable transport error, the logger:

1. flushes the CSV;
2. logs the outage in `linien_connection_events.jsonl`;
3. disconnects the stale client safely where possible;
4. retries indefinitely using 1, 2, 5, 10, 20, then 30 second backoffs;
5. always tries to attach first, and autostarts only on the specific
   missing-server response; and
6. checks whether the recovered server retained the lock.

If lock is still present, a fresh readback of the saved locking configuration
must match. Logging then continues with a real wall-clock gap in the CSV.

### Guarded relock

If the recovered server remains unlocked for 10 seconds, the logger permits at
most one relock attempt for that outage. It uses a robust reference from valid
locked samples in the 30 seconds before the loss, excluding the final 2
seconds, and requires at least 10 samples.

The starting value is the median **raw Red Pitaya FAST OUT voltage** from the
logged control signal after Linien's `/8192` conversion. It is not FPGA counts,
not an amplified EOM voltage, and not a value inferred from the Moku.

The logger restores and read-verifies the saved signal path, modulation,
demodulation, offsets, polarities, filters, PID parameters, lock checks, and
related normal-lock settings while unlocked. It writes the reference voltage
to `sweep_center` and invokes Linien's normal simple/manual PID lock—the
programmatic equivalent of positioning the GUI sweep near the previous dark
point and pressing the ordinary lock control.

The console and event log report one of:

```text
ATTEMPTING RELOCK AT ... V
RELOCK SUCCESSFUL AT ... V
RELOCK FAILED OR ABORTED ...
```

After acquisition and settling allowances, a 10-second validation window must
contain at least five valid locked samples, remain below 0.98 V magnitude, and
keep error RMS, robust error variation, and robust control variation within
three times their pre-loss baselines. A channel mismatch, inadequate history,
configuration/readback mismatch, second interruption, lost lock, output rail,
or quality failure ends the Linien logger with code 1. The master detects that
exit, stops the other components, and performs normal cleanup.

This is intentionally a conservative recovery, not a general replacement for
an operator using Linien.

## 16. Moku connection recovery

> **Current acquisition/recovery semantics:** every reduced frame validates its
> returned ChannelB trace and classifies the actual trigger candidate. It then
> measures a sustained per-frame ChannelA delay or applies the configured fixed
> calibrated delay before selecting guarded regions. Duration actions are continuous and their
> monotonic timers continue through outages. A duration that expires while
> disconnected is never re-enabled. Recovery is polled without blocking TEC or
> Linien work, retries indefinitely when `maximum_moku_outage_s` is null, and
> caps backoff at 30 seconds. Count mode is strict by default or may use the
> documented bounded-uncertainty chunk policy; interrupted chunks are never
> replayed.

The Moku runs in a separate worker process. The master can stop a worker whose
SDK call exceeds a hard deadline and can create a new session after a confirmed
transport, stale-connection, ownership, or watchdog failure.

During a configured recovery:

- Output 2 is initially disabled in the replacement session;
- Multi-Instrument routing and active settings are reapplied;
- continuous output restarts at the first LUT sample;
- timing and session identifiers mark the discontinuity;
- later acquired frames are independently validated before reduction;
- data from before and after the discontinuity are not averaged together; and
- recovery stops after a finite `maximum_moku_outage_s`, or retries indefinitely
  when that value is null.

Repeated expected trigger timeouts are reported and retried but are not, by
themselves, proof of lost Moku control. Five consecutive malformed frames or
two consecutive transient transport errors trigger replacement-session
recovery in the current runtime.

If a strict exact-count burst is interrupted, its physical cycle count is
unknown. The run aborts that action rather than replaying it. The optional
`bounded_uncertainty` policy instead allocates separately triggered chunks,
never replays an interrupted chunk, and records lower and upper delivery bounds.

## 17. Stop, interruption, and cleanup

For an operator-controlled run, press `Ctrl+C` once in the master terminal.
The program asks owned components to stop, applies the temperature schedule's
completion behavior when possible, closes sessions, writes a final checkpoint
and actual timeline, and updates the manifest.

After any abnormal stop, physically verify:

- Moku Output 2 state;
- TEC output/target state;
- Linien lock and Red Pitaya output state; and
- whether the optical/electrical apparatus is safe to leave unattended.

If cleanup reports `UNKNOWN` or cannot confirm an output state, do not assume
that output is off.

## 18. Resume an interrupted configured run

Resume is for a stopped Python experiment. It is different from automatic
in-process Moku or Linien reconnection.

Find the interrupted run's `experiment_manifest.json`, then validate it without
hardware:

```powershell
python run_experiment.py --resume "runs\20260813_143000_my_first_experiment\experiment_manifest.json"
```

This defaults to a configured resume dry run. It verifies the saved
configuration tree, effective configuration, hashes, LUTs, checkpoint, output
schemas, elapsed-time order, and absence of recorded live processes.

Save a hardware-free resume plan for inspection:

```powershell
python run_experiment.py --resume "runs\20260813_143000_my_first_experiment\experiment_manifest.json" --preview
```

Then, only after reviewing the current apparatus state:

```powershell
python run_experiment.py --resume "runs\20260813_143000_my_first_experiment\experiment_manifest.json" --execute
```

Type uppercase `RESUME` at the final prompt. The runtime trusts the immutable
configuration copies in the run directory, not edited source files in the
repository. It appends only after confirming that raw outputs and the
checkpoint agree. Unsafe mid-action states, hash changes, missing files,
schema differences, or outputs ahead of the checkpoint cause refusal.

Do not copy only `runtime_checkpoint.json`; the complete run directory is the
resume unit.

## 19. Run directory explained

A configured run resembles:

```text
runs/20260813_143000_my_first_experiment/
|-- experiment_manifest.json
|-- effective_experiment.yaml
|-- analysis_profile.json
|-- configuration_hashes.json
|-- lut_hashes.json
|-- waveform_program.json
|-- runtime_checkpoint.json
|-- experiment_events.jsonl
|-- waveform_timeline.csv
|-- waveform_timeline.png
|-- original_configs/
|-- reloadable_config/
|-- waveforms/
|   |-- <waveform>.npy
|   `-- <waveform>_preview.png
|-- moku/
|   |-- moku_samples.csv
|   |-- moku_sample_provenance.csv
|   |-- acquisition_events.jsonl
|   |-- raw_trace_index.csv          # raw-only or enabled reduced diagnostics
|   `-- raw_traces/*.npz             # raw-only or enabled reduced diagnostics
|-- temperature/
|   `-- tec_log.csv
|-- linien/
|   |-- linien_log.csv
|   `-- linien_connection_events.jsonl
`-- resume_plans/                    # created when resume is previewed/executed
```

Not every file exists in every run. Component folders appear only when that
component is selected. Raw trace files always appear for raw-only acquisition
and can also appear under an enabled reduced `raw_capture` policy.

Important files:

- `experiment_manifest.json`: run identity, status, timestamps, process IDs,
  Git provenance, and pointers to other artifacts;
- `effective_experiment.yaml`: the fully expanded configuration actually used;
- `configuration_hashes.json` and `lut_hashes.json`: digital fingerprints used
  to detect later modification;
- `runtime_checkpoint.json`: restart position, not a replacement for raw logs;
- `experiment_events.jsonl`: master state transitions, warnings, recovery, and
  cleanup events;
- `moku_samples.csv`: reduced photodiode measurements with explicit time;
- `moku_sample_provenance.csv`: matching temperature/waveform/session state for
  every reduced row, with the same stable sample ID and exact timestamp;
- `tec_log.csv`: targets, temperatures, current, voltage, stability, status,
  and explicit failed-read messages;
- `linien_log.csv`: historical-compatible wall time, scaled error signal, raw
  FAST OUT lock voltage, and raw photodiode monitor signal; and
- `waveform_timeline.csv/png`: planned at preview time and rewritten from
  actual runtime events at cleanup.

Treat the run directory as raw provenance. Do not overwrite or manually
"repair" its CSV files. Put derived or cleaned results in new files.

## 20. Analyse Moku reduced measurements

For a run with `moku_samples.csv`, use:

```powershell
python analyse_eom_csv.py "runs\RUN_NAME\moku\moku_samples.csv" --no-show
```

`--no-show` saves results without opening interactive plot windows. By default,
the analyser looks for the saved `analysis_profile.json` beside or above the
CSV, uses the acquisition event log when present, and exactly joins the adjacent
`moku_sample_provenance.csv`. It refuses missing, duplicated, reordered, or
time-shifted provenance rows instead of performing a nearest-time guess.

Every relevant time-series plot shows subtle solid temperature-stage boundaries
and visually distinct dotted action/waveform boundaries by default. These are
independent saved schedules: a waveform boundary never implies a temperature
change. Optional dashed session boundaries represent reconnect/resume changes
and are not mislabeled as actions; a coincident action and session change stays
present in both layers. One legend entry is used per boundary type;
labels alternate and are automatically thinned for long experiments while the
lines remain. Useful controls are:

```powershell
python analyse_eom_csv.py "runs\RUN_NAME\moku\moku_samples.csv" `
  --show-temperature-boundaries `
  --show-waveform-boundaries `
  --no-show-session-boundaries `
  --annotate-temperature-labels `
  --no-annotate-waveform-labels `
  --no-show
```

The cleaned export retains temperature-stage, action, waveform, session, and
combined regime columns. Rolling means restart at a regime boundary, allowing
downstream grouping without manually rejoining the sidecar or blending two
settings in one smoothing window.

Useful options are:

```powershell
python analyse_eom_csv.py "path\to\moku_samples.csv" `
  --analysis-profile "path\to\analysis_profile.json" `
  --events-path "path\to\acquisition_events.jsonl" `
  --analysis-dir "path\to\derived_analysis" `
  --output-dir "path\to\plots" `
  --no-show
```

The PowerShell backtick at the end of a line continues one command on the next
line. Do not put spaces after the backtick.

You can override individual filters for an explicitly documented reanalysis:

- `--dark-offset-v VALUE`;
- `--min-high-level-v VALUE` or `--no-high-level-filter`;
- `--max-minimum-v VALUE` or `--no-minimum-filter`; and
- `--min-sample-count COUNT`.

These overrides change analysis, not raw data. Record why an override is
scientifically justified. The analyser preserves the distinction between
minimum and high/offset level and marks incomplete/recovery intervals where
the event data support it.

Running without a CSV makes the script search for the newest compatible Moku
CSV under both the legacy `Experiment Results` tree and configured `runs` tree:

```powershell
python analyse_eom_csv.py --no-show
```

For reproducibility, supplying the exact CSV is preferable.

Raw-only `.npz` traces do not contain reduced `minimum` and `high_level`
columns and therefore need a separate, scientifically defined reduction before
this analyser can calculate those quantities.

### Linien and temperature plotting utilities

The retained Linien plotter can read the configured Linien CSV because that
CSV preserves the historical column names:

```powershell
python plot_control.py "runs\RUN_NAME\linien\linien_log.csv" --no-show
```

However, `plot_control.py` currently multiplies Red Pitaya FAST OUT voltage by
the source-code constant `AMPLIFIER_GAIN = 10.0` and labels the result as EOM
lock voltage. Use that plot only if the real external gain for the run is
indeed 10, or update the plotting method in a reviewable way while preserving
the raw CSV. The relock algorithm itself uses the unamplified FAST OUT value.

`plot_temp_log.py` accepts both historical TEC CSVs and configured
`temperature/tec_log.csv`, preserving timezone-aware UTC/local timestamps and
writing derived PNGs outside the raw log. `plot_all_temp.py` and
`plot_lab_temp.py` remain historical plotters for legacy files under
`Experiment Results`; do not assume they are configured-v2 analysis tools.

Configured runs automatically launch noninteractive plot snapshots at the
`run_settings.monitoring.plot_interval_*` interval and final plotting after
hardware cleanup. Set the plot interval to `null` to disable only live
snapshots, or set `final_plots: false` to disable the final batch. The manifest
records plot commands, log files, PIDs, exit codes, and errors. Moku plotting
includes the aggregate drift analysis and the newest saved raw trace.

`measure_dark_offset.py` is also a retained apparatus-specific hardware script.
It has hard-coded Moku address/frontend constants, imports the Moku SDK, and
connects when run; it has no dry-run interface. Do not launch it blindly. If it
is deliberately used for a blocked-detector calibration, first review its
address and ensure its input impedance, coupling, and range are identical to
the measurement whose `dark_offset_v` will use the result.

## 21. Worked experiment recipes

Use these as starting points, then copy and edit them rather than changing the
shared example in place:

| Experiment | Master configuration |
| --- | --- |
| Temperature sweep only | [`temperature_only_sweep.yaml`](../configs/examples/temperature_only_sweep.yaml) |
| Moku duty-cycle waveform | [`moku_only_duty_cycle.yaml`](../configs/examples/moku_only_duty_cycle.yaml) |
| Two pulses of different levels in one LUT | [`moku_only_two_pulse_sequence.yaml`](../configs/examples/moku_only_two_pulse_sequence.yaml) |
| Exact five-cycle burst | [`moku_only_100khz_10percent_for_50us.yaml`](../configs/examples/moku_only_100khz_10percent_for_50us.yaml) |
| Several duty cycles in sequence | [`duty_cycle_parameter_sweep.yaml`](../configs/examples/duty_cycle_parameter_sweep.yaml) |
| Continuous staircase until `Ctrl+C` | [`continuous_staircase.yaml`](../configs/examples/continuous_staircase.yaml) |
| Generated raised-cosine waveform | [`custom_python_waveform.yaml`](../configs/examples/custom_python_waveform.yaml) |
| Imported CSV waveform | [`imported_lut_waveform.yaml`](../configs/examples/imported_lut_waveform.yaml) |
| Pulse train saved as raw traces | [`pulse_train.yaml`](../configs/examples/pulse_train.yaml) |
| Temperature and Moku running independently | [`independent_temperature_and_moku.yaml`](../configs/examples/independent_temperature_and_moku.yaml) |
| Start waveform only after temperature stability | [`waveform_started_when_temperature_stable.yaml`](../configs/examples/waveform_started_when_temperature_stable.yaml) |
| Stop waveform at a temperature-stage boundary | [`waveform_until_temperature_stage_end.yaml`](../configs/examples/waveform_until_temperature_stage_end.yaml) |

For every recipe, the safe learning sequence is:

```powershell
python run_experiment.py --config configs\examples\EXAMPLE.yaml --dry-run
python run_experiment.py --config configs\examples\EXAMPLE.yaml --preview
```

The examples deliberately retain public placeholders. Previewing them is safe;
executing them is blocked until a private, verified run-settings copy replaces
those values.

## 22. Troubleshooting by symptom

| Symptom | Likely meaning and first action |
| --- | --- |
| `public placeholder` or missing sensor bounds | You requested real execution with example apparatus values. Fill a private verified run-settings file; continue using dry-run meanwhile. |
| YAML `unknown field`, duplicate key, or indentation error | The schema is strict. Compare the field with [Experiment configuration](experiment_configuration.md), use spaces, and change one section at a time. |
| Waveform will not compile | Requested timing may be impossible at the achieved LUT resolution, a segment may be invalid, or duration may contain a rejected partial cycle. Read the exact compiler error and inspect [Waveform modes](waveform_modes.md). |
| No `minimum`/`high_level` values | The waveform may be raw-only, measurement roles may be absent, the trigger crossing may be ambiguous, or windows may fall outside the timebase. Inspect the effective plan and waveform preview. |
| Repeated trigger timeouts | The expected ChannelB edge or photodiode trace is not arriving in the configured timebase. Inspect physical signals and trigger settings; do not assume a Moku network failure. |
| Moku connection recovery starts | The worker detected transport/ownership/stale API/watchdog failure, consecutive transport errors, or repeated malformed frames. Review `experiment_events.jsonl` and `moku/acquisition_events.jsonl`. |
| Linien says no server is running | Startup/recovery first attaches, then autostarts only after that specific response. If it still fails, verify the Red Pitaya, client/server compatibility, credentials, and host. |
| `RELOCK FAILED OR ABORTED` | The guarded recovery could not verify history, configuration, channels, lock state, output range, or post-lock quality. The run stops intentionally; use the JSONL event for the exact reason. |
| Resume is refused | A hash, checkpoint, output schema/order, saved file, safe boundary, or recorded process check failed. Preserve the directory and diagnose the stated mismatch; do not edit raw files to force it. |
| Cleanup state is `UNKNOWN` | Software lost the ability to confirm the physical output. Inspect the apparatus directly. |
| Plots seem to bridge a gap | Confirm the matching provenance sidecar was loaded. Rolling means split at recorded regimes, and optional session lines reveal reconnects; never interpolate across a long outage. |

The longer diagnostic guide is [Troubleshooting](troubleshooting.md).

## 23. Legacy compatibility commands

The following interface predates the YAML-configured v2 scheduler:

```powershell
python run_experiment.py full
python run_experiment.py temperature
python run_experiment.py temperature-lock
python run_experiment.py drift
python run_experiment.py temperature-log
python run_experiment.py --components lock moku
```

Legacy modes start root-level component scripts and write under
`Experiment Results\run_...` using historical folder/file names. They obtain
many experiment values from Python constants, use `START`/legacy `RESUME`
prompts, and have their own resume behavior. Do not expect a legacy manifest to
behave like a hash-verified configured manifest.

Preview a legacy plan without hardware:

```powershell
python run_experiment.py drift --dry-run
```

Standalone retained tools include:

```powershell
python pulse_control.py --mode traditional --dry-run
python pulse_control.py --mode custom --dry-run --save-preview pulse_previews
python tec_temperature_controller.py --dry-run
```

`collect_data.py` is the old fixed-pulse Moku collection path.
`linien_logger.py` is normally started by the master and continues until it is
stopped. Use these paths only when you understand their source-code settings
and output conventions. Never let two programs own the same Moku, TEC, or
Linien session simultaneously.

## 24. Quick command reference

This section lists the common paths. For every positional argument, option,
alias, default, conflict, and legacy hardware warning, use the full
[Command reference](command_reference.md).

```powershell
# Show the master interface
python run_experiment.py --help

# Validate, no artifacts and no hardware
python run_experiment.py --config configs\my_run\experiment.yaml --dry-run

# Validate and save previews, no hardware
python run_experiment.py --config configs\my_run\experiment.yaml --preview

# Prepare and request a real run; still requires typing EXECUTE
python run_experiment.py --config configs\my_run\experiment.yaml --execute

# Validate an interrupted configured run, no hardware
python run_experiment.py --resume "runs\RUN_NAME\experiment_manifest.json"

# Save a resume plan, no hardware
python run_experiment.py --resume "runs\RUN_NAME\experiment_manifest.json" --preview

# Request resume; still requires typing RESUME
python run_experiment.py --resume "runs\RUN_NAME\experiment_manifest.json" --execute

# Analyse one exact reduced Moku CSV without opening plot windows
python analyse_eom_csv.py "runs\RUN_NAME\moku\moku_samples.csv" --no-show

# Run the hardware-free automated tests
python -m pytest
```

## 25. Recommended learning path

1. Read sections 1 through 6 of this tutorial.
2. Dry-run and preview `configs/experiment.yaml`.
3. Dry-run two or three focused examples from section 21.
4. Copy one appropriate example into a private configuration folder.
5. Fill only apparatus values that have been independently verified.
6. Review [Experiment configuration](experiment_configuration.md),
   [Waveform modes](waveform_modes.md), and
   [Scheduling, recovery, and resumption](scheduling_and_resumption.md) for the
   features used in that run.
7. Complete the apparatus checklist with a second competent operator where
   laboratory practice requires it.
8. Perform a short, conservative real-hardware smoke test before an unattended
   multi-hour run.
9. Inspect the complete run directory and practise a hardware-free resume
   validation.
10. Analyse a copied or derived dataset while preserving the raw run.

The repository can validate internal consistency and preserve provenance. It
cannot decide whether a waveform, temperature, optical power, lock setting, or
completion action is physically safe or scientifically appropriate for an
apparatus it has not independently characterised.
