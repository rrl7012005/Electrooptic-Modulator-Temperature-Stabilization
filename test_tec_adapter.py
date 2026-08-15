"""Fake-session tests for the configured MeCom TEC adapter."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


SRC_DIRECTORY = Path(__file__).resolve().parent / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from eom_stabilisation.config.models import TemperatureControllerSettings  # noqa: E402
from eom_stabilisation.tec.mecom_adapter import (  # noqa: E402
    DEVICE_STATUS_ID,
    OBJECT_TEMPERATURE_ID,
    OUTPUT_CURRENT_ID,
    OUTPUT_ENABLE_ID,
    OUTPUT_VOLTAGE_ID,
    SINK_TEMPERATURE_ID,
    TARGET_TEMPERATURE_ID,
    TEMPERATURE_STABLE_ID,
    MeComTecController,
    TecConnectionError,
)


class FakeSession:
    def __init__(self, *, serialport: str) -> None:
        self.serialport = serialport
        self.values = {
            DEVICE_STATUS_ID: 2,
            OBJECT_TEMPERATURE_ID: 24.5,
            SINK_TEMPERATURE_ID: 22.0,
            OUTPUT_CURRENT_ID: 0.1,
            OUTPUT_VOLTAGE_ID: 0.5,
            TEMPERATURE_STABLE_ID: 1,
            OUTPUT_ENABLE_ID: 0,
            TARGET_TEMPERATURE_ID: 25.0,
        }
        self.writes: list[tuple[int, float, int]] = []
        self.stopped = False

    def identify(self) -> int:
        return 7

    def get_parameter(self, *, parameter_id, address, parameter_instance):
        if address != 7 or parameter_instance != 1:
            raise AssertionError("wrong address/channel")
        return self.values[parameter_id]

    def set_parameter(
        self, *, parameter_id, value, address, parameter_instance
    ) -> bool:
        if address != 7 or parameter_instance != 1:
            raise AssertionError("wrong address/channel")
        self.values[parameter_id] = value
        self.writes.append((parameter_id, value, parameter_instance))
        return True

    def stop(self) -> None:
        self.stopped = True


class TecAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = FakeSession(serialport="COM_TEST")
        settings = TemperatureControllerSettings(
            serial_port="COM_TEST",
            channel=1,
            min_target_c=20.0,
            max_target_c=35.0,
            safe_target_c=24.0,
            object_temperature_min_c=5.0,
            object_temperature_max_c=45.0,
            sink_temperature_min_c=5.0,
            sink_temperature_max_c=45.0,
        )
        self.controller = MeComTecController(
            settings,
            session_factory=lambda **_: self.session,
        )

    def test_connect_is_read_only_and_records_initial_state(self):
        reading = self.controller.connect()
        self.assertEqual(reading.object_temperature_c, 24.5)
        self.assertEqual(self.controller.identify(), "MeCom address 7")
        self.assertEqual(self.controller.initial_target_c, 25.0)
        self.assertFalse(self.controller.initial_output_enabled)
        self.assertEqual(self.session.writes, [])

    def test_parameter_1200_maps_only_status_two_to_stable(self):
        self.controller.connect()
        for raw_status, expected in ((0, False), (1, False), (2, True)):
            with self.subTest(raw_status=raw_status):
                self.session.values[TEMPERATURE_STABLE_ID] = raw_status
                self.assertIs(
                    self.controller.read_snapshot().temperature_stable,
                    expected,
                )

    def test_target_and_output_writes_are_validated_and_read_back(self):
        self.controller.connect()
        self.controller.set_target_temperature(30.0)
        self.controller.set_output_enabled(True)
        self.assertEqual(self.session.values[TARGET_TEMPERATURE_ID], 30.0)
        self.assertEqual(self.session.values[OUTPUT_ENABLE_ID], 1)
        self.assertEqual(
            [item[0] for item in self.session.writes],
            [TARGET_TEMPERATURE_ID, OUTPUT_ENABLE_ID],
        )
        with self.assertRaisesRegex(ValueError, "outside configured limits"):
            self.controller.set_target_temperature(50.0)

    def test_completion_behaviors_are_explicit(self):
        self.controller.connect()
        self.controller.apply_completion_behavior("hold_current_target")
        self.assertEqual(self.session.writes, [])

        self.controller.apply_completion_behavior("disable_output")
        self.assertEqual(self.session.writes[-1][0], OUTPUT_ENABLE_ID)
        self.assertEqual(self.session.writes[-1][1], 0)

        self.controller.apply_completion_behavior("return_to_safe_target")
        self.assertEqual(self.session.values[TARGET_TEMPERATURE_ID], 24.0)
        self.assertEqual(self.session.values[OUTPUT_ENABLE_ID], 1)

    def test_controller_error_blocks_connection_without_writes(self):
        self.session.values[DEVICE_STATUS_ID] = 3
        with self.assertRaises(TecConnectionError):
            self.controller.connect()
        self.assertEqual(self.session.writes, [])
        self.assertTrue(self.session.stopped)

    def test_close_never_changes_output(self):
        self.controller.connect()
        self.controller.close()
        self.assertTrue(self.session.stopped)
        self.assertEqual(self.session.writes, [])

    def test_real_connect_requires_complete_apparatus_sensor_bounds(self):
        factory_called = False

        def factory(**_):
            nonlocal factory_called
            factory_called = True
            return self.session

        controller = MeComTecController(
            TemperatureControllerSettings(
                serial_port="COM_TEST",
                channel=1,
                min_target_c=20.0,
                max_target_c=35.0,
            ),
            session_factory=factory,
        )
        with self.assertRaisesRegex(TecConnectionError, "plausibility bounds"):
            controller.connect()
        self.assertFalse(factory_called)

    def test_every_snapshot_validates_both_sensor_bounds(self):
        self.controller.connect()
        self.session.values[OBJECT_TEMPERATURE_ID] = 100.0
        with self.assertRaisesRegex(TecConnectionError, "object temperature"):
            self.controller.read_snapshot()
        self.session.values[OBJECT_TEMPERATURE_ID] = 24.5
        self.session.values[SINK_TEMPERATURE_ID] = -20.0
        with self.assertRaisesRegex(TecConnectionError, "sink temperature"):
            self.controller.read_snapshot()

    def test_output_enable_reads_reject_nonbinary_values(self):
        self.session.values[OUTPUT_ENABLE_ID] = 2
        with self.assertRaisesRegex(TecConnectionError, "expected exactly 0 or 1"):
            self.controller.connect()
        self.assertTrue(self.session.stopped)


if __name__ == "__main__":
    unittest.main()
