# Waveform modes

This page explains how to describe the electrical waveform requested at the
Moku connector. Choose the simplest waveform type that represents the
experiment:

| Need | Waveform type |
| --- | --- |
| One repeating high/low pulse | `square` |
| A hand-written list of levels, pulses, and gaps | `segments` |
| Several identical pulses followed by a final gap | `pulse_train` |
| A sequence of voltage steps | `staircase` |
| A reviewed waveform-generating Python function | `custom_python` |
| Voltage samples from a file | `csv_lut` |

Waveforms are named entries in the `waveforms` section of a pulse-schedule
file. Schedule actions use the name. An action cannot quietly override part of
the waveform definition.

Every waveform body has required `type`. Its name normally comes from the YAML
mapping key. An optional internal `name` field is accepted only when it exactly
matches that key, so omitting the redundant field is clearer. Every waveform
type also accepts optional `measurement_windows`; the detailed window fields
are documented below. Compiled waveform, action, segment, role, and window names
start with a letter or number and then use only letters, numbers, underscores,
or hyphens.

The compiler converts each waveform into a **lookup table (LUT)**: an ordered
list of requested connector-voltage values for one complete waveform cycle.
The Moku holds each value for one point interval, moves through the list at the
selected sample rate, and repeats the list when the action requires another
cycle. LUT timing is programmed timing; it is not a measurement of the analogue
voltage that physically reaches the EOM.

All voltage fields ending in `_v` are requested Moku connector volts. They do
not imply the voltage delivered to the EOM. All time-like values use an
explicit unit-bearing field name. For each conceptual duration, provide exactly
one of `_s`, `_ms`, `_us`, or `_ns`; values are normalised to seconds before
compilation.

The dry-run report separates requested geometry from achieved, quantised
geometry. It reports LUT size, selected sample rate, point interval, frequency,
each achieved segment duration, and the largest timing error. A preview shows
the achieved digital programme, not a measured analogue waveform.

Always check both requested and achieved values. A waveform can be valid while
still being quantised slightly differently from the requested timing.

Here, **quantised** means rounded to timing steps that the selected Moku sample
rate can represent. The **point interval** is the time spent on one LUT value.
The **sample rate** is the number of LUT values processed per second.

## Square or traditional pulse

`type: square` represents one high interval followed by one low interval per
cycle.

| Field | Required/default | Meaning |
| --- | --- | --- |
| `low_level_v` | Required | Requested connector voltage during the low/gap interval. |
| `high_level_v` | Required | Requested connector voltage during the pulse. It must be greater than `low_level_v`. |
| timing fields | Required | Exactly one complete timing form from the next table. |
| one `edge_time_*` | Optional; 0 s | Programmed rise and fall time. The high duration must exceed twice this value so a high plateau remains. |
| `measurement_roles` | Optional | Mapping with optional `low` and `high` role names. Defaults are `minimum` and `high_level`. |
| `measurement_windows` | Optional | Explicit measurement windows; when present, they replace automatic segment windows. |

Set explicit role names when the scientific meaning differs. In particular,
do not call an offset measurement the true maximum. A role value may be YAML
`null` to suppress that automatic low or high role; suppress both and omit
explicit windows only when the effective acquisition plan is intentionally
raw-only.

Choose exactly one timing parameterization:

| Parameterization | Required fields |
| --- | --- |
| Frequency and duty | `frequency_hz`, `duty_cycle_percent` |
| Period and high interval | one `period_*`, one `pulse_duration_*` |
| High and low intervals | one `pulse_duration_*`, one `gap_duration_*` |

Fields from different rows must not be mixed. Duty cycle is a percentage in the
open interval from 0 to 100; it is the percentage of each cycle spent at the
high level. An edge time is the programmed time used to move between the low
and high levels. Durations, period, and frequency must be finite and positive,
and the achieved high and low portions must each be representable.

```yaml
waveforms:
  square_100khz:
    type: square
    low_level_v: 0.0
    high_level_v: 5.0
    frequency_hz: 100000
    duty_cycle_percent: 10
    edge_time_ns: 100
```

This requests a 10 microsecond period, a 1 microsecond high interval, and a
9 microsecond gap. Waveform geometry does not determine how long a schedule
action runs.

## Arbitrary segment sequence

