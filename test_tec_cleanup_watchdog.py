"""Hardware-free regression tests for independent configured TEC cleanup."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


SRC_DIRECTORY = Path(__file__).resolve().parent / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.tec_cleanup_watchdog import monitor_parent  # noqa: E402


class FakeController:
    instances = []

    def __init__(self, settings):
        self.settings = settings
        self.actions = []
        self.initial_target_c = None
        self.initial_output_enabled = None
        self.__class__.instances.append(self)

    def connect(self):
        self.actions.append("connect")

    def apply_completion_behavior(self, behavior):
        self.actions.append(behavior)

    def close(self):
        self.actions.append("close")


class TecCleanupWatchdogTests(unittest.TestCase):
    def test_parent_loss_applies_saved_completion_behavior_from_fresh_controller(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = root / "cleanup.json"
            config.write_text(
                json.dumps(
                    {
                        "settings": {
                            "serial_port": "COM_TEST",
                            "channel": 1,
                            "min_target_c": 10.0,
                            "max_target_c": 40.0,
                            "safe_target_c": None,
                            "sampling_interval_s": 1.0,
                            "object_temperature_min_c": 0.0,
                            "object_temperature_max_c": 50.0,
                            "sink_temperature_min_c": 0.0,
                            "sink_temperature_max_c": 50.0,
                        },
                        "completion_behavior": "disable_output",
                        "initial_target_c": 25.0,
                        "initial_output_enabled": True,
                    }
                ),
                encoding="utf-8",
            )
            FakeController.instances.clear()
            result = monitor_parent(
                parent_pid=123,
                configuration_path=config,
                disarm_path=root / "not-disarmed",
                log_path=root / "cleanup.jsonl",
                process_is_running=lambda _pid: False,
                sleep=lambda _seconds: None,
                controller_factory=FakeController,
            )

            self.assertEqual(result, 0)
            self.assertEqual(
                FakeController.instances[0].actions,
                ["connect", "disable_output", "close"],
            )
            self.assertIn(
                "tec_emergency_completion_applied",
                (root / "cleanup.jsonl").read_text(encoding="utf-8"),
            )

    def test_disarm_prevents_any_controller_connection(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            disarm = root / "disarm"
            disarm.touch()
            FakeController.instances.clear()
            result = monitor_parent(
                parent_pid=123,
                configuration_path=root / "unused.json",
                disarm_path=disarm,
                log_path=root / "cleanup.jsonl",
                process_is_running=lambda _pid: True,
                sleep=lambda _seconds: None,
                controller_factory=FakeController,
            )
            self.assertEqual(result, 0)
            self.assertEqual(FakeController.instances, [])


if __name__ == "__main__":
    unittest.main()
