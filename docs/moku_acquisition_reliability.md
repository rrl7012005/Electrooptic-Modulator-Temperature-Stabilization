# Moku acquisition reliability

This note records what is known about the Moku:Go acquisition freeze, what the
collector now guarantees, and what still needs a controlled hardware test. It
applies to the direct USB-C connection used by this apparatus. Windows exposes
that USB connection as a virtual network adapter, so HTTP, hostname resolution,
and link-local IPv6 failures can still occur without Wi-Fi being involved.

## Why `get_data(timeout=1)` was not sufficient

The inspected laboratory environment used Moku Python SDK 4.2.2.1. Its
`Oscilloscope.get_data()` sends `timeout` to the device as the trigger/frame
wait and calls `RequestSession.post()`. That session passes Requests a tuple of
the SDK connection timeout and `read_timeout + timeout`.

This provides a device trigger wait and lower-level connection/read inactivity
timeouts. It is not a total wall-clock deadline around the entire SDK call.
Name resolution, a stalled dependency, or another blocked SDK path can prevent
the call from returning to the collector's `try/except`. This is consistent
with the observed process remaining alive with no CSV growth and reacting
unusually late to `Ctrl+C`.

Requests itself documents that its `timeout` is not a total duration limit on
the complete response; it is based on connection/read inactivity. See the
[Requests timeout documentation](https://requests.readthedocs.io/en/latest/user/quickstart/#timeouts).

The root cause of the specific historical freeze cannot be proved after the
fact. The strongest current diagnosis is a blocked SDK/HTTP call or USB virtual
network/API-server path below the collector's exception handler. An ordinary
missing Input 1 trigger is less consistent with the event because that path
normally returns the explicit new-frame timeout.

## Hard watchdog design

`collect_data.py` is import-safe and starts the SDK with Windows
`multiprocessing` using the `spawn` method. Each worker:

1. imports the Moku SDK inside the child;
2. resolves and claims the configured device;
3. creates its own live `Oscilloscope` object;
4. accepts one allow-listed SDK call at a time; and
5. returns frames or a pickle-safe description of the remote exception.

No live SDK object is sent between processes. A parent lock permits only one
in-flight command. The parent polls the worker connection in short intervals,
which keeps `Ctrl+C` responsive. `get_data()` has a 15-second hard deadline by
default. The device trigger wait remains 1 second and retains its separate
meaning.

After a hard deadline expires, the parent:

1. records the watchdog event;
2. atomically saves every valid raw and provenance row;
3. marks waveform continuity unconfirmed;
4. terminates and joins the blocked worker, using a bounded kill fallback;
5. refuses to open another connection if that process cannot be confirmed
   dead;
6. creates a fresh worker;
7. reapplies the frontend, sources, timebase, Input 1 trigger, and Output 2
   waveform; and
8. verifies the session with `summary()`.

The late result of a retired worker cannot enter the replacement connection:
the process and its private pipe are gone. Tests use a real spawned fake child
whose `get_data()` never returns; they verify the deadline, save-before-kill
ordering, child termination, replacement configuration, and resumed operation.

Configuration and cleanup SDK calls also have finite parent deadlines. If
Output 2 shutdown cannot be confirmed through the active session, the collector
retires that worker and makes one separately bounded cleanup connection. It
reports rather than hides a final unconfirmed output state.

## Failure handling

| Condition | Behaviour |
| --- | --- |
| No Input 1 crossing at 0.6 V | Expected trigger timeout; retry after 0.1 s, report the first and then at most once per minute, no reconnect. |
| HTTP/USB transport exception | Reconnect after two consecutive errors. |
| Hung `get_data()` | Hard watchdog, immediate save, terminate worker, reconnect. |
| Stale API connection | Immediate save and reconstruction. |
| Ownership loss | Immediate save and reconstruction. |
| Malformed frame | Drop the frame; reconstruct after five consecutive malformed frames. |
| Device absent | Retry at capped backoff; no placeholder measurement rows. |
| Unknown SDK error | Save during outer cleanup and fail visibly; do not retry an unclassified state forever. |

Normal 48-hour runs use indefinite recovery delays of 1, 2, 5, 10, 20, then
30 seconds. Status is recorded every minute while recovery remains active.
Bounded mode and an optional maximum outage duration are available through the
environment variables documented in `README.md`. Python does not catch
`KeyboardInterrupt` in acquisition or recovery sleeps.

The USB diagnostics supplied for this apparatus showed a Windows `MokuGo`
virtual Ethernet adapter and a scoped link-local IPv6 device address. Runtime
events preserve the configured hostname, every resolved address, IPv6 scope
and interface name where available, selected primary/fallback role, configured
USB interface type, SDK version, and `force_connect` value. The collector does
not suggest or switch to Wi-Fi.

Windows could not read this adapter's power-management settings (`System Error
31`), so USB selective-suspend state remains unknown. Cable/port resets,
adapter power policy, and API-server failure while the physical USB link stays
up also remain possible causes. `force_connect=True` is useful for reclaiming a
stale session, but it cannot interrupt a Python call already blocked in another
process; retiring that process is the watchdog's job.

## Waveform continuity

The software can distinguish what it changed, but it cannot measure Output 2
while the API is unavailable.

| Path | Output 2 interpretation |
| --- | --- |
| Expected trigger timeout | The collector issues no waveform command. It records that software did not reconfigure the output, but physical continuity is not independently measured. |
| One transport error below the reconnect threshold | No waveform command is issued. Device state is not independently confirmed. |
| Hung call or lost USB/API path | The blocked child is killed without another SDK call. Output state is explicitly unconfirmed. |
| Relinquish / force-connect replacement | Official ownership documentation does not promise waveform continuity. Treat the boundary as unconfirmed. |
| Full reconstruction | `generate_waveform()` is issued again. The waveform is treated as restarted and its phase/timing may have reset. |
| Intentional shutdown | Output 2 `Off` is requested with a bounded call. Failure to confirm it is logged. |

Each reconstruction increments `waveform_session_id`. A partially accumulated
one-second result is discarded if its frames straddle a reconstruction. The
primary CSV remains the historical three-column format, and the one-to-one
provenance sidecar records the acquisition and waveform session. Gaps are not
interpolated or backfilled.

## On-device logging assessment

Liquid Instruments documents that the Moku:Go Data Logger can save one channel
at up to 1 MSa/s or two channels at up to 500 kSa/s to 8 GB of internal storage.
The documented rate range starts at 10 Sa/s and the configured duration can be
up to 10,000 hours. The native `.li` format is proprietary and optimized for
size and speed; it can later be converted. The API recommends polling
`logging_progress()` to establish completion.

The official material found does **not** establish that a log is guaranteed to
continue and remain valid after all of the following: USB removal, loss of the
Python process, ownership takeover, `force_connect`, or instrument
redeployment. That independence must be tested on the laboratory Moku before
it is described as a recovery source. No local logging was enabled by this
change.

The reviewed specifications also do not state the absolute timestamp accuracy,
an independent maximum file size, or exactly how a file is finalized when the
8 GB storage becomes full. Those properties remain unknown rather than being
inferred from the maximum configured duration.

Oscilloscope `save_high_res_buffer()` saves the current high-resolution channel
buffer to Moku:Go's `persist` storage. It is not documented as a historical or
segmented sequence of triggered frames. Therefore it cannot be treated as an
automatic record of frames missed during a laptop outage.

Multi-Instrument Mode can deploy separate instruments in slots, and Liquid
Instruments publishes a Moku:Go Waveform Generator plus Oscilloscope example.
Changing this experiment to Data Logger plus another instrument would require
separately verified slot routing, trigger semantics, sample reduction, output
continuity, ownership, and file recovery. It is not a drop-in safety feature.

Official references:

- [Moku:Go specifications](https://download.liquidinstruments.com/documentation/specs/hardware/mokugo/MokuGo-Specifications.pdf)
- [Data Logger `start_logging`](https://apis.liquidinstruments.com/api/reference/datalogger/start_logging.html)
- [Data Logger API](https://apis.liquidinstruments.com/api/reference/datalogger/)
- [Oscilloscope `save_high_res_buffer`](https://apis.liquidinstruments.com/api/reference/oscilloscope/save_high_res_buffer.html)
- [Multi-Instrument Mode example](https://apis.liquidinstruments.com/api/moku-examples/python-api/)
- [Moku ownership reference](https://apis.liquidinstruments.com/api/reference/)

## Forty-eight-hour storage feasibility

At 1 MSa/s a 10 µs pulse contains only about ten samples before accounting for
the 100 ns edges. At 500 kSa/s it contains about five. Lower continuous rates
quickly stop resolving the pulse plateau.

The table gives raw payload sizes for 48 hours. It brackets a 16-bit stored
value (2 bytes) and a 32-bit value (4 bytes); actual `.li` size must be measured
because its on-disk encoding is proprietary. Headers and filesystem overhead
are excluded.

| Rate per channel | One channel, 2--4 bytes/sample | Two channels, 2--4 bytes/sample |
| --- | ---: | ---: |
| 1 MSa/s | 345.6--691.2 GB | Not supported on Moku:Go (hypothetical size 691.2 GB--1.3824 TB) |
| 500 kSa/s | 172.8--345.6 GB | 345.6--691.2 GB |
| 100 kSa/s | 34.56--69.12 GB | 69.12--138.24 GB |
| 10 kSa/s | 3.456--6.912 GB | 6.912--13.824 GB |
| 1 kSa/s | 345.6--691.2 MB | 691.2 MB--1.3824 GB |
| 100 Sa/s | 34.56--69.12 MB | 69.12--138.24 MB |

Continuous full-rate capture is therefore far beyond 8 GB. Rates that fit for
48 hours do not resolve a 10 µs pulse reliably. The desired one-low/one-high
record per second needs triggered short-window capture or device-side
reduction, neither of which the standard Data Logger API documents as an
autonomous reduced-statistics mode. A custom FPGA/Cloud Compile design may be
possible, but it is a separate hardware-development and validation task.

If controlled testing later proves that a suitable on-device log survives the
required failure boundaries, integration should keep it as a distinct raw
source: start and identify the device file before PC acquisition, record its
device filename in JSONL, poll status without making laptop collection depend
on it, discover and download it after reconnection, and never overwrite the
Oscilloscope CSV. Conversion should retain the device timestamps and add UTC,
Europe/London, source-file, run, and waveform-session fields. Alignment should
identify overlap with live samples, choose one explicitly documented source in
overlapping intervals, retain local-only rows as a different acquisition
source, and leave periods missing from both sources as gaps. This architecture
does not make continuous low-rate Data Logger samples scientifically equivalent
to the present trigger-aligned plateau measurement.

## Controlled checks still required

Before adopting on-device files as outage recovery data, use a short,
non-critical run to verify each boundary separately:

1. Start a small local log and confirm `logging_progress()` plus download.
2. Repeat while temporarily disconnecting and reconnecting the USB link.
3. Repeat while terminating only the controlling Python process.
4. Verify Output 2 on an independent oscilloscope during those events.
5. Test ownership relinquish, force-connect, and instrument redeployment one at
   a time, then check file completeness and timestamps.
6. Fill only a controlled portion of storage to establish actual bytes/sample;
   do not intentionally fill the device during an experiment.

These are real-hardware operations and are not run by automated tests. Until
they pass, measurements missed during an API outage remain genuine gaps and
cannot be recovered from the Moku.
