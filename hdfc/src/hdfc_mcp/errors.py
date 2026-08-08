"""Typed exceptions mirroring InvestRight's documented error/response structure.

Reference: /ir-docs/docs/error_structure and /ir-docs/docs/response — the API
replies with standard HTTP status codes (400/401/403/404/422/5xx) and a JSON
envelope shaped like {"status": "success"|"error", "data": ..., "message": ...}.
"""
from __future__ import annotations

from typing import Any


class InvestRightError(RuntimeError):
    """Base class for all InvestRight API errors."""

    def __init__(self, message: str, *, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class BadRequestError(InvestRightError):
    """HTTP 400 — malformed or invalid request parameters."""


class AuthenticationError(InvestRightError):
    """HTTP 401 — missing, expired, or invalid access token."""


class ForbiddenError(InvestRightError):
    """HTTP 403 — authenticated but not permitted to perform this action."""


class NotFoundError(InvestRightError):
    """HTTP 404 — resource (order, endpoint) does not exist."""


class UnprocessableEntityError(InvestRightError):
    """HTTP 422 — well-formed request rejected by business validation (e.g. RMS)."""


class ServerError(InvestRightError):
    """HTTP 5xx — failure on InvestRight's side. Safe to retry idempotent GETs only."""


class SessionExpiredError(AuthenticationError):
    """Raised when the cached access token is missing or the API reports it invalid."""


_STATUS_MAP: dict[int, type[InvestRightError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: ForbiddenError,
    404: NotFoundError,
    422: UnprocessableEntityError,
}


def error_for_status(status_code: int, message: str, payload: Any = None) -> InvestRightError:
    if status_code in _STATUS_MAP:
        return _STATUS_MAP[status_code](message, status_code=status_code, payload=payload)
    if status_code >= 500:
        return ServerError(message, status_code=status_code, payload=payload)
    return InvestRightError(message, status_code=status_code, payload=payload)
