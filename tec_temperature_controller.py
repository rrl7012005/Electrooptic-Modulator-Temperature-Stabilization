"""Run a timed temperature programme on a Meerstetter TEC-1091.

Edit the temperature-schedule settings, preview them with

    python tec_temperature_controller.py --dry-run

and run it with

    python tec_temperature_controller.py

The Meerstetter configuration software and tec_temp_logger.py must be closed
because only one program can use the serial port at a time.
"""

import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SRC_DIRECTORY = Path(__file__).resolve().parent / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.output_layout import (
    append_output_requested,
    component_output_file,
    resolve_component_directory,
)


# ---------------- USER SETTINGS ----------------

COM_PORT = "COM10"

# "manual" uses TEMPERATURE_SCHEDULE exactly as written below. "generated"
# builds a gradual forward sweep and, optionally, one reverse sweep.
TEMPERATURE_SCHEDULE_MODE = "generated"

# Generated-schedule settings. These are ignored in manual mode. Measurement
# temperatures receive the long experimental hold. Smaller transition
# increments approach each measurement temperature gradually.
INITIAL_TEC_OFF_HOLD_MINUTES = 360.0
START_TEMPERATURE_C = 25.0
FINISH_TEMPERATURE_C = 30.0
MEASUREMENT_TEMPERATURE_INTERVAL_C = 5.0
TRANSITION_TEMPERATURE_INCREMENT_C = 2.5
TRANSITION_HOLD_MINUTES = 10.0
MEASUREMENT_HOLD_MINUTES = 360.0

# True performs one forward and one reverse measurement sweep without
# repeating the finish endpoint. False performs only the forward sweep.
INCLUDE_REVERSE_SWEEP = True

# Each manual entry is: (target temperature in degC, hold duration in minutes).
# Use None instead of a temperature for a period with the TEC output OFF:
#     (None, 10.0),  # no temperature lock for 10 minutes
# Add, remove, or reorder as many steps as required.
TEMPERATURE_SCHEDULE = [
    (25.0, 5.0),
    (30.0, 5.0),
    (25.0, 5.0),
]

# True means each duration starts only after the TEC reports that the target
# is stable. False means it starts as soon as the new setpoint is sent.
WAIT_UNTIL_STABLE = True
STABILITY_TIMEOUT_MINUTES = 30.0

SAMPLE_INTERVAL = 5.0  # seconds

# Software guardrails. The TEC's configured current, voltage, and temperature
# limits remain the primary hardware protection.
MIN_ALLOWED_TARGET_C = 10.0
MAX_ALLOWED_TARGET_C = 50.0

# TODO: Add independently verified apparatus-specific plausible ranges for
# measured object and sink temperatures before enforcing numeric preflight
# bounds. Do not assume the allowed target range is also a valid sink range.

# "auto" supports current firmware v6 and legacy firmware v5 or earlier.
# Override with "v6" or "legacy" only if automatic detection is unsuccessful.
FIRMWARE_MODE = "auto"

# The safest default is to turn the output off after the last step, on Ctrl+C,
# or after an error. Set this to False only if the final temperature must remain
# controlled after this program exits.
TURN_OUTPUT_OFF_AT_END = True

# Require the operator to review the programme and type START before power is
# enabled. The --yes command-line option deliberately bypasses this prompt.
REQUIRE_START_CONFIRMATION = True

# Meerstetter TEC-1091 has one temperature-control channel.
TEC_CHANNEL = 1

UK_TIME = ZoneInfo("Europe/London")

# ------------------------------------------------


# Meerstetter TEC-family parameter IDs.
DEVICE_STATUS_ID = 104
FIRMWARE_VERSION_ID = 112
OBJECT_TEMPERATURE_ID = 1000
SINK_TEMPERATURE_ID = 1001
OUTPUT_CURRENT_ID = 1020
OUTPUT_VOLTAGE_ID = 1021
TEMPERATURE_STABLE_ID = 1200
OUTPUT_STAGE_INPUT_ID = 2000
OUTPUT_ENABLE_ID = 2010
TARGET_TEMPERATURE_ID = 3000


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Run the temperature steps configured in "
            "TEMPERATURE_SCHEDULE."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and display the schedule without connecting",
    )
    parser.add_argument(
        "--disable-output",
        action="store_true",
        help="immediately disable the TEC output and exit",
    )
    parser.add_argument(
        "--resume-from",
        type=Path,
        metavar="CSV",
        help="continue the configured schedule from a previous control CSV",
    )
    parser.add_argument(
        "--print-schedule-json",
        action="store_true",
        help="print the validated schedule as JSON without connecting",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="start without requiring the START confirmation",
    )
    args = parser.parse_args()

    if args.disable_output and (
        args.dry_run
        or args.resume_from is not None
        or args.print_schedule_json
    ):
        parser.error(
            "--disable-output cannot be combined with another action"
        )
    if args.print_schedule_json and (args.dry_run or args.resume_from is not None):
        parser.error(
            "--print-schedule-json cannot be combined with --dry-run or "
            "--resume-from"
        )

    return args


