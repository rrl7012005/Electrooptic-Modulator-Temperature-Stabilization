# Troubleshooting

Start with a dry run:

```powershell
python run_experiment.py --config configs/experiment.yaml --dry-run
```

This is the safest place to diagnose configuration, waveform, and schedule
errors because it must not import hardware SDKs or open a device.

In the messages below, **LUT** means waveform **lookup table**: the ordered list
of voltage values that represents one complete waveform cycle inside the Moku.
Its size is the number of stored values, and its sample rate is how quickly the
Moku steps through them.

The **Moku SDK** is the manufacturer's Python package used to control the
instrument. A **hard SDK deadline** is the maximum time the main program will
wait for one Moku command before it stops the helper process that issued it.

Use the error message to choose a section:

| Symptom | Go to |
| --- | --- |
| YAML or referenced file error | [Configuration cannot be loaded](#configuration-cannot-be-loaded) |
| Temperature-linked action rejected | [A temperature event is rejected](#a-temperature-event-is-rejected) |
| LUT, voltage, or timing error | [A waveform does not compile](#a-waveform-does-not-compile) |
| Missing reduced optical values | [No minimum or high-level value is produced](#no-minimum-or-high-level-value-is-produced) |
| Acquisition gap or timeout | [Optical trigger timeout versus loss of Moku control](#optical-trigger-timeout-versus-loss-of-moku-control) |
| Resume validation failure | [Resume is refused](#resume-is-refused) |
| Missing plot markers | [Plots appear to omit an outage or transition](#plots-appear-to-omit-an-outage-or-transition) |
| Output-off warning | [Cleanup could not confirm output off](#cleanup-could-not-confirm-output-off) |

## Configuration cannot be loaded

Referenced paths are resolved relative to the YAML file containing the
reference, not the current PowerShell directory. Check the path printed in the
error and keep Windows paths either slash-separated or quoted.

Duplicate YAML keys, unknown fields, non-finite numbers, missing referenced
files, circular references, and values with two unit representations are
errors. Remove the ambiguity rather than relying on ordering. Files are
composed by role; there is no recursive dictionary override.

If `yaml` cannot be imported, install the repository dependency in the active
environment:

```powershell
python -m pip install -r requirements.txt
```

## A temperature event is rejected

A waveform start or stop condition referring to a temperature stage requires
the `temp-control` component and a non-null `temperature_schedule_file`. Check
that the referenced stage name exists after schedule expansion. A Moku-only
experiment cannot wait for temperature stability.

Temperature holds begin only after the target has remained within tolerance for
the configured stable duration unless the schedule explicitly selects another
behavior. A settling timeout is not a successful hold.

## A waveform does not compile

Read both the requested and achieved values in the dry-run report. Common
causes are:

- requested connector voltage outside the configured Moku limits;
- more than one square-wave timing parameterization;
- a duration with two unit-bearing fields, such as both `duration_s` and
  `duration_us`;
- a pulse or edge too short for the selected LUT resolution;
- a LUT that exceeds memory or sample-rate limits;
- non-finite custom-function output;
- a CSV time column that is not strictly increasing; or
- an imported CSV path resolved relative to the wrong file.

For duration actions, the default `reject_partial_cycle` policy rejects a
duration that is not an exact number of achieved cycles. Select `round_down` or
`round_up` explicitly if that is scientifically acceptable. `truncate` is valid
only when the runtime reports that the selected hardware path supports it.

## No minimum or high-level value is produced

Measurement values are calculated only from explicit measurement roles or
windows. A waveform without meaningful windows may record raw traces, but the
software must not invent `minimum` or `high_level` metrics. Add a measurement
role to an appropriate segment or supply an explicit measurement plan. Ensure
edge-exclusion intervals leave at least one achieved sample in each window.

The historical field `maximum_voltage` means the measured high/offset level,
not necessarily the transfer-curve maximum. New data use the canonical
`high_level_voltage` name; historical input remains supported.

## Optical trigger timeout versus loss of Moku control

An optical trigger timeout means the Oscilloscope did not see the expected
photodiode or waveform-reference threshold crossing. The Python program may
still be communicating normally with the Moku, so this timeout alone does not
justify rebuilding the Moku session.

Loss of Moku control means something different: the Python program can no
longer send commands to, or receive replies from, the Moku through its software
control link. Transport errors, stale software ownership, a hard SDK deadline,
repeated malformed frames, and uncertainty about Output 2 follow that recovery
path. Inspect `experiment_events.jsonl` and the Moku acquisition event log
rather than diagnosing from a gap in the primary CSV alone.

When its recovery policy allows a continuous waveform to restart, it begins at
the first sample of its LUT; it does not continue from the point reached before
the failure. If a finite burst was interrupted, the software records that the
delivered cycle count is unknown, stops the action, and does not trigger the
burst again.

## Resume is refused

Resume reads the copy of the configuration saved inside the run directory, not
the original source YAML. This saved copy is called the run snapshot. Resume is
refused when a file's digital fingerprint (hash) differs, required output/log
files are inconsistent, an old recorded process still appears active, or a
finite burst has no safe resume position. Do not edit a run snapshot to bypass
the check. Create a new experiment if the configuration needs to change.

The resume preview reports independent temperature and Moku positions. Time
spent stopped or in a configured paused outage does not count toward the
corresponding hold or waveform duration.

## Plots appear to omit an outage or transition

Primary Moku time-series plots intentionally contain only data and the
60-second mean. Reconnects, waveform changes, and temperature-stage boundaries
are kept in `waveform_timeline.csv`, `waveform_timeline.png`, and event logs so
the scientific traces remain readable. Missing acquisition intervals remain
gaps; they are not interpolated.

## Time labels look wrong

Run records contain UTC, `Europe/London` local time, and monotonic elapsed time
where applicable. Install the Python timezone database on Windows if
`Europe/London` cannot be resolved. Never repair a run by replacing timezone-
aware timestamps with naive local times. Preserve the original timestamp and
record any import assumption separately.

## Cleanup could not confirm output off

Treat the output state as unknown. Do not start another owner merely to make the
warning disappear. Follow the laboratory isolation procedure, verify the
physical output independently, then diagnose device ownership or connectivity.
See [Safety and operator review](safety.md) before any real-hardware check.
