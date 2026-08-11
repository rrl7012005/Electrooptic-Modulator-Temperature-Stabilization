"""Hardware-free tests for Moku acquisition failure handling and recovery."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import csv
import io
from pathlib import Path
import numpy as np
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parent
SRC_DIRECTORY = REPOSITORY_ROOT / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.moku.acquisition import (
    AcquisitionFailureKind,
    AcquisitionRecoveryError,
    AcquisitionWatchdogExpired,
    AcquisitionWorkerTerminationError,
    JsonlAcquisitionEventWriter,
    MokuAcquisitionManager,
    MokuConnectionAddressesExhausted,
    MokuConnectionFactory,
    OscilloscopeConfiguration,
    RecoveryPolicy,
    apply_oscilloscope_configuration,
    classify_acquisition_exception,
    describe_resolved_address,
    resolve_connection_address,
)
from eom_stabilisation.moku.process_worker import (
    ProcessIsolatedOscilloscope,
    WorkerTimeouts,
)


class MokuException(Exception):
    pass


class InvalidRequestException(MokuException):
    pass


class ReadTimeout(Exception):
    pass


class NameResolutionError(Exception):
    pass


class CollectorConnectionConfigurationTests(unittest.TestCase):
    def test_verified_usb_fallback_is_part_of_collector_configuration(self):
        from collect_data import MOKU_CONFIGURATION

        self.assertEqual(
            MOKU_CONFIGURATION.fallback_address,
            "[fe80::7269:79ff:feb9:7dea%10]",
        )


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds

    def advance(self, seconds):
        self.value += seconds


class RecordingEventWriter:
    def __init__(self):
        self.events = []

    def write(self, event, **fields):
        self.events.append({"event": event, **fields})


class FakeInstrument:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []

    def get_data(self, **kwargs):
        self.calls.append(("get_data", kwargs))
        if not self.outcomes:
            raise AssertionError("fake instrument has no configured outcome")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def set_frontend(self, *args):
        self.calls.append(("set_frontend", args))

    def set_sources(self, sources):
        self.calls.append(("set_sources", sources))

    def set_timebase(self, *args, **kwargs):
        self.calls.append(("set_timebase", args, kwargs))

    def set_trigger(self, **kwargs):
        self.calls.append(("set_trigger", kwargs))

    def generate_waveform(self, **kwargs):
        self.calls.append(("generate_waveform", kwargs))

    def summary(self):
        self.calls.append(("summary",))
        return {"status": "ok"}

    def relinquish_ownership(self):
        self.calls.append(("relinquish_ownership",))


class SequenceFactory:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def fake_spawn_worker(connection, configuration):
    """Spawn-safe fake implementing the isolated worker wire protocol."""

    import os

    connection.send(
        {
            "kind": "ready",
            "worker_pid": os.getpid(),
            "selected_address": configuration.address,
            "selected_address_role": "primary",
            "selected_resolved_addresses": ["fe80::1%10"],
        }
    )
    try:
        while True:
            message = connection.recv()
            if message.get("kind") == "shutdown":
                return
            method = message["method"]
            request_id = message["request_id"]
            if configuration.address == "fake-hang" and method == "get_data":
                time.sleep(3600)
            if configuration.address == "fake-late" and method == "get_data":
                time.sleep(0.4)
            if (
                configuration.address == "fake-cleanup-hang"
                and method == "generate_waveform"
                and message.get("kwargs", {}).get("type") == "Off"
            ):
                time.sleep(3600)
            if configuration.address == "fake-slow" and method == "summary":
                time.sleep(0.1)

            result = None
            if method == "get_data":
                result = {
                    "time": [0.0, 1.0],
                    "ch1": [0.0, 1.0],
                    "ch2": [0.0, 5.0],
                }
            elif method == "summary":
                result = {"status": "ok", "worker_pid": os.getpid()}
            connection.send(
                {
                    "kind": "response",
                    "request_id": request_id,
                    "ok": True,
                    "result": result,
                }
            )
    except (EOFError, BrokenPipeError, OSError):
        return


def short_worker_timeouts(**overrides):
    values = {
        "get_data_s": 0.15,
        "rpc_s": 0.5,
        "startup_s": 2.0,
        "cleanup_s": 0.15,
        "terminate_grace_s": 0.5,
        "kill_grace_s": 0.5,
        "poll_interval_s": 0.01,
    }
    values.update(overrides)
    return WorkerTimeouts(**values)


CONFIGURATION = OscilloscopeConfiguration(
    address="fake-moku",
    force_connect=True,
    frontend_channel=1,
    frontend_impedance="1MOhm",
    frontend_coupling="DC",
    frontend_range="10Vpp",
    channel_sources=((1, "Input1"), (2, "Output2")),
    timebase_start_s=-45e-6,
    timebase_end_s=45e-6,
    timebase_max_length=16384,
    trigger_mode="Normal",
    trigger_type="Edge",
    trigger_source="Input1",
    trigger_level_v=0.6,
    trigger_edge="Rising",
    waveform_channel=2,
    waveform_type="Pulse",
    waveform_amplitude_vpp=5.0,
    waveform_offset_v=2.5,
    waveform_frequency_hz=100.0,
    waveform_pulse_width_s=10e-6,
    waveform_edge_time_s=100e-9,
)


def make_manager(
    instrument,
    *,
    factory=None,
    event_writer=None,
    clock=None,
    policy=None,
    saved=None,
    cleaned=None,
):
    factory = factory or SequenceFactory([])
    event_writer = event_writer or RecordingEventWriter()
    clock = clock or FakeClock()
    saved = saved if saved is not None else []
    cleaned = cleaned if cleaned is not None else []

    manager = MokuAcquisitionManager(
        instrument,
        instrument_factory=factory,
        apply_configuration=lambda candidate: apply_oscilloscope_configuration(
            candidate, CONFIGURATION
        ),
        verify_connection=lambda candidate: candidate.summary(),
        event_writer=event_writer,
        before_reconnect=saved.append,
        cleanup_failed_instrument=cleaned.append,
        policy=policy or RecoveryPolicy(reconnect_backoff_s=(0.0, 0.0, 0.0)),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    return manager, event_writer, clock, saved, cleaned


class FailureClassificationTests(unittest.TestCase):
    def test_expected_trigger_timeout_is_narrowly_classified(self):
        error = MokuException("['Timeout before fetching the new frame']")
        self.assertEqual(
            classify_acquisition_exception(error),
            AcquisitionFailureKind.EXPECTED_TRIGGER_TIMEOUT,
        )

    def test_api_connection_message_is_not_a_trigger_timeout(self):
        error = InvalidRequestException("['API Connection already exists']")
        self.assertEqual(
            classify_acquisition_exception(error),
            AcquisitionFailureKind.STALE_API_CONNECTION,
        )

    def test_transport_and_unknown_errors_remain_distinct(self):
        self.assertEqual(
            classify_acquisition_exception(ReadTimeout("read timed out")),
            AcquisitionFailureKind.TRANSIENT_TRANSPORT,
        )
        self.assertEqual(
            classify_acquisition_exception(
                MokuException("Timeout while waiting for a response.")
            ),
            AcquisitionFailureKind.TRANSIENT_TRANSPORT,
        )
        self.assertEqual(
            classify_acquisition_exception(MokuException("bad parameter")),
            AcquisitionFailureKind.UNRECOVERABLE,
        )

    def test_windows_name_resolution_failure_is_transport_failure(self):
        error = NameResolutionError(
            "Failed to resolve 'mokugo-008058' "
            "([Errno 11001] getaddrinfo failed)"
        )
        self.assertEqual(
            classify_acquisition_exception(error),
            AcquisitionFailureKind.TRANSIENT_TRANSPORT,
        )


class ConnectionAddressTests(unittest.TestCase):
    def test_bracketed_scoped_ipv6_is_unwrapped_only_for_resolution(self):
        address = "[fe80::7269:79ff:feb9:7dea%10]"
        socket_result = [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("fe80::7269:79ff:feb9:7dea", 80, 0, 10),
            )
        ]

        with patch(
            "eom_stabilisation.moku.acquisition.socket.getaddrinfo",
            return_value=socket_result,
        ) as getaddrinfo:
            resolved = resolve_connection_address(address)

        getaddrinfo.assert_called_once_with(
            "fe80::7269:79ff:feb9:7dea%10",
            80,
            type=socket.SOCK_STREAM,
        )
        self.assertEqual(resolved, ("fe80::7269:79ff:feb9:7dea%10",))

    def test_scoped_link_local_address_records_usb_interface_scope(self):
        with patch(
            "eom_stabilisation.moku.acquisition.socket.if_indextoname",
            return_value="Ethernet 9",
        ):
            details = describe_resolved_address("fe80::7269:79ff:feb9:7dea%10")

        self.assertEqual(details["network_address_kind"], "link_local_ipv6")
        self.assertEqual(details["interface_scope"], "10")
        self.assertEqual(details["interface_name"], "Ethernet 9")

    def test_fallback_must_be_distinct_from_primary(self):
        with self.assertRaisesRegex(ValueError, "must differ"):
            replace(CONFIGURATION, fallback_address="FAKE-MOKU")

    def test_unresolved_primary_uses_only_explicit_fallback(self):
        configuration = replace(
            CONFIGURATION,
            fallback_address="192.0.2.58",
        )
        constructor_calls = []
        fallback_instrument = FakeInstrument()
        writer = RecordingEventWriter()

        def resolver(address):
            if address == "fake-moku":
                raise OSError("[Errno 11001] getaddrinfo failed")
            return ("192.0.2.58",)

        def constructor(address, *, force_connect):
            constructor_calls.append((address, force_connect))
            return fallback_instrument

        factory = MokuConnectionFactory(
            constructor,
            configuration,
            event_writer=writer,
            address_resolver=resolver,
        )

        self.assertIs(factory(), fallback_instrument)
        self.assertEqual(constructor_calls, [("192.0.2.58", True)])
        self.assertEqual(factory.selected_address, "192.0.2.58")
        self.assertEqual(factory.selected_address_role, "fallback")
        self.assertEqual(
            [event["event"] for event in writer.events],
            [
                "connection_address_resolution_failed",
                "connection_address_resolved",
                "connection_address_selected",
            ],
        )
        self.assertTrue(writer.events[-1]["fallback_used"])

    def test_primary_connection_failure_then_uses_fallback(self):
        configuration = replace(
            CONFIGURATION,
            fallback_address="192.0.2.58",
        )
        constructor_calls = []
        fallback_instrument = FakeInstrument()

        def constructor(address, *, force_connect):
            constructor_calls.append((address, force_connect))
            if address == "fake-moku":
                raise ReadTimeout("read timed out")
            return fallback_instrument

        factory = MokuConnectionFactory(
            constructor,
            configuration,
            event_writer=RecordingEventWriter(),
            address_resolver=lambda address: (
                "192.0.2.1" if address == "fake-moku" else address,
            ),
        )

        self.assertIs(factory(), fallback_instrument)
        self.assertEqual(
            constructor_calls,
            [("fake-moku", True), ("192.0.2.58", True)],
        )
        self.assertEqual(factory.selected_address_role, "fallback")

    def test_no_fallback_is_discovered_or_guessed(self):
        constructor_calls = []
        writer = RecordingEventWriter()

        def unresolved(address):
            raise OSError("[Errno 11001] getaddrinfo failed")

        factory = MokuConnectionFactory(
            lambda address, *, force_connect: constructor_calls.append(address),
            CONFIGURATION,
            event_writer=writer,
            address_resolver=unresolved,
        )

        with self.assertRaises(MokuConnectionAddressesExhausted):
            factory()

        self.assertEqual(constructor_calls, [])
        self.assertEqual(
            CONFIGURATION.connection_addresses(),
            (("primary", "fake-moku"),),
        )
        self.assertEqual(len(writer.events), 1)
        self.assertEqual(
            writer.events[0]["event"],
            "connection_address_resolution_failed",
        )


class TriggerTimeoutTests(unittest.TestCase):
    def test_one_timeout_then_valid_frame_does_not_reconnect(self):
        frame = {"time": [0.0, 1.0], "ch1": [0.0, 1.0], "ch2": [0.0, 5.0]}
        instrument = FakeInstrument(
            [MokuException("Timeout before fetching the new frame"), frame]
        )
        factory = SequenceFactory([])
        manager, writer, _, saved, _ = make_manager(
            instrument,
            factory=factory,
        )

        self.assertIsNone(
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            )
        )
        self.assertIs(
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            ),
            frame,
        )
        manager.record_valid_frame()

        self.assertEqual(factory.calls, 0)
        self.assertEqual(saved, [])
        self.assertEqual(manager.health.total_trigger_timeouts, 1)
        self.assertEqual(manager.health.consecutive_trigger_timeouts, 0)
        self.assertEqual(
            [event["event"] for event in writer.events],
            ["expected_trigger_timeout", "valid_frame_resumed"],
        )

    def test_ctrl_c_during_trigger_timeout_retry_is_not_swallowed(self):
        instrument = FakeInstrument(
            [MokuException("Timeout before fetching the new frame")]
        )
        manager, _, _, _, _ = make_manager(instrument)
        manager.sleep = lambda seconds: (_ for _ in ()).throw(
            KeyboardInterrupt()
        )

        with self.assertRaises(KeyboardInterrupt):
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            )
        self.assertEqual(manager.health.total_trigger_timeouts, 1)
        self.assertEqual(manager.health.consecutive_trigger_timeouts, 1)

    def test_several_minutes_without_input_trigger_never_reconnects(self):
        timeout = MokuException("Timeout before fetching the new frame")
        instrument = FakeInstrument([timeout] * 181)
        factory = SequenceFactory([])
        policy = RecoveryPolicy(
            trigger_timeout_retry_delay_s=0.0,
            trigger_timeout_report_interval_s=60.0,
            reconnect_backoff_s=(0.0, 0.0, 0.0),
        )
        manager, writer, clock, saved, _ = make_manager(
            instrument,
            factory=factory,
            policy=policy,
        )

        for _ in range(181):
            clock.advance(1.0)
            self.assertIsNone(
                manager.acquire_frame(
                    timeout=1.0,
                    wait_reacquire=True,
                    wait_complete=True,
                )
            )

        reports = [
            event for event in writer.events
            if event["event"] == "expected_trigger_timeout"
        ]
        self.assertEqual(len(reports), 4)
        self.assertEqual(manager.health.total_trigger_timeouts, 181)
        self.assertEqual(factory.calls, 0)
        self.assertEqual(saved, [])


class RecoveryTests(unittest.TestCase):
    def test_transport_recovery_uses_configured_fallback_after_error_11001(self):
        configuration = replace(
            CONFIGURATION,
            fallback_address="192.0.2.58",
        )
        old = FakeInstrument([ReadTimeout("one"), ReadTimeout("two")])
        replacement = FakeInstrument()
        writer = RecordingEventWriter()
        constructor_calls = []

        def constructor(address, *, force_connect):
            constructor_calls.append((address, force_connect))
            return replacement

        def resolver(address):
            if address == "fake-moku":
                raise OSError("[Errno 11001] getaddrinfo failed")
            return (address,)

        connection_factory = MokuConnectionFactory(
            constructor,
            configuration,
            event_writer=writer,
            address_resolver=resolver,
        )
        manager = MokuAcquisitionManager(
            old,
            instrument_factory=connection_factory,
            apply_configuration=lambda candidate: (
                apply_oscilloscope_configuration(candidate, configuration)
            ),
            verify_connection=lambda candidate: candidate.summary(),
            event_writer=writer,
            before_reconnect=lambda reason: None,
            cleanup_failed_instrument=lambda candidate: None,
            policy=RecoveryPolicy(reconnect_backoff_s=(0.0,)),
            sleep=lambda seconds: None,
            monotonic=lambda: 0.0,
        )

        self.assertIsNone(
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            )
        )
        self.assertIsNone(
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            )
        )

        self.assertIs(manager.instrument, replacement)
        self.assertEqual(constructor_calls, [("192.0.2.58", True)])
        self.assertEqual(connection_factory.selected_address_role, "fallback")
        self.assertIn(("summary",), replacement.calls)
        trigger_call = next(
            call for call in replacement.calls if call[0] == "set_trigger"
        )
        self.assertEqual(trigger_call[1]["source"], "Input1")
        self.assertEqual(trigger_call[1]["level"], 0.6)

    def test_stale_api_connection_saves_and_reconstructs_exact_configuration(self):
        old = FakeInstrument(
            [InvalidRequestException("API Connection already exists")]
        )
        replacement = FakeInstrument()
        factory = SequenceFactory([replacement])
        saved = []
        manager, writer, _, _, _ = make_manager(
            old,
            factory=factory,
            saved=saved,
        )

        self.assertIsNone(
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            )
        )

        self.assertIs(manager.instrument, replacement)
        self.assertEqual(saved, ["stale_api_connection"])
        self.assertIn(("relinquish_ownership",), old.calls)
        self.assertEqual(factory.calls, 1)
        self.assertEqual(
            [call[0] for call in replacement.calls],
            [
                "set_frontend",
                "set_sources",
                "set_timebase",
                "set_trigger",
                "generate_waveform",
                "summary",
            ],
        )
        trigger_call = replacement.calls[3][1]
        self.assertEqual(trigger_call["source"], "Input1")
        self.assertEqual(trigger_call["level"], 0.6)
        waveform_call = replacement.calls[4][1]
        self.assertEqual(waveform_call["amplitude"], 5.0)
        self.assertEqual(waveform_call["offset"], 2.5)
        self.assertEqual(waveform_call["pulse_width"], 10e-6)
        self.assertTrue(waveform_call["strict"])
        self.assertIn(
            "reconnection_succeeded",
            [event["event"] for event in writer.events],
        )

    def test_transport_failure_then_failed_reconstruction_is_bounded(self):
        old = FakeInstrument([ReadTimeout("one"), ReadTimeout("two")])
        factory = SequenceFactory(
            [ConnectionError("a"), ConnectionError("b"), ConnectionError("c")]
        )
        saved = []
        manager, writer, clock, _, _ = make_manager(
            old,
            factory=factory,
            saved=saved,
        )

        self.assertIsNone(
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            )
        )
        with self.assertRaises(AcquisitionRecoveryError):
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            )

        self.assertEqual(factory.calls, 3)
        self.assertEqual(saved, ["transient_transport"])
        self.assertEqual(clock.sleeps, [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(
            len(
                [
                    event for event in writer.events
                    if event["event"] == "reconnection_attempt_failed"
                ]
            ),
            3,
        )
        self.assertEqual(writer.events[-1]["event"], "reconnection_exhausted")

    def test_repeated_malformed_frames_trigger_reconstruction(self):
        old = FakeInstrument()
        replacement = FakeInstrument()
        policy = RecoveryPolicy(
            malformed_frames_before_reconnect=2,
            reconnect_backoff_s=(0.0,),
        )
        manager, _, _, saved, _ = make_manager(
            old,
            factory=SequenceFactory([replacement]),
            policy=policy,
        )

        manager.record_malformed_frame(ValueError("missing ch1"))
        self.assertIs(manager.instrument, old)
        manager.record_malformed_frame(ValueError("missing ch1"))

        self.assertIs(manager.instrument, replacement)
        self.assertEqual(saved, ["malformed_frame"])

    def test_failed_partial_candidate_is_cleaned_before_next_attempt(self):
        old = FakeInstrument([InvalidRequestException("API Connection already exists")])
        broken = FakeInstrument()
        replacement = FakeInstrument()
        factory = SequenceFactory([broken, replacement])
        cleaned = []

        def apply(candidate):
            if candidate is broken:
                raise RuntimeError("configuration failed")
            apply_oscilloscope_configuration(candidate, CONFIGURATION)

        writer = RecordingEventWriter()
        clock = FakeClock()
        manager = MokuAcquisitionManager(
            old,
            instrument_factory=factory,
            apply_configuration=apply,
            verify_connection=lambda candidate: candidate.summary(),
            event_writer=writer,
            before_reconnect=lambda reason: None,
            cleanup_failed_instrument=cleaned.append,
            policy=RecoveryPolicy(reconnect_backoff_s=(0.0, 0.0)),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        manager.acquire_frame(
            timeout=1.0,
            wait_reacquire=True,
            wait_complete=True,
        )

        self.assertEqual(cleaned, [broken])
        self.assertIs(manager.instrument, replacement)

    def test_recovery_cycles_cannot_loop_forever_without_valid_frame(self):
        instruments = [
            FakeInstrument([InvalidRequestException("API Connection already exists")])
            for _ in range(4)
        ]
        factory = SequenceFactory(instruments[1:])
        policy = RecoveryPolicy(
            reconnect_backoff_s=(0.0,),
            max_recovery_cycles_without_valid_frame=2,
        )
        manager, _, _, _, _ = make_manager(
            instruments[0],
            factory=factory,
            policy=policy,
        )

        for _ in range(2):
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            )
        with self.assertRaises(AcquisitionRecoveryError):
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            )
        self.assertEqual(factory.calls, 2)

    def test_keyboard_interrupt_is_not_caught_or_retried(self):
        instrument = FakeInstrument([KeyboardInterrupt()])
        factory = SequenceFactory([])
        manager, writer, _, saved, _ = make_manager(
            instrument,
            factory=factory,
        )

        with self.assertRaises(KeyboardInterrupt):
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            )
        self.assertEqual(factory.calls, 0)
        self.assertEqual(saved, [])
        self.assertEqual(writer.events, [])

    def test_ownership_loss_recovers_immediately(self):
        old = FakeInstrument([MokuException("client key is no longer owner")])
        replacement = FakeInstrument()
        manager, _, _, saved, _ = make_manager(
            old,
            factory=SequenceFactory([replacement]),
        )

        self.assertIsNone(
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            )
        )
        self.assertIs(manager.instrument, replacement)
        self.assertEqual(saved, ["ownership_loss"])

    def test_indefinite_recovery_eventually_succeeds_with_capped_backoff(self):
        old = FakeInstrument(
            [InvalidRequestException("API Connection already exists")]
        )
        replacement = FakeInstrument()
        factory = SequenceFactory(
            [ConnectionError(str(index)) for index in range(5)]
            + [replacement]
        )
        policy = RecoveryPolicy(
            reconnect_backoff_s=(1.0, 2.0),
            recovery_mode="indefinite",
            recovery_status_report_interval_s=2.0,
            max_recovery_cycles_without_valid_frame=None,
        )
        manager, writer, clock, _, _ = make_manager(
            old,
            factory=factory,
            policy=policy,
        )

        manager.acquire_frame(
            timeout=1.0,
            wait_reacquire=True,
            wait_complete=True,
        )

        self.assertIs(manager.instrument, replacement)
        self.assertEqual(clock.sleeps, [1.0, 2.0, 2.0, 2.0, 2.0, 2.0])
        self.assertIn(
            "recovery_still_active",
            [event["event"] for event in writer.events],
        )
        self.assertIn(
            "waveform_restarted",
            [event["event"] for event in writer.events],
        )

    def test_maximum_outage_stops_bounded_recovery(self):
        old = FakeInstrument(
            [InvalidRequestException("API Connection already exists")]
        )
        factory = SequenceFactory(
            [ConnectionError("one"), ConnectionError("two")]
        )
        policy = RecoveryPolicy(
            reconnect_backoff_s=(1.0, 2.0, 3.0),
            recovery_mode="bounded",
            maximum_recovery_outage_s=5.0,
            max_recovery_cycles_without_valid_frame=None,
        )
        manager, writer, clock, _, _ = make_manager(
            old,
            factory=factory,
            policy=policy,
        )

        with self.assertRaises(AcquisitionRecoveryError):
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            )

        self.assertEqual(factory.calls, 2)
        self.assertEqual(clock.sleeps, [1.0, 2.0])
        self.assertEqual(writer.events[-1]["event"], "reconnection_exhausted")

    def test_ctrl_c_during_reconnect_backoff_is_not_swallowed(self):
        old = FakeInstrument(
            [InvalidRequestException("API Connection already exists")]
        )
        manager, _, _, _, _ = make_manager(
            old,
            factory=SequenceFactory([]),
            policy=RecoveryPolicy(reconnect_backoff_s=(1.0,)),
        )
        manager.sleep = lambda seconds: (_ for _ in ()).throw(
            KeyboardInterrupt()
        )

        with self.assertRaises(KeyboardInterrupt):
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            )

    def test_unretired_worker_prevents_replacement_connection(self):
        class UnretirableInstrument(FakeInstrument):
            def relinquish_ownership(self):
                raise AcquisitionWorkerTerminationError("still alive")

        old = UnretirableInstrument(
            [InvalidRequestException("API Connection already exists")]
        )
        factory = SequenceFactory([FakeInstrument()])
        manager, writer, _, _, _ = make_manager(old, factory=factory)

        with self.assertRaises(AcquisitionRecoveryError):
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            )

        self.assertEqual(factory.calls, 0)
        self.assertIn(
            "acquisition_worker_termination_failed",
            [event["event"] for event in writer.events],
        )

    def test_failed_data_preservation_retires_old_session_and_stops(self):
        old = FakeInstrument(
            [InvalidRequestException("API Connection already exists")]
        )
        factory = SequenceFactory([FakeInstrument()])
        manager, writer, _, _, _ = make_manager(old, factory=factory)

        def fail_preservation(reason):
            raise OSError("disk unavailable")

        manager.before_reconnect = fail_preservation
        with self.assertRaises(AcquisitionRecoveryError):
            manager.acquire_frame(
                timeout=1.0,
                wait_reacquire=True,
                wait_complete=True,
            )

        self.assertIn(("relinquish_ownership",), old.calls)
        self.assertEqual(factory.calls, 0)
        self.assertIn(
            "recovery_data_preservation_failed",
            [event["event"] for event in writer.events],
        )


class ProcessWatchdogTests(unittest.TestCase):
    def make_client(self, address="fake-clean", *, writer=None, **timeouts):
        configuration = replace(CONFIGURATION, address=address)
        return ProcessIsolatedOscilloscope(
            configuration,
            event_writer=writer or RecordingEventWriter(),
            timeouts=short_worker_timeouts(**timeouts),
            worker_target=fake_spawn_worker,
        )

    def test_never_returning_get_data_expires_hard_watchdog(self):
        writer = RecordingEventWriter()
        client = self.make_client("fake-hang", writer=writer)
        try:
            with self.assertRaises(AcquisitionWatchdogExpired):
                client.get_data(timeout=1.0)
            self.assertTrue(client.is_alive)
            self.assertFalse(client.is_usable)
            self.assertIn(
                "sdk_call_watchdog_expired",
                [event["event"] for event in writer.events],
            )
        finally:
            client.relinquish_ownership()
        self.assertFalse(client.is_alive)

    def test_hung_worker_is_retired_before_reconstruction(self):
        writer = RecordingEventWriter()
        old = self.make_client("fake-hang", writer=writer)
        old_pid = old.worker_pid
        replacements = []

        def factory():
            replacement = self.make_client("fake-hang", writer=writer)
            replacements.append(replacement)
            return replacement

        def preserve(reason):
            writer.write("csv_preserved_before_recovery", reason=reason)

        manager = MokuAcquisitionManager(
            old,
            instrument_factory=factory,
            apply_configuration=lambda candidate: (
                apply_oscilloscope_configuration(candidate, CONFIGURATION)
            ),
            verify_connection=lambda candidate: candidate.summary(),
            event_writer=writer,
            before_reconnect=preserve,
            cleanup_failed_instrument=lambda candidate: (
                candidate.relinquish_ownership()
            ),
            policy=RecoveryPolicy(reconnect_backoff_s=(0.0,)),
            sleep=lambda seconds: None,
        )

        try:
            self.assertIsNone(
                manager.acquire_frame(
                    timeout=1.0,
                    wait_reacquire=True,
                    wait_complete=True,
                )
            )
            self.assertFalse(old.is_alive)
            self.assertNotEqual(manager.instrument.worker_pid, old_pid)
            events = [event["event"] for event in writer.events]
            self.assertLess(
                events.index("acquisition_watchdog_expired"),
                events.index("csv_preserved_before_recovery"),
            )
            self.assertLess(
                events.index("csv_preserved_before_recovery"),
                events.index("acquisition_worker_terminated"),
            )
            self.assertIn("waveform_restarted", events)
        finally:
            if replacements:
                replacements[-1].relinquish_ownership()

    def test_late_worker_response_cannot_reach_replacement_session(self):
        old = self.make_client("fake-late", get_data_s=0.05)
        with self.assertRaises(AcquisitionWatchdogExpired):
            old.get_data(timeout=1.0)
        old.relinquish_ownership()
        self.assertFalse(old.is_alive)

        replacement = self.make_client("fake-clean")
        try:
            frame = replacement.get_data(timeout=1.0)
            self.assertEqual(frame["ch1"], [0.0, 1.0])
            self.assertNotEqual(old.worker_pid, replacement.worker_pid)
        finally:
            replacement.relinquish_ownership()

    def test_cleanup_call_is_bounded_and_worker_is_terminated(self):
        client = self.make_client("fake-cleanup-hang")
        with self.assertRaises(AcquisitionWatchdogExpired):
            client.generate_waveform(channel=2, type="Off")
        client.relinquish_ownership()
        self.assertFalse(client.is_alive)

    def test_ctrl_c_while_waiting_terminates_worker(self):
        client = self.make_client("fake-hang")
        with patch.object(
            client,
            "_wait_for_kind",
            side_effect=KeyboardInterrupt(),
        ):
            with self.assertRaises(KeyboardInterrupt):
                client.get_data(timeout=1.0)
        self.assertFalse(client.is_alive)

    def test_ctrl_c_during_cleanup_terminates_worker(self):
        client = self.make_client("fake-cleanup-hang")
        with patch.object(
            client,
            "_wait_for_kind",
            side_effect=KeyboardInterrupt(),
        ):
            with self.assertRaises(KeyboardInterrupt):
                client.generate_waveform(channel=2, type="Off")
        self.assertFalse(client.is_alive)

    def test_parent_serialises_concurrent_sdk_requests(self):
        client = self.make_client("fake-slow")
        results = []
        errors = []

        def call_summary():
            try:
                results.append(client.summary())
            except BaseException as error:
                errors.append(error)

        start = time.monotonic()
        threads = [threading.Thread(target=call_summary) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.monotonic() - start
        client.relinquish_ownership()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertGreaterEqual(elapsed, 0.18)


class EventLoggingTests(unittest.TestCase):
    def test_jsonl_event_contains_provenance_and_london_timestamp(self):
        fixed_utc = datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary_directory:
            event_path = Path(temporary_directory) / "events.jsonl"
            writer = JsonlAcquisitionEventWriter(
                event_path,
                sdk_version="3.3.3",
                configuration=CONFIGURATION,
                utc_now=lambda: fixed_utc,
            )
            writer.write(
                "connection_error",
                error=InvalidRequestException("API Connection already exists"),
                include_traceback=True,
                consecutive_timeout_count=7,
                consecutive_connection_error_count=1,
                time_since_last_valid_frame_s=65.0,
                last_valid_frame_timestamp_utc="2026-08-02T12:28:55Z",
                reconnection_attempt_number=1,
            )

            payload = json.loads(event_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["timestamp_utc"], "2026-08-02T12:30:00Z")
        self.assertEqual(payload["timestamp_local"], "2026-08-02T13:30:00+01:00")
        self.assertEqual(payload["moku_sdk_version"], "3.3.3")
        self.assertEqual(payload["trigger_source"], "Input1")
        self.assertEqual(payload["trigger_level_v"], 0.6)
        self.assertEqual(payload["waveform_settings"]["waveform_amplitude_vpp"], 5.0)
        self.assertEqual(payload["exception_class"].split(".")[-1], "InvalidRequestException")
        self.assertIn("API Connection already exists", payload["exception_repr"])
        self.assertIn("traceback", payload)


class DataPreservationTests(unittest.TestCase):
    def test_gap_writes_no_fake_primary_or_provenance_rows(self):
        from collect_data import save_sample_provenance, save_voltage_samples

        with tempfile.TemporaryDirectory() as temporary_directory:
            primary = Path(temporary_directory) / "raw.csv"
            provenance = Path(temporary_directory) / "provenance.csv"
            buffer = np.zeros((4, 3))
            save_voltage_samples(primary, buffer, 0)
            save_sample_provenance(provenance, [])

            primary_rows = list(
                csv.reader(io.StringIO(primary.read_text(encoding="utf-8")))
            )
            provenance_rows = list(
                csv.reader(
                    io.StringIO(provenance.read_text(encoding="utf-8"))
                )
            )

        self.assertEqual(len(primary_rows), 1)
        self.assertEqual(len(provenance_rows), 1)

    def test_primary_and_provenance_rows_stay_one_to_one(self):
        from collect_data import save_sample_provenance, save_voltage_samples

        records = [
            {
                "wall_time": "1.0",
                "timestamp_utc": "1970-01-01T00:00:01Z",
                "acquisition_source": "live_oscilloscope",
                "run_session_id": "run_test",
                "waveform_session_id": 2,
                "first_sample_after_reconnect": True,
                "waveform_timing_may_have_restarted": True,
            }
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            primary = Path(temporary_directory) / "raw.csv"
            provenance = Path(temporary_directory) / "provenance.csv"
            buffer = np.array([[1.0, 0.2, 2.8], [0.0, 0.0, 0.0]])
            save_voltage_samples(primary, buffer, 1)
            save_sample_provenance(provenance, records)

            primary_rows = list(
                csv.reader(io.StringIO(primary.read_text(encoding="utf-8")))
            )
            provenance_rows = list(
                csv.reader(
                    io.StringIO(provenance.read_text(encoding="utf-8"))
                )
            )

        self.assertEqual(len(primary_rows) - 1, len(provenance_rows) - 1)


if __name__ == "__main__":
    unittest.main()
