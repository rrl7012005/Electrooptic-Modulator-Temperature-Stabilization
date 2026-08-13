"""Explicit registry for custom Python waveform functions.

YAML may refer only to a name already present in this registry.  No module
paths, expressions, or source strings are evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping

import numpy as np


WaveformFunction = Callable[[np.ndarray, Mapping[str, Any]], np.ndarray]
ParameterValidator = Callable[[Mapping[str, Any]], Mapping[str, Any]]

_SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class RegisteredWaveform:
    """A callable and its strict parameter validator."""

    name: str
    function: WaveformFunction
    parameter_validator: ParameterValidator


_REGISTRY: dict[str, RegisteredWaveform] = {}


def _validate_json_value(value: Any, path: str) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{path} must be finite")
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _validate_json_value(item, f"{path}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _validate_json_value(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ValueError(
        f"{path} must contain only finite numbers, strings, booleans, null, "
        "lists, and mappings"
    )


def validate_safe_parameters(parameters: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate a generic JSON-like parameter mapping."""

    if not isinstance(parameters, Mapping):
        raise ValueError("custom waveform parameters must be a mapping")
    return MappingProxyType(
        {
            str(key): _validate_json_value(value, f"parameters.{key}")
            for key, value in parameters.items()
        }
    )


def register_waveform(
    name: str,
    function: WaveformFunction | None = None,
    *,
    parameter_validator: ParameterValidator = validate_safe_parameters,
    replace: bool = False,
) -> WaveformFunction | Callable[[WaveformFunction], WaveformFunction]:
    """Register a custom waveform function, directly or as a decorator."""

    normalized_name = str(name).strip()
    if not _SAFE_NAME.fullmatch(normalized_name):
        raise ValueError(
            "registered waveform names must start with a letter and contain "
            "only letters, numbers, underscores, and hyphens"
        )

    def perform_registration(candidate: WaveformFunction) -> WaveformFunction:
        if not callable(candidate):
            raise TypeError("custom waveform function must be callable")
        if not callable(parameter_validator):
            raise TypeError("custom waveform parameter validator must be callable")
        if normalized_name in _REGISTRY and not replace:
            raise ValueError(f"custom waveform {normalized_name!r} is already registered")
        _REGISTRY[normalized_name] = RegisteredWaveform(
            normalized_name,
            candidate,
            parameter_validator,
        )
        return candidate

    if function is None:
        return perform_registration
    return perform_registration(function)


def get_registered_waveform(name: str) -> RegisteredWaveform:
    """Return a registered waveform or raise a configuration error."""

    normalized_name = str(name).strip()
    try:
        return _REGISTRY[normalized_name]
    except KeyError as error:
        available = ", ".join(sorted(_REGISTRY)) or "none"
        raise ValueError(
            f"unknown registered waveform {normalized_name!r}; available: {available}"
        ) from error


def registered_waveform_names() -> tuple[str, ...]:
    """Return stable registry names for validation messages and previews."""

    return tuple(sorted(_REGISTRY))


def _validate_raised_cosine(parameters: Mapping[str, Any]) -> Mapping[str, Any]:
    parameters = validate_safe_parameters(parameters)
    unknown = set(parameters) - {"low_level_v", "high_level_v"}
    if unknown:
        raise ValueError(f"raised_cosine has unknown parameters: {sorted(unknown)}")
    missing = {"low_level_v", "high_level_v"} - set(parameters)
    if missing:
        raise ValueError(f"raised_cosine is missing parameters: {sorted(missing)}")
    low = float(parameters["low_level_v"])
    high = float(parameters["high_level_v"])
    if high <= low:
        raise ValueError("raised_cosine high_level_v must be above low_level_v")
    return MappingProxyType({"low_level_v": low, "high_level_v": high})


@register_waveform("raised_cosine", parameter_validator=_validate_raised_cosine)
def raised_cosine(
    phase: np.ndarray,
    parameters: Mapping[str, Any],
) -> np.ndarray:
    """Return one low-high-low raised-cosine cycle in connector volts."""

    low = float(parameters["low_level_v"])
    high = float(parameters["high_level_v"])
    return low + (high - low) * 0.5 * (1.0 - np.cos(2.0 * np.pi * phase))
