# Experiment configuration

Use this page as the field-by-field reference for the YAML configuration. If
you are setting up a run for the first time, begin with one of the complete
files in `configs/examples/`, run it with `--dry-run`, and then change one
section at a time.

YAML is a human-readable text format in which indentation groups related
settings. Spaces at the start of a line therefore matter. The examples use two
spaces for each indentation level and never use tab characters.

An experiment uses up to four YAML files. Each file has one clear job, so the
same setting cannot be silently overridden somewhere else.

```text
configs/
|-- experiment.yaml             # component selection and master completion
|-- run_settings.yaml           # timezone and recovery policy
|-- temperature_schedule.yaml   # temperature stages or generator
`-- pulse_schedule.yaml         # waveform definitions and Moku actions
```

Paths are resolved relative to the file containing the reference. For example,
`pulse_schedule_file: includes/pulses.yaml` in
`configs/examples/example.yaml` resolves to
`configs/examples/includes/pulses.yaml`, regardless of the PowerShell working
directory.

YAML mappings are strict. Duplicate keys, unknown fields, non-finite numeric
values, missing files, wrong file roles, and circular references are rejected.
YAML merge keys are not a configuration override mechanism.

In practical terms:

1. Choose the components in the master experiment file.
2. Put device and recovery settings in the run-settings file.
3. Add a temperature schedule only when temperature control is needed.
4. Add a pulse schedule only when Moku waveform generation is needed.
5. Run a dry run and read the expanded plan before considering real execution.

### Terms used in this guide

Another term used throughout this page is **waveform lookup table (LUT)**. A
LUT is the ordered list of requested voltage values for one complete waveform
cycle. The Moku moves through those values at the selected sample rate. Starting
the LUT from its first sample means starting that waveform cycle from the
beginning.

The **Moku software-control connection** is the communication link between the
Python program on the laboratory computer and the Moku instrument. A **Moku
session** is one period during which the program owns and controls that
instrument. If this connection fails, Python may no longer know what the Moku
physically output after the last confirmed command. This is separate from the
optical path, the waveform cable connected to Output 2, the TEC controller
connection, and the Linien connection.

**Output 2** is the physical Moku output connector selected for the requested
waveform by the current runtime. It is not the same as internal ChannelB,
which is only a routed reference signal inside the Moku Oscilloscope slot.

## Master experiment file

```yaml
name: example_experiment
components: [lock, moku, temp-control]
run_settings_file: run_settings.yaml
temperature_schedule_file: temperature_schedule.yaml
pulse_schedule_file: pulse_schedule.yaml
end_when: all_schedules_complete
```

| Field | Type | Required/default | Allowed value or meaning |
| --- | --- | --- | --- |
| `name` | non-empty string | required | Windows-safe experiment label |
| `components` | unique list of strings | required | `lock`, `moku`, `temp-control`, and existing supported `temp-log` combinations |
| `run_settings_file` | path string or null | optional, null | Run settings relative to this file |
| `temperature_schedule_file` | path string or null | optional, null | Temperature schedule relative to this file |
| `pulse_schedule_file` | path string or null | optional, null | Pulse schedule relative to this file |
| `end_when` | string or mapping | required | Master completion policy described below |

An active `temp-control` component requires a temperature schedule. A Moku
component may omit `pulse_schedule_file` only to select an explicitly supported
compatibility waveform path; new configurable waveform operation supplies the
pulse schedule. Temperature-only and Moku-only files are valid. A null schedule
is not an empty schedule. A temperature-linked waveform condition is invalid
unless `temp-control` and a temperature schedule are both active.

The scalar `end_when` values are:

| Value | Meaning |
| --- | --- |
| `all_schedules_complete` | Stop after every selected finite schedule completes. |
| `moku_schedule_complete` | Stop when the Moku schedule completes. |
| `temperature_schedule_complete` | Stop when the temperature schedule completes. |
| `operator_ctrl_c` | Continue until the operator interrupts the run. |

These four policies must be written directly as the scalar strings shown above.
Do not put them inside a `mode` mapping. For example, use:

```yaml
end_when: moku_schedule_complete
```

The mapping form of `end_when` is used only for a fixed master duration. Its
only supported `mode` value is `fixed_duration`, followed by exactly one
unit-bearing duration:

```yaml
end_when:
  mode: fixed_duration
  duration_hours: 2
