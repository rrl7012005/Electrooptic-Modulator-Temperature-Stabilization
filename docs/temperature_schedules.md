# Temperature schedules

This guide explains every temperature-schedule field, when each timer starts,
how generated schedules expand, and what happens when a stage or schedule
finishes. Read [Safety and operator review](safety.md) before requesting a real
run.

The values in the examples are illustrations, not recommendations for an EOM,
thermistor, TEC, heatsink, or controller. A real target is accepted only when it
is also inside the verified limits in the run-settings file.

## The timing rule that matters most

A normal temperature stage runs in this order:

```text
validated target write and readback
        |
        v
wait for a valid, stable reading
        |
        v
remain continuously stable for stable_duration_*
        |
        v
start hold_duration_*
        |
        v
complete the stage
```

Therefore, `hold_duration_minutes: 30` means **30 minutes after stable
qualification**, not 30 minutes from the target write. Settling time is extra.

If `stability.required` is `false`, there is no settling wait and the hold
starts as soon as the validated target write/readback finishes. A stage with
`target_c: null` explicitly requests TEC output off and starts its hold without
waiting for stability.

## Choose a schedule type

| Type | Use it when | What you write |
| --- | --- | --- |
| `explicit` | Stages have different holds, notes, sampling rates, stability rules, or stop points. | Every stage. |
| `sweep` | You want long measurement holds joined by shorter transition steps. | Measurement spacing and transition spacing. |
| `targets` | You already know the exact ordered list of measurement temperatures. | A `targets_c` list and one shared hold. |
| `range` | You want evenly spaced measurement temperatures. | Start, finish, and a positive maximum step size. |

All four forms are expanded into an immutable list of named stages before any
hardware access. Dry-run prints that list. Preview also saves it in the run
snapshot. The runner never generates a new target halfway through a run.

## Duration field names

Every duration carries its unit in its key. The accepted suffixes are:

| Suffix | Unit | Example |
| --- | --- | --- |
| `_s` | seconds | `hold_duration_s: 30` |
| `_ms` | milliseconds | `sampling_interval_ms: 500` |
| `_us` | microseconds | `sampling_interval_us: 500000` |
| `_ns` | nanoseconds | `sampling_interval_ns: 500000000` |
| `_minutes` | minutes | `hold_duration_minutes: 30` |
| `_hours` | hours | `measurement_hold_hours: 6` |

Use exactly one unit form for one duration. For example, do not put both
`hold_duration_s` and `hold_duration_minutes` in the same stage. The parser
rejects the conflict rather than guessing which one should win.

## Fields shared by every schedule

