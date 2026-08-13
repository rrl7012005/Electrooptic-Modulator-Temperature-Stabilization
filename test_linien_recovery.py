"""Hardware-free tests for Linien disconnection recovery."""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
import pickle
import sys
import tempfile
import unittest
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parent
SRC_DIRECTORY = REPOSITORY_ROOT / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

import linien_logger
from eom_stabilisation.linien.recovery import (
    JsonlLinienEventWriter,
    LinienConnectionRecovery,
    LinienLockNotRestoredError,
    LinienRecoveryPolicy,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class RecordingEventWriter:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def write(
        self,
        event: str,
        *,
        error: BaseException | None = None,
        include_traceback: bool = False,
        **fields: object,
    ) -> None:
        record: dict[str, object] = {"event": event, **fields}
        if error is not None:
            record["error_type"] = type(error).__name__
            record["error_message"] = str(error)
        if include_traceback:
            record["traceback_requested"] = True
        self.events.append(record)


class FakeClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.disconnect_calls = 0


class SequenceFactory:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        if not self.outcomes:
            raise AssertionError("fake connection factory is exhausted")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def event_names(writer: RecordingEventWriter) -> list[str]:
    return [str(record["event"]) for record in writer.events]


class LinienRecoveryPolicyTests(unittest.TestCase):
    def test_rejects_empty_or_invalid_timing_values(self) -> None:
        invalid_policies = (
            {"reconnect_backoff_s": ()},
            {"reconnect_backoff_s": (0.0,)},
            {"lock_recovery_timeout_s": 0.0},
            {"lock_poll_interval_s": float("nan")},
        )

        for values in invalid_policies:
            with self.subTest(values=values), self.assertRaises(ValueError):
                LinienRecoveryPolicy(**values)


class LinienConnectionRecoveryTests(unittest.TestCase):
    def make_recovery(
        self,
        *,
        factory: SequenceFactory,
        refresh_and_read_lock,
        clock: FakeClock,
        events: RecordingEventWriter,
        policy: LinienRecoveryPolicy | None = None,
    ) -> LinienConnectionRecovery:
        def disconnect(client: FakeClient) -> None:
            client.disconnect_calls += 1

        return LinienConnectionRecovery(
            connection_factory=factory,
            refresh_and_read_lock=refresh_and_read_lock,
            disconnect_client=disconnect,
            is_recoverable_error=lambda error: isinstance(
                error,
                (EOFError, OSError, ConnectionError, TimeoutError),
            ),
            event_writer=events,
            policy=policy
            or LinienRecoveryPolicy(
                reconnect_backoff_s=(1.0, 2.0, 5.0),
                lock_recovery_timeout_s=10.0,
                lock_poll_interval_s=1.0,
            ),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    def test_retries_connection_and_waits_briefly_for_lock(self) -> None:
        failed_client = FakeClient("failed")
        replacement = FakeClient("replacement")
        factory = SequenceFactory(
            [ConnectionResetError("still disconnected"), replacement]
        )
        clock = FakeClock()
        events = RecordingEventWriter()
        lock_results = iter((False, True))
        recovery = self.make_recovery(
            factory=factory,
            refresh_and_read_lock=lambda client: next(lock_results),
            clock=clock,
            events=events,
        )

        result = recovery.recover(failed_client, EOFError("connection reset"))

        self.assertIs(result, replacement)
        self.assertEqual(factory.calls, 2)
        self.assertEqual(clock.sleeps, [1.0, 2.0, 1.0])
        self.assertEqual(failed_client.disconnect_calls, 1)
        self.assertIn("connection_lost", event_names(events))
        self.assertIn("reconnection_attempt_failed", event_names(events))
        self.assertIn("reconnected_but_unlocked", event_names(events))
        self.assertIn("reconnection_succeeded", event_names(events))

    def test_connected_but_unlocked_for_ten_seconds_is_fatal(self) -> None:
        failed_client = FakeClient("failed")
        replacement = FakeClient("replacement")
        factory = SequenceFactory([replacement])
        clock = FakeClock()
        events = RecordingEventWriter()
        recovery = self.make_recovery(
            factory=factory,
            refresh_and_read_lock=lambda client: False,
            clock=clock,
            events=events,
            policy=LinienRecoveryPolicy(
                reconnect_backoff_s=(1.0,),
                lock_recovery_timeout_s=10.0,
                lock_poll_interval_s=1.0,
            ),
        )

        with self.assertRaisesRegex(
            LinienLockNotRestoredError,
            "within 10 seconds",
        ):
            recovery.recover(failed_client, EOFError("connection reset"))

        self.assertEqual(clock.value, 11.0)
        self.assertEqual(replacement.disconnect_calls, 1)
        self.assertEqual(event_names(events).count("reconnected_but_unlocked"), 1)
        self.assertIn("lock_not_restored", event_names(events))
        self.assertNotIn("reconnection_succeeded", event_names(events))

    def test_transport_failure_during_lock_check_restarts_reconnection(self) -> None:
        failed_client = FakeClient("failed")
        first_candidate = FakeClient("first")
        second_candidate = FakeClient("second")
        factory = SequenceFactory([first_candidate, second_candidate])
        clock = FakeClock()
        events = RecordingEventWriter()

        def refresh_and_read_lock(client: FakeClient) -> bool:
            if client is first_candidate:
                raise EOFError("connection failed again")
            return True

        recovery = self.make_recovery(
            factory=factory,
            refresh_and_read_lock=refresh_and_read_lock,
            clock=clock,
            events=events,
        )

        result = recovery.recover(failed_client, EOFError("initial loss"))

        self.assertIs(result, second_candidate)
        self.assertEqual(clock.sleeps, [1.0, 2.0])
        self.assertEqual(first_candidate.disconnect_calls, 1)
        self.assertIn("reconnection_attempt_failed", event_names(events))
        self.assertIn("reconnection_succeeded", event_names(events))

    def test_non_transport_validation_error_is_not_retried(self) -> None:
        failed_client = FakeClient("failed")
        candidate = FakeClient("candidate")
        factory = SequenceFactory([candidate])
        clock = FakeClock()
        events = RecordingEventWriter()
        recovery = self.make_recovery(
            factory=factory,
            refresh_and_read_lock=lambda client: (_ for _ in ()).throw(
                ValueError("invalid server response")
            ),
            clock=clock,
            events=events,
        )

        with self.assertRaisesRegex(ValueError, "invalid server response"):
            recovery.recover(failed_client, EOFError("connection reset"))

        self.assertEqual(factory.calls, 1)
        self.assertEqual(candidate.disconnect_calls, 1)
        self.assertIn("reconnection_fatal_error", event_names(events))

    def test_ctrl_c_interrupts_reconnection_backoff(self) -> None:
        failed_client = FakeClient("failed")
        factory = SequenceFactory([FakeClient("unused")])
        events = RecordingEventWriter()

        def interrupted_sleep(seconds: float) -> None:
            raise KeyboardInterrupt

        recovery = LinienConnectionRecovery(
            connection_factory=factory,
            refresh_and_read_lock=lambda client: True,
            disconnect_client=lambda client: setattr(
                client,
                "disconnect_calls",
                client.disconnect_calls + 1,
            ),
            is_recoverable_error=lambda error: isinstance(error, EOFError),
            event_writer=events,
            policy=LinienRecoveryPolicy(),
            monotonic=lambda: 0.0,
            sleep=interrupted_sleep,
        )

        with self.assertRaises(KeyboardInterrupt):
            recovery.recover(failed_client, EOFError("connection reset"))

        self.assertEqual(factory.calls, 0)
        self.assertEqual(failed_client.disconnect_calls, 1)


class JsonlLinienEventWriterTests(unittest.TestCase):
    def test_writes_machine_readable_timestamps_and_error_details(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "events.jsonl"
            writer = JsonlLinienEventWriter(path)

            try:
                raise EOFError("connection reset")
            except EOFError as error:
                writer.write(
                    "connection_lost",
                    error=error,
                    include_traceback=True,
                    reconnection_attempt_number=1,
                )

            record = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(record["event"], "connection_lost")
        self.assertEqual(record["error_type"], "EOFError")
        self.assertEqual(record["error_message"], "connection reset")
        self.assertTrue(record["timestamp_utc"].endswith("Z"))
        self.assertIn(record["timestamp_local"][-6:], ("+00:00", "+01:00"))
        self.assertIn("EOFError: connection reset", record["traceback"])


class ValueParameter:
    def __init__(self, value: object) -> None:
        self.value = value


class FakeParameters:
    def __init__(
        self,
        *,
        lock: bool,
        serialized_plot_data: bytes,
        check_error: BaseException | None = None,
    ) -> None:
        self.lock = ValueParameter(lock)
        self.dual_channel = ValueParameter(False)
        self.to_plot = ValueParameter(serialized_plot_data)
        self.check_error = check_error
        self.check_calls = 0

    def check_for_changed_parameters(self) -> None:
        self.check_calls += 1
        if self.check_error is not None:
            error = self.check_error
            self.check_error = None
            raise error


class FakeControl:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def exposed_pause_acquisition(self) -> None:
        self.calls.append(("pause",))

    def exposed_set_csr_direct(self, name: str, value: int) -> None:
        self.calls.append(("set", name, value))

    def exposed_continue_acquisition(self) -> None:
        self.calls.append(("continue",))


class LoggerFakeClient:
    def __init__(self, parameters: FakeParameters) -> None:
        self.parameters = parameters
        self.control = FakeControl()


class StubRecovery:
    def __init__(self, replacement: LoggerFakeClient) -> None:
        self.replacement = replacement
        self.recover_calls: list[tuple[object, BaseException]] = []
        self.disconnect_calls: list[tuple[object, str]] = []

    def recover(self, client: object, error: BaseException) -> LoggerFakeClient:
        self.recover_calls.append((client, error))
        return self.replacement

    def disconnect_safely(self, client: object, *, context: str) -> None:
        self.disconnect_calls.append((client, context))


class StopAfterCalls:
    def __init__(self, allowed_calls: int) -> None:
        self.allowed_calls = allowed_calls
        self.calls = 0

    def __call__(self, seconds: float) -> None:
        self.calls += 1
        if self.calls > self.allowed_calls:
            raise KeyboardInterrupt


def valid_plot_data() -> bytes:
    return pickle.dumps(
        {
            "error_signal": [8192.0],
            "control_signal": [4096.0],
            "monitor_signal": [2048.0],
        }
    )


class LinienLoggerLoopTests(unittest.TestCase):
    def test_eof_from_parameter_refresh_recovers_and_appends_a_sample(self) -> None:
        failed_client = LoggerFakeClient(
            FakeParameters(
                lock=True,
                serialized_plot_data=valid_plot_data(),
                check_error=EOFError("connection reset"),
            )
        )
        replacement = LoggerFakeClient(
            FakeParameters(
                lock=True,
                serialized_plot_data=valid_plot_data(),
            )
        )
        recovery = StubRecovery(replacement)
        output_stream = io.StringIO()
        writer = csv.writer(output_stream)
        sleep = StopAfterCalls(allowed_calls=1)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EOM_READY_FILE", None)
            with self.assertRaises(KeyboardInterrupt):
                linien_logger.run_logging_loop(
                    failed_client,
                    recovery,
                    (EOFError, OSError),
                    writer,
                    output_stream,
                    Path("fake.csv"),
                    sleep=sleep,
                )

        rows = list(csv.reader(io.StringIO(output_stream.getvalue())))
        self.assertEqual(len(recovery.recover_calls), 1)
        self.assertIs(recovery.recover_calls[0][0], failed_client)
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0][1]), 1.0)
        self.assertEqual(float(rows[0][2]), 0.5)
        self.assertEqual(float(rows[0][3]), 0.25)
        self.assertEqual(
            replacement.control.calls,
            [
                ("pause",),
                ("set", "scopegen_adc_a_q_sel", 1),
                ("continue",),
            ],
        )
        self.assertEqual(
            recovery.disconnect_calls,
            [(replacement, "logger_shutdown")],
        )

    def test_malformed_plot_pickle_is_fatal_not_a_reconnect(self) -> None:
        client = LoggerFakeClient(
            FakeParameters(
                lock=False,
                serialized_plot_data=b"not a pickle",
            )
        )
        recovery = StubRecovery(client)
        output_stream = io.StringIO()

        with self.assertRaises(pickle.UnpicklingError):
            linien_logger.run_logging_loop(
                client,
                recovery,
                (EOFError, OSError),
                csv.writer(output_stream),
                output_stream,
                Path("fake.csv"),
                sleep=lambda seconds: None,
            )

        self.assertEqual(recovery.recover_calls, [])
        self.assertEqual(
            recovery.disconnect_calls,
            [(client, "logger_shutdown")],
        )


if __name__ == "__main__":
    unittest.main()
