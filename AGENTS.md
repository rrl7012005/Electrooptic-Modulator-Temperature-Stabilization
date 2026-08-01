# AGENTS.md

## Purpose

This repository contains software and documentation for characterising and reducing drift in a commercial electro-optic modulator (EOM), with particular emphasis on whether active temperature stabilisation improves:

- the voltage required to maintain the optical dark point;
- the minimum achievable optical power;
- extinction ratio;
- pulsed optical-output stability; and
- repeatability after different thermal and electrical histories.

The intended final deliverable is a reproducible public GitHub repository that another laboratory user can understand, configure, and run on a comparable EOM setup.

## Experimental context

The current apparatus may include:

- Exail/iXblue NIR-MX800-LN20 lithium-niobate intensity modulator;
- approximately 795 nm laser input;
- Meerstetter TEC-1091 temperature controller;
- GA10K3MCD1 10 kΩ NTC thermistor;
- PCX8-152-F2-1773-TA-RT-W6 thermoelectric cooler;
- Moku:Go for pulse generation and photodiode acquisition;
- Red Pitaya with Linien for dark-point locking and control-voltage logging;
- laboratory-temperature sensors; and
- photodiodes and optical components used to monitor low and high optical levels.

Do not assume that settings safe for this apparatus are safe for another EOM, thermistor, TEC, heatsink, power supply, or optical configuration.

## Scientific objective

The central scientific distinction is between:

1. **Bias-point drift**: movement of the voltage corresponding to minimum transmission.
2. **Extinction-floor drift**: change in the minimum optical power achievable at that operating point.

Do not combine these into one metric. Preserve both in the data model, analysis, plots, and documentation.

The column historically called `maximum` does not necessarily represent the true transfer-curve maximum. In the existing experiment it may represent the optical power measured at approximately 0.5 V above the minimum. Use an accurate name in new code, such as `high_level`, `offset_level`, or `power_at_minimum_plus_0p5_v`, while preserving compatibility with existing files.

## General operating rules

- Inspect existing code before modifying it.
- Prefer small, reviewable changes over large rewrites.
- Preserve working behaviour until replacement code has been tested.
- State assumptions explicitly.
- Do not invent hardware details, protocol behaviour, parameter IDs, units, calibration constants, limits, file formats, or wiring.
- Mark unresolved information with a clear `TODO`, warning, or configuration requirement.
- Do not silently change experimental definitions or data-processing conventions.
- Explain hardware-facing changes in plain language.
- Keep the repository understandable to an engineering student who did not build the original setup.

## Protected files

Files under:

```text
legacy/original_scripts/
```

are frozen copies of the original working scripts.

Never modify, format, move, rename, or delete anything in this directory unless the user explicitly instructs otherwise.

Root-level scripts may still be active working versions during the refactor. Do not delete them merely because replacements exist. Remove or archive them only after the replacement has been tested and the user explicitly approves the change.

## Repository architecture

New reusable code should normally live under:

```text
src/eom_stabilisation/
```

Use clear separation between:

```text
src/eom_stabilisation/
├── tec/          # TEC communication, safety, scheduling and logging
├── moku/         # Moku acquisition and pulse-generation interfaces
├── linien/       # Linien data acquisition or import
├── analysis/     # Loading, alignment, metrics and plots
├── config/       # Configuration parsing and validation
└── cli.py        # User-facing command-line entry points
```

Avoid placing new reusable modules loosely in the repository root.

Keep these concerns separate:

- hardware communication;
- experiment sequencing;
- configuration;
- safety validation;
- data logging;
- data alignment;
- scientific calculations;
- plotting; and
- command-line interaction.

A scheduler should call a controller interface. It should not implement serial-protocol details itself.

## Hardware safety

### Default behaviour

- Default to simulation, dry-run, fake-device, or read-only operation.
- Tests must never communicate with real hardware.
- Do not send a setpoint, enable an output, disable an output, change polarity, or alter controller configuration unless the task explicitly requires it.
- Do not infer permission to control hardware from permission to edit code.

### TEC controller

Do not silently modify:

- PID parameters;
- thermistor coefficients;
- temperature limits;
- current limits;
- voltage limits;
- Peltier polarity;
- sensor selection;
- error thresholds;
- protection settings; or
- persistent controller parameters.

Treat these as apparatus configuration, not ordinary experiment variables.

Before any real temperature setpoint write, the code should:

1. establish communication successfully;
2. identify the controller or verify the expected configuration where possible;
3. read current object and heatsink temperatures;
4. validate that readings are finite and plausible;
5. validate the requested target against configured safety limits;
6. confirm the correct channel;
7. log the request;
8. write the setpoint;
9. read back the active target where supported; and
10. report any mismatch or controller error.

Do not repeatedly write persistent flash-backed parameters during schedule execution when a documented volatile or temporary setpoint mechanism is available.

Never guess a Meerstetter parameter ID. Use the official protocol documentation or a verified existing implementation.

### Completion and interruption behaviour

A schedule must define what happens:

- after the final stage;
- after a timeout;
- after loss of communication;
- after invalid sensor data;
- after a controller error; and
- after `Ctrl+C`.

Do not use ambiguous actions such as `stop` without defining whether this means:

- stop schedule timing;
- hold the current setpoint;
- return to a configured safe target;
- revert to the stored target; or
- disable TEC output.

Disabling the output is physically different from stopping the Python program and should require an explicit command or configuration.

## Temperature schedules

A temperature schedule should be stored in a human-readable configuration file such as YAML.

A stage should support, where relevant:

- stage name;
- target temperature;
- temperature tolerance;
- required stable duration;
- settling timeout;
- hold duration;
- sampling interval;
- notes; and
- completion behaviour.

By default, interpret “hold at X °C for Y hours” as:

> reach X °C, remain within the specified tolerance for the required stable period, and then begin the Y-hour hold.

Do not start the hold timer immediately after changing the setpoint unless the schedule explicitly requests that behaviour.

Validate the entire schedule before controlling hardware. Reject:

- missing targets;
- non-finite values;
- impossible durations;
- targets outside configured limits;
- duplicate or ambiguous fields;
- unknown completion actions; and
- unsupported controller channels.

## Logging and provenance

Each experiment run should create a unique run directory, for example:

```text
runs/20260731_153000_temperature_cycle/
```

Where practical, save:

- the exact schedule used;
- effective controller and apparatus configuration;
- experiment metadata;
- TEC log;
- Moku log;
- Linien log or imported source reference;
- laboratory-temperature data;
- warnings and errors;
- start and end timestamps;
- software version or Git commit hash; and
- user notes.

Use machine-readable timestamps. Prefer storing:

- UTC timestamp;
- Europe/London local timestamp where useful; and
- elapsed seconds from run start.

Do not rely only on sample number for alignment.

Useful TEC log fields include:

- `timestamp_utc`;
- `timestamp_local`;
- `elapsed_s`;
- `stage_index`;
- `stage_name`;
- `schedule_state`;
- `requested_target_c`;
- `active_target_c`;
- `object_temperature_c`;
- `sink_temperature_c`;
- `output_current_a`;
- `output_voltage_v`;
- `temperature_stable`;
- `controller_status`; and
- `error_message`.

Do not silently discard failed reads. Record missing data and the associated error.

## Time handling

The laboratory operates in the `Europe/London` timezone.

- Make timezone handling explicit.
- Account for daylight-saving time.
- Avoid mixing timezone-aware and naive datetimes without deliberate conversion.
- Preserve original timestamps when importing historical files.
- Document any assumptions made when a source file has no timezone information.
- Align TEC, Moku, Linien, and laboratory-temperature data by wall-clock time, not only by row index.

## Data integrity

- Never overwrite raw experimental data by default.
- Treat raw logs as immutable.
- Write cleaned, aligned, or derived data to new files.
- Preserve column names from historical datasets during loading, then map them to canonical internal names.
- Record units in names, metadata, or schemas.
- Do not interpolate across long gaps without warning.
- Do not hide dropped rows, clock discontinuities, duplicated timestamps, or parsing failures.
- Distinguish absent measurements from real zero values.
- Keep detector gain, attenuation, and calibration changes visible in metadata.

Large experimental datasets should not be committed to the public repository. Include only a small, non-sensitive example dataset sufficient to demonstrate the analysis.

## Analysis requirements

Where supported by the data, analyse:

- lock-point voltage drift;
- minimum optical-power drift;
- high-level or offset-level optical-power drift;
- extinction ratio;
- drift rate;
- RMS variation;
- settling time after temperature changes;
- correlation with EOM temperature;
- correlation with laboratory temperature;
- time-lagged relationships;
- repeatability across runs;
- differences between TEC-off and TEC-stabilised conditions; and
- dependence on thermal or electrical history.

Do not claim causation from correlation alone.

Account for possible confounding effects such as:

- laser-power drift;
- fibre movement;
- polarisation changes;
- free-space alignment changes;
- photodiode gain changes;
- attenuation changes;
- detector saturation;
- different lock states;
- DC sweeps altering the EOM state;
- different RF pulse histories;
- temperature gradients between the thermistor and waveguide; and
- reconfiguration of the optical setup.