def format_duration(total_seconds: float) -> str:
    """Return a compact human-readable duration."""

    rounded_seconds = int(round(total_seconds))
    hours, remainder = divmod(rounded_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []

    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if seconds or not parts:
        parts.append(f"{seconds} s")

    return " ".join(parts)


def generate_temperature_schedule(
    start_temperature_c,
    finish_temperature_c,
    measurement_temperature_interval_c,
    transition_temperature_increment_c,
    transition_hold_minutes,
    measurement_hold_minutes,
    include_reverse_sweep,
    initial_tec_off_hold_minutes=None,
):
    """Build gradual transitions between experimental hold temperatures."""

    try:
        start_temperature_c = float(start_temperature_c)
        finish_temperature_c = float(finish_temperature_c)
        measurement_temperature_interval_c = float(
            measurement_temperature_interval_c
        )
        transition_temperature_increment_c = float(
            transition_temperature_increment_c
        )
        transition_hold_minutes = float(transition_hold_minutes)
        measurement_hold_minutes = float(measurement_hold_minutes)
        if initial_tec_off_hold_minutes is not None:
            initial_tec_off_hold_minutes = float(
                initial_tec_off_hold_minutes
            )
    except (TypeError, ValueError) as error:
        raise ValueError(
            "Generated temperatures and durations must be numeric."
        ) from error

    if not math.isfinite(start_temperature_c) or not math.isfinite(
        finish_temperature_c
    ):
        raise ValueError("Generated start and finish temperatures must be finite.")
    if math.isclose(
        start_temperature_c,
        finish_temperature_c,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError(
            "Generated start and finish temperatures must be different."
        )
    if (
        not math.isfinite(measurement_temperature_interval_c)
        or measurement_temperature_interval_c <= 0
    ):
        raise ValueError(
            "MEASUREMENT_TEMPERATURE_INTERVAL_C must be above zero."
        )
    if (
        not math.isfinite(transition_temperature_increment_c)
        or transition_temperature_increment_c <= 0
    ):
        raise ValueError(
            "TRANSITION_TEMPERATURE_INCREMENT_C must be above zero."
        )
    if (
        not math.isfinite(transition_hold_minutes)
        or transition_hold_minutes <= 0
    ):
        raise ValueError("TRANSITION_HOLD_MINUTES must be above zero.")
    if (
        not math.isfinite(measurement_hold_minutes)
        or measurement_hold_minutes <= 0
    ):
        raise ValueError("MEASUREMENT_HOLD_MINUTES must be above zero.")
    if not isinstance(include_reverse_sweep, bool):
        raise ValueError("INCLUDE_REVERSE_SWEEP must be True or False.")
    if initial_tec_off_hold_minutes is not None and (
        not math.isfinite(initial_tec_off_hold_minutes)
        or initial_tec_off_hold_minutes <= 0
    ):
        raise ValueError(
            "INITIAL_TEC_OFF_HOLD_MINUTES must be above zero or None."
        )

    def temperatures_between(start_c, finish_c, maximum_increment_c):
        """Return an inclusive path with no increment above the maximum."""

        direction = 1.0 if finish_c > start_c else -1.0
        values = [start_c]
        next_temperature_c = start_c + direction * maximum_increment_c
        if direction > 0:
            while next_temperature_c < finish_c - 1e-12:
                values.append(next_temperature_c)
                next_temperature_c += maximum_increment_c
        else:
            while next_temperature_c > finish_c + 1e-12:
                values.append(next_temperature_c)
                next_temperature_c -= maximum_increment_c
        values.append(finish_c)
        return values

    forward_measurement_temperatures = temperatures_between(
        start_temperature_c,
        finish_temperature_c,
        measurement_temperature_interval_c,
    )

    if include_reverse_sweep:
        measurement_temperatures = (
            forward_measurement_temperatures
            + forward_measurement_temperatures[-2::-1]
        )
    else:
        measurement_temperatures = forward_measurement_temperatures

    schedule = []
    if initial_tec_off_hold_minutes is not None:
        schedule.append((None, initial_tec_off_hold_minutes))
    schedule.append(
        (measurement_temperatures[0], measurement_hold_minutes)
    )
    previous_measurement_temperature_c = measurement_temperatures[0]
    for measurement_temperature_c in measurement_temperatures[1:]:
        transition_temperatures = temperatures_between(
            previous_measurement_temperature_c,
            measurement_temperature_c,
            transition_temperature_increment_c,
        )
        schedule.extend(
            (temperature_c, transition_hold_minutes)
            for temperature_c in transition_temperatures[1:-1]
        )
        schedule.append(
            (measurement_temperature_c, measurement_hold_minutes)
        )
        previous_measurement_temperature_c = measurement_temperature_c

    return schedule


def get_configured_temperature_schedule():
    """Return either the manual schedule or the generated sweep schedule."""

    mode = str(TEMPERATURE_SCHEDULE_MODE).strip().lower()
    if mode == "manual":
        return TEMPERATURE_SCHEDULE
    if mode == "generated":
        return generate_temperature_schedule(
            START_TEMPERATURE_C,
            FINISH_TEMPERATURE_C,
            MEASUREMENT_TEMPERATURE_INTERVAL_C,
            TRANSITION_TEMPERATURE_INCREMENT_C,
            TRANSITION_HOLD_MINUTES,
            MEASUREMENT_HOLD_MINUTES,
            INCLUDE_REVERSE_SWEEP,
            INITIAL_TEC_OFF_HOLD_MINUTES,
        )
    raise ValueError(
        "TEMPERATURE_SCHEDULE_MODE must be 'manual' or 'generated'."
    )


def validate_schedule(schedule):
    """Validate settings and return normalized schedule pairs."""

    if FIRMWARE_MODE not in {"auto", "v6", "legacy"}:
        raise ValueError(
            "FIRMWARE_MODE must be 'auto', 'v6', or 'legacy'."
        )

    if not math.isfinite(SAMPLE_INTERVAL) or SAMPLE_INTERVAL <= 0:
        raise ValueError("SAMPLE_INTERVAL must be greater than zero.")

    if MIN_ALLOWED_TARGET_C >= MAX_ALLOWED_TARGET_C:
        raise ValueError(
            "MIN_ALLOWED_TARGET_C must be below MAX_ALLOWED_TARGET_C."
        )

    if WAIT_UNTIL_STABLE and (
        not math.isfinite(STABILITY_TIMEOUT_MINUTES)
        or STABILITY_TIMEOUT_MINUTES <= 0
    ):
        raise ValueError(
            "STABILITY_TIMEOUT_MINUTES must be greater than zero."
        )

    if not schedule:
        raise ValueError("TEMPERATURE_SCHEDULE must contain at least one step.")

    normalized_schedule = []

    for step_number, step in enumerate(schedule, start=1):
        if not isinstance(step, (tuple, list)) or len(step) != 2:
            raise ValueError(
                f"Schedule step {step_number} must be "
                "(temperature_C, duration_minutes)."
            )

        try:
            target_temperature = (
                None if step[0] is None else float(step[0])
            )
            duration_minutes = float(step[1])
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Schedule step {step_number} contains a non-numeric value."
            ) from error

        if (
            target_temperature is not None
            and not math.isfinite(target_temperature)
        ):
            raise ValueError(
                f"Step {step_number} target temperature is not finite."
            )

        if (
            target_temperature is not None
            and not (
                MIN_ALLOWED_TARGET_C
                <= target_temperature
                <= MAX_ALLOWED_TARGET_C
            )
        ):
            raise ValueError(
                f"Step {step_number} target {target_temperature:g} degC is "
                f"outside the allowed {MIN_ALLOWED_TARGET_C:g} to "
                f"{MAX_ALLOWED_TARGET_C:g} degC range."
            )

        if not math.isfinite(duration_minutes) or duration_minutes <= 0:
            raise ValueError(
                f"Step {step_number} duration must be greater than zero."
            )

        normalized_schedule.append(
            (target_temperature, duration_minutes * 60.0)
        )

    return normalized_schedule


def print_schedule(schedule):
    total_hold_seconds = sum(duration for _, duration in schedule)

    print("\nTemperature programme:")

    for step_number, (target, duration_seconds) in enumerate(
        schedule,
        start=1,
    ):
        if target is None:
            description = "TEC output OFF"
        else:
            description = f"{target:.3f} degC"

        print(
            f"  {step_number}: {description} for "
            f"{format_duration(duration_seconds)}"
        )

    timer_description = (
        "after the TEC reports stable"
        if WAIT_UNTIL_STABLE
        else "when each setpoint is sent"
    )

    print(f"\nTemperature-step timers start {timer_description}.")
    print("Output-off step timers start immediately.")
    print(
        "Minimum programme time: "
        f"{format_duration(total_hold_seconds)}"
    )
    print(
        "Output at the end: "
        + ("OFF" if TURN_OUTPUT_OFF_AT_END else "left ON")
    )


def schedule_as_dict(schedule):
    schedule_mode = str(TEMPERATURE_SCHEDULE_MODE).strip().lower()
    result = {
        "schedule_mode": schedule_mode,
        "steps": [
            {
                "target_temperature_C": target,
                "hold_seconds": duration_seconds,
            }
            for target, duration_seconds in schedule
        ],
        "wait_until_stable": WAIT_UNTIL_STABLE,
        "stability_timeout_minutes": STABILITY_TIMEOUT_MINUTES,
        "turn_output_off_at_end": TURN_OUTPUT_OFF_AT_END,
    }

    if schedule_mode == "generated":
        result["generated_schedule_settings"] = {
            "initial_tec_off_hold_minutes": (
                INITIAL_TEC_OFF_HOLD_MINUTES
            ),
            "start_temperature_C": START_TEMPERATURE_C,
            "finish_temperature_C": FINISH_TEMPERATURE_C,
            "measurement_temperature_interval_C": (
                MEASUREMENT_TEMPERATURE_INTERVAL_C
            ),
            "transition_temperature_increment_C": (
                TRANSITION_TEMPERATURE_INCREMENT_C
            ),
            "transition_hold_minutes": TRANSITION_HOLD_MINUTES,
            "measurement_hold_minutes": MEASUREMENT_HOLD_MINUTES,
            "include_reverse_sweep": INCLUDE_REVERSE_SWEEP,
        }

    return result


def build_execution_steps(schedule, resume_from=None):
    """Return (original step number, target, hold seconds) to execute."""
    full_steps = [
        (step_number, target, hold_seconds)
        for step_number, (target, hold_seconds) in enumerate(
            schedule,
            start=1,
        )
    ]

    if resume_from is None:
        return full_steps

    resume_path = Path(resume_from).resolve()
    if not resume_path.is_file():
        raise FileNotFoundError(f"Resume CSV does not exist: {resume_path}")

    required_columns = {
        "step_number",
        "phase",
        "target_temperature_C",
        "step_remaining_s",
    }
    last_state = None

    with resume_path.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        missing_columns = required_columns - set(reader.fieldnames or [])
        if missing_columns:
            raise ValueError(
                "Resume CSV is missing columns: "
                + ", ".join(sorted(missing_columns))
            )

        for row in reader:
            try:
                step_number = int(row["step_number"])
            except (TypeError, ValueError):
                continue

            if row["phase"] in {
                "waiting_for_stability",
                "holding",
                "output_off",
            }:
                last_state = (step_number, row)

    if last_state is None:
        print(
            "\nResume CSV contains no completed control samples; "
            "the programme will restart at step 1."
        )
        return full_steps

    step_number, row = last_state
    if not 1 <= step_number <= len(schedule):
        raise ValueError(
            f"Resume CSV step {step_number} is outside the current schedule."
        )

    target_temperature, scheduled_hold_seconds = schedule[step_number - 1]
    logged_target_text = row["target_temperature_C"].strip()
    logged_target = None if logged_target_text == "" else float(logged_target_text)

    targets_match = (
        target_temperature is None
        and logged_target is None
    ) or (
        target_temperature is not None
        and logged_target is not None
        and math.isclose(target_temperature, logged_target, abs_tol=1e-6)
    )
    if not targets_match:
        raise ValueError(
            f"Current schedule step {step_number} does not match the resume "
            "CSV target. Restore the original schedule before resuming."
        )

    phase = row["phase"]
    if phase == "output_off" and target_temperature is not None:
        raise ValueError("Resume CSV phase does not match the current schedule.")
    if phase != "output_off" and target_temperature is None:
        raise ValueError("Resume CSV phase does not match the current schedule.")

    if phase == "waiting_for_stability":
        remaining_seconds = scheduled_hold_seconds
    else:
        try:
            remaining_seconds = float(row["step_remaining_s"])
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Resume CSV has no valid remaining time for its last step."
            ) from error

        if not math.isfinite(remaining_seconds) or remaining_seconds < 0:
            raise ValueError("Resume CSV contains an invalid remaining time.")
        if remaining_seconds > scheduled_hold_seconds + SAMPLE_INTERVAL:
            raise ValueError(
                "Resume CSV remaining time exceeds the current step duration."
            )
        remaining_seconds = min(remaining_seconds, scheduled_hold_seconds)

    start_index = step_number - 1
    if remaining_seconds == 0:
        start_index += 1

    if start_index >= len(schedule):
        raise ValueError("The previous temperature programme already completed.")

    execution_steps = full_steps[start_index:]
    if start_index == step_number - 1:
        _, first_target, _ = execution_steps[0]
        execution_steps[0] = (
            step_number,
            first_target,
            remaining_seconds,
        )

    first_step, first_target, first_duration = execution_steps[0]
    target_text = "OFF" if first_target is None else f"{first_target:.3f} degC"
    print(f"\nResuming from: {resume_path}")
    print(
        f"Resume point: step {first_step}, target {target_text}, "
        f"{format_duration(first_duration)} remaining."
    )
    print("Time while the programme was stopped is not counted as hold time.")

    return execution_steps


