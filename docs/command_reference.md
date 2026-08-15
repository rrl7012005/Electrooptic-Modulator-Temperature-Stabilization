# Command reference

This page explains the public command-line arguments in the repository root.
Commands are shown for PowerShell opened in the repository directory.

For new experiments, use the versioned configuration path:

```powershell
python run_experiment.py --config configs/experiment.yaml --dry-run
python run_experiment.py --config configs/experiment.yaml --preview
python run_experiment.py --config configs/experiment.yaml --execute
```

Dry-run and preview do not connect to hardware. `--execute`, direct legacy
commands, and emergency output-disable commands can access real equipment.
Never run two programs that both try to own the same Moku or TEC connection.

## `run_experiment.py`

This is the main entry point. It supports the recommended configured workflow
and the retained legacy preset workflow.

### Configured workflow arguments

| Argument | Value | Meaning |
| --- | --- | --- |
| `--config` | `EXPERIMENT_YAML` | Load a master experiment YAML file and every referenced settings, temperature, pulse, and CSV file. Paths inside YAML are resolved relative to the YAML file containing them. This mode defaults to dry-run. |
| `--dry-run` | none | Validate and compile the entire experiment, print the effective plan, and stop. It creates no run/preview directory and imports no hardware SDK. This is the default when `--config` or a configured `--resume` is supplied without another action. |
| `--preview` | none | Perform the same hardware-free validation, then write an immutable effective plan, LUT data, waveform plots, hashes, and copied inputs under `previews/`. |
| `--execute` | none | Validate, create a run snapshot, check hardware-readiness fields, request the explicit word `EXECUTE`, and only then import hardware-facing code. |
| `--resume` | previous run directory or `experiment_manifest.json` | Validate and resume a specific configured run from its immutable snapshot and checkpoint. It does not reload edited live source YAML. Without an action it only dry-runs the resume plan. |
| `--yes` | none | Retained for legacy compatibility. It does **not** bypass the configured `EXECUTE` or `RESUME` confirmation. |

`--dry-run`, `--preview`, and `--execute` are mutually exclusive.
`--preview` and `--execute` require `--config` or `--resume`. `--config` cannot
be combined with a legacy preset, `--components`, `--resume-latest`, or
`--resume`.

Examples:

```powershell
# Validate only: no artifacts, SDK import, or hardware connection.
python run_experiment.py --config configs/experiment.yaml --dry-run

# Save the achieved LUTs and fully expanded temperature stages.
python run_experiment.py --config configs/experiment.yaml --preview

# Check a stopped run's checkpoint without resuming hardware.
python run_experiment.py --resume runs/20260813_120000_example --dry-run

# Save a human-reviewable resume plan without hardware.
python run_experiment.py --resume runs/20260813_120000_example --preview

# Request real resume; the program still requires typing RESUME.
python run_experiment.py --resume runs/20260813_120000_example --execute
```

### Legacy preset arguments

These arguments start the older root-level scripts whose apparatus settings
are partly defined in Python. They remain for compatibility; they are not a
replacement for versioned YAML.

| Argument | Value | Meaning |
| --- | --- | --- |
| positional `mode` | `full`, `both`, `temperature`, `temperature-lock`, `drift`, or `temperature-log` | Select a predefined component set. Omit it, all selection/resume arguments, and `--config` to open the interactive menu. `both` is an alias of `full`. |
| `--components` | one or more component names | Run a custom set chosen from `lock`, `moku`, `temp-control`, and `temp-log`. If both temperature components are requested, `temp-log` is removed because active temperature control already logs TEC data. |
| `--resume-latest` | none | Resume the newest readable legacy master manifest. It is not used for configured immutable-snapshot resume. |
| `--resume` | previous run directory or manifest | Resume one specific legacy manifest. The same option routes to configured resume when the manifest declares the configured format. |
| `--resume-warning-minutes` | number; default 30 | For legacy resume, warn and require acknowledgement when last activity is older than this many minutes. Use a non-negative value. The parser accepts a float but does not prove that the optical or thermal state is unchanged. |
| `--yes` | none | Skip the legacy `START` confirmation. It does not skip a stale-resume acknowledgement or configured confirmation. |
| `--dry-run` | none | Resolve and validate the legacy plan without launching experiment hardware. |

