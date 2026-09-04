class TriCycleError(Exception):
    """Base exception for framework-level errors."""


class PluginRegistrationError(TriCycleError):
    """Raised when a plugin cannot be registered safely."""


class InvalidRequestError(TriCycleError, ValueError):
    """Raised when a plugin request violates its public contract."""


class IntegrationNotConfiguredError(TriCycleError):
    """Raised when a real external adapter has not been configured."""
