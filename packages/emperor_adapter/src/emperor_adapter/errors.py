"""Adapter-specific exceptions."""


class AdapterError(RuntimeError):
    """Base class for migration adapter failures."""


class InvalidLegacyProject(AdapterError):
    """Raised when required Emperor Simulator metadata is absent or malformed."""


class PathSafetyError(AdapterError):
    """Raised when a source or destination path escapes its declared root."""

