"""Error types that map to Kubernetes API status codes."""


class ApiError(Exception):
    status = 500
    reason = "InternalError"

    def __init__(self, message: str, *, status: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        if reason is not None:
            self.reason = reason

    def to_dict(self) -> dict:
        return {
            "kind": "Status",
            "apiVersion": "v1",
            "status": "Failure",
            "message": self.message,
            "reason": self.reason,
            "code": self.status,
        }


class BadRequest(ApiError):
    status = 400
    reason = "BadRequest"


class NotFound(ApiError):
    status = 404
    reason = "NotFound"


class AlreadyExists(ApiError):
    status = 409
    reason = "AlreadyExists"


class Conflict(ApiError):
    status = 409
    reason = "Conflict"


class Unauthorized(ApiError):
    status = 401
    reason = "Unauthorized"


class Forbidden(ApiError):
    status = 403
    reason = "Forbidden"


class Invalid(ApiError):
    status = 422
    reason = "Invalid"


class Timeout(ApiError):
    status = 504
    reason = "Timeout"


class ResourceVersionTooOld(ApiError):
    status = 410
    reason = "Expired"