def read_parameter(session, address, parameter_id):
    """Read a parameter value stored by the TEC"""
    return session.get_parameter(
        parameter_id=parameter_id,
        address=address,
        parameter_instance=TEC_CHANNEL,
    )


def set_parameter(session, address, parameter_id, value):
    """Set a parameter on the TEC to a value"""
    acknowledged = session.set_parameter(
        parameter_id=parameter_id,
        value=value,
        address=address,
        parameter_instance=TEC_CHANNEL,
    )

    if not acknowledged:
        raise RuntimeError(
            f"TEC did not acknowledge parameter {parameter_id} = {value}."
        )


def detect_firmware_mode(session, address):
    """Return the compatible output-stage input value and a label."""

    if FIRMWARE_MODE == "v6":
        return 1, "v6 (manual override)"

    if FIRMWARE_MODE == "legacy":
        return 2, "legacy v5 or earlier (manual override)"

    if not hasattr(session, "get_parameter_raw"):
        raise RuntimeError(
            "This pyMeCom version cannot automatically detect the firmware. "
            "Set FIRMWARE_MODE to 'v6' or 'legacy' after checking the "
            "controller firmware."
        )

    try:
        firmware_version = float(
            session.get_parameter_raw(
                parameter_id=FIRMWARE_VERSION_ID,
                parameter_format="FLOAT32",
                address=address,
                parameter_instance=TEC_CHANNEL,
            )
        )
    except Exception:
        # Parameter 112 was introduced with TEC firmware v6.00. A controller
        # that does not provide it uses the legacy input-selection values.
        return 2, "legacy v5 or earlier (auto-detected)"

    if not math.isfinite(firmware_version):
        raise RuntimeError(
            f"TEC returned invalid firmware version {firmware_version!r}."
        )

    if firmware_version < 6.0:
        return 2, f"legacy firmware {firmware_version:g}"

    return 1, f"firmware {firmware_version:g}"