```

The duration field is exactly one of `duration_s`, `duration_ms`,
`duration_minutes`, or `duration_hours`; it must be finite and positive. The
master timer starts after selected components report ready. Stopped time and
outage time configured to pause do not count.

## Run settings

```yaml
timezone: Europe/London
recovery:
  temperature_hold_during_moku_outage: pause_timer
  waveform_duration_during_outage: pause_timer
  continuous_waveform: restart_from_phase_zero
  finite_burst_interrupted: abort
  maximum_moku_outage_s: 1800
measurement:
  dark_offset_v: 0.0
  minimum_high_level_v: 0.6
  maximum_minimum_v: 0.6
  minimum_sample_count: 10

temperature:
  serial_port: COM_PORT
  channel: 1
  min_target_c: 10
  max_target_c: 50
  sampling_interval_s: 1.0
  object_temperature_min_c: null
  object_temperature_max_c: null
  sink_temperature_min_c: null
  sink_temperature_max_c: null

linien:
  host: RED_PITAYA_HOSTNAME_OR_IP

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
```

This is a complete structural example, not a hardware-ready configuration.
`COM_PORT`, `RED_PITAYA_HOSTNAME_OR_IP`, and `MokuGo-XXXXXX` are public
placeholders. The four null temperature-sensor bounds are deliberately unknown.
These values allow dry-run and preview, but the execution gate rejects them
before importing a hardware module.

| Field | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `timezone` | string | optional, `Europe/London` | Experiment local timezone; the current runtime accepts `Europe/London` |
| `recovery` | mapping | optional, safe defaults shown above | Moku-outage policy |
| `measurement` | mapping | optional; historical defaults `0.0`, `0.6`, `0.6`, and `10` | Detector offset, optional plausibility thresholds, and post-filter sample requirement |
| `temperature` | mapping or null | required with `temp-control` or `temp-log` | Verified TEC connection/channel and schedule target bounds |
| `linien` | mapping or null | required before executing `lock` | Explicit Linien/Red Pitaya host; public placeholders remain valid for dry-run |
| `moku` | mapping or null | required with `moku` | Computer-to-Moku control address, instrument layout, physical connectors, input settings, trigger, and acquisition settings |

### What the recovery settings control

A **continuous waveform** repeats until its schedule tells it to stop. A
**finite burst** is a request for an exact number of waveform cycles. A **Moku
outage** is a period during which the Python program cannot obtain valid data or
reliable replies from the Moku through the software-control connection.

The recovery settings decide whether schedule timers pause during that outage
and whether a continuous waveform is allowed to start again afterward. They do
not change the TEC target during the outage.

If communication with the Moku is lost, the old software session can no longer
be trusted. The program creates a replacement session and keeps Output 2 off
while it restores the instruments, routing, Oscilloscope settings, LUT, and
repeat settings. Restoring these settings does **not** mean that waveform cycles
are sent again. The `continuous_waveform` choice controls what happens only
after this setup is complete and while Output 2 is still off.

| Field | Value/default | Meaning |
| --- | --- | --- |
| `temperature_hold_during_moku_outage` | `pause_timer` (default) or `continue_timer` | Choose whether the temperature stage's elapsed-time counter pauses while Moku data are unavailable. The TEC continues holding its current target either way. |
| `waveform_duration_during_outage` | `pause_timer` (default) or `continue_timer` | Choose whether time without valid Moku data counts toward the requested running time of a duration-based waveform action. |
| `continuous_waveform` | `restart_from_phase_zero` (default) or `abort` | `restart_from_phase_zero` turns Output 2 back on and starts the LUT again from its first sample. `abort` leaves Output 2 disabled and ends the action without restarting it. |
| `finite_burst_interrupted` | `abort` only | Stop if the Python program loses its software-control connection to the Moku while a finite burst is running. The program cannot then know how many cycles physically reached Output 2, so it records the count as unknown and never triggers that burst again automatically. |
| one `maximum_moku_outage_*` | positive duration or null, default 1800 s | Limit how long reconnection attempts may continue; explicit null means no time limit. Supported suffixes are `_s`, `_ms`, `_us`, `_ns`, `_minutes`, and `_hours`. |

Starting a continuous LUT again from its first sample breaks its timing
relationship with the waveform that existed before the disconnection. The
program records this break and starts a new waveform session so measurements
from the two sides are not averaged together.

When `continuous_waveform: abort` is selected and the Python program loses its
software-control connection to the Moku, it chooses the abort path before it
reaches any command that can start the waveform. It attempts to establish a new
Moku session with Output 2 disabled, restores the configuration while the
output remains off, and then ends the action. It never turns Output 2 back on.
If the Moku still does not reply, the program cannot confirm the physical state
of Output 2. It reports that state as unknown and still does not try to restart
the waveform.

### Measurement analysis settings

The `measurement` mapping is strict when supplied and is always written into
the fully expanded configuration saved with the run. For backward
compatibility, omitting the mapping or one of its fields uses the historical
defaults shown below:

| Field | Type/default | Meaning |
| --- | --- | --- |
| `dark_offset_v` | finite number, `0.0` | Independently determined detector dark offset in measured input volts |
| `minimum_high_level_v` | finite number or null, `0.6` | Optional historical plausibility filter: require the measured high/offset level to be greater than this value; null disables only this threshold |
| `maximum_minimum_v` | finite number or null, `0.6` | Optional historical plausibility filter: require the measured minimum level to be less than this value; null disables only this threshold |
| `minimum_sample_count` | positive integer, `10` | Minimum complete samples remaining after all enabled filters |

Every published configuration states the historical illustrative values
`0.0`, `0.6`, `0.6`, and `10` explicitly; they are not universal detector
limits or calibration data.
Setting either nullable threshold to null disables that one plausibility gate,
but never disables the scientific-domain requirement
`high_level_voltage > minimum_voltage > dark_offset_v`. That ordering is still
required for finite, meaningful dark-corrected extinction metrics.

### Temperature controller settings

The `temperature` mapping is apparatus configuration and has no inferred
defaults:

| Field | Type | Meaning |
| --- | --- | --- |
| `serial_port` | non-empty string | Verified local serial port; public examples use `COM_PORT` |
| `channel` | positive integer | Controller channel |
| `min_target_c` | finite number | Lowest schedule target permitted by this run |
| `max_target_c` | finite number greater than `min_target_c` | Highest schedule target permitted by this run |
| `safe_target_c` | finite number within the configured target bounds | Optional unless the temperature schedule selects `return_to_safe_target`, when it is required |
| `sampling_interval_s` | positive finite seconds, default `1.0` | TEC sampling interval when there is no active temperature stage, including passive `temp-log` runs |
| `object_temperature_min_c` | finite number or null, default null | Apparatus-verified lower plausibility bound for the object sensor |
| `object_temperature_max_c` | finite number or null, default null | Apparatus-verified upper plausibility bound for the object sensor |
| `sink_temperature_min_c` | finite number or null, default null | Apparatus-verified lower plausibility bound for the heatsink sensor |
| `sink_temperature_max_c` | finite number or null, default null | Apparatus-verified upper plausibility bound for the heatsink sensor |

The bounds are software validation gates, not recommended hardware limits. They
do not alter controller protection settings. The public 10 to 50 degree example
is deliberately labelled illustrative and must be replaced or explicitly
verified before real execution. `safe_target_c` is an apparatus-reviewed target,
not a universally safe default. The loader rejects `return_to_safe_target`
unless this field is present and lies between `min_target_c` and
`max_target_c`. The four sensor plausibility bounds must either all be finite
with each minimum below its corresponding maximum, or all be null/omitted.
Public files deliberately use null because verified bounds cannot be inferred:
this remains valid for dry-run and preview, but blocks `--execute` until all
four apparatus-specific bounds are supplied and reviewed.

An active temperature-control stage uses the sampling interval expanded from
its temperature schedule. The run-settings `sampling_interval_s` value applies
when no stage is active, such as a passive `temp-log` experiment.

### Linien settings

The optional `linien` mapping has one field:

| Field | Type | Meaning |
| --- | --- | --- |
| `host` | non-empty string | Explicit Linien/Red Pitaya hostname or IP address used by the retained logger |

The `lock` component requires `linien.host`. Public examples use
`RED_PITAYA_HOSTNAME_OR_IP`; that placeholder is accepted for dry-run and
preview but blocks `--execute` before any hardware module is imported.

### Moku settings

The `moku` mapping contains every setting needed to build the Moku session at
the start of a run or build a replacement after the computer loses its
software-control connection to the Moku. Except for nullable
`fallback_address`, each listed field is required when the Moku component is
active.

In this configuration, **Multi-Instrument Mode (MIM)** divides one Moku into an
**Arbitrary Waveform Generator (AWG)** slot and an **Oscilloscope** slot. The AWG
creates the waveform requested at physical Output 2. The Oscilloscope records
the photodiode through physical Input 1. Inside the Oscilloscope slot,
`ChannelA` is the routed photodiode signal and `ChannelB` is an internal copy of
the generated waveform used as the timing reference. ChannelA and ChannelB are
internal signal names, not physical connectors.

An **ADC** (analogue-to-digital converter) turns the voltage at a physical input
into numerical samples for the Oscilloscope. A **DAC** (digital-to-analogue
converter) turns numerical waveform samples into voltage at a physical output.
The **frontend** settings describe how the physical input is electrically
configured before conversion, including impedance, AC/DC coupling, and
attenuation. The **timebase** is the span of time shown in each Oscilloscope
trace relative to its trigger at `t = 0`.

| Field | Type | Meaning |
| --- | --- | --- |
| `address` | non-empty string | Hostname or IP address used by the computer to contact the Moku; public examples use `MokuGo-XXXXXX` |
| `fallback_address` | string or null, default null | Second verified control address tried only if the primary address fails; never discovered or guessed |
| `force_connect` | boolean | Whether the Moku Python SDK may take software ownership away from an older session; use only after checking that no other program should own the device |
| `platform_id` | integer | Moku SDK identifier for the device family; the current runtime supports only the verified Moku:Go value `2` |
| `awg_slot`, `oscilloscope_slot` | integer | Locations of the two virtual instruments inside MIM; the current runtime requires AWG slot 1 and Oscilloscope slot 2 |
| `output_channel` | integer | Physical Moku output connector used by this configuration; the current runtime requires Output 2 |
| `input_channel` | integer | Physical Moku input connector receiving the photodiode signal; the current runtime requires Input 1 |
| `frontend_impedance` | string | Input electrical impedance; the current runtime requires `1MOhm` |
| `frontend_coupling` | string | `DC` retains the signal's DC level; `AC` removes it according to the instrument's verified behavior |
| `frontend_attenuation` | string | Moku input-attenuation setting; supported values are `0dB` and `14dB` and must be reviewed for the connected signal |
| `trigger_source` | string | Signal the Oscilloscope watches to decide when a trace starts; configured examples use internal `ChannelB` |
| `trigger_level_v` | finite number | Voltage that the trigger source must cross |
| `trigger_edge` | string | Direction of that crossing, such as `Rising` |
| `trigger_mode` | string | Moku Oscilloscope trigger mode; the current runtime accepts `Normal` or `Auto` |
| `trigger_type` | string | Kind of trigger; the current runtime supports only a voltage-threshold edge trigger, written `Edge` |
| `timebase_start_s`, `timebase_end_s` | finite seconds | Start and end of each recorded trace relative to the trigger at `t = 0` |
| `timebase_max_length` | positive integer | Maximum number of points requested in one returned Oscilloscope trace |
| `sample_period_s` | positive seconds | Requested time between saved reduced measurement rows |
| `frames_per_sample` | positive integer | Number of valid Oscilloscope traces averaged to make one saved reduced measurement row |

Every compiled non-raw measurement window must lie inside the configured
Oscilloscope timebase. The shared examples use -50 microseconds to 1.5
milliseconds so their longest published window, ending at 1.3 milliseconds,
is observable. A narrower experiment-specific timebase is valid only when all
of that experiment's windows still fit.

These values describe commands requested through the Moku Python SDK. Exact
platform, slot, route, and trigger behavior remains subject to the official
vendor API (the set of supported control commands) and the real-hardware
smoke-test boundary. A placeholder address or unverified selection must prevent
`--execute` even though it can be used for dry-run validation.
`ChannelA` may be selected deliberately when triggering from the routed physical
photodiode input, but it is a legacy/measurement-trigger option rather than the
configured MIM default.

## Temperature schedule

Every temperature schedule starts with these common fields:

| Field | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `name` | non-empty string | required | Schedule identifier |
| `type` | string | required | `explicit`, `sweep`, `targets`, or `range` |
| `stability` | mapping | optional, `required: true`, zero stable duration, 1800 s timeout | Schedule-level stability behavior |
| one `sampling_interval_*` | positive duration | optional, 5 s | TEC logging/sample interval |
| `completion_behavior` | enum string | required | `disable_output`, `hold_current_target`, `return_to_safe_target`, or `revert_to_stored_target` |

Duration suffixes are `_s`, `_ms`, `_us`, `_ns`, `_minutes`, and `_hours`
where the field is supported. Provide at most one representation for each
conceptual duration.

### Stability

```yaml
stability:
  required: true
  stable_duration_s: 300
  timeout_s: 1800
