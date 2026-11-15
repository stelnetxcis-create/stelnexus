"""Domain errors. Every one maps to a safe, non-leaking HTTP response."""
from __future__ import annotations


class StelError(Exception):
    status_code = 400
    code = "error"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


class ValidationError(StelError):
    status_code = 422
    code = "validation_error"


class AuthError(StelError):
    status_code = 401
    code = "not_authenticated"


class Forbidden(StelError):
    status_code = 403
    code = "forbidden"


class NotFound(StelError):
    status_code = 404
    code = "not_found"


class Conflict(StelError):
    status_code = 409
    code = "conflict"


class InsufficientFunds(StelError):
    status_code = 409
    code = "insufficient_funds"


class RateLimited(StelError):
    status_code = 429
    code = "rate_limited"


class LockedState(StelError):
    """Operation not allowed in the entity's current lifecycle state."""
    status_code = 409
    code = "locked_state"