def verify_integer_parameter(
    session,
    address,
    parameter_id,
    expected_value,
    description,
):
    """Verify a parameter has the expected value"""
    actual_value = int(read_parameter(session, address, parameter_id))

    if actual_value != expected_value:
        raise RuntimeError(
            f"{description} verification failed: expected "
            f"{expected_value}, read back {actual_value}."
        )


def set_target_temperature(session, address, target_temperature):
    set_parameter(
        session,
        address,
        TARGET_TEMPERATURE_ID,
        float(target_temperature),
    )

    actual_target = float(
        read_parameter(session, address, TARGET_TEMPERATURE_ID)
    )

    if not math.isclose(
        actual_target,
        target_temperature,
        rel_tol=0.0,
        abs_tol=0.01,
    ):
        raise RuntimeError(
            "Target-temperature verification failed: requested "
            f"{target_temperature:.3f} degC, read back "
            f"{actual_target:.3f} degC."
        )


def read_measurements(session, address):
    measurements = {
        "object_temperature_C": float(
            read_parameter(session, address, OBJECT_TEMPERATURE_ID)
        ),
        "sink_temperature_C": float(
            read_parameter(session, address, SINK_TEMPERATURE_ID)
        ),
        "tec_output_current_A": float(
            read_parameter(session, address, OUTPUT_CURRENT_ID)
        ),
        "tec_output_voltage_V": float(
            read_parameter(session, address, OUTPUT_VOLTAGE_ID)
        ),
        "temperature_stable": int(
            read_parameter(session, address, TEMPERATURE_STABLE_ID)
        ),
        "device_status": int(
            read_parameter(session, address, DEVICE_STATUS_ID)
        ),
    }

    for name in (
        "object_temperature_C",
        "sink_temperature_C",
        "tec_output_current_A",
        "tec_output_voltage_V",
    ):
        if not math.isfinite(measurements[name]):
            raise RuntimeError(
                f"TEC returned a non-finite {name}: "
                f"{measurements[name]!r}."
            )

    return measurements