`type: segments` compiles an ordered `segments` list.

| Waveform field | Required/default | Meaning |
| --- | --- | --- |
| `low_level_v` | Required | Connector voltage used by every `gap` and by pulse ramps. |
| `segments` | Required | Non-empty ordered list described below. |
| `measurement_windows` | Optional | Explicit measurement windows replacing automatic segment windows. |

A segment has these fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `type` | `pulse`, `gap`, or `level` | Segment kind. |
| `name` | string, optional | Stable name used in reports. |
| `level_v` | number | Required for `pulse` and `level`; forbidden for a gap. |
| one `duration_*` | positive number | Requested segment duration. |
| one `edge_time_*` | non-negative number, optional | Programmed ramp time for `pulse` or `level`; forbidden for a gap. A pulse needs room for two edges, while a level needs room for its starting edge. |
| `measurement_role` | string, optional | For example `minimum` or `high_level`. |

```yaml
waveforms:
  two_pulse_cycle:
    type: segments
    low_level_v: 0.0
    segments:
      - type: pulse
        name: first_pulse
        level_v: 5.0
        duration_us: 20
        edge_time_ns: 100
      - type: gap
        name: middle_gap
        duration_us: 80
        measurement_role: minimum
      - type: pulse
        name: second_pulse
        level_v: 3.0
        duration_us: 40
        edge_time_ns: 100
        measurement_role: high_level
      - type: gap
        name: final_gap
        duration_us: 60
```

Repetition begins immediately after the last supplied segment. The compiler
does not invent a trailing gap. A `pulse` rises from and returns to
`low_level_v`. A `level` ramps from the previous segment's terminal voltage and
remains at its new value. A `gap` holds `low_level_v`.

## Generated pulse train

`type: pulse_train` expands a compact definition into pulse and gap segments.

| Field | Type | Requirement |
| --- | --- | --- |
| `pulse_count` | integer | At least one. |
| `low_level_v`, `high_level_v` | number | Required connector levels; high must be greater than low. |
| one `pulse_duration_*` | positive number | Duration of every pulse. |
| one `inter_pulse_gap_*` | non-negative number | Gap between pulses; no inter-pulse gap follows the final pulse. Zero places pulses next to one another. |
| one `final_gap_*` | Optional; 0 s | Low interval after the final pulse. It may be zero. |
| one `edge_time_*` | Optional; 0 s | Programmed rise and fall for each pulse. Pulse duration must exceed twice this value. |
| `measurement_roles` | Optional | Optional `low` and `high` role names; defaults are `minimum` and `high_level`. |
| `measurement_windows` | Optional | Explicit windows replacing automatic segment windows. |

The optional `measurement_roles: {low: minimum, high: high_level}` mapping has
the same defaults and meaning as square mode.

```yaml
waveforms:
  five_pulse_train:
    type: pulse_train
    pulse_count: 5
    low_level_v: 0.0
    high_level_v: 5.0
    pulse_duration_us: 10
    inter_pulse_gap_us: 5
    final_gap_us: 250
    edge_time_ns: 100
```

## Generated staircase

`type: staircase` creates one repeating cycle from a sequence of voltage
steps. It is useful for transfer-curve sampling and other experiments where
each requested level needs the same amount of time.

### Every staircase field

