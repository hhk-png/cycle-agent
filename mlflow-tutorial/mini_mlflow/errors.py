"""Error model shared by the Python client, the HTTP server and the CLI.

Each exception carries a stable ``error_code`` and an HTTP status.  The HTTP
layer serializes them as::

    {"error_code": "RESOURCE_DOES_NOT_EXIST", "message": "..."}

Stable codes matter: automation and client libraries should branch on the
code, not on a human-readable message that may change.
"""

from __future__ import annotations

from typing import Any


class MiniMlflowError(Exception):
    """Base class for every expected MiniMLflow failure."""

    code = "INTERNAL_ERROR"
    http_status = 500

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error_code": self.code,
            "message": self.message,
        }
        if self.details is not None:
            payload["details"] = self.details
        return payload


class InvalidParameter(MiniMlflowError):
    code = "INVALID_PARAMETER_VALUE"
    http_status = 400


class MissingParameter(MiniMlflowError):
    code = "INVALID_PARAMETER_VALUE"
    http_status = 400


class ResourceNotFound(MiniMlflowError):
    code = "RESOURCE_DOES_NOT_EXIST"
    http_status = 404


class ResourceAlreadyExists(MiniMlflowError):
    code = "RESOURCE_ALREADY_EXISTS"
    http_status = 400


class BadRequest(MiniMlflowError):
    code = "BAD_REQUEST"
    http_status = 400


class Unauthorized(MiniMlflowError):
    code = "PERMISSION_DENIED"
    http_status = 401


class PermissionDenied(MiniMlflowError):
    code = "PERMISSION_DENIED"
    http_status = 403


class PayloadTooLarge(MiniMlflowError):
    code = "REQUEST_TOO_LARGE"
    http_status = 413


class UnsupportedMediaType(MiniMlflowError):
    code = "UNSUPPORTED_MEDIA_TYPE"
    http_status = 415


class MethodNotAllowed(MiniMlflowError):
    code = "METHOD_NOT_ALLOWED"
    http_status = 405


class Conflict(MiniMlflowError):
    code = "RESOURCE_CONFLICT"
    http_status = 409


class NotReady(MiniMlflowError):
    code = "RESOURCE_NOT_READY"
    http_status = 409


class ProjectExecutionError(MiniMlflowError):
    code = "PROJECT_EXECUTION_FAILED"
    http_status = 400


class ModelLoadError(MiniMlflowError):
    code = "MODEL_LOAD_FAILED"
    http_status = 400


class UnsupportedFlavor(MiniMlflowError):
    code = "UNSUPPORTED_MODEL_FLAVOR"
    http_status = 400


class SearchSyntaxError(InvalidParameter):
    code = "INVALID_PARAMETER_VALUE"


# Compatibility alias used in a few examples.
MlflowException = MiniMlflowError