Legacy preset contents are:

| Preset | Components |
| --- | --- |
| `full` or `both` | Linien lock logger, Moku acquisition, active temperature schedule |
| `temperature` | Active temperature schedule only |
| `temperature-lock` | Linien lock logger and active temperature schedule |
| `drift` | Passive TEC logger, Linien lock logger, Moku acquisition |
| `temperature-log` | Passive TEC logger only |

Component meanings are:

| Component | Program and behavior |
| --- | --- |
| `lock` | `linien_logger.py`; logs and recovers the Linien connection. |
| `moku` | `collect_data.py`; generates the legacy fixed pulse and records photodiode data. |
| `temp-control` | `tec_temperature_controller.py`; advances the legacy Python-defined TEC schedule and logs it. |
| `temp-log` | `tec_temp_logger.py --mode temperature_and_peltier`; reads temperature, current, and voltage without advancing targets. |

Example:

```powershell
python run_experiment.py --components lock moku --dry-run
```

## `collect_data.py`

With `--config`, this is another entry point to the shared configured runtime.
With no arguments, it runs the older fixed-pulse acquisition workflow and can
connect to the Moku.

| Argument | Meaning |
| --- | --- |
| `--config EXPERIMENT_YAML` | Use the shared configured compiler, Moku Multi-Instrument runtime, and acquisition path. Defaults to dry-run. |
| `--dry-run` | With `--config`, validate and print without hardware. It is rejected without `--config`. |
| `--preview` | With `--config`, save effective plans and waveform previews without hardware. |
| `--execute` | With `--config`, request the confirmed real configured run. |
| `--print-experiment-length` | Print the duration used by the no-config legacy acquisition and exit. It cannot be combined with another action. The legacy master runner uses it when deciding which component leads. |
| `--disable-output` | Connect to the Moku, request output disable, and exit. This is an immediate hardware action and cannot be combined with any other argument. |

Examples:

```powershell
python collect_data.py --config configs/experiment.yaml --dry-run
python collect_data.py --print-experiment-length
```

Running `python collect_data.py` with no arguments is a real-hardware legacy
operation, not a dry-run.

## `pulse_control.py`

This script retains a standalone legacy pulse generator and also routes
configured requests to the shared runtime.

| Argument | Meaning |
| --- | --- |
| `--config EXPERIMENT_YAML` | Use the shared configured runtime; defaults to dry-run. It cannot be combined with legacy `--mode` or `--save-preview`. |
| `--dry-run` | With a config, validate the shared plan. Without a config, validate and print the selected legacy waveform plan without importing Moku. |
| `--preview` | With a config, save the shared effective plan and LUT previews. It requires `--config`. |
| `--execute` | With a config, request the confirmed shared Moku/acquisition runtime. It requires `--config`. |
| `--mode traditional` | Select the legacy single traditional pulse, overriding the `PULSE_MODE` constant for this invocation. |
| `--mode custom` | Select the legacy custom sequence, overriding `PULSE_MODE`. |
| `--save-preview FOLDER` | Save legacy requested-waveform previews to this folder. This option does not stop later execution by itself; combine it with legacy `--dry-run` to guarantee no hardware connection. |
| `--yes` | Skip the legacy request to type `START`. It does not bypass configured confirmation. |

Safe legacy preview example:

```powershell
python pulse_control.py --mode custom --save-preview previews/legacy_custom --dry-run
```

Without `--config` or `--dry-run`, this script may enable Moku output.

## `tec_temperature_controller.py`

This is the retained standalone controller for the Python-defined legacy
temperature schedule. New versioned schedules should be run through
`run_experiment.py --config ...`.

