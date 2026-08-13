"""End-to-end configured runner tests using only fake devices and clocks."""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parent
SRC_DIRECTORY = REPOSITORY_ROOT / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.config import load_experiment  # noqa: E402
from eom_stabilisation.config.models import (  # noqa: E402
    CompletionMode,
    CompletionPolicy,
)
from eom_stabilisation.experiment.checkpoint import (  # noqa: E402
    AtomicCheckpointStore,
)
from eom_stabilisation.experiment.data_writer import (  # noqa: E402
    ConfiguredMokuDataWriter,
)
from eom_stabilisation.experiment.planner import (  # noqa: E402
    build_effective_plan,
    save_plan_artifacts,
)
from eom_stabilisation.experiment.runtime_runner import (  # noqa: E402
    LinienSubprocess,
    _recover_moku,
    run_experiment_loop,
    run_hardware_experiment,
)
from eom_stabilisation.experiment.supervisor import ExperimentSupervisor  # noqa: E402
from eom_stabilisation.moku.models import (  # noqa: E402
    OutputState,
    RunMode,
    WaveformContinuity,
)
from eom_stabilisation.moku.runtime import RuntimeFrame  # noqa: E402
from eom_stabilisation.tec.interface import TecSnapshot  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.base = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.now

    def utc_now(self) -> datetime:
        return self.base + timedelta(seconds=self.now)

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeMokuRuntime:
    def __init__(self) -> None:
        self.state = SimpleNamespace(
            session_id=1,
            output_state=OutputState.DISABLED,
            pending_discard_reason=None,
        )
        self.waveform = None
        self.run = None
        self.closed = False
        self.disable_calls = 0
        self.summary_calls = 0

    def summary(self):
        self.summary_calls += 1
        return {"fake": True, "outputs_confirmed_disabled": True}

    def switch_waveform(self, waveform, run, *, start=True):
        self.waveform = waveform
        self.run = run
        self.state.output_state = OutputState.ENABLED if start else OutputState.DISABLED

    def get_frame(self, **_):
        period = self.waveform.achieved_period_s
        time_axis = np.linspace(0.0, period, 2000, endpoint=False)
        voltage = np.where(time_axis < period * 0.25, 0.9, 0.1)
        return RuntimeFrame(
            data={"time": time_axis, "ch1": voltage, "ch2": voltage},
            accepted=True,
            discard_reason=None,
            session_id=self.state.session_id,
            waveform_run_id=1,
            waveform_name=self.waveform.name,
            waveform_timing_sha256=self.waveform.timing_sha256,
            continuity=WaveformContinuity.CONTINUOUS,
        )

    def disable(self):
        self.disable_calls += 1
        self.state.output_state = OutputState.DISABLED

    def close(self):
        self.disable()
        self.closed = True


class InterruptingMokuRuntime(FakeMokuRuntime):
    def get_frame(self, **_):
        raise KeyboardInterrupt


class FailingDispatchMokuRuntime(FakeMokuRuntime):
    def switch_waveform(self, waveform, run, *, start=True):
        raise OSError("waveform dispatch failed")


class FailingDisableMokuRuntime(FakeMokuRuntime):
    def disable(self):
        raise OSError("output disable failed")


