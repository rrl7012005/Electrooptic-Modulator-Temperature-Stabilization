# Scheduling, recovery, and resumption

This page explains when temperature stages and waveforms start and stop, what
happens when the laboratory computer loses software control of the Moku, and
how a stopped run can be resumed.

Here, a **Moku control connection** means the communication link used by the
Python program to send commands to the Moku and read replies. On the present
apparatus, the direct USB-C link appears to Windows as a network connection.
Losing this control connection does not necessarily mean that any optical or
electrical cable was disconnected, and it does not tell the program what the
physical output continued to do after communication stopped.

**Output 2** is the physical Moku connector used for the requested waveform.
Internal ChannelB is only a copy used by the Oscilloscope as a timing reference;
it is not another physical output.

A **waveform lookup table (LUT)** is the ordered list of voltage values for one
complete waveform cycle. The Moku steps through the list at the selected sample
rate. "First LUT sample" therefore means the beginning of that stored waveform
cycle.

The short version is:

- temperature and waveform schedules run independently;
- an explicit event is required when one schedule must wait for the other;
- elapsed durations use a clock that cannot jump when the computer clock changes;
- restoring Moku communication while Python is still running is different from
  resuming an experiment after Python itself stopped;
- continuous waveforms may start again from the first LUT sample, but an
  interrupted finite burst is never triggered again automatically; and
- resume trusts only the configuration copy saved inside the run directory and
  checks its digital fingerprints before use.

Internally, the supervisor advances separate temperature, Moku waveform, and
acquisition state machines from one monotonic experiment clock. Here,
"monotonic" means elapsed time only moves forward, even if the wall clock is
corrected.

```text
Shared monotonic experiment clock
|-- temperature state machine
|-- Moku waveform state machine
|-- Moku acquisition state machine
`-- supervisor and event timeline
```

UTC and `Europe/London` timestamps provide an unambiguous wall-clock record.
Monotonic elapsed time controls durations and is not affected by daylight-saving
changes or corrections to the computer clock.

## Independent timelines

A temperature transition does not stop, restart, or change the active waveform.
A waveform transition does not write a temperature target or change temperature
stage timing. The state machines interact only through an explicit condition,
such as starting a waveform when a named stage becomes stable or stopping it at
the end of a named stage.

This permits, for example:

- one waveform to continue across several temperature stages;
- temperature stages to advance during one long waveform action;
- a waveform to begin on a stability event and continue after later temperature
  transitions; and
- either schedule to run without the other component.

Temperature-linked conditions are rejected when temperature control or its
schedule is absent. References are resolved against expanded stage names before
execution.

## Temperature phases

An explicit target stage normally proceeds through:

```text
target requested -> settling -> stable-duration qualification -> hold -> complete
```

When `stability.required` is false, the hold begins after the validated target
write/readback. When it is true, the stable-duration timer resets whenever the
reading leaves the controller's configured tolerance. The hold timer starts
only after the complete stable interval.

A settling timeout is a distinct failure/policy event. It must not be reported
as a stable stage. Invalid sensor readings, controller errors, and communication
loss are recorded rather than converted to zero or discarded.

## Waveform starts

A waveform action may start:

- immediately;
- at a monotonic elapsed experiment time;
- after the previous waveform action;
- when a named temperature stage starts;
- when a named temperature stage becomes stable;
- when a named temperature stage completes; or
- on a named supervisor event.

The event that satisfied the condition, its UTC/local timestamps, and monotonic
elapsed time are recorded. SDK upload/switch latency after that event is also
observable; it is not described as deterministic waveform timing.

## Waveform stops and repeats

`count` requests an exact hardware cycle count. `duration` converts a requested
duration to achieved cycles according to its explicit end policy.
`until_temperature_stage_end` and `fill_temperature_stage` name one explicit
stage. `until_experiment_end` and `continuous` follow master completion.
`forever` is only valid as the last reachable action and relies on operator
`Ctrl+C`.

Within-LUT segment timing and a supported hardware burst count are deterministic
subject to the achieved quantisation reported by the compiler. Python/API
changeover between separate waveform actions is not deterministic.

## Master completion

The master policy is separate from either component's local completion:

| Policy | Master behavior |
| --- | --- |
| Fixed duration | Stop selected components at the configured monotonic duration. |
| All schedules complete | Stop after every selected finite schedule completes. |
| Moku schedule complete | Stop when the Moku schedule completes. |
| Temperature schedule complete | Stop when the temperature schedule completes. |
| Operator Ctrl+C | Continue until interrupted. |

Validation rejects a completion graph that cannot be reached, such as waiting
for all schedules when the only terminal action is `forever`.

## Automatic recovery during a run

Automatic Moku recovery handles an outage inside a still-running master process.
It is not the same as resuming a stopped experiment.

The recommended policy is:

```yaml
recovery:
  temperature_hold_during_moku_outage: pause_timer
  waveform_duration_during_outage: pause_timer
  continuous_waveform: restart_from_phase_zero
  finite_burst_interrupted: abort
  maximum_moku_outage_s: 1800