| Argument | Meaning |
| --- | --- |
| `--dry-run` | Validate and print the legacy configured schedule, including resume calculations, without opening a TEC connection. |
| `--print-schedule-json` | Print the validated expanded legacy schedule as JSON and exit without connecting. It cannot be combined with `--dry-run` or `--resume-from`. |
| `--resume-from CSV` | Continue the legacy schedule from an existing scheduled-control CSV. The program validates the log and determines remaining steps; it does not prove the physical state is unchanged. |
| `--yes` | Skip the legacy request to type `START`. |
| `--disable-output` | Immediately connect, request TEC output disable, verify the disabled state, and exit. This is a real hardware action and cannot be combined with another action. |

Safe inspection examples:

```powershell
python tec_temperature_controller.py --dry-run
python tec_temperature_controller.py --print-schedule-json
python tec_temperature_controller.py --resume-from "Experiment Results/run_x/TEC_logs/tec_temperature_control.csv" --dry-run
```

Running the script without `--dry-run` or `--print-schedule-json` can change the
TEC target and enable output after confirmation.

## `tec_temp_logger.py`

This legacy program opens the TEC serial connection for read-only logging. It
does not advance a target schedule.

| Argument | Meaning |
| --- | --- |
| `--mode temperature_only` | Log object temperature. |
| `--mode temperature_and_peltier` | Log object temperature plus available Peltier current and voltage. |

If `--mode` is omitted, the `LOG_MODE` constant decides whether to use a mode or
show an interactive menu. This script imports the MeCom package at startup and
uses the legacy apparatus connection configured in its source file.

## `analyse_eom_csv.py`

This command reads Moku reduced data, applies an explicit analysis profile,
writes cleaned/derived tables and a summary, and creates plots. It never
controls hardware and does not overwrite the input CSV.

| Argument | Meaning |
| --- | --- |
| positional `csv_path` | Exact Moku CSV to analyse. If omitted, use `CSV_PATH` when defined, otherwise the newest discoverable Moku pulse-run CSV. Supplying the path is more reproducible. |
| `--output-dir FOLDER` | Plot destination. Default: `plots/final` beside the CSV, or `plots/in_progress` with `--in-progress`. |
| `--analysis-dir FOLDER` | Destination for cleaned CSVs and the text/JSON summary. Default: beside the input CSV. |
| `--events-path FILE` | Acquisition JSONL event log used to mark outages and failures. Default: `acquisition_events.jsonl` beside the input CSV when present. |
| `--provenance-path FILE` | Exact `moku_sample_provenance.csv` sidecar. By default the analyser uses the matching file beside the measurement CSV when present. Rows, stable sample IDs, and timestamps must align exactly; mismatches stop analysis. |
| `--show-temperature-boundaries` / `--no-show-temperature-boundaries` | Show or hide subtle temperature-stage lines on every time-series plot. Shown by default. |
| `--show-waveform-boundaries` / `--no-show-waveform-boundaries` | Show or hide independently derived action/waveform lines. Shown by default. |
| `--show-session-boundaries` / `--no-show-session-boundaries` | Show or hide dashed reconnect/resume-session lines. Hidden by default. Session facts remain a separate layer even if one occurs at the same timestamp as an action change. |
| `--annotate-temperature-labels` / `--no-annotate-temperature-labels` | Show or hide a bounded, alternating set of stage labels near the top of each time-series plot. Shown by default. |
| `--annotate-waveform-labels` / `--no-annotate-waveform-labels` | Show or hide a bounded, alternating set of action/waveform labels. Hidden by default to keep dense plots readable. |
| `--analysis-profile FILE` | Strict analysis-profile JSON. If omitted, auto-discover `analysis_profile.json` beside the CSV or in its run directory, then use documented historical defaults if none exists. |
| `--no-show` | Save figures without opening interactive plot windows. Use this in automated or remote runs. |
| `--in-progress` | Mark plots and summaries as unfinished snapshots. This prevents a growing file from looking like a final result. |
| `--dark-offset-v VOLTS` | Override the independently calibrated detector dark offset. This affects extinction calculation; it is not estimated from the measurement file. |
| `--min-high-level-v VOLTS` | Reject high/offset-level values at or below this threshold. Alias: `--minimum-high-level-v`. |
| `--no-high-level-filter` | Disable the optional high/offset threshold. Alias: `--disable-high-level-filter`. It conflicts with `--min-high-level-v`. |
| `--max-minimum-v VOLTS` | Reject minimum values at or above this threshold. Alias: `--maximum-minimum-v`. |
| `--no-minimum-filter` | Disable the optional minimum threshold. Alias: `--disable-minimum-filter`. It conflicts with `--max-minimum-v`. |
| `--min-sample-count COUNT` | Require at least this many complete post-filter samples; default is 10 unless the selected profile says otherwise. Alias: `--minimum-sample-count`. |

