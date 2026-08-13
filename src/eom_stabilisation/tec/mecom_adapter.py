"""Lazy, safety-checked MeCom adapter for configured experiments.

The parameter identifiers are the same verified identifiers retained by the
existing ``tec_temperature_controller.py`` implementation.  This adapter does
not change PID, thermistor, polarity, limits, sensor/input selection, or any
other apparatus configuration.  Importing it never opens a serial port.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable

from eom_stabilisation.config.models import TemperatureControllerSettings

from .interface import TecSnapshot


LOGGER = logging.getLogger(__name__)

# Verified existing TEC-1091 volatile/status parameter identifiers.  Do not
# extend this list without checking the official protocol or an independently
# verified implementation for the controller firmware in use.
DEVICE_STATUS_ID = 104
OBJECT_TEMPERATURE_ID = 1000
SINK_TEMPERATURE_ID = 1001
OUTPUT_CURRENT_ID = 1020
OUTPUT_VOLTAGE_ID = 1021
TEMPERATURE_STABLE_ID = 1200
OUTPUT_ENABLE_ID = 2010
TARGET_TEMPERATURE_ID = 3000


class TecConnectionError(RuntimeError):
    """Raised when the configured controller cannot be safely prepared."""


class MeComTecController:
    """Implement the hardware-neutral TEC protocol using a lazy MeCom session."""

    def __init__(
        self,
        settings: TemperatureControllerSettings,
        *,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self._session_factory = session_factory
        self._session: Any | None = None
        self._address: Any | None = None
        self._identity: str | None = None
        self.initial_target_c: float | None = None
        self.initial_output_enabled: bool | None = None
        self.output_enabled: bool | None = None

    @property
    def connected(self) -> bool:
        return self._session is not None and self._address is not None

    def _factory(self) -> Callable[..., Any]:
        if self._session_factory is not None:
            return self._session_factory
        try:
            from mecom import MeComSerial
        except ModuleNotFoundError as error:
            raise ModuleNotFoundError(
                "pyMeCom is required only for an explicitly confirmed TEC run."
            ) from error
        return MeComSerial

    def connect(self) -> TecSnapshot:
        """Connect, identify, and read finite preflight values without writing."""

        if self.connected:
            raise TecConnectionError("TEC controller is already connected.")
        if self.settings.channel != 1:
            raise TecConnectionError(
                "Only verified TEC-1091 channel 1 is supported by this adapter."
            )
        self._require_sensor_bounds()
        factory = self._factory()
        try:
            self._session = factory(serialport=self.settings.serial_port)
            self._address = self._session.identify()
            self._identity = f"MeCom address {self._address}"
            snapshot = self.read_snapshot()
            self.initial_target_c = snapshot.active_target_c
            self.initial_output_enabled = self.output_enabled
        except Exception:
            self.close()
            raise
        if snapshot.controller_status == "error":
            self.close()
            raise TecConnectionError(
                "TEC reported its error state during preflight; no setting was written."
            )
        LOGGER.info(
            "TEC preflight identity=%s object=%.6g C sink=%.6g C channel=%d",
            self._identity,
            snapshot.object_temperature_c,
            snapshot.sink_temperature_c,
            self.settings.channel,
        )
        return snapshot

    def identify(self) -> str:
        if not self.connected or self._identity is None:
            raise TecConnectionError("TEC controller has not been connected.")
        return self._identity

    def _read(self, parameter_id: int) -> Any:
        if not self.connected:
            raise TecConnectionError("TEC controller is not connected.")
        return self._session.get_parameter(
            parameter_id=parameter_id,
            address=self._address,
            parameter_instance=self.settings.channel,
        )

    def _write(self, parameter_id: int, value: Any) -> None:
        if not self.connected:
            raise TecConnectionError("TEC controller is not connected.")
        acknowledged = self._session.set_parameter(
            parameter_id=parameter_id,
            value=value,
            address=self._address,
            parameter_instance=self.settings.channel,
        )
        if not acknowledged:
            raise TecConnectionError(
                f"TEC did not acknowledge parameter {parameter_id}."
            )

    @staticmethod
    def _finite(value: Any, label: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise TecConnectionError(f"TEC returned nonnumeric {label}.") from error
        if not math.isfinite(number):
            raise TecConnectionError(f"TEC returned non-finite {label}: {value!r}.")
        return number

    @staticmethod
    def _binary_flag(value: Any, label: str) -> bool:
        """Accept only controller-native binary values without string coercion."""

        if value not in (0, 1) or isinstance(value, (str, bytes)):
            raise TecConnectionError(
                f"TEC returned invalid {label} {value!r}; expected exactly 0 or 1."
            )
        return bool(value)

    def _require_sensor_bounds(self) -> tuple[float, float, float, float]:
        """Return the complete apparatus-supplied sensor envelope or fail safe."""

        if not self.settings.has_sensor_bounds:
            raise TecConnectionError(
                "Real TEC access requires apparatus-verified object and sink "
                "temperature plausibility bounds in run_settings.temperature."
            )
        object_min = self.settings.object_temperature_min_c
        object_max = self.settings.object_temperature_max_c
        sink_min = self.settings.sink_temperature_min_c
        sink_max = self.settings.sink_temperature_max_c
        # ``has_sensor_bounds`` proves the Optional values are concrete.
        assert object_min is not None and object_max is not None
        assert sink_min is not None and sink_max is not None
        return (
            float(object_min),
            float(object_max),
            float(sink_min),
            float(sink_max),
        )

    def _validate_sensor_temperatures(self, object_c: float, sink_c: float) -> None:
        object_min, object_max, sink_min, sink_max = self._require_sensor_bounds()
        if not object_min <= object_c <= object_max:
            raise TecConnectionError(
                f"TEC object temperature {object_c:g} C is outside configured "
                f"plausibility bounds {object_min:g} to {object_max:g} C."
            )
        if not sink_min <= sink_c <= sink_max:
            raise TecConnectionError(
                f"TEC sink temperature {sink_c:g} C is outside configured "
                f"plausibility bounds {sink_min:g} to {sink_max:g} C."
            )

    def read_snapshot(self) -> TecSnapshot:
        """Read all runtime values, surfacing every failed or invalid read."""

        object_c = self._finite(self._read(OBJECT_TEMPERATURE_ID), "object temperature")
        sink_c = self._finite(self._read(SINK_TEMPERATURE_ID), "sink temperature")
        self._validate_sensor_temperatures(object_c, sink_c)
        current_a = self._finite(self._read(OUTPUT_CURRENT_ID), "output current")
        voltage_v = self._finite(self._read(OUTPUT_VOLTAGE_ID), "output voltage")
        target_c = self._finite(self._read(TARGET_TEMPERATURE_ID), "active target")
        stable_raw = self._read(TEMPERATURE_STABLE_ID)
        status_raw = int(self._read(DEVICE_STATUS_ID))
        output_enabled = self._binary_flag(
            self._read(OUTPUT_ENABLE_ID), "output-enable flag"
        )
        if stable_raw not in (0, 1, False, True):
            raise TecConnectionError(
                f"TEC returned invalid stability flag {stable_raw!r}."
            )
        self.output_enabled = output_enabled
        return TecSnapshot(
            object_temperature_c=object_c,
            sink_temperature_c=sink_c,
            temperature_stable=bool(stable_raw),
            output_current_a=current_a,
            output_voltage_v=voltage_v,
            active_target_c=target_c,
            controller_status="error" if status_raw == 3 else str(status_raw),
            error_message=(
                "controller reports error state" if status_raw == 3 else None
            ),
        )

    def read_active_target(self) -> float:
        return self._finite(self._read(TARGET_TEMPERATURE_ID), "active target")

    def set_target_temperature(self, target_c: float) -> None:
        """Validate, log, write, and verify one target-temperature request."""

        target = self._finite(target_c, "requested target")
        self._require_sensor_bounds()
        if not self.settings.min_target_c <= target <= self.settings.max_target_c:
            raise ValueError(
                f"Requested target {target:g} C is outside configured limits "
                f"{self.settings.min_target_c:g} to {self.settings.max_target_c:g} C."
            )
        preflight = self.read_snapshot()
        if preflight.controller_status == "error":
            raise TecConnectionError(
                "TEC is in its error state; target write was not attempted."
            )
        LOGGER.info(
            "Requesting volatile TEC target %.6g C on verified channel %d",
            target,
            self.settings.channel,
        )
        self._write(TARGET_TEMPERATURE_ID, target)
        actual = self.read_active_target()
        if not math.isclose(actual, target, rel_tol=0.0, abs_tol=0.01):
            raise TecConnectionError(
                f"TEC target verification failed: requested {target:g} C, "
                f"read back {actual:g} C."
            )

    def set_output_enabled(self, enabled: bool) -> None:
        """Explicitly change and verify output enable state."""

        if not isinstance(enabled, bool):
            raise TypeError("enabled must be boolean.")
        self._write(OUTPUT_ENABLE_ID, int(enabled))
        actual = self._binary_flag(
            self._read(OUTPUT_ENABLE_ID), "output-enable readback"
        )
        if actual is not enabled:
            raise TecConnectionError(
                "TEC output-enable verification failed: requested "
                f"{int(enabled)}, read back {int(actual)}."
            )
        self.output_enabled = enabled

    def apply_completion_behavior(self, behavior: str) -> None:
        """Apply an explicit schedule completion/error/interruption behavior."""

        if behavior == "hold_current_target":
            return
        if behavior == "disable_output":
            self.set_output_enabled(False)
            return
        if behavior == "return_to_safe_target":
            if self.settings.safe_target_c is None:
                raise TecConnectionError(
                    "return_to_safe_target requires configured safe_target_c."
                )
            self.set_target_temperature(self.settings.safe_target_c)
            self.set_output_enabled(True)
            return
        if behavior == "revert_to_stored_target":
            if self.initial_target_c is None:
                raise TecConnectionError("No initial TEC target was recorded.")
            self.set_target_temperature(self.initial_target_c)
            if self.initial_output_enabled is not None:
                self.set_output_enabled(self.initial_output_enabled)
            return
        raise ValueError(f"Unknown TEC completion behavior {behavior!r}.")

    def close(self) -> None:
        """Release communication without implicitly changing output state."""

        session, self._session = self._session, None
        self._address = None
        if session is not None:
            try:
                session.stop()
            except Exception as error:
                LOGGER.warning("TEC session close failed: %s", error)


__all__ = ["MeComTecController", "TecConnectionError"]
