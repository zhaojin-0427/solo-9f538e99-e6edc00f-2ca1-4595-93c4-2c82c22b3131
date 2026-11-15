"""Domain errors mapped to HTTP responses in main.py."""


class LineageError(Exception):
    status_code = 400
    code = "lineage_error"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationLineageError(LineageError):
    status_code = 422
    code = "validation_error"


class ConflictLineageError(LineageError):
    status_code = 409
    code = "conflict"


class CycleDetectedError(ConflictLineageError):
    code = "cycle_detected"


class NotFoundLineageError(LineageError):
    status_code = 404
    code = "not_found"