| Field | Required/default | Meaning |
| --- | --- | --- |
| `type` | Required | Must be `staircase`. |
| `levels_v` | One level form is required | Explicit list of at least two requested connector voltages. Do not combine it with `start_v`, `finish_v`, or `step_v`. |
| `start_v` | Required in generated form | First generated voltage before `direction` is applied. |
| `finish_v` | Required in generated form | Last generated voltage. It must be reached by a whole number of `step_v` steps. |
| `step_v` | Required in generated form | Non-zero signed increment. Its sign must move from `start_v` toward `finish_v`: positive when finish is higher, negative when finish is lower. |
| one `dwell_*` | Required | Positive total programmed time assigned to each step. See [What dwell means](#what-dwell-means). |
| `direction` | Optional; `up` | `up` uses the supplied/generated order, `down` reverses it, and `up_and_down` appends a return path. The names describe list traversal; they do not sort the voltages. |
| `repeat_endpoints` | Optional; `false` | In `up_and_down`, controls whether the turn and return endpoint receive an extra dwell. It must be a boolean. It has no sequence effect for `up` or `down`. |
| one `edge_time_*` | Optional; 0 s | Programmed ramp at the start of every level step. It must be non-negative and shorter than `dwell_*`. |
| one `final_gap_*` | Optional; 0 s | Low-level interval added once after all staircase steps in one LUT cycle. It may be zero. |
| `low_level_v` | Optional; 0 V | Connector voltage used during `final_gap_*`. It does not replace the first staircase level. |
| `measurement_windows` | Optional | Explicit windows used instead of the automatically generated per-level roles. See [Measurement roles and windows](#measurement-roles-and-windows). |

Staircase duration fields accept `_s`, `_ms`, `_us`, or `_ns`. Supply exactly
one unit form for each duration.

### What dwell means

`dwell_*` is the **total requested time from the start of one step to the start
of the next step**. It is not extra time added after an edge.

For every step, the requested timing is:

```text
step begins                                             next step begins
|                                                                    |
|<--------------------------- dwell ------------------------------->|
|<------ edge_time ------>|<------- constant-level plateau -------->|
```

So, before Moku timing quantisation:

```text
constant plateau per step = dwell - edge_time
```

Example: `dwell_us: 100` with `edge_time_us: 10` requests 10 microseconds to
move from the preceding level and about 90 microseconds at the new level. It
does **not** request 110 microseconds. The achieved edge and plateau can differ
slightly because both must use whole LUT points; the preview reports the exact
achieved values.

Every generated staircase level receives a measurement role such as
`level_0`, `level_1`, and so on. Its automatic measurement window excludes the
achieved edge at the start and uses the remaining constant-level plateau. The
window must still satisfy acquisition delay and minimum-point rules before
reduced acquisition is allowed.

The first step also has an edge interval. Its transition begins from
`low_level_v`, which defaults to 0 V. At the end of a cycle, the next cycle
begins immediately unless `final_gap_*` was supplied. This makes the LUT
boundary part of the waveform and worth checking in preview.

### Direction and endpoint examples

For base levels `[0, 0.5, 1.0]`:

| Settings | One compiled cycle |
| --- | --- |
| `direction: up` | `0, 0.5, 1.0` |
| `direction: down` | `1.0, 0.5, 0` |
| `direction: up_and_down`, `repeat_endpoints: false` | `0, 0.5, 1.0, 0.5` |
| `direction: up_and_down`, `repeat_endpoints: true` | `0, 0.5, 1.0, 1.0, 0.5, 0` |

With `repeat_endpoints: false`, neither endpoint is given two adjacent dwell
intervals: the finish is not repeated at the turn, and the return path stops
before the start because the next LUT cycle begins at that start value. With
`repeat_endpoints: true`, both are present twice—once explicitly in the return
path and once at the adjoining turn or cycle boundary.

### Worked staircase

```yaml
waveforms:
  voltage_staircase:
    type: staircase
    start_v: 0.0
    finish_v: 5.0
    step_v: 0.5
    dwell_us: 100
    direction: up_and_down
    repeat_endpoints: false
    final_gap_us: 500
```

The generated outward list has 11 values: `0.0, 0.5, ... 5.0`. The return path
adds 9 values: `4.5, 4.0, ... 0.5`. Therefore one LUT cycle contains:

```text
20 staircase dwells x 100 us + one 500 us final gap = 2.5 ms
```

The requested cycle frequency is therefore 400 Hz. Dry-run may show slightly
different achieved step boundaries after LUT quantisation, so the preview is
the source of truth for exact programmed timing.

An explicit-list version is:

```yaml
waveforms:
  selected_levels:
    type: staircase
    levels_v: [0.0, 0.2, 0.8, 1.0]
    dwell_ms: 5
    direction: down
    edge_time_us: 50
    final_gap_ms: 1
    low_level_v: 0.0
```

Its traversal is `1.0, 0.8, 0.2, 0.0`, each for a total requested 5 ms, then
1 ms at 0 V. `direction: down` reverses the supplied list; it does not check
whether the numeric values were originally ascending.

Each achieved level occupies LUT points and therefore has deterministic timing
within one LUT cycle. This describes the programmed connector waveform only.
It does not claim an instantaneous analogue transition or prove the voltage at
the EOM after cables, loading, amplification, or bandwidth limits.

## Registered Python function

`type: custom_python` refers to a function name in the application's explicit
waveform registry and passes a `parameters` mapping. Provide exactly one
`frequency_hz` or one `period_*`; `point_count` is optional. YAML cannot contain
Python expressions, module paths to import, or code to evaluate.

| Field | Required/default | Meaning |
| --- | --- | --- |
| `function` | Required | Exact name of a function already registered and reviewed in repository code. |
| `parameters` | Function-specific | Mapping validated by that function. Unknown parameters are rejected. |
| `frequency_hz` | Choose frequency or period | Positive requested cycle frequency. |
| one `period_*` | Choose frequency or period | Positive requested cycle duration. Do not combine it with `frequency_hz`. |
| `point_count` | Optional; compiler-selected | Integer of at least two. Fixes LUT point count when the chosen Moku memory mode can represent it. |
| `measurement_windows` | Optional | Explicit windows for reduced measurement. Custom functions do not create semantic roles automatically. |

```yaml
waveforms:
  smooth_custom:
    type: custom_python
    function: raised_cosine
    period_us: 20
    parameters:
      low_level_v: 0.0
      high_level_v: 1.0
```

A registered function receives a validated phase/time array and validated
parameters and returns requested connector volts. Compilation rejects an
unknown function, non-finite output, wrong output shape, out-of-range voltage,
unachievable point spacing, invalid frequency/period, or an oversized LUT.
Optional `point_count` is an integer of at least two; when omitted, the compiler
selects the finest safe point count and Moku memory mode for the requested
period.

Registration is code review, not a YAML override. A function needed by another
laboratory must be included and tested in the repository.

The built-in `raised_cosine` registry entry accepts exactly
`low_level_v` and `high_level_v` in `parameters` and generates one
low-to-high-to-low cosine cycle over phase `[0, 1)`.

A registered function is evaluated **once during hardware-free compilation**.
The resulting finite array is saved and uploaded as one repeating LUT. If a
function uses a random-number generator, the result is one random-looking
realisation repeated identically on every cycle; it is not fresh randomness per
cycle. Save and document the seed inside the reviewed function/parameters when
reproducibility matters. Continuously stochastic per-cycle waveform generation
is not supported by this LUT scheduler. The compiled `.npy` file and its hash
are the authoritative realisation used by the run.

There is no separate `ramp` or `triangle` type. Build a ramp from `level`
segments or a fine staircase, and build an up/down triangle with
`direction: up_and_down`, a reviewed `custom_python` function, or an imported
CSV LUT. In every case the trigger rules are the same: the achieved LUT must
cross the configured threshold, and repeated candidates need distinguishable
complete transition signatures for reduced acquisition.

## Imported CSV LUT

`type: csv_lut` uses `path` resolved relative to the pulse-schedule file. The
CSV must contain at least:

| Field | Required/default | Meaning |
| --- | --- | --- |
| `path` | Required | CSV path, resolved relative to the YAML file containing it. |
| `measurement_windows` | Optional | Explicit windows for reduced measurement. A CSV does not create semantic roles automatically. |

```csv
time_s,voltage_v
0.000000,0.0
0.000001,1.0
```

Times and voltages must be finite; time must be strictly increasing. Point
spacing, voltage range, frequency/period, sample rate, and LUT size are checked
before hardware access. The original CSV is copied into the run directory. A
digital fingerprint, called a hash, is saved so later changes can be detected.
Nonuniform spacing is rejected unless an explicitly documented compiler mode
resamples it and records that transformation; it is never silently treated as
uniform.

## Measurement roles and windows

An Oscilloscope frame is a voltage-versus-time trace. A **measurement window**
is a selected time interval within that trace. The program averages the
photodiode samples inside each named window to produce a smaller summary value,
such as `minimum` or `high_level`. This is called a **reduced measurement**.

`measurement_role: minimum` and `measurement_role: high_level` attach semantic
meaning to achieved segment intervals. Explicit measurement windows may be used
when a role covers only part of a segment. A compiled window records its
requested and achieved start/end time and its edge exclusions.

Window definitions begin in achieved LUT phase time. For configured reduced
measurements, the planner examines the achieved ChannelB waveform-reference
LUT for crossings of `run_settings.moku.trigger_level_v` in the configured
edge direction. It linearly interpolates the one matching crossing between LUT
points and treats that phase as oscilloscope `t = 0`. Every reduced window is
then shifted from LUT phase into trigger-relative oscilloscope time. A window
that would straddle the trigger phase is rejected unless its edge is excluded
or the window is split deliberately.

No matching ChannelB crossing is an invalid reduced trigger configuration.
Exactly one crossing is the simplest case. Multiple crossings are supported
when the complete cyclic pattern of rising and falling threshold transitions
uniquely identifies each candidate. The runtime classifies every returned
ChannelB trace against those compiled signatures, so a frame triggered on the
second identical-direction edge is mapped to the correct LUT phase. If two
candidates have indistinguishable transition signatures, planning stops rather
than guessing. Repeated names or voltage levels do not make a crossing unique;
the achieved phases and complete transition timing do.

A waveform that cannot provide an unambiguous reference remains usable as an
explicit **raw-only** workflow with no measurement roles or windows. Raw-only
means retaining the captured voltage-versus-time trace without calculating
minimum, high-level, or extinction summaries. ChannelA triggering likewise
does not establish a known position within the LUT cycle and is supported only
for raw-only capture in the configured runtime.

An explicit `measurement_windows` entry accepts:

| Field | Required/default | Meaning |
| --- | --- | --- |
| `name` | Optional; `window_1`, `window_2`, ... | Unique safe name saved in the measurement plan. |
| `role` | Required | Scientific role written to the reduced output, such as `minimum` or `high_level`. |
| one `start_*` | Required | Non-negative start time in achieved LUT phase. |
| one `end_*` | Choose end or duration | Positive absolute end time in achieved LUT phase. |
| one `duration_*` | Choose end or duration | Positive window length measured from `start_*`. Do not combine it with `end_*`. |
| one `exclude_start_*` | Optional; 0 s | Non-negative amount trimmed from the start. |
| one `exclude_end_*` | Optional; 0 s | Non-negative amount trimmed from the end. |

These fields accept `_s`, `_ms`, `_us`, or `_ns`. The window after exclusions
must be non-empty, remain inside one achieved period, and not overlap another
explicit window.

```yaml
measurement_windows:
  - name: settled_low
    role: minimum
    start_us: 2
    duration_us: 6
    exclude_start_ns: 200
    exclude_end_ns: 200
  - name: settled_high
    role: high_level
    start_us: 12
    end_us: 18
```

The first raw interval is 2–8 microseconds; exclusions reduce its usable
interval to 2.2–7.8 microseconds. The second interval uses an absolute end and
remains 12–18 microseconds.

In manual timebase mode, the span must include `t = 0`, at least two expected
samples on each side of it, all transitions needed to classify the reference,
and enough delayed role samples to meet
`run_settings.measurement.minimum_valid_points_per_role`. Automatic mode
derives each action's timebase from the guarded roles, optical-delay policy,
frame-length cap, point-count requirement, and full reference signature. A
single-edge plan stays inside neighbouring equivalent edges. A multi-edge plan
may need a wider interval so the complete signature is visible; planning fails
when the configured duration cap makes that classification impossible.

The returned ChannelB trace is checked again on every reduced frame. The
observed crossing, classified trigger candidate, measured or fixed ChannelA
optical delay, confidence, and selected, finite and rejected point counts are
written to `moku/measurement_alignment.csv`. `optical_delay_mode: per_frame`
requires a sustained optical step with the configured SNR;
`optical_delay_mode: fixed` uses an independently calibrated delay instead.
`optical_settling_guard_s` always removes the beginning of each shifted window
after ordinary edge exclusions. Non-finite ChannelA points outside aligned
roles do not matter; selected non-finite points are removed before the minimum
valid count is enforced.

Frames captured during a waveform or measurement-plan transition are discarded.
If a waveform provides neither suitable roles nor explicit windows, the
effective planner records it as raw-only; minimum, high-level, and extinction
metrics are then absent rather than fabricated.

When several windows have the **same role**, reduction pools every finite sample
from those windows and calculates a sample-count-weighted mean. A longer window
therefore contributes more weight than a shorter one. This is useful when, for
example, pre-pulse and post-pulse dark plateaux genuinely estimate the same
physical level. It is not automatically appropriate for distinct pulses or
different thermal/electrical histories. Give those windows separate roles such
as `pulse_1_high` and `pulse_2_high` when one value per pulse is required.
Staircases already use distinct `level_0`, `level_1`, ... roles by default.

### Raw trace policy

`run_settings.moku.raw_capture` controls storage independently of the waveform
definition:

| Field | Meaning |
| --- | --- |
| `reduced_mode` | `none`, `all`, or `periodic` raw frames alongside reduced rows. |
| `interval_*` | In periodic mode, save at most once per elapsed interval; choose one duration unit. |
| `every_n_accepted_samples` | Alternative periodic selector based on accepted reduced rows. Do not combine it with `interval_*`. |
| `first_after_action` | Save the first accepted frame after each Moku action change. |
| `first_after_session` | Save the first accepted frame after each real Moku session/reconnect change. This does not create a new action. |
| `save_rejected` | Preserve rejected frames for diagnosis. Disabled by default because a fault can otherwise consume large disk space. |
| `maximum_rejected_frames_per_sample` | Hard per-sample bound when rejected-frame saving is enabled. |
| `raw_only_window` | `full_period` or `trigger_window`, defining what automatic raw-only capture means. |
| `trigger_window_pre_*`, `trigger_window_post_*` | Positive spans required only with `trigger_window`; each accepts the documented duration suffixes. |

For example, this keeps a diagnostic frame every ten minutes and at genuine
action/session boundaries without turning every reduced sample into a large raw
file:

```yaml
moku:
  raw_capture:
    reduced_mode: periodic
    interval_minutes: 10
    first_after_action: true
    first_after_session: true
    save_rejected: false
    maximum_rejected_frames_per_sample: 1
    raw_only_window: full_period
```

Raw-only actions ignore `reduced_mode` and always retain their trace. With
`full_period`, an automatic timebase cap smaller than the achieved period is an
error. Select `trigger_window` with explicit pre/post durations when a cropped,
trigger-centred trace is scientifically intended.

## Waveform actions

`waveforms` defines reusable one-cycle shapes. `moku_schedule` is an ordered
list telling the runtime when to use each shape and when to stop it. Only one
Moku action runs at a time.

Every action accepts:

| Field | Required/default | Meaning |
| --- | --- | --- |
| `name` | Optional; `action_1`, `action_2`, ... | Unique safe name used in events and provenance. |
| `waveform` | Required | Name of an entry in the same file's `waveforms` mapping. |
| `start` | Optional; `immediately` | Start condition, written as a mode string or a mapping. |
| `run` | Required | Mapping defining the action's stop condition. |

Actions are visited in list order. A later action cannot run concurrently with
an earlier one. When the scheduler reaches an action, it waits until that
action's `start` condition is true. If the event happened earlier, the
condition is already satisfied and the action can begin immediately after the
previous action stops. Loading a LUT and calling the SDK adds non-deterministic
software latency between actions.

### Start modes

| Start mode | Extra field | Exact meaning |
| --- | --- | --- |
| `immediately` | None | Start as soon as the scheduler reaches this action. This is the default. |
| `after_previous_waveform_action` | None | Start after the previous listed action completes. Invalid on the first action. Since actions are ordered, this is mainly an explicit statement of intent. |
| `elapsed_experiment_time` | Exactly one `elapsed_*` | Start when monotonic time since the waveform schedule began reaches this non-negative duration. Accepted suffixes are `_s`, `_ms`, `_us`, and `_ns`. |
| `temperature_stage_started` | `temperature_stage` | Start after the named expanded temperature stage has begun. |
| `temperature_became_stable` | `temperature_stage` | Start after that stage completes stable qualification and enters its hold. |
| `temperature_stage_completed` | `temperature_stage` | Start after that stage's hold finishes. |
| `named_event` | `event` | Start after the supervisor records the exact named event. Use only an event supported by the experiment runtime. |

Temperature stage names must exist in the fully expanded temperature schedule.
Generated names are visible in dry-run, but explicit names are easier to read
and more stable when coordinating Moku actions. Use
`temperature_became_stable` only for a stage with `stability.required: true`;
a stage that skips stability enters its hold directly and does not emit that
qualification event.

Examples:

```yaml
# Scalar form: begin when the scheduler reaches this action.
start: immediately

# Mapping form: begin 90 seconds after the waveform schedule started.
start:
  mode: elapsed_experiment_time
  elapsed_s: 90

# Begin when baseline_25_c has finished stable qualification.
start:
  mode: temperature_became_stable
  temperature_stage: baseline_25_c
```

### Run modes

| `run.mode` | Required or allowed fields | Completion |
| --- | --- | --- |
| `count` | Positive integer `count`; optional `recovery` | Deliver the requested number of LUT cycles using strict NCycle or bounded-uncertainty chunks. |
| `duration` | Exactly one positive `duration_*`; optional `end_policy` | Start continuous output and stop it at a monotonic wall-clock deadline. |
| `until_experiment_end` | None | Continue until the master experiment ends. |
| `until_temperature_stage_end` | Required `temperature_stage` | Continue until the named stage completes. |
| `fill_temperature_stage` | Required `temperature_stage` | Equivalent intent: fill the named temperature stage. |
| `continuous` | None | Continue according to master completion. |
| `fill_experiment` | None | Alias for filling the experiment, normalised to experiment-end behavior. |
| `forever` | None | Continue until operator interruption. It must be the final reachable action. |

Fields belonging to another mode are rejected. For example, `count` cannot
also contain `duration_s`, and `duration` cannot contain `temperature_stage`.

`duration_*` accepts `_s`, `_ms`, `_us`, or `_ns`. The
runtime uses continuous output, not one enormous NCycle request. The exact
requested monotonic duration governs the stop, so the last cycle may be
partial. Existing `end_policy` values—`reject_partial_cycle`, `round_down`,
`round_up`, and `truncate`—remain accepted for migration and cycle-equivalent
provenance, but they do not change this continuous-output stop mechanism.
Moku outage time continues to count toward a duration action.

For the 100 kHz square example, an exact five-cycle action is:

```yaml
moku_schedule:
  - name: five_cycles
    waveform: square_100khz
    run:
      mode: count
      count: 5
```

The exact five-cycle example is count mode. Strict count becomes indeterminate
after a disconnect because software cannot prove how many cycles physically
left the connector before control was lost.

Count recovery has these fields:

| Field | Required/default | Meaning |
| --- | --- | --- |
| `recovery.mode` | Optional; `strict` | `strict` uses one exact hardware burst and rejects counts above the Moku:Go NCycle limit of 1,000,000. `bounded_uncertainty` allows a larger request to be split into finite chunks. |
| `recovery.maximum_uncertain_fraction` | Required only for `bounded_uncertainty` | Fraction strictly between 0 and 1 that bounds the total permitted ambiguous cycles relative to `count`. |

For bounded uncertainty, the first chunk is:

```text
min(1,000,000, floor(count * maximum_uncertain_fraction / 2))
```

That result must be at least one cycle. If control is lost during a chunk, the
chunk is never replayed: delivery within it is bounded from zero through the
chunk size. Later chunks shrink as needed, and the action fails if another
ambiguous chunk would exceed the total uncertainty budget. Every delivery
bound and interrupted chunk is recorded.

Example allowing at most one percent ambiguity:

```yaml
moku_schedule:
  - name: long_count
    waveform: square_100khz
    start: immediately
    run:
      mode: count
      count: 5000000
      recovery:
        mode: bounded_uncertainty
        maximum_uncertain_fraction: 0.01
```

Here the first chunk contains 25,000 cycles and the total ambiguity budget is
50,000 cycles. Chunk boundaries are controlled by Python and the SDK and are
not gap-free. Use an external counter or gate when uninterrupted, externally
verified delivery is a scientific requirement.

A duration example is:

```yaml
moku_schedule:
  - name: two_hour_observation
    waveform: square_100khz
    start:
      mode: temperature_became_stable
      temperature_stage: baseline_25_c
    run:
      mode: duration
      duration_s: 7200
```

This starts after `baseline_25_c` enters its hold and requests two hours of
continuous waveform output. It does not stop merely because that temperature
stage ends; use `until_temperature_stage_end` when the stop must be linked to
the stage.
