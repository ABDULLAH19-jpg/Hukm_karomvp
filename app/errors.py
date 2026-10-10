class DomainError(Exception):
    """A business rule refused the request. `code` is stable for callers and tools."""

    status = 422

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class NotFound(DomainError):
    status = 404


class Conflict(DomainError):
    status = 409


class Unavailable(DomainError):
    status = 503


class MenuDataError(Exception):
    """The menu file itself is malformed. Raised at startup, never per request."""