```

| Field | Type | Meaning |
| --- | --- | --- |
| `required` | boolean, default true | Whether the hold waits for stability |
| `tolerance_c` | positive number or null, default null | Explicit controller tolerance where configured; never inferred |
| one `stable_duration_*` | non-negative duration, default 0 s | Continuous time required within tolerance |
| one `timeout_*` | positive duration, default 1800 s | Maximum settling time before the timeout policy applies |

When stability is required, a stage hold begins only after the reading has
remained stable for the required duration. It does not begin when the target is
written. A stage may supply a complete `stability` mapping to override the
schedule-level value.

Temperature tolerance and safe target limits are apparatus configuration, not
values inferred from this schedule. Validation must have those verified values
before a real target write.

### Explicit stages

```yaml
name: two_stages
type: explicit
completion_behavior: hold_current_target
stability:
  required: false
stages:
  - name: baseline
    target_c: 25
    hold_duration_minutes: 10
    notes: Illustrative target only.
```

Each stage allows only:

| Field | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `name` | non-empty string | optional, deterministic `stage_NNN` | Unique event-reference name |
| `target_c` | finite number or explicit null | required | Target in degrees Celsius; null is an explicit TEC-output-off stage |
| one `hold_duration_*` | positive duration | required | Hold after stability, or immediately when stability is not required |
| `stability` | mapping | optional | Complete stage override |
| one `sampling_interval_*` | positive duration | optional | Stage logging interval override |
| `notes` | string | optional, empty | Operator/scientific note |
| `completion_behavior` | enum string | optional, `advance` | `advance` or `stop_schedule` |

After a stage has met its stability requirement and completed its hold,
`advance` starts the next stage. `stop_schedule` ends the temperature schedule
at that point, so any later stages are skipped. When the schedule ends, the
schedule-level `completion_behavior` still determines what happens to the TEC
(for example, holding the current target or disabling output).

Target null is not the same as stopping Python. It requests the explicitly
documented TEC-output-off behavior and therefore remains hardware-gated.

### Generated sweep

The sweep form preserves the current gradual-sweep experiment:

```yaml
name: sweep_25_to_30
type: sweep
completion_behavior: hold_current_target
initial_tec_off_hold_minutes: 360
start_c: 25
finish_c: 30
measurement_interval_c: 5
transition_increment_c: 2.5
transition_hold_minutes: 10
measurement_hold_minutes: 360
reverse: true
cycles: 1
stability:
  required: true
  stable_duration_s: 300
  timeout_s: 1800