def write_measurement(
    writer,
    csv_file,
    programme_start,
    step_number,
    phase,
    target_temperature,
    phase_start,
    remaining_seconds,
    measurements,
    read_status="OK",
):
    now_monotonic = time.monotonic()

    row = {
        "wall_time": datetime.now(UK_TIME).isoformat(
            timespec="milliseconds"
        ),
        "elapsed_s": f"{now_monotonic - programme_start:.3f}",
        "step_number": step_number,
        "phase": phase,
        "target_temperature_C": (
            ""
            if target_temperature is None
            else f"{target_temperature:.6f}"
        ),
        "phase_elapsed_s": f"{now_monotonic - phase_start:.3f}",
        "step_remaining_s": (
            ""
            if remaining_seconds is None
            else f"{max(0.0, remaining_seconds):.3f}"
        ),
        "read_status": read_status,
    }

    if measurements:
        row.update({
            "object_temperature_C": (
                f"{measurements['object_temperature_C']:.6f}"
            ),
            "sink_temperature_C": (
                f"{measurements['sink_temperature_C']:.6f}"
            ),
            "tec_output_current_A": (
                f"{measurements['tec_output_current_A']:.6f}"
            ),
            "tec_output_voltage_V": (
                f"{measurements['tec_output_voltage_V']:.6f}"
            ),
            "temperature_stable": measurements["temperature_stable"],
            "device_status": measurements["device_status"],
        })

    writer.writerow(row)
    csv_file.flush()


