from enum import Enum

class TaskStatusResponse_status(str, Enum):
    Pending = "pending",
    Started = "started",
    Success = "success",
    Failure = "failure",
    Retry = "retry",
    Revoked = "revoked",

