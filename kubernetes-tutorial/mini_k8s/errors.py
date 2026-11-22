"""Kubernetes-style API errors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ApiError(Exception):
    status: int
    reason: str
    message: str
    details: dict[str, Any] | None = None

    def to_status(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "kind": "Status",
            "apiVersion": "v1",
            "status": "Failure",
            "message": self.message,
            "reason": self.reason,
            "code": self.status,
        }
        if self.details:
            body["details"] = self.details
        return body


def bad_request(message: str, *, field: str | None = None) -> ApiError:
    details = {"field": field} if field else None
    return ApiError(400, "BadRequest", message, details)


def not_found(kind: str, name: str) -> ApiError:
    return ApiError(404, "NotFound", f'{kind} "{name}" not found', {"name": name, "kind": kind})


def already_exists(kind: str, name: str) -> ApiError:
    return ApiError(409, "AlreadyExists", f'{kind} "{name}" already exists', {"name": name, "kind": kind})


def conflict(message: str) -> ApiError:
    return ApiError(409, "Conflict", message)


def invalid(kind: str, name: str, message: str) -> ApiError:
    return ApiError(422, "Invalid", f'{kind} "{name}" is invalid: {message}', {"name": name, "kind": kind})


def forbidden(message: str) -> ApiError:
    return ApiError(403, "Forbidden", message)


def unauthorized(message: str = "Unauthorized") -> ApiError:
    return ApiError(401, "Unauthorized", message)


def method_not_allowed(message: str = "Method not allowed") -> ApiError:
    return ApiError(405, "MethodNotAllowed", message)


def unsupported(api_version: str, kind: str) -> ApiError:
    return ApiError(400, "BadRequest", f"no kind {kind!r} is registered for version {api_version!r}")


def too_large(message: str) -> ApiError:
    return ApiError(413, "RequestEntityTooLarge", message)


def internal(message: str) -> ApiError:
    return ApiError(500, "InternalError", message)