def display_measurement(
    step_number,
    target_temperature,
    phase,
    measurements,
    remaining_seconds,
):
    remaining_text = (
        "waiting for stable"
        if remaining_seconds is None
        else f"{format_duration(remaining_seconds)} remaining"
    )

    target_text = (
        "OFF"
        if target_temperature is None
        else f"{target_temperature:.3f} degC"
    )

    print(
        f"{datetime.now(UK_TIME).isoformat(timespec='seconds')}  "
        f"step {step_number}  {phase}  "
        f"target={target_text}  "
        f"object={measurements['object_temperature_C']:.4f} degC  "
        f"sink={measurements['sink_temperature_C']:.4f} degC  "
        f"I={measurements['tec_output_current_A']:+.4f} A  "
        f"V={measurements['tec_output_voltage_V']:.4f} V  "
        f"{remaining_text}"
    )


def sample_phase(
    session,
    address,
    writer,
    csv_file,
    programme_start,
    step_number,
    phase,
    target_temperature,
    phase_start,
    remaining_seconds,
):
    try:
        measurements = read_measurements(session, address)
    except Exception as error:
        error_message = (
            f"{type(error).__name__}: {error}"
        ).replace("\n", " ")

        write_measurement(
            writer,
            csv_file,
            programme_start,
            step_number,
            phase,
            target_temperature,
            phase_start,
            remaining_seconds,
            measurements=None,
            read_status=error_message,
        )
        raise

    write_measurement(
        writer,
        csv_file,
        programme_start,
        step_number,
        phase,
        target_temperature,
        phase_start,
        remaining_seconds,
        measurements,
    )

    display_measurement(
        step_number,
        target_temperature,
        phase,
        measurements,
        remaining_seconds,
    )

    if measurements["device_status"] == 3:
        raise RuntimeError(
            "TEC entered its error state; the temperature programme was "
            "aborted. Check the controller error report before restarting."
        )

    return measurements


def wait_for_stability(
    session,
    address,
    writer,
    csv_file,
    programme_start,
    step_number,
    target_temperature,
):
    phase_start = time.monotonic()
    timeout_seconds = STABILITY_TIMEOUT_MINUTES * 60.0

    while True:
        cycle_start = time.monotonic()

        measurements = sample_phase(
            session,
            address,
            writer,
            csv_file,
            programme_start,
            step_number,
            "waiting_for_stability",
            target_temperature,
            phase_start,
            remaining_seconds=None,
        )

        if measurements["temperature_stable"] == 2:
            print(f"Step {step_number} is stable; hold timer started.")
            return

        elapsed = time.monotonic() - phase_start

        if elapsed >= timeout_seconds:
            raise TimeoutError(
                f"Step {step_number} did not report stable within "
                f"{STABILITY_TIMEOUT_MINUTES:g} minutes."
            )

        cycle_duration = time.monotonic() - cycle_start
        time.sleep(
            min(
                max(0.0, SAMPLE_INTERVAL - cycle_duration),
                max(0.0, timeout_seconds - elapsed),
            )
        )


def hold_period(
    session,
    address,
    writer,
    csv_file,
    programme_start,
    step_number,
    phase,
    target_temperature,
    hold_seconds,
):
    phase_start = time.monotonic()
    deadline = phase_start + hold_seconds

    while True:
        cycle_start = time.monotonic()
        remaining_seconds = deadline - cycle_start

        if remaining_seconds <= 0:
            return

        sample_phase(
            session,
            address,
            writer,
            csv_file,
            programme_start,
            step_number,
            phase,
            target_temperature,
            phase_start,
            remaining_seconds,
        )

        cycle_duration = time.monotonic() - cycle_start
        time.sleep(
            min(
                max(0.0, SAMPLE_INTERVAL - cycle_duration),
                max(0.0, deadline - time.monotonic()),
            )
        )


def close_session(session):
    if session is not None:
        try:
            session.stop()
        except Exception:
            pass


def signal_master_ready(output_file):
    """Tell run_experiment.py that TEC control and its log are ready."""
    ready_file = os.environ.get("EOM_READY_FILE")
    if ready_file:
        ready_path = Path(ready_file)
        temporary_path = ready_path.with_suffix(ready_path.suffix + ".tmp")
        temporary_path.write_text(
            str(Path(output_file).resolve()),
            encoding="utf-8",
        )
        temporary_path.replace(ready_path)