Data collected before and after major optical realignment should not automatically be treated as one continuous calibrated dataset.

## Plotting

Plots should:

- label axes and units;
- use readable timestamps;
- distinguish raw and processed data;
- mark temperature-stage boundaries;
- mark lock loss, read failures, and excluded intervals where relevant;
- avoid misleading dual axes unless clearly justified;
- avoid calling a measured offset level the true maximum;
- preserve enough resolution to inspect drift; and
- be reproducible from code and configuration.

Do not manually edit scientific plots in a way that cannot be reproduced from the repository.

## Code quality

Use:

- Python type hints;
- descriptive names;
- docstrings for public interfaces;
- `pathlib.Path` rather than fragile string path construction;
- the `logging` module rather than scattered `print` calls for reusable code;
- context managers where appropriate;
- specific exceptions;
- explicit units;
- configuration files instead of hard-coded laboratory paths; and
- dependency injection for hardware interfaces.

Avoid:

- bare `except`;
- silent exception handling;
- hidden global mutable state;
- hard-coded user directories;
- hard-coded COM ports in reusable modules;
- unexplained magic numbers;
- duplicate timestamp-parsing code;
- mixing plotting with hardware acquisition;
- destructive file writes; and
- broad refactors unrelated to the requested task.

Keep compatibility with Windows and PowerShell because the current laboratory computer uses Windows and VS Code.

## Tests

All automated tests must use fake, mocked, recorded, or simulated devices.

At minimum, test:

- schedule parsing;
- schedule validation;
- target-range validation;
- settling detection;
- settling timeout;
- hold timing;
- stage transitions;
- invalid sensor values;
- communication failures;
- `Ctrl+C` or cancellation handling where practical;
- output file creation;
- timestamp handling;
- historical CSV parsing; and
- alignment of data with different sampling intervals.

Tests must not require:

- a connected TEC;
- a connected Moku;
- a Red Pitaya;
- an optical setup;
- a specific COM port; or
- laboratory network access.

When fixing a bug, add a regression test where practical.

## Dependencies

Keep dependencies minimal and documented.

Use established libraries where they add clear value, but avoid adding a dependency for trivial functionality.

When adding or changing a dependency:

1. explain why it is needed;
2. update the relevant project configuration;
3. update setup instructions;
4. consider Windows compatibility; and
5. run the available tests.

Do not assume an internet connection during experiment execution.

## Documentation

The public repository should eventually document:

- project objective;
- apparatus and signal flow;
- mechanical and thermal arrangement;
- thermistor placement;
- Peltier orientation;
- heatsink arrangement;
- electrical connections;
- controller configuration;
- software installation;
- configuration files;
- schedule format;
- running an experiment;
- data formats;
- analysis workflow;
- safety limitations;
- troubleshooting;
- reproducibility limitations; and
- the distinction between results specific to this apparatus and claims that may generalise.

Do not publish passwords, private notes, unpublished confidential information, personal data, access tokens, or unnecessary device identifiers.

Do not imply that another user can safely copy the current PID values, current limits, voltage limits, or temperature limits without checking their own hardware.

## Git and change management

- Check `git status` before and after meaningful work.
- Do not modify Git history.
- Do not force-push.
- Do not delete branches.
- Do not run destructive commands such as `git reset --hard`, `git clean -fd`, or broad file deletion unless explicitly instructed.
- Do not commit private planning files or raw datasets.
- Keep changes focused.
- Prefer one logical change per commit.
- Do not commit automatically unless the user asks.
- Summarise changed files and tests run at the end of each task.

Before suggesting that the repository is ready to become public, check for:

- secrets and credentials;
- personal file paths;
- private planning notes;
- raw or confidential data;
- large files;
- unnecessary IP addresses;
- unnecessary serial numbers; and
- files unintentionally retained in Git history.

## Working procedure for agents

For each task:

1. Read this file and the relevant repository documentation.
2. Inspect the affected files before proposing changes.
3. Identify uncertainties and safety implications.
4. Make the smallest coherent change.
5. Use fake hardware for tests.
6. Run the relevant tests or checks.
7. Review the diff.
8. Report:
   - files changed;
   - behaviour changed;
   - assumptions made;
   - tests run and results;
   - unresolved risks; and
   - any real-hardware action still requiring user approval.

When a task could affect real equipment, stop at a safe simulation or read-only boundary unless real hardware control was explicitly requested.