Example:

```powershell
python analyse_eom_csv.py "runs/example/moku/measurements.csv" `
  --analysis-profile "runs/example/config/analysis_profile.json" `
  --analysis-dir "runs/example/analysis" `
  --output-dir "runs/example/plots/final" `
  --no-show
```

When the provenance sidecar is present, the cleaned analysis CSV retains its
temperature-stage, Moku action, waveform, session, and stable regime columns.
The analyser gets boundaries from those saved facts, never by guessing from a
step in measured voltage. Repeated names remain distinct because action/stage
indices and exact transition timestamps are retained. Rolling lines restart at
regime changes so a moving average does not bridge two experimental settings.

The historical column named `maximum` is treated as a compatibility input. New
outputs use `high_level` or another scientifically accurate role; the program
does not claim it is the transfer-curve maximum.

## Plot-only commands

`plot_control.py` plots Linien lock voltage. `plot_temp_log.py` plots object
temperature and any available TEC current/voltage columns. Neither controls
hardware or modifies the source CSV.

`plot_temp_log.py` accepts both legacy TEC headers and the configured-v2
`temperature/tec_log.csv` schema. Configured execution invokes these plotters
automatically according to `run_settings.monitoring`; direct commands remain
available for reproducible re-plotting.

Both accept the same argument pattern:

| Argument | Meaning |
| --- | --- |
| positional `csv_path` | Exact source CSV. If omitted, find the newest compatible log under the legacy results tree. An explicit path is safer for reproducibility. |
| `--output-dir FOLDER` | Destination for PNG files. If omitted, use the script's final-plot folder convention. |
| `--no-show` | Save without opening an interactive Matplotlib window. |
| `--in-progress` | Label the result as an unfinished live snapshot and use in-progress naming/metadata. |

Examples:

```powershell
python plot_control.py "runs/example/linien/control.csv" --no-show
python plot_temp_log.py "runs/example/temperature/tec_log.csv" --no-show --in-progress
```

## Other root scripts without command-line options

These scripts do not use command-line arguments. Their behavior comes from
constants in the source and, where noted, environment variables. Passing an
unrecognised argument does not configure them safely.

| Command | Hardware access | Meaning |
| --- | --- | --- |
| `python linien_logger.py` | Yes | Connect to the configured Red Pitaya/Linien server, require an existing initial lock, and log until interrupted. `LINIEN_PASSWORD` is required; `LINIEN_HOST` can override the legacy host. Mid-run reconnect/relock behavior is described in the tutorial. |
| `python measure_dark_offset.py` | Yes | Directly connect to the Moku Oscilloscope and measure blocked-photodiode DC offset using constants in the file. Block the photodiode and verify identical frontend settings before running it. It has no dry-run flag. |
| `python plot_all_temp.py` | No | Find the newest laboratory and TEC logs under the legacy results tree, combine them, save a final PNG, and open a plot window. File selection and annotations come from source constants. |
| `python plot_lab_temp.py` | No | Find the newest `Lab_Temp.csv` under the legacy results tree, plot its first three sensor columns, save a PNG, and open a plot window. |

The no-argument hardware scripts are compatibility tools. They are not
versioned configuration entry points and should not be assumed to inherit the
settings in an experiment YAML file.

## Help and exit status

Append `--help` to any command using `argparse` to see the installed program's
current syntax, for example:

```powershell
python run_experiment.py --help
python analyse_eom_csv.py --help
```

A successful validation or completed command returns exit status 0. Invalid
command combinations normally return 2. An operator interruption can return
130. Hardware/runtime failures can return another non-zero status and must be
read together with the run manifest and event logs.