def emergency_disable(MeComSerial, session, address):
    """Make a best-effort attempt to switch the TEC output off."""

    try:
        if session is None:
            raise RuntimeError("Original TEC session is unavailable.")

        set_parameter(session, address, OUTPUT_ENABLE_ID, 0)
        verify_integer_parameter(
            session,
            address,
            OUTPUT_ENABLE_ID,
            0,
            "Output disable",
        )
        print("TEC output disabled.")
        return True
    except Exception as first_error:
        print(
            "Could not disable through the existing connection: "
            f"{type(first_error).__name__}: {first_error}"
        )

    close_session(session)
    retry_session = None

    try:
        retry_session = MeComSerial(serialport=COM_PORT)
        retry_address = retry_session.identify()
        set_parameter(
            retry_session,
            retry_address,
            OUTPUT_ENABLE_ID,
            0,
        )
        verify_integer_parameter(
            retry_session,
            retry_address,
            OUTPUT_ENABLE_ID,
            0,
            "Output disable",
        )
        print("TEC output disabled using a new connection.")
        return True
    except Exception as retry_error:
        print(
            "CRITICAL: Python could not confirm that the TEC output is OFF. "
            "Disable it at the controller and check the hardware immediately."
        )
        print(
            f"Disable error: {type(retry_error).__name__}: {retry_error}"
        )
        return False
    finally:
        close_session(retry_session)


def disable_output_now():
    """Connect solely to disable the TEC output, then close the session."""
    try:
        from mecom import MeComSerial
    except ModuleNotFoundError as error:
        raise ModuleNotFoundError(
            "pyMeCom is not installed in this Python environment."
        ) from error

    session = None
    try:
        print(f"Connecting on {COM_PORT} for emergency output disable...")
        session = MeComSerial(serialport=COM_PORT)
        address = session.identify()
        set_parameter(session, address, OUTPUT_ENABLE_ID, 0)
        verify_integer_parameter(
            session,
            address,
            OUTPUT_ENABLE_ID,
            0,
            "Emergency output disable",
        )
        print("TEC output is confirmed OFF.")
    finally:
        close_session(session)


