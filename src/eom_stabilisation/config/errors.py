"""Configuration-specific exceptions with operator-facing messages."""


class ConfigurationError(ValueError):
    """Base class for invalid experiment configuration."""


class DuplicateKeyError(ConfigurationError):
    """Raised when a YAML mapping contains the same key more than once."""


class UnknownFieldError(ConfigurationError):
    """Raised when a strict configuration mapping has an unknown field."""


class ReferenceCycleError(ConfigurationError):
    """Raised when configuration files reference each other cyclically."""


class ResumeConfigurationMismatch(ConfigurationError):
    """Raised when a resume snapshot does not match the saved configuration."""