class FailingCleanupMokuRuntime(InterruptingMokuRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.force_terminate_calls = 0

    def close(self):
        raise OSError("Moku close failed")

    def force_terminate(self, _reason):
        self.force_terminate_calls += 1
        raise OSError("Moku force terminate failed")


class FailingCloseTec:
    def __init__(self) -> None:
        self.output_enabled = False
        self.close_calls = 0

    def connect(self):
        return SimpleNamespace()

    def close(self):
        self.close_calls += 1
        raise OSError("TEC close failed")


class InterruptingReadOnlyTec:
    """Fake passive logger source which interrupts after three timed reads."""

    def __init__(self) -> None:
        self.output_enabled = True
        self.read_calls = 0
        self.close_calls = 0
        self.completion_calls: list[str] = []

    @staticmethod
    def _snapshot() -> TecSnapshot:
        return TecSnapshot(
            object_temperature_c=24.5,
            sink_temperature_c=22.0,
            temperature_stable=True,
            output_current_a=0.1,
            output_voltage_v=0.5,
            active_target_c=25.0,
            controller_status="2",
            error_message=None,
        )

    def connect(self):
        return self._snapshot()

    def read_snapshot(self):
        if self.read_calls >= 3:
            raise KeyboardInterrupt
        self.read_calls += 1
        return self._snapshot()

    def apply_completion_behavior(self, behavior):
        self.completion_calls.append(behavior)

    def close(self):
        self.close_calls += 1


class FakeLinien:
    def __init__(self) -> None:
        self.process = None
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


class InterruptingLinien(FakeLinien):
    def start(self):
        raise KeyboardInterrupt


def duty_cycle_plan():
    return build_effective_plan(
        load_experiment(
            REPOSITORY_ROOT / "configs" / "examples" / "moku_only_duty_cycle.yaml"
        )
    )


def continuous_interrupt_plan():
    plan = duty_cycle_plan()
    action = plan.waveform_program.actions[0]
    run = replace(
        action.run,
        mode=RunMode.FOREVER,
        repeat_count=None,
        requested_duration_s=None,
        achieved_duration_s=None,
        end_policy=None,
        exact_hardware_burst=False,
    )
    program = replace(
        plan.waveform_program,
        actions=(replace(action, run=run),),
    )
    experiment = replace(
        plan.experiment,
        completion_policy=CompletionPolicy(CompletionMode.OPERATOR_CTRL_C),
    )
    return replace(plan, experiment=experiment, waveform_program=program)


class RuntimeRunnerTests(unittest.TestCase):
    def test_continuous_abort_policy_never_reenables_during_recovery(self):
        plan = continuous_interrupt_plan()
        plan = replace(
            plan,
            experiment=replace(
                plan.experiment,
                run_settings=replace(
                    plan.experiment.run_settings,
                    recovery=replace(
                        plan.experiment.run_settings.recovery,
                        continuous_waveform="abort",
                    ),
                ),
            ),
        )
        clock = FakeClock()
        supervisor = ExperimentSupervisor(plan, monotonic=clock.monotonic)
        supervisor.start(now=clock.monotonic())

        class RecoveryRuntime:
            def __init__(self):
                self.starts = []

            def recover(self, *, start=True):
                self.starts.append(start)

        runtime = RecoveryRuntime()
        writer = SimpleNamespace(flush=lambda: None)
        events = SimpleNamespace(write=lambda *args, **kwargs: None)
        with self.assertRaisesRegex(RuntimeError, "policy is abort"):
            _recover_moku(
                supervisor=supervisor,
                runtime=runtime,
                moku_writer=writer,
                event_writer=events,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(runtime.starts, [False])

    def test_linien_subprocess_exports_only_the_configured_host(self):
        class FakeProcess:
            pid = 1234

            @staticmethod
            def poll():
                return None

        captured_environment = None

        def fake_popen(*_, **kwargs):
            nonlocal captured_environment
            captured_environment = kwargs["env"]
            Path(captured_environment["EOM_READY_FILE"]).write_text(
                captured_environment["EOM_COMPONENT_OUTPUT_FILE"],
                encoding="utf-8",
            )
            return FakeProcess()

        with tempfile.TemporaryDirectory() as temporary:
            wrapper = LinienSubprocess(
                Path(temporary),
                host="red-pitaya.example.test",
            )
            with patch(
                "eom_stabilisation.experiment.runtime_runner.subprocess.Popen",
                side_effect=fake_popen,
            ):
                wrapper.start(timeout_s=0.5)

        self.assertEqual(
            captured_environment["LINIEN_HOST"],
            "red-pitaya.example.test",
        )

    def test_hardware_wrapper_constructs_tec_for_temp_log_without_schedule(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "run.yaml").write_text(
                """temperature:
  serial_port: COM_TEST
  channel: 1
  min_target_c: 10
  max_target_c: 40
  sampling_interval_s: 1
  object_temperature_min_c: 0
  object_temperature_max_c: 50
  sink_temperature_min_c: 0
  sink_temperature_max_c: 50
""",
                encoding="utf-8",
            )
            master = root / "experiment.yaml"
            master.write_text(
                """name: passive_temperature_log
components: [temp-log]
run_settings_file: run.yaml
end_when: operator_ctrl_c
""",
                encoding="utf-8",
            )
            plan = build_effective_plan(load_experiment(master))
            run_directory = root / "artifacts"
            run_directory.mkdir()
            save_plan_artifacts(plan, run_directory)

            with (
                patch(
                    "eom_stabilisation.tec.mecom_adapter.MeComTecController"
                ) as controller_class,
                patch(
                    "eom_stabilisation.experiment.runtime_runner.run_experiment_loop",
                    return_value=17,
                ) as loop,
            ):
                result = run_hardware_experiment(plan, run_directory)

            self.assertEqual(result, 17)
            controller_class.assert_called_once_with(
                plan.experiment.run_settings.temperature
            )
            self.assertIs(
                loop.call_args.kwargs["tec_controller"],
                controller_class.return_value,
            )

            (root / "run.yaml").write_text(
                """temperature:
  serial_port: COM_TEST
  channel: 1
  min_target_c: 10
  max_target_c: 40
""",
                encoding="utf-8",
            )
            incomplete_plan = build_effective_plan(load_experiment(master))
            incomplete_directory = root / "incomplete"
            incomplete_directory.mkdir()
            save_plan_artifacts(incomplete_plan, incomplete_directory)
            with (
                patch(
                    "eom_stabilisation.tec.mecom_adapter.MeComTecController"
                ) as incomplete_controller,
                patch(
                    "eom_stabilisation.experiment.runtime_runner.run_experiment_loop"
                ) as incomplete_loop,
            ):
                with self.assertRaisesRegex(ValueError, "all four"):
                    run_hardware_experiment(
                        incomplete_plan,
                        incomplete_directory,
                    )
            incomplete_controller.assert_not_called()
            incomplete_loop.assert_not_called()

    def test_hardware_wrapper_requires_and_passes_configured_linien_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_path = root / "run.yaml"
            run_path.write_text(
                """linien:
  host: red-pitaya.example.test
""",
                encoding="utf-8",
            )
            master = root / "experiment.yaml"
            master.write_text(
                """name: lock_log
components: [lock]
run_settings_file: run.yaml
end_when: operator_ctrl_c
""",
                encoding="utf-8",
            )
            plan = build_effective_plan(load_experiment(master))
            run_directory = root / "artifacts"
            run_directory.mkdir()
            save_plan_artifacts(plan, run_directory)

            with (
                patch(
                    "eom_stabilisation.experiment.runtime_runner.LinienSubprocess"
                ) as linien_class,
                patch(
                    "eom_stabilisation.experiment.runtime_runner.run_experiment_loop",
                    return_value=23,
                ) as loop,
            ):
                result = run_hardware_experiment(plan, run_directory)

            self.assertEqual(result, 23)
            linien_class.assert_called_once_with(
                run_directory.resolve(),
                host="red-pitaya.example.test",
            )
            self.assertIs(
                loop.call_args.kwargs["linien"],
                linien_class.return_value,
            )

            run_path.write_text("{}\n", encoding="utf-8")
            missing_plan = build_effective_plan(load_experiment(master))
            missing_directory = root / "missing"
            missing_directory.mkdir()
            save_plan_artifacts(missing_plan, missing_directory)
            with patch(
                "eom_stabilisation.experiment.runtime_runner.run_experiment_loop"
            ) as missing_loop:
                with self.assertRaisesRegex(ValueError, "linien.host"):
                    run_hardware_experiment(missing_plan, missing_directory)
            missing_loop.assert_not_called()

    def test_temp_log_without_schedule_is_sampled_and_cleanup_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_settings = root / "run.yaml"
            run_settings.write_text(
                """temperature:
  serial_port: COM_TEST
  channel: 1
  min_target_c: 10
  max_target_c: 40
  sampling_interval_s: 0.2
  object_temperature_min_c: 0
  object_temperature_max_c: 50
  sink_temperature_min_c: 0
  sink_temperature_max_c: 50
""",
                encoding="utf-8",
            )
            master = root / "experiment.yaml"
            master.write_text(
                """name: passive_temperature_log
components: [temp-log]
run_settings_file: run.yaml
end_when: operator_ctrl_c
""",
                encoding="utf-8",
            )
            plan = build_effective_plan(load_experiment(master))
            run_directory = root / "run"
            run_directory.mkdir()
            save_plan_artifacts(plan, run_directory)
            clock = FakeClock()
            controller = InterruptingReadOnlyTec()

            result = run_experiment_loop(
                plan,
                run_directory,
                moku_runtime=None,
                tec_controller=controller,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                utc_now=clock.utc_now,
            )

            self.assertEqual(result, 130)
            self.assertEqual(controller.close_calls, 1)
            self.assertEqual(controller.completion_calls, [])
            self.assertTrue(controller.output_enabled)
            with (run_directory / "temperature" / "tec_log.csv").open(
                encoding="utf-8", newline=""
            ) as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 4)  # preflight plus three timed reads
            self.assertEqual(rows[0]["schedule_state"], "preflight")
            self.assertTrue(
                all(
                    row["schedule_state"] == "read_only_logging"
                    for row in rows[1:]
                )
            )
            sample_times = [float(row["elapsed_s"]) for row in rows[1:]]
            self.assertAlmostEqual(sample_times[0], 0.0)
            sample_gaps = [
                later - earlier
                for earlier, later in zip(sample_times, sample_times[1:])
            ]
            self.assertTrue(all(0.2 <= gap <= 0.25 for gap in sample_gaps))

    def test_fake_moku_run_writes_exact_outputs_and_cleans_up(self):
        plan = duty_cycle_plan()
        clock = FakeClock()
        runtime = FakeMokuRuntime()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary) / "run"
            run_directory.mkdir()
            save_plan_artifacts(plan, run_directory)
            result = run_experiment_loop(
                plan,
                run_directory,
                moku_runtime=runtime,
                tec_controller=None,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                utc_now=clock.utc_now,
            )
            self.assertEqual(result, 0)
            self.assertTrue(runtime.closed)
            self.assertGreaterEqual(runtime.disable_calls, 1)
            self.assertTrue((run_directory / "moku" / "moku_samples.csv").is_file())
            self.assertTrue(
                (run_directory / "moku" / "moku_sample_provenance.csv").is_file()
            )
            self.assertTrue((run_directory / "runtime_checkpoint.json").is_file())
            self.assertTrue((run_directory / "experiment_events.jsonl").is_file())
            with (run_directory / "waveform_timeline.csv").open(
                encoding="utf-8", newline=""
            ) as source:
                timeline = list(csv.DictReader(source))
            self.assertTrue(timeline)
            self.assertIn("experiment_started", {row["event"] for row in timeline})
            self.assertTrue((run_directory / "waveform_timeline.png").is_file())
            manifest = json.loads(
                (run_directory / "experiment_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["exit_code"], 0)

            checkpoint = AtomicCheckpointStore(
                run_directory / "runtime_checkpoint.json"
            ).load()
            self.assertEqual(
                checkpoint.moku.last_confirmed_output_state,
                "disabled",
            )
            with (
                run_directory / "moku" / "moku_sample_provenance.csv"
            ).open(encoding="utf-8", newline="") as source:
                provenance = list(csv.DictReader(source))
            self.assertEqual(
                checkpoint.moku.last_valid_sample_timestamp_utc,
                provenance[-1]["timestamp_utc"],
            )

            samples = (run_directory / "moku" / "moku_samples.csv").read_text(
                encoding="utf-8"
            )
            self.assertIn("minimum_voltage", samples)
            self.assertIn("high_level_voltage", samples)
            self.assertNotIn("maximum_voltage", samples)

    def test_fresh_run_refuses_existing_runtime_output_before_device_use(self):
        plan = duty_cycle_plan()
        clock = FakeClock()
        runtime = FakeMokuRuntime()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary) / "run"
            run_directory.mkdir()
            save_plan_artifacts(plan, run_directory)
            event_path = run_directory / "experiment_events.jsonl"
            event_path.write_text("sentinel\n", encoding="utf-8")

            with self.assertRaisesRegex(FileExistsError, "runtime output"):
                run_experiment_loop(
                    plan,
                    run_directory,
                    moku_runtime=runtime,
                    tec_controller=None,
                    monotonic=clock.monotonic,
                    sleep=clock.sleep,
                    utc_now=clock.utc_now,
                )
            self.assertEqual(runtime.summary_calls, 0)
            self.assertEqual(event_path.read_text(encoding="utf-8"), "sentinel\n")

    def test_resume_preserves_elapsed_time_and_last_valid_sample(self):
        plan = duty_cycle_plan()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary) / "run"
            run_directory.mkdir()
            save_plan_artifacts(plan, run_directory)
            first_clock = FakeClock()
            self.assertEqual(
                run_experiment_loop(
                    plan,
                    run_directory,
                    moku_runtime=FakeMokuRuntime(),
                    tec_controller=None,
                    monotonic=first_clock.monotonic,
                    sleep=first_clock.sleep,
                    utc_now=first_clock.utc_now,
                ),
                0,
            )
            store = AtomicCheckpointStore(run_directory / "runtime_checkpoint.json")
            prior = store.load()
            prior_timestamp = prior.moku.last_valid_sample_timestamp_utc
            manifest_before = json.loads(
                (run_directory / "experiment_manifest.json").read_text(
                    encoding="utf-8"
                )
            )

            resume_clock = FakeClock()
            self.assertEqual(
                run_experiment_loop(
                    plan,
                    run_directory,
                    moku_runtime=FakeMokuRuntime(),
                    tec_controller=None,
                    monotonic=resume_clock.monotonic,
                    sleep=resume_clock.sleep,
                    utc_now=resume_clock.utc_now,
                    resume=True,
                    checkpoint=prior,
                ),
                0,
            )
            resumed = store.load()
            self.assertEqual(
                resumed.moku.last_valid_sample_timestamp_utc,
                prior_timestamp,
            )
            self.assertGreaterEqual(
                resumed.experiment_elapsed_s,
                prior.experiment_elapsed_s,
            )
            manifest_after = json.loads(
                (run_directory / "experiment_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest_after["started_timestamp_utc"],
                manifest_before["started_timestamp_utc"],
            )
            self.assertIn("last_resumed_timestamp_utc", manifest_after)
            events = [
                json.loads(line)
                for line in (run_directory / "experiment_events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            resumed_event = next(
                event for event in events if event["event"] == "experiment_resumed"
            )
            self.assertGreaterEqual(
                resumed_event["elapsed_s"],
                prior.experiment_elapsed_s,
            )

    def test_resume_rejects_output_checkpoint_mismatch_before_device_use(self):
        plan = duty_cycle_plan()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary) / "run"
            run_directory.mkdir()
            save_plan_artifacts(plan, run_directory)
            clock = FakeClock()
            run_experiment_loop(
                plan,
                run_directory,
                moku_runtime=FakeMokuRuntime(),
                tec_controller=None,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                utc_now=clock.utc_now,
            )
            store = AtomicCheckpointStore(run_directory / "runtime_checkpoint.json")
            checkpoint = store.load()
            mismatched = replace(
                checkpoint,
                moku=replace(
                    checkpoint.moku,
                    last_valid_sample_timestamp_utc="2026-08-11T12:59:59Z",
                ),
            )
            store.save(mismatched)
            runtime = FakeMokuRuntime()

            with self.assertRaisesRegex(ValueError, "timestamp mismatch"):
                run_experiment_loop(
                    plan,
                    run_directory,
                    moku_runtime=runtime,
                    tec_controller=None,
                    monotonic=FakeClock().monotonic,
                    resume=True,
                    checkpoint=mismatched,
                )
            self.assertEqual(runtime.summary_calls, 0)

    def test_cleanup_failures_do_not_skip_remaining_cleanup_or_manifest(self):
        plan = continuous_interrupt_plan()
        clock = FakeClock()
        runtime = FailingCleanupMokuRuntime()
        tec = FailingCloseTec()
        linien = FakeLinien()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary) / "run"
            run_directory.mkdir()
            save_plan_artifacts(plan, run_directory)
            with patch.object(
                ConfiguredMokuDataWriter,
                "flush",
                side_effect=OSError("buffer flush failed"),
            ):
                result = run_experiment_loop(
                    plan,
                    run_directory,
                    moku_runtime=runtime,
                    tec_controller=tec,
                    linien=linien,
                    monotonic=clock.monotonic,
                    sleep=clock.sleep,
                    utc_now=clock.utc_now,
                )

            self.assertEqual(result, 1)
            self.assertEqual(runtime.force_terminate_calls, 1)
            self.assertEqual(tec.close_calls, 1)
            self.assertTrue(linien.closed)
            manifest = json.loads(
                (run_directory / "experiment_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "failed")
            checkpoint = AtomicCheckpointStore(
                run_directory / "runtime_checkpoint.json"
            ).load()
            self.assertEqual(
                checkpoint.moku.last_confirmed_output_state,
                "unknown",
            )

    def test_hardware_dispatch_failure_sets_manifest_stop_reason(self):
        plan = duty_cycle_plan()
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary) / "run"
            run_directory.mkdir()
            save_plan_artifacts(plan, run_directory)
            result = run_experiment_loop(
                plan,
                run_directory,
                moku_runtime=FailingDispatchMokuRuntime(),
                tec_controller=None,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                utc_now=clock.utc_now,
            )

            self.assertEqual(result, 1)
            manifest = json.loads(
                (run_directory / "experiment_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("waveform dispatch failed", manifest["stop_reason"])

    def test_keyboard_interrupt_before_supervisor_start_is_bounded(self):
        plan = duty_cycle_plan()
        clock = FakeClock()
        runtime = FakeMokuRuntime()
        linien = InterruptingLinien()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary) / "run"
            run_directory.mkdir()
            save_plan_artifacts(plan, run_directory)
            result = run_experiment_loop(
                plan,
                run_directory,
                moku_runtime=runtime,
                tec_controller=None,
                linien=linien,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                utc_now=clock.utc_now,
            )

            self.assertEqual(result, 130)
            self.assertTrue(runtime.closed)
            self.assertTrue(linien.closed)
            manifest = json.loads(
                (run_directory / "experiment_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "interrupted")
            self.assertEqual(
                manifest["stop_reason"],
                "operator_ctrl_c_during_preflight",
            )

    def test_completion_dispatch_failure_overrides_success_stop_reason(self):
        plan = duty_cycle_plan()
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary) / "run"
            run_directory.mkdir()
            save_plan_artifacts(plan, run_directory)
            result = run_experiment_loop(
                plan,
                run_directory,
                moku_runtime=FailingDisableMokuRuntime(),
                tec_controller=None,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                utc_now=clock.utc_now,
            )

            self.assertEqual(result, 1)
            manifest = json.loads(
                (run_directory / "experiment_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("moku_schedule_complete", manifest["stop_reason"])
            self.assertIn("output disable failed", manifest["stop_reason"])


if __name__ == "__main__":
    unittest.main()
