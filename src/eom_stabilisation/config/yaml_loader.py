"""Small strict-YAML helpers shared by configuration parsers."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from .errors import ConfigurationError, DuplicateKeyError, UnknownFieldError


DURATION_UNIT_SECONDS: dict[str, float] = {
    "s": 1.0,
    "ms": 1e-3,
    "us": 1e-6,
    "ns": 1e-9,
    "minutes": 60.0,
    "hours": 3600.0,
}


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader variant which rejects duplicate mapping keys."""

    source_path: Path | None = None

    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise ConfigurationError("Expected a YAML mapping node.")
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as error:
                raise ConfigurationError(
                    "YAML mapping keys must be scalar, hashable values."
                ) from error
            if duplicate:
                source = f" in {self.source_path}" if self.source_path else ""
                raise DuplicateKeyError(
                    f"Duplicate YAML key {key!r}{source} at line "
                    f"{key_node.start_mark.line + 1}."
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def load_yaml_mapping(path: str | Path) -> dict[str, Any]:
    """Load one YAML document safely and require a string-keyed root mapping."""

    source_path = Path(path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Configuration file does not exist: {source_path}")
    try:
        text = source_path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise ConfigurationError(
            f"Could not read configuration file {source_path}: {error}"
        ) from error

    loader = _UniqueKeySafeLoader(text)
    loader.source_path = source_path
    try:
        document = loader.get_single_data()
    except ConfigurationError:
        raise
    except yaml.YAMLError as error:
        raise ConfigurationError(f"Invalid YAML in {source_path}: {error}") from error
    finally:
        loader.dispose()

    if document is None:
        raise ConfigurationError(f"Configuration file is empty: {source_path}")
    mapping = ensure_mapping(document, f"root of {source_path}")
    for key in mapping:
        if not isinstance(key, str):
            raise ConfigurationError(
                f"All root keys in {source_path} must be strings; got {key!r}."
            )
    return dict(mapping)


def ensure_mapping(value: Any, context: str) -> Mapping[str, Any]:
    """Return *value* as a mapping or raise a contextual error."""

    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{context} must be a mapping.")
    for key in value:
        if not isinstance(key, str):
            raise ConfigurationError(f"Every field in {context} must be a string.")
    return value


def ensure_sequence(value: Any, context: str) -> Sequence[Any]:
    """Return a non-string sequence or raise a contextual error."""

    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ConfigurationError(f"{context} must be a list.")
    return value


def reject_unknown_fields(
    mapping: Mapping[str, Any],
    allowed: Iterable[str],
    context: str,
) -> None:
    """Reject keys not explicitly owned by the current schema."""

    unknown = set(mapping) - set(allowed)
    if unknown:
        raise UnknownFieldError(
            f"Unknown field(s) in {context}: {', '.join(sorted(unknown))}."
        )


def require_fields(
    mapping: Mapping[str, Any],
    required: Iterable[str],
    context: str,
) -> None:
    """Reject a mapping which omits required fields."""

    missing = set(required) - set(mapping)
    if missing:
        raise ConfigurationError(
            f"Missing required field(s) in {context}: {', '.join(sorted(missing))}."
        )


def strict_string(value: Any, context: str, *, allow_empty: bool = False) -> str:
    """Parse a string without coercing numbers or booleans."""

    if not isinstance(value, str):
        raise ConfigurationError(f"{context} must be a string.")
    result = value.strip()
    if not result and not allow_empty:
        raise ConfigurationError(f"{context} must not be empty.")
    return result


def strict_bool(value: Any, context: str) -> bool:
    """Parse a real YAML boolean without accepting integer lookalikes."""

    if not isinstance(value, bool):
        raise ConfigurationError(f"{context} must be true or false.")
    return value


def strict_float(value: Any, context: str) -> float:
    """Parse a finite number without accepting booleans."""

    if isinstance(value, bool):
        raise ConfigurationError(f"{context} must be a finite number.")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{context} must be a finite number.") from error
    if not math.isfinite(result):
        raise ConfigurationError(f"{context} must be finite.")
    return result


def strict_positive_int(value: Any, context: str) -> int:
    """Parse an integer above zero without silently truncating a float."""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"{context} must be an integer above zero.")
    return value


def duration_field_names(prefix: str) -> frozenset[str]:
    """Return all accepted explicit-unit field names for a duration."""

    return frozenset(f"{prefix}_{unit}" for unit in DURATION_UNIT_SECONDS)


def parse_duration_seconds(
    mapping: Mapping[str, Any],
    prefix: str,
    context: str,
    *,
    required: bool = True,
    default: float | None = None,
    allow_zero: bool = False,
) -> float | None:
    """Read exactly one unit-bearing duration and normalise it to seconds."""

    present = sorted(duration_field_names(prefix) & set(mapping))
    if len(present) > 1:
        raise ConfigurationError(
            f"{context} defines conflicting durations: {', '.join(present)}."
        )
    if not present:
        if required:
            choices = ", ".join(sorted(duration_field_names(prefix)))
            raise ConfigurationError(
                f"{context} requires exactly one of: {choices}."
            )
        return default

    field = present[0]
    suffix = field[len(prefix) + 1 :]
    value = strict_float(mapping[field], f"{context}.{field}")
    if value < 0 or (value == 0 and not allow_zero):
        qualifier = "zero or greater" if allow_zero else "above zero"
        raise ConfigurationError(f"{context}.{field} must be {qualifier}.")
    return value * DURATION_UNIT_SECONDS[suffix]


def resolve_referenced_path(
    containing_file: str | Path,
    reference: Any,
    context: str,
) -> Path | None:
    """Resolve a nullable file reference relative to its containing file."""

    if reference is None:
        return None
    text = strict_string(reference, context)
    reference_path = Path(text).expanduser()
    if not reference_path.is_absolute():
        reference_path = Path(containing_file).resolve().parent / reference_path
    return reference_path.resolve()