```

During recovery, the TEC holds its current target, the temperature stage does
not advance, valid-data hold timing pauses, and waveform duration timing pauses.
Recovery is complete only after the active Moku settings have been applied to a
replacement session with Output 2 initially disabled and one valid acquisition
frame has arrived.

A recovered continuous waveform starts again at the first sample of its LUT.
It does not continue from the exact point reached before communication failed.
The scheduled action keeps its waveform run identifier, but its session
identifier increments and the timing break is logged. No average may include
frames from both sides of that break.

If communication fails during a finite exact-count burst, software cannot know
how many physical cycles reached Output 2. The checkpoint records the delivered
count as unknown (called `indeterminate` in logs), stops that action, and never
triggers the same burst again automatically. A later action does not silently
continue as if the burst completed.

## Runtime checkpoint

The runtime checkpoint is a small file that records where the independent
schedules have reached. It is written to a temporary file and then moved into
place as one operation, so an interruption should leave either the old complete
checkpoint or the new complete checkpoint rather than half a file. It records
at least:

- effective configuration hash and every active LUT hash;
- temperature stage index/name and phase;
- completed temperature hold time;
- Moku action index/name and waveform name;
- waveform run and session identifiers;
- repeat mode and requested/completed duration or count;
- the latest safe resume boundary;
- last valid sample timestamp;
- last confirmed Moku output state; and
- the monotonic accounting needed for paused timers.

Event and raw-data logs remain the authoritative history; the checkpoint is a
restart position, not a replacement for them.

Here, a **hash** is a digital fingerprint of a file. Any change to the copied
configuration or compiled waveform changes its hash. A **checkpoint** is the
small state file described above; it records where each independent schedule
had reached, but it does not replace the raw measurements or event history.

## Resuming a stopped experiment

Validate or inspect the immutable resume plan without hardware first, then use
the explicit execution form only after apparatus review:

```powershell
python run_experiment.py --resume "path\to\experiment_manifest.json"
python run_experiment.py --resume "path\to\experiment_manifest.json" --preview
python run_experiment.py --resume "path\to\experiment_manifest.json" --execute
```

The first command is a read-only dry run. Preview writes a uniquely named
resume-plan record without overwriting raw data. Execute still pauses for the
operator to type `RESUME` before any hardware runtime is imported.

Resume performs these steps before enabling any output:

1. Locate the run's manifest (the summary/index file for the run) and the copied
   configuration files saved inside that run directory.
2. Verify copied source, effective-configuration, imported-asset, and LUT hashes.
3. Verify raw output and event-log consistency without rewriting them.
4. Check whether a recorded old process still appears active.
5. Load the last valid atomic checkpoint.
6. Determine temperature and waveform resume positions independently.
7. Connect to hardware with Moku output disabled.
8. Display the exact resume plan, paused/remaining times, any continuous
   waveform that must start again from its first LUT sample, and any finite
   burst whose delivered count is unknown.
9. Require explicit `RESUME` confirmation.
10. Restore only from a recorded safe boundary.

Resume never consumes later edits to the original YAML. If the copied snapshot
or a LUT hash differs, start a new experiment instead of bypassing the check.
Raw logs are extended only through an explicit append/resume path and are never
silently replaced.

A finite burst with an unknown delivered count has no automatic safe
continuation. The operator must preserve that status and either end the
experiment or begin a separately identified new run under a deliberately
chosen policy.

## Normal completion, errors, and Ctrl+C

On normal completion, each state machine records its final state and applies its
explicit completion behavior. On a settling timeout, invalid sensor value,
controller error, Moku outage limit, or another component failure, the
supervisor stops advancement, preserves buffers and checkpoints, and attempts
bounded cleanup.

On `Ctrl+C`, monotonic waits and reconnect backoff remain interruptible. The
runtime saves current buffers, retires workers, and performs bounded output-off
cleanup. If communication prevents confirmation, the event log states that the
physical output is unknown. Stopping Python is not proof that Moku or TEC output
is disabled.

## Timeline files

`waveform_timeline.csv` is the machine-readable record of temperature stages,
waveform actions/sessions, recovery boundaries, and condition events.
`waveform_timeline.png` presents those lanes separately from primary scientific
plots. Primary minimum, high/offset, and extinction plots retain only their data
and 60-second mean.

Dry-run and preview artifacts initially contain the validated planned action
timeline. At the end of a real runtime, the timeline writer reads the
append-only `experiment_events.jsonl` file, which stores one event record per
line. It derives timestamped rows from the events that actually occurred, then
writes complete replacement versions of `waveform_timeline.csv` and
`waveform_timeline.png`. An incomplete final line from an interrupted append is
ignored, while a malformed completed record is reported instead of being
silently discarded. The resulting files record actual events; they do not claim
that every planned transition occurred.
