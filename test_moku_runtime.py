"""SDK-free tests for Moku:Go MIM replay and runtime state handling."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parent
SRC_DIRECTORY = REPOSITORY_ROOT / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.moku.acquisition import AcquisitionWatchdogExpired
from eom_stabilisation.moku.models import OutputState, WaveformContinuity
from eom_stabilisation.moku.process_worker import (
    ProcessIsolatedMokuRuntime,
    WorkerTimeouts,
)
from eom_stabilisation.moku.runtime import (
    FiniteBurstIndeterminateError,
    MokuRuntime,
)
from eom_stabilisation.moku.sdk_adapter import (
    MOKU_GO_MIM_CONNECTIONS,
    MokuConfigurationReplayError,
    MokuRuntimeConfiguration,
    MokuSdkSession,
)
from eom_stabilisation.moku.waveform_compiler import (
    compile_waveform,
    parse_run_spec,
)


def compiled_square():
    return compile_waveform(
        "square",
        {
            "type": "square",
            "low_level_v": 0.0,
            "high_level_v": 5.0,
            "frequency_hz": 100_000,
            "duty_cycle_percent": 10,
            "edge_time_ns": 100,
        },
    )


WAVEFORM = compiled_square()
CONTINUOUS_RUN = parse_run_spec({"mode": "continuous"}, WAVEFORM.achieved_period_s)
FINITE_RUN = parse_run_spec(
    {"mode": "duration", "duration_us": 50},
    WAVEFORM.achieved_period_s,
)


class RecordingEventWriter:
    def __init__(self):
        self.events = []

    def write(self, event, **fields):
        self.events.append({"event": event, **fields})


class FakeRuntimeSession:
    def __init__(
        self,
        *,
        replay_error=None,
        activate_error=None,
        disable_error=None,
    ):
        self.calls = []
        self.replay_error = replay_error
        self.activate_error = activate_error
        self.disable_error = disable_error
        self.frames = [{"time": [0.0, 1e-6], "ch1": [0.1, 0.2]}] * 4

    def replay_configuration(self, waveform, run):
        self.calls.append(("replay_configuration", waveform.name, run.repeat_count))
        if self.replay_error is not None:
            raise self.replay_error
        return {"status": "ok"}

    def activate(self, run):
        self.calls.append(("activate", run.repeat_count))
        if self.activate_error is not None:
            raise self.activate_error

    def get_data(self, **kwargs):
        self.calls.append(("get_data", kwargs))
        return self.frames.pop(0)

    def disable_all_outputs(self):
        self.calls.append(("disable_all_outputs",))
        if self.disable_error is not None:
            raise self.disable_error

    def summary(self):
        self.calls.append(("summary",))
        return {"status": "ok"}

    def relinquish_ownership(self):
        self.calls.append(("relinquish_ownership",))


class SessionFactory:
    def __init__(self, sessions):
        self.sessions = list(sessions)
        self.created = []

    def __call__(self, configuration):
        session = self.sessions.pop(0)
        self.created.append((configuration, session))
        return session


class FakeAwg:
    def __init__(self, calls, fail_at=None):
        self.calls = calls
        self.fail_at = fail_at

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))
        if self.fail_at == name:
            raise RuntimeError(f"failure at {name}")

    def enable_output(self, **kwargs):
        self._record("enable_output", kwargs)

    def generate_waveform(self, **kwargs):
        self._record("generate_waveform", kwargs)

    def disable_modulation(self, **kwargs):
        self._record("disable_modulation", kwargs)

    def burst_modulate(self, **kwargs):
        self._record("burst_modulate", kwargs)

    def manual_trigger(self):
        self._record("manual_trigger", {})

    def summary(self):
        self._record("awg_summary", {})
        return {"instrument": "awg"}


class FakeOscilloscope:
    def __init__(self, calls, fail_at=None):
        self.calls = calls
        self.fail_at = fail_at

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.fail_at == name:
            raise RuntimeError(f"failure at {name}")

    def set_timebase(self, *args, **kwargs):
        self._record("set_timebase", *args, **kwargs)

    def set_trigger(self, **kwargs):
        self._record("set_trigger", **kwargs)

    def get_data(self, **kwargs):
        self._record("get_data", **kwargs)
        return {"time": [0.0, 1.0], "ch1": [0.2, 0.3], "ch2": [0.0, 5.0]}

    def summary(self):
        self._record("osc_summary")
        return {"instrument": "osc"}


class FakeMultiInstrument:
    def __init__(self, calls, fail_at=None):
        self.calls = calls
        self.fail_at = fail_at

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if self.fail_at == name:
            raise RuntimeError(f"failure at {name}")

    def set_connections(self, **kwargs):
        self._record("set_connections", **kwargs)

    def set_frontend(self, **kwargs):
        self._record("set_frontend", **kwargs)

    def summary(self):
        self._record("mim_summary")
        return {"instrument": "mim"}

    def relinquish_ownership(self):
        self._record("relinquish_ownership")


def fake_runtime_worker(connection, configuration):
    """Spawn-safe implementation of the generalized runtime wire protocol."""

    connection.send(
        {
            "kind": "ready",
            "worker_pid": os.getpid(),
            "selected_address": configuration.address,
            "selected_address_role": "primary",
            "selected_resolved_addresses": [],
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
            if method == "get_data":
                result = {"time": [0.0, 1e-6], "ch1": [0.1, 0.2], "ch2": [0, 5]}
            elif method == "summary":
                result = {"worker_pid": os.getpid(), "status": "ok"}
            elif method == "replay_configuration":
                waveform, run = message.get("args", ())
                result = {
                    "waveform": waveform.name,
                    "repeat_count": run.repeat_count,
                    "point_count": waveform.point_count,
                }
            else:
                result = None
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


def short_timeouts():
    return WorkerTimeouts(
        get_data_s=0.1,
        rpc_s=0.5,
        startup_s=3.0,
        cleanup_s=0.25,
        terminate_grace_s=0.5,
        kill_grace_s=0.5,
        poll_interval_s=0.01,
    )


class SdkAdapterTests(unittest.TestCase):
    def test_timebase_requires_positive_end_and_documented_frame_length(self):
        with self.assertRaisesRegex(ValueError, "positive end"):
            MokuRuntimeConfiguration(
                address="fake",
                timebase_start_s=-2.0,
                timebase_end_s=-1.0,
            )
        with self.assertRaisesRegex(ValueError, "one of 128"):
            MokuRuntimeConfiguration(
                address="fake",
                timebase_max_length=1_000,
            )

    def make_session(self, fail_at=None):
        calls = []
        configuration = MokuRuntimeConfiguration(address="fake")
        session = MokuSdkSession(
            FakeMultiInstrument(calls, fail_at),
            FakeAwg(calls, fail_at),
            FakeOscilloscope(calls, fail_at),
            configuration,
            selected_address="fake",
        )
        return session, calls

    def test_full_replay_is_output_disabled_and_uses_verified_routes(self):
        session, calls = self.make_session()
        summary = session.replay_configuration(WAVEFORM, FINITE_RUN)
        names = [call[0] for call in calls]
        self.assertEqual(names[:2], ["enable_output", "enable_output"])
        self.assertTrue(all(not call[1]["enable"] for call in calls[:2]))
        connections = next(call[1] for call in calls if call[0] == "set_connections")
        self.assertEqual(connections["connections"], list(MOKU_GO_MIM_CONNECTIONS))
        frontend = next(call[1] for call in calls if call[0] == "set_frontend")
        self.assertEqual(frontend["attenuation"], "0dB")
        self.assertNotIn("set_output", names)
        self.assertFalse(any(
            name == "enable_output" and values["enable"]
            for name, values, *rest in calls
        ))
        self.assertTrue(summary["outputs_confirmed_disabled"])
        burst = next(call[1] for call in calls if call[0] == "burst_modulate")
        self.assertEqual(burst["trigger_source"], "Manual")
        self.assertEqual(burst["trigger_mode"], "NCycle")
        self.assertEqual(burst["burst_cycles"], 5)

    def test_connect_uses_fallback_and_disables_new_awg_before_routing(self):
        calls = []

        class AwgType:
            pass

        class OscType:
            pass

        class FakeMimConstructor(FakeMultiInstrument):
            def __init__(self, address, **kwargs):
                calls.append(("connect", {"address": address, **kwargs}))
                if address == "primary":
                    raise ConnectionError("primary unavailable")
                super().__init__(calls)

            def set_instrument(self, slot, instrument_type):
                calls.append(("set_instrument", {"slot": slot}))
                if instrument_type is AwgType:
                    return FakeAwg(calls)
                if instrument_type is OscType:
                    return FakeOscilloscope(calls)
                raise AssertionError("unexpected fake instrument type")

        session = MokuSdkSession.connect(
            MokuRuntimeConfiguration(
                address="primary",
                fallback_address="fallback",
            ),
            sdk_types=(FakeMimConstructor, AwgType, OscType),
            address_resolver=lambda address: (f"resolved-{address}",),
        )
        self.assertEqual(session.selected_address, "fallback")
        connect_addresses = [
            call[1]["address"] for call in calls if call[0] == "connect"
        ]
        self.assertEqual(connect_addresses, ["primary", "fallback"])
        route_index = next(
            index for index, call in enumerate(calls) if call[0] == "set_connections"
        )
        disable_calls = [
            call
            for call in calls[:route_index]
            if call[0] == "enable_output"
        ]
        self.assertGreaterEqual(len(disable_calls), 2)
        self.assertTrue(all(not call[1]["enable"] for call in disable_calls))

    def test_every_replay_failure_point_precedes_any_enable_true(self):
        for fail_at in (
            "enable_output",
            "set_connections",
            "set_frontend",
            "set_timebase",
            "set_trigger",
            "generate_waveform",
            "disable_modulation",
            "burst_modulate",
            "mim_summary",
            "awg_summary",
            "osc_summary",
        ):
            with self.subTest(fail_at=fail_at):
                session, calls = self.make_session(fail_at)
                with self.assertRaises(MokuConfigurationReplayError):
                    session.replay_configuration(WAVEFORM, FINITE_RUN)
                self.assertFalse(any(
                    name == "enable_output" and values["enable"]
                    for name, values, *rest in calls
                ))

    def test_channel_semantics_are_normalized_in_one_adapter(self):
        session, _ = self.make_session()
        data = session.get_data(timeout=1.0)
        self.assertEqual(data["photodiode_v"], data["ch1"])
        self.assertEqual(data["waveform_reference_v"], data["ch2"])


class RuntimeStateTests(unittest.TestCase):
    def test_switch_marks_exactly_one_frame_for_discard(self):
        session = FakeRuntimeSession()
        runtime = MokuRuntime(
            MokuRuntimeConfiguration(address="fake"),
            SessionFactory([session]),
        )
        runtime.configure(WAVEFORM, CONTINUOUS_RUN)
        runtime.start()
        first = runtime.get_frame()
        second = runtime.get_frame()
        self.assertFalse(first.accepted)
        self.assertEqual(first.discard_reason, "waveform_start")
        self.assertTrue(second.accepted)
        runtime.switch_waveform(WAVEFORM, CONTINUOUS_RUN)
        switched = runtime.get_frame()
        self.assertFalse(switched.accepted)
        self.assertEqual(switched.discard_reason, "waveform_switch")
        self.assertEqual(runtime.state.waveform_run_id, 2)

    def test_continuous_recovery_restarts_phase_and_marks_frame(self):
        old = FakeRuntimeSession()
        replacement = FakeRuntimeSession()
        runtime = MokuRuntime(
            MokuRuntimeConfiguration(address="fake"),
            SessionFactory([old, replacement]),
        )
        runtime.configure(WAVEFORM, CONTINUOUS_RUN)
        runtime.start()
        original_run_id = runtime.state.waveform_run_id
        runtime.recover()
        self.assertIn(("relinquish_ownership",), old.calls)
        self.assertEqual(replacement.calls[0][0], "replay_configuration")
        self.assertEqual(replacement.calls[1][0], "activate")
        self.assertEqual(runtime.state.session_id, 2)
        self.assertEqual(runtime.state.waveform_session_id, 2)
        self.assertEqual(runtime.state.waveform_run_id, original_run_id)
        self.assertEqual(
            runtime.state.continuity,
            WaveformContinuity.RESTARTED_FROM_PHASE_ZERO,
        )
        frame = runtime.get_frame()
        self.assertEqual(frame.discard_reason, "session_recovery")

    def test_finite_burst_recovery_never_retriggers(self):
        old = FakeRuntimeSession()
        replacement = FakeRuntimeSession()
        runtime = MokuRuntime(
            MokuRuntimeConfiguration(address="fake"),
            SessionFactory([old, replacement]),
        )
        runtime.configure(WAVEFORM, FINITE_RUN)
        runtime.start()
        with self.assertRaises(FiniteBurstIndeterminateError):
            runtime.recover()
        self.assertEqual(
            [call[0] for call in replacement.calls],
            ["replay_configuration"],
        )
        self.assertTrue(runtime.state.finite_burst_indeterminate)
        self.assertEqual(runtime.state.output_state, OutputState.DISABLED)

    def test_first_finite_burst_frame_is_accepted_without_retrigger(self):
        session = FakeRuntimeSession()
        runtime = MokuRuntime(
            MokuRuntimeConfiguration(address="fake"),
            SessionFactory([session]),
        )
        runtime.configure(WAVEFORM, FINITE_RUN)
        runtime.start()
        frame = runtime.get_frame()
        self.assertTrue(frame.accepted)
        self.assertIsNone(frame.discard_reason)
        self.assertEqual(
            [call[0] for call in session.calls].count("activate"),
            1,
        )

    def test_replay_failure_never_calls_activate(self):
        error = MokuConfigurationReplayError(
            "upload",
            outputs_confirmed_disabled=True,
            cause=RuntimeError("fake"),
        )
        session = FakeRuntimeSession(replay_error=error)
        runtime = MokuRuntime(
            MokuRuntimeConfiguration(address="fake"),
            SessionFactory([session]),
        )
        with self.assertRaises(MokuConfigurationReplayError):
            runtime.switch_waveform(WAVEFORM, CONTINUOUS_RUN)
        self.assertEqual([call[0] for call in session.calls], ["replay_configuration"])
        self.assertEqual(runtime.state.output_state, OutputState.DISABLED)

    def test_close_uses_fresh_output_off_session_after_ambiguous_failure(self):
        old = FakeRuntimeSession(disable_error=RuntimeError("ambiguous disable"))
        cleanup = FakeRuntimeSession()
        runtime = MokuRuntime(
            MokuRuntimeConfiguration(address="fake"),
            SessionFactory([old, cleanup]),
        )
        runtime.close()
        self.assertIn(("relinquish_ownership",), old.calls)
        self.assertEqual(
            cleanup.calls,
            [("disable_all_outputs",), ("relinquish_ownership",)],
        )
        self.assertEqual(runtime.state.output_state, OutputState.DISABLED)


class ProcessBoundaryTests(unittest.TestCase):
    def test_compiled_objects_cross_spawn_and_parent_never_imports_sdk(self):
        sys.modules.pop("moku", None)
        runtime = ProcessIsolatedMokuRuntime(
            MokuRuntimeConfiguration(address="fake-clean"),
            worker_target=fake_runtime_worker,
            timeouts=short_timeouts(),
        )
        try:
            result = runtime.configure(WAVEFORM, CONTINUOUS_RUN)
            runtime.start()
            frame = runtime.get_frame(timeout=1.0)
            self.assertEqual(result["point_count"], WAVEFORM.point_count)
            self.assertFalse(frame.accepted)
            self.assertNotIn("moku", sys.modules)
        finally:
            runtime.close()
        self.assertFalse(runtime.is_alive)

    def test_get_frame_has_hard_parent_deadline(self):
        runtime = ProcessIsolatedMokuRuntime(
            MokuRuntimeConfiguration(address="fake-hang"),
            worker_target=fake_runtime_worker,
            timeouts=short_timeouts(),
        )
        runtime.configure(WAVEFORM, CONTINUOUS_RUN)
        runtime.start()
        try:
            with self.assertRaises(AcquisitionWatchdogExpired):
                runtime.get_frame(timeout=1.0)
        finally:
            runtime.force_terminate("test_cleanup")
        self.assertFalse(runtime.is_alive)


if __name__ == "__main__":
    unittest.main()