def main():
    args = parse_arguments()

    if args.disable_output:
        disable_output_now()
        return

    schedule = validate_schedule(get_configured_temperature_schedule())

    if args.print_schedule_json:
        print(json.dumps(schedule_as_dict(schedule), sort_keys=True))
        return

    print_schedule(schedule)
    execution_steps = build_execution_steps(schedule, args.resume_from)

    if args.dry_run:
        print("\nDry run complete; no hardware connection was opened.")
        return

    print(
        "\nClose the Meerstetter software and tec_temp_logger.py before "
        "continuing."
    )

    if REQUIRE_START_CONFIRMATION and not args.yes:
        confirmation = input(
            "Type START to connect and enable temperature control: "
        ).strip()

        if confirmation != "START":
            print("Start cancelled; no hardware connection was opened.")
            return

    try:
        from mecom import MeComSerial
    except ModuleNotFoundError as error:
        raise ModuleNotFoundError(
            "pyMeCom is not installed in this Python environment. Install "
            "the same 'mecom' package used by tec_temp_logger.py."
        ) from error

    if args.resume_from is not None:
        filename = Path(args.resume_from).expanduser().resolve()
        run_folder = filename.parent
    else:
        _, run_folder = resolve_component_directory(
            Path(__file__).resolve().parent,
            "temp-control",
        )
        filename = component_output_file(
            run_folder / "tec_temperature_control.csv"
        )
    run_folder.mkdir(parents=True, exist_ok=True)
    append_existing_output = (
        (args.resume_from is not None or append_output_requested())
        and filename.is_file()
    )

    columns = [
        "wall_time",
        "elapsed_s",
        "step_number",
        "phase",
        "target_temperature_C",
        "phase_elapsed_s",
        "step_remaining_s",
        "object_temperature_C",
        "sink_temperature_C",
        "tec_output_current_A",
        "tec_output_voltage_V",
        "temperature_stable",
        "device_status",
        "read_status",
    ]

    session = None
    address = None
    control_preparation_started = False
    original_input_selection = None
    programme_completed = False
    output_is_enabled = False
    interrupted = False

    print(f"\nConnecting on {COM_PORT}...")

    try:
        session = MeComSerial(serialport=COM_PORT)
        address = session.identify()
        print(f"Connected to MeCom address {address}.")

        temperature_controller_value, firmware_label = (
            detect_firmware_mode(session, address)
        )
        print(f"Detected {firmware_label}.")

        original_input_selection = int(
            read_parameter(session, address, OUTPUT_STAGE_INPUT_ID)
        )

        # Make the transition deterministic even if the controller was left
        # enabled by another program. From this point onward, any unsuccessful
        # exit makes another best-effort output-disable attempt.
        control_preparation_started = True
        set_parameter(session, address, OUTPUT_ENABLE_ID, 0)
        verify_integer_parameter(
            session,
            address,
            OUTPUT_ENABLE_ID,
            0,
            "Initial output disable",
        )

        # Select the TEC's closed-loop temperature controller. This value is
        # 1 on firmware v6 and 2 on firmware v5 or earlier.
        set_parameter(
            session,
            address,
            OUTPUT_STAGE_INPUT_ID,
            temperature_controller_value,
        )
        verify_integer_parameter(
            session,
            address,
            OUTPUT_STAGE_INPUT_ID,
            temperature_controller_value,
            "Temperature-controller input selection",
        )

        print(f"TEC prepared for scheduled control.\nLogging to:\n{filename}")

        preflight_measurements = read_measurements(session, address)
        print(
            "TEC preflight readings: "
            f"object={preflight_measurements['object_temperature_C']:.4f} "
            "degC, "
            f"sink={preflight_measurements['sink_temperature_C']:.4f} degC, "
            f"status={preflight_measurements['device_status']}"
        )
        if preflight_measurements["device_status"] == 3:
            raise RuntimeError(
                "TEC is in its error state before the schedule starts."
            )

        print("Press Ctrl+C to stop and disable the TEC output.\n")

        programme_start = time.monotonic()

        with filename.open(
            mode="a" if append_existing_output else "w",
            newline="",
            encoding="utf-8",
            buffering=1,
        ) as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=columns)
            if not append_existing_output or filename.stat().st_size == 0:
                writer.writeheader()
                csv_file.flush()
            signal_master_ready(filename)

            for (
                step_number,
                target_temperature,
                hold_seconds,
            ) in execution_steps:
                if target_temperature is None:
                    print(
                        f"\nStarting step {step_number}/{len(schedule)}: "
                        f"TEC output OFF for "
                        f"{format_duration(hold_seconds)}"
                    )

                    set_parameter(
                        session,
                        address,
                        OUTPUT_ENABLE_ID,
                        0,
                    )
                    verify_integer_parameter(
                        session,
                        address,
                        OUTPUT_ENABLE_ID,
                        0,
                        "Scheduled output disable",
                    )
                    output_is_enabled = False

                    hold_period(
                        session,
                        address,
                        writer,
                        csv_file,
                        programme_start,
                        step_number,
                        "output_off",
                        target_temperature=None,
                        hold_seconds=hold_seconds,
                    )
                    continue

                print(
                    f"\nStarting step {step_number}/{len(schedule)}: "
                    f"{target_temperature:.3f} degC for "
                    f"{format_duration(hold_seconds)}"
                )

                set_target_temperature(
                    session,
                    address,
                    target_temperature,
                )

                if not output_is_enabled:
                    set_parameter(
                        session,
                        address,
                        OUTPUT_ENABLE_ID,
                        1,
                    )
                    verify_integer_parameter(
                        session,
                        address,
                        OUTPUT_ENABLE_ID,
                        1,
                        "Output enable",
                    )
                    output_is_enabled = True

                if WAIT_UNTIL_STABLE:
                    wait_for_stability(
                        session,
                        address,
                        writer,
                        csv_file,
                        programme_start,
                        step_number,
                        target_temperature,
                    )

                hold_period(
                    session,
                    address,
                    writer,
                    csv_file,
                    programme_start,
                    step_number,
                    "holding",
                    target_temperature,
                    hold_seconds,
                )

        programme_completed = True
        print("\nTemperature programme completed successfully.")

    except KeyboardInterrupt:
        interrupted = True
        print("\nTemperature programme stopped by user.")

    finally:
        should_disable_output = (
            session is not None
            and control_preparation_started
            and (TURN_OUTPUT_OFF_AT_END or not programme_completed)
        )

        if should_disable_output:
            if emergency_disable(MeComSerial, session, address):
                output_is_enabled = False

            # Restore the original input source only after the output is off.
            if original_input_selection is not None:
                try:
                    set_parameter(
                        session,
                        address,
                        OUTPUT_STAGE_INPUT_ID,
                        original_input_selection,
                    )
                except Exception as restore_error:
                    print(
                        "Could not restore the original input selection: "
                        f"{type(restore_error).__name__}: {restore_error}"
                    )

        close_session(session)

        if filename.exists():
            print(f"CSV saved to:\n{filename}")

        if (
            programme_completed
            and not TURN_OUTPUT_OFF_AT_END
            and output_is_enabled
        ):
            print(
                "WARNING: The TEC output was deliberately left ON at the "
                "final target temperature."
            )

    return 130 if interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