| Field | Required? | Meaning |
| --- | --- | --- |
| `name` | Yes | Non-empty schedule name saved in provenance. It does not control hardware. |
| `type` | Yes | One of `explicit`, `sweep`, `targets`, or `range`. |
| `completion_behavior` | Yes | Physical request made when the schedule ends or settling times out. See [Schedule completion](#schedule-completion). |
| `stability` | No | Default stability rules inherited by every generated or explicit target stage. |
| one `sampling_interval_*` | No; default 5 s | How often the TEC state is read and logged. It must be above zero. A shorter interval produces more rows and more controller traffic. |

Unknown fields are rejected. YAML keys may not be repeated.

## Stability fields and inheritance

The schedule-level `stability` mapping may contain:

| Field | Required/default | Meaning |
| --- | --- | --- |
| `required` | Optional; default `true` | If `true`, stable qualification must finish before the hold timer begins. If `false`, the hold begins immediately after the validated target write/readback. |
| `tolerance_c` | Optional; default `null` | Additional software check: the object-temperature reading must be within this positive number of degrees Celsius of `target_c`. `null` disables this extra check. It does not change a controller's own stability configuration. |
| one `stable_duration_*` | Optional; default 0 s | How long stability must remain continuous before the hold begins. Zero accepts the first stable sample. It cannot be negative or exceed the timeout when stability is required. |
| one `timeout_*` | Optional; default 1800 s | Maximum time from stage entry to stable qualification. It must be above zero. |

When stability is required, the software requires the controller's reported
`temperature_stable` flag to be true. If `tolerance_c` is not null, the object
temperature must also be within that tolerance. Both conditions must remain
true for the entire stable duration. A failed sample resets the continuous
stable timer.

The timeout is measured from stage entry. If it expires first, the schedule
fails and applies the schedule-level `completion_behavior`.

Example:

```yaml
stability:
  required: true
  tolerance_c: 0.1
  stable_duration_minutes: 5
  timeout_minutes: 30
```

This means: wait for the controller to report stable **and** for the object
temperature to be within 0.1 °C of the target. Both must stay true for five
continuous minutes. If that has not happened within 30 minutes of entering the
stage, fail the schedule.

An explicit stage may override only the stability fields it needs. Unspecified
fields remain inherited:

```yaml
stability:
  required: true
  tolerance_c: 0.1
  stable_duration_minutes: 5
  timeout_minutes: 30
stages:
  - name: quicker_qualification
    target_c: 25
    hold_duration_minutes: 20
    stability:
      stable_duration_s: 30
```

The stage above still inherits `required: true`, `tolerance_c: 0.1`, and the
30-minute timeout. Only the stable duration changes.

## Explicit schedules

Use `type: explicit` when a human should be able to read every stage exactly as
it will run.

### Every explicit-stage field

| Field | Required/default | Meaning |
| --- | --- | --- |
| `name` | Optional; `stage_001`, `stage_002`, ... | Unique stage name used in logs and in Moku start/run references. Use a descriptive explicit name when another schedule refers to it. |
| `target_c` | Required | Finite target in °C. The literal YAML value `null` means an explicit TEC-output-off stage. |
| one `hold_duration_*` | Required | Positive time spent holding after stable qualification. For `target_c: null`, it is the output-off duration. |
| `stability` | Optional | Partial override of the schedule-level stability mapping. |
| one `sampling_interval_*` | Optional | Positive stage-specific logging interval. If omitted, the schedule value is inherited. |
| `notes` | Optional | Free human-readable note copied into the effective plan. An empty string is allowed. |
| `completion_behavior` | Optional; `advance` | `advance` enters the next stage. `stop_schedule` ends the schedule here and skips later stages. |

### Complete example

```yaml
name: baseline_then_measurement
type: explicit
completion_behavior: return_to_safe_target
sampling_interval_s: 2
stability:
  required: true
  tolerance_c: 0.1
  stable_duration_minutes: 5
  timeout_minutes: 45

stages:
  - name: baseline_25_c
    target_c: 25
    hold_duration_minutes: 30
    notes: Record a stable baseline.

  - name: measurement_30_c
    target_c: 30
    hold_duration_hours: 2
    sampling_interval_s: 1
    stability:
      stable_duration_minutes: 10
    notes: Longer qualification and faster logging for this measurement.

  - name: tec_off_observation
    target_c: null
    hold_duration_minutes: 15
    notes: Explicitly disable the TEC output and observe passive response.
```

The second stage inherits the tolerance and timeout but changes the stable
duration. Its two-hour hold starts only after ten continuous stable minutes.
The last stage requests output off; it is not shorthand for stopping Python.
After its 15-minute hold, the schedule requests `return_to_safe_target`.

### Stopping at a particular stage

```yaml
  - name: final_measurement
    target_c: 30
    hold_duration_minutes: 20
    completion_behavior: stop_schedule
```

When this stage completes, all later stages are skipped. The schedule-level
completion behavior still runs. `stop_schedule` only changes schedule flow; it
does not itself disable the TEC or choose a final target.

## Sweep schedules

Use `type: sweep` when measurement targets need long holds and the path between
them needs smaller, shorter transition stages.

### Every sweep field

| Field | Required/default | Meaning |
| --- | --- | --- |
| `start_c` | Required | First measurement target in °C. It must differ from `finish_c`. |
| `finish_c` | Required | Final outward measurement target. It is always included exactly. |
| `measurement_interval_c` | Required | Positive maximum spacing between long measurement targets. Direction comes from start and finish. |
| `transition_increment_c` | Required | Positive maximum spacing between short transition targets. It is applied between adjacent measurement targets. |
| one `transition_hold_*` | Required | Positive hold for each generated transition stage. |
| one `measurement_hold_*` | Required | Positive hold for each generated measurement stage. |
| one `initial_tec_off_hold_*` | Optional | If present and positive, insert a first stage named `initial_tec_off` with `target_c: null`. The field may be explicitly `null` to omit it. |
| `reverse` | Optional; `false` | If `true`, return through the measurement targets to the start. The finish is not duplicated at the turn. |
| `cycles` | Optional; 1 | Positive integer number of outward paths, or outward-and-return paths when `reverse: true`. A shared junction is not duplicated between cycles. |

All generated non-off stages inherit the common stability and sampling rules.
Measurement stages have role `measurement`; intermediate stages have role
`transition`. Names are deterministic, for example
`stage_003_transition_23_c`.

### Worked expansion

```yaml
name: worked_sweep
type: sweep
completion_behavior: hold_current_target
start_c: 20
finish_c: 24
measurement_interval_c: 2
transition_increment_c: 1
transition_hold_minutes: 10
measurement_hold_hours: 1
reverse: true
cycles: 1
stability:
  required: true
  stable_duration_minutes: 5
  timeout_minutes: 30
```

The measurement path is `20 -> 22 -> 24 -> 22 -> 20`. The finish value `24`
appears once at the turn. One-degree transition steps are inserted between
those values, producing:

| Order | Target | Role | Hold after stability |
| --- | ---: | --- | ---: |
| 1 | 20 °C | measurement | 1 hour |
| 2 | 21 °C | transition | 10 minutes |
| 3 | 22 °C | measurement | 1 hour |
| 4 | 23 °C | transition | 10 minutes |
| 5 | 24 °C | measurement | 1 hour |
| 6 | 23 °C | transition | 10 minutes |
| 7 | 22 °C | measurement | 1 hour |
| 8 | 21 °C | transition | 10 minutes |
| 9 | 20 °C | measurement | 1 hour |

Each row has its own settling and five-minute stable qualification before the
listed hold. If an interval does not divide the range evenly, the finish is
still appended exactly and the last step is shorter.

## Target-list schedules

Use `type: targets` for an exact list where every target uses the same hold,
stability, and sampling interval.

| Field | Required/default | Meaning |
| --- | --- | --- |
| `targets_c` | Required | Non-empty list of finite targets in their outward order. Values are used as written; the parser does not sort or deduplicate the list. |
| one `hold_duration_*` | Required | Positive post-stability hold for every generated stage. |
| `reverse` | Optional; `false` | Return through the list to its first value without duplicating the last value at the turn. |
| `cycles` | Optional; 1 | Positive integer. The common value where one cycle ends and the next starts is not duplicated. |

```yaml
name: selected_targets
type: targets
completion_behavior: hold_current_target
targets_c: [20, 22.5, 27]
hold_duration_minutes: 30
reverse: true
cycles: 2
stability:
  required: false
```

The expanded path is:

```text
20, 22.5, 27, 22.5, 20, 22.5, 27, 22.5, 20
```

There is one `27` at each turn and one shared `20` at the boundary between the
two cycles. With stability disabled, each 30-minute hold starts after its
validated target write/readback.

## Range schedules

Use `type: range` for regularly spaced measurement targets.

| Field | Required/default | Meaning |
| --- | --- | --- |
| `start_c` | Required | First target. It must differ from `finish_c`. |
| `finish_c` | Required | Final outward target, always included exactly. |
| `step_c` | Required | Positive maximum step magnitude. Do not make it negative for a descending range; direction is inferred from the endpoints. |
| one `hold_duration_*` | Required | Positive post-stability hold for every target. |
| `reverse` | Optional; `false` | Return to the start without duplicating the finish at the turn. |
| `cycles` | Optional; 1 | Positive integer, with a shared cycle junction written only once. |

Ascending example:

```yaml
name: ascending_range
type: range
completion_behavior: hold_current_target
start_c: 20
finish_c: 25
step_c: 2
hold_duration_minutes: 30
reverse: false
cycles: 1
```

This expands to `20, 22, 24, 25`. The final step is 1 °C so that `25` is
included exactly.

Descending example:

```yaml
name: descending_range
type: range
completion_behavior: hold_current_target
start_c: 30
finish_c: 24
step_c: 2
hold_duration_minutes: 30
```

This expands to `30, 28, 26, 24`. `step_c` stays positive.

## Schedule completion

`completion_behavior` is required at schedule level. It is applied after the
last stage, after a stage using `stop_schedule`, and after a settling timeout.

| Value | Physical request |
| --- | --- |
| `hold_current_target` | Make no further target or output-state write. Python schedule timing stops; the controller keeps the target and enabled/disabled state already in effect. |
| `disable_output` | Explicitly request TEC output disable. This is physically different from ending the program. |
| `return_to_safe_target` | Request the separately configured and validated safe target. It is rejected if the required safe-target configuration is unavailable. |
| `revert_to_stored_target` | Restore the target read and recorded during preflight. |

No value means “do whatever seems safe”; the choice must be explicit. A
communication failure can prevent the requested cleanup from being confirmed,
so the event log and controller state still need operator review after an
error.

## Validation and preview

First validate without writing files or connecting to hardware:

```powershell
python run_experiment.py --config configs/experiment.yaml --dry-run
```

Then save the complete expanded plan and previews, still without hardware:

```powershell
python run_experiment.py --config configs/experiment.yaml --preview
```

Check all of the following in the expanded stage list:

- target order, including reversal and repeated cycles;
- which stages are measurements, transitions, or output-off intervals;
- exact hold and sampling durations after unit conversion;
- stability inheritance and timeout values;
- deterministic stage names used by Moku event references; and
- final schedule completion behavior.

The schedule validator rejects missing targets, non-finite numbers, zero or
negative required durations, duplicate stage names, unknown fields, unknown
completion actions, and generated values outside the configured apparatus
limits. Real execution adds controller communication, channel, sensor,
readback, and safety-limit checks before a target is written.

For how a Moku action can start or stop on one of these named stages, continue
with [Scheduling, recovery, and resumption](scheduling_and_resumption.md).
