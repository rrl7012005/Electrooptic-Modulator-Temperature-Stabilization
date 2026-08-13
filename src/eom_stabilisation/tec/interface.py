"""Hardware-agnostic TEC controller contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class TecSnapshot:
    """One validated controller observation supplied to the scheduler."""

    object_temperature_c: float
    sink_temperature_c: float
    temperature_stable: bool
    output_current_a: float | None = None
    output_voltage_v: float | None = None
    active_target_c: float | None = None
    controller_status: str = "unknown"
    error_message: str | None = None


@runtime_checkable
class TecController(Protocol):
    """Minimum interface a real or fake temperature controller must expose."""

    def identify(self) -> str:
        """Return a human-readable, verified controller identity."""

    def read_snapshot(self) -> TecSnapshot:
        """Read current temperatures and controller state."""

    def set_target_temperature(self, target_c: float) -> None:
        """Request a validated volatile/temporary target where supported."""

    def read_active_target(self) -> float:
        """Read back the active target temperature."""

    def set_output_enabled(self, enabled: bool) -> None:
        """Explicitly enable or disable the TEC output."""

    def close(self) -> None:
        """Release communication resources without changing output implicitly."""
