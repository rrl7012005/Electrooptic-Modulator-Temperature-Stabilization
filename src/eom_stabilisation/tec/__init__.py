"""Temperature-controller interfaces and hardware-free scheduling."""

from .interface import TecController, TecSnapshot
from .schedule import (
    StabilitySpec,
    TemperatureSchedule,
    TemperatureStage,
    expand_temperature_schedule,
    generate_temperature_sweep,
    parse_temperature_schedule,
    validate_temperature_schedule,
)
from .state_machine import (
    InvalidTecSnapshot,
    TemperatureMachineSnapshot,
    TemperaturePhase,
    TemperatureSettlingTimeout,
    TemperatureStateError,
    TemperatureStateMachine,
    TemperatureTransition,
)

__all__ = [
    "StabilitySpec",
    "InvalidTecSnapshot",
    "TecController",
    "TecSnapshot",
    "TemperatureMachineSnapshot",
    "TemperaturePhase",
    "TemperatureSettlingTimeout",
    "TemperatureStateError",
    "TemperatureSchedule",
    "TemperatureStage",
    "TemperatureStateMachine",
    "TemperatureTransition",
    "expand_temperature_schedule",
    "generate_temperature_sweep",
    "parse_temperature_schedule",
    "validate_temperature_schedule",
]
