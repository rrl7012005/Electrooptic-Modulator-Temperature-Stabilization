# Safety and operator review

This software coordinates laboratory equipment; it does not make a hardware
configuration safe. Limits that are valid for one EOM, thermistor, TEC,
heatsink, power supply, detector, or cable arrangement may be unsafe for
another.

Before reading the detail below, remember these four rules:

1. Use dry-run or preview until the complete plan has been reviewed.
2. Treat every voltage as a requested Moku connector voltage, not an EOM voltage.
3. Never copy TEC limits, controller tuning, thermistor data, or polarity
   assumptions from another apparatus.
4. If cleanup cannot confirm an output is off, treat its physical state as
   unknown and check the apparatus directly.

A **waveform lookup table (LUT)** is the ordered list of requested voltage
values for one complete waveform cycle. The Moku steps through that list at a
selected sample rate. A LUT is a software instruction to the instrument, not a
measurement of the physical output.

The **Moku control connection** is the communication link between the Python
program on the laboratory computer and the Moku instrument. Losing this link
means software may no longer be able to command the Moku or confirm its state.
It does not prove that the physical Output 2 waveform stopped, and it is not the
same as disconnecting an optical or electrical cable.

**Output 2** is the physical Moku connector selected for waveform output in the
configured setup. The **Moku SDK** is the manufacturer's Python package used to
control the instrument; it is software, not another piece of hardware.

## Safe software boundary

The default command path is validation and preview only. A dry run must not
import a hardware SDK, claim a device, write a temperature target, or enable a
Moku output. Real execution requires the explicit `--execute` option and an
operator confirmation after the complete effective plan has been printed and
saved.

Configuration is validated as one experiment before any hardware access. This
includes resolving referenced files, expanding generated schedules, compiling
all waveforms, checking requested connector voltages and timing limits, and
creating previews. A validation failure means that no component should start.

## Voltage meaning

Every waveform voltage in YAML is a **requested voltage at the selected Moku
connector**. It is not the voltage across the EOM. Source impedance, load
impedance, termination, cabling, bias networks, combiners, and bandwidth can
change the voltage and edge shape seen by the EOM. The software does not infer
or compensate for that apparatus transfer function.

An achieved LUT or preview describes the digital programme sent to the Moku.
It is not a calibration of the physical connector waveform. Verify critical
levels and edges with suitable independent measurement equipment.

## Moku review

Before real execution, verify all of the following for the actual device and
SDK version:

- the Moku model, platform identifier, and available Multi-Instrument slots;
- the physical input and output channels;
- physical analogue-to-digital input, digital-to-analogue output, and input
  electrical settings;
- internal routing and trigger sources;
- requested connector-voltage range and the connected load;
- waveform frequency, LUT length, sample rate, and repeat limits; and
- that the optical measurement windows correspond to achieved waveform timing.

The output must remain disabled while the session, slots, routing,
Oscilloscope, and LUT are configured. It may be enabled only after the entire
active configuration is ready. Upload or output-enable ambiguity is treated as
an unknown output state: the worker is retired, bounded cleanup is attempted,
and a replacement starts with outputs disabled.

Software cannot guarantee the physical output state while communication is
lost. An `output_off_unconfirmed` or equivalent event requires operator action
at the apparatus; it is not evidence that the output is off.

## Finite bursts and phase continuity

A failure of the software-control connection to the Moku during a finite
exact-count burst leaves an important unknown: the software cannot tell how
many cycles physically reached Output 2.
Logs call this state `indeterminate`. The safe default is to stop the action and
never trigger that burst again automatically. Never estimate the delivered
cycle count from Python wall-clock time.

A continuous waveform may be started again after reconnection when configured
to do so. It starts from the first sample of its LUT, not from the point reached
before the failure. The program creates a new waveform session and records that
the timing relationship was lost. Data from opposite sides of that boundary
must not be averaged together.

Software-controlled changes between different LUTs include SDK and transport
latency. Only timing within one achieved LUT, and a verified hardware burst
count, should be described as deterministic.

## TEC review

Experiment schedules may request temporary temperature targets; they must not
silently alter proportional-integral-derivative (PID) controller tuning,
thermistor coefficients, current or voltage limits, Peltier polarity, sensor
selection, protection thresholds, or other persistent controller settings.

Before a real target write, the runtime must establish communication, verify
the channel, read finite and plausible object and heatsink temperatures,
validate the requested target against explicitly configured safety limits, log
the request, write the volatile target mechanism where supported, and read back
the active target. A mismatch or controller error stops schedule advancement.

Disabling TEC output is physically different from stopping schedule timing.
Completion and recovery settings must name the intended action explicitly:
hold the current target, return to an explicitly configured safe target, revert
to a stored target, or disable output. The software must not guess.
`return_to_safe_target` is rejected unless `run_settings.temperature.safe_target_c`
is explicitly configured within that run's validated target bounds; the name
"safe" does not replace an apparatus review.

## Completion, errors, and Ctrl+C

The master completion policy determines when otherwise healthy components are
asked to stop. On normal completion, timeout, an error, or `Ctrl+C`, each
component records its final state and follows its configured completion policy.
The runtime makes bounded cleanup attempts and reports any output state it
cannot confirm.

`Ctrl+C` stops software supervision; it does not by itself prove that a Moku
output is disabled or that TEC output is off. Read the final console and event
records and verify the apparatus when cleanup was not confirmed.

## Real-hardware verification boundary

Automated tests use fakes and do not establish that Multi-Instrument deployment,
routing names, trigger alignment, connector levels, output-disable behavior, or
TEC setpoint behavior is correct on the laboratory apparatus. A controlled
smoke test remains a separate real-hardware action requiring explicit approval.
It should use a non-critical thermal target and a safe electrical load, monitor
the requested Moku output independently, verify that Output 2 stays off during
setup and is switched off during cleanup, then verify one short continuous
waveform and one small finite burst before any optical experiment. No such test
is performed by installation, dry-run, preview, or the automated test suite.
