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

`type: square` represents one low and one high interval per cycle. Required
level fields are `low_level_v` and `high_level_v`. `edge_time_s`,
`edge_time_ms`, `edge_time_us`, or `edge_time_ns` controls the programmed edge;
the default is zero only when the compiler and selected hardware path permit
it.

The optional `measurement_roles` mapping has `low` and `high` keys. Their
defaults are `minimum` and `high_level`, respectively. Set an explicit role
name when the scientific meaning differs.

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

`type: segments` compiles an ordered `segments` list. `low_level_v` is the
default for gaps. A segment has these fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `type` | `pulse`, `gap`, or `level` | Segment kind. |
| `name` | string, optional | Stable name used in reports. |
| `level_v` | number | Required for `pulse` and `level`; omitted for a gap. |
| one `duration_*` | positive number | Requested segment duration. |
| one `edge_time_*` | non-negative number, optional | Programmed ramp time where supported. |
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
does not invent a trailing gap.

## Generated pulse train

`type: pulse_train` expands a compact definition into pulse and gap segments.

| Field | Type | Requirement |
| --- | --- | --- |
| `pulse_count` | integer | At least one. |
| `low_level_v`, `high_level_v` | number | Requested connector levels. |
| one `pulse_duration_*` | positive number | Duration of every pulse. |
| one `inter_pulse_gap_*` | non-negative number | Gap between pulses; no gap follows the final pulse unless configured. |
| one `final_gap_*` | non-negative number, optional | Low interval after the final pulse. |
| one `edge_time_*` | non-negative number, optional | Programmed edge for each pulse. |

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

`type: staircase` accepts either an explicit `levels_v` list or the mutually
exclusive generated form `start_v`, `finish_v`, and non-zero `step_v`.
`dwell_s`, `dwell_ms`, `dwell_us`, or `dwell_ns` is required.

`direction` is `up` by default or `up_and_down`. For `up_and_down`,
`repeat_endpoints: false` prevents the finish and start values from being held
twice at the turn and cycle boundary. An optional `final_gap_*` adds a final
interval at the configured low or start level as defined by the effective plan.
`direction: down` reverses the supplied/generated levels. `low_level_v`
defaults to 0 V for a final gap, `repeat_endpoints` defaults false, and an
optional `edge_time_*` defaults to zero.

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

Each achieved level occupies LUT points and therefore uses deterministic
within-LUT timing. The software does not claim physically instantaneous steps;
analogue bandwidth and loading still apply.

## Registered Python function

`type: custom_python` refers to a function name in the application's explicit
waveform registry and passes a `parameters` mapping. Provide exactly one
`frequency_hz` or one `period_*`; `point_count` is optional. YAML cannot contain
Python expressions, module paths to import, or code to evaluate.

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

## Imported CSV LUT

`type: csv_lut` uses `path` resolved relative to the pulse-schedule file. The
CSV must contain at least:

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

Reduced measurements require exactly one matching ChannelB threshold crossing
per achieved period. No crossing is an invalid trigger configuration. Multiple
matching crossings make the LUT phase ambiguous and therefore reject roles or
explicit reduced windows. Such a waveform remains usable only as an explicit
**raw-only** workflow with no measurement roles or windows. Raw-only means the
captured voltage-versus-time trace is retained without calculating minimum,
high-level, or extinction summaries. ChannelA triggering likewise does not
establish a known position within the LUT cycle and is supported only for
raw-only capture in the configured runtime.

An explicit `measurement_windows` entry has optional unique `name`, required
`role`, exactly one `start_*`, and exactly one `end_*` or `duration_*`.
Optional `exclude_start_*` and `exclude_end_*` durations trim programmed edges.
Windows must remain within one achieved period, must not overlap, and must fit
inside `run_settings.moku.timebase_start_s` through `timebase_end_s`. The
complete composed experiment is rejected before hardware access when a
non-raw window falls outside the acquisition timebase.

Frames captured during a waveform or measurement-plan transition are discarded.
If a waveform provides neither suitable roles nor explicit windows, the
effective planner records it as raw-only; minimum, high-level, and extinction
metrics are then absent rather than fabricated.

## Action run modes

Each `moku_schedule` action names a waveform and contains a `run` mapping.

| `mode` | Additional fields | Completion |
| --- | --- | --- |
| `count` | positive integer `count` | Exactly that many hardware cycles when supported. |
| `duration` | exactly one `duration_*`; optional `end_policy` | Requested action duration. |
| `until_experiment_end` | none | Stops with the master experiment. |
| `until_temperature_stage_end` | `temperature_stage` | Stops when that explicit stage completes. |
| `fill_temperature_stage` | `temperature_stage` | Shorthand for filling one explicit stage. |
| `forever` | none | Final reachable action; stopped by `Ctrl+C`. |
| `continuous` | none | Fills the experiment according to master completion. |
| `fill_experiment` | none | Alias normalised to `until_experiment_end`. |

For `duration`, `end_policy` is `reject_partial_cycle` by default. The other
values are `round_down`, `round_up`, and `truncate`. The first three operate on
whole achieved cycles and the selected policy is recorded. `truncate` is
accepted only on a hardware path that actually supports an exact partial cycle.
The runtime never silently rounds.

For the 100 kHz square example:

```yaml
moku_schedule:
  - name: five_cycles
    waveform: square_100khz
    run:
      mode: duration
      duration_us: 50
      end_policy: reject_partial_cycle
```

50 microseconds is exactly five cycles and is compiled as a five-cycle hardware
burst when that path is supported. Python sleep timing is not substituted for
an available exact hardware count.