```

`start_c`, `finish_c`, `measurement_interval_c`, and
`transition_increment_c` are finite numbers; the two increments are positive.
`transition_hold_*` and `measurement_hold_*` are positive durations.
`initial_tec_off_hold_*` is optional and positive when present. `reverse` is a boolean,
default false. `cycles` is a positive integer, default 1. Generated stages use
unique deterministic names so event references and resume positions are stable.

### Target list and generated range

`type: targets` uses a non-empty finite `targets_c` list and one
`hold_duration_*`. `type: range` uses finite `start_c`, `finish_c`, and non-zero
`step_c`, plus one `hold_duration_*`. Both allow `reverse` and `cycles` with the
same meanings as a sweep. Expansion is completed and validated before hardware
access; the finish target is included exactly and is not duplicated at a
reverse turn.

## Pulse schedule

```yaml
waveforms:
  pulse_name:
    type: square
    low_level_v: 0.0
    high_level_v: 1.0
    frequency_hz: 100000
    duty_cycle_percent: 10
moku_schedule:
  - name: five_cycles
    waveform: pulse_name
    start: immediately
    run:
      mode: count
      count: 5
```

| Field | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `waveforms` | name-to-mapping object | required with Moku | Definitions compiled before execution |
| `moku_schedule` | list of action mappings | required with Moku | Independent Moku timeline |

An action allows `name` (optional unique string), required `waveform`, optional
`start` (default `immediately` for the first action and after-previous semantics
where validated), and required `run`. Waveform bodies and run modes are fully
documented in [Waveform modes](waveform_modes.md).

Start may be a scalar mode or a mapping with `mode` and the mode-specific
field:

| Mode | Additional field |
| --- | --- |
| `immediately` | none |
| `elapsed_experiment_time` | exactly one `elapsed_s`, `elapsed_ms`, `elapsed_us`, `elapsed_ns`, `elapsed_minutes`, or `elapsed_hours` |
| `after_previous_waveform_action` | none |
| `temperature_stage_started` | `temperature_stage` |
| `temperature_became_stable` | `temperature_stage` |
| `temperature_stage_completed` | `temperature_stage` |
| `named_event` | `event` |

Stage/event names must resolve after temperature expansion. A `forever` action
must be the final reachable action.

## Validation and snapshots

Use:

```powershell
python run_experiment.py --config configs/experiment.yaml --dry-run
python run_experiment.py --config configs/experiment.yaml --preview
```

Validation loads the entire graph before any component starts. The run snapshot
contains exact source bytes, a fully expanded `effective_experiment.yaml`,
configuration hashes, compiled LUT hashes, and imported assets. Hashes use the
copied snapshot during resume. A changed source file outside the run cannot
silently change the resumed experiment.

A **snapshot** is the private copy of the input files saved inside the run
directory. A **hash** is a digital fingerprint: changing even one source or LUT
value changes that fingerprint. Resume checks these fingerprints so it cannot
silently combine old experimental data with edited settings.

Example master files are under `configs/examples/`; automated tests load and
compile every one so the examples cannot drift from the parser.
