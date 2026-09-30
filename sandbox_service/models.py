"""Request/response protocol models. Binary fields are Base64 encoded."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ExecuteRequest(BaseModel):
    # Base64 encoded WebAssembly module bytes.
    module: str = Field(..., min_length=1, description="Base64 encoded .wasm module")
    # Base64 encoded bytes fed to the guest's stdin. Defaults to empty.
    stdin: str = Field(default="", description="Base64 encoded stdin bytes")
    # Optional tightened budgets. Each must be positive and below server caps.
    fuel: Optional[int] = Field(default=None, ge=1)
    timeout_ms: Optional[int] = Field(default=None, ge=1)
    memory_bytes: Optional[int] = Field(default=None, ge=1)
    output_bytes: Optional[int] = Field(default=None, ge=1)


class ExecutionStatus(str, Enum):
    EXITED = "exited"          # _start returned normally (or called proc_exit(0))
    TRAPPED = "trapped"        # program trap: unreachable, OOB, abort, ...
    RESOURCE_EXHAUSTED = "resource_exhausted"  # fuel/time/memory/output limit
    INPUT_ERROR = "input_error"               # invalid module or request


class ExecutionResult(BaseModel):
    status: ExecutionStatus
    detail: str
    exit_code: Optional[int] = None
    stdout: str = ""           # Base64 encoded bytes
    stderr: str = ""           # Base64 encoded bytes
    output_truncated: bool = False
    limit_reason: Optional[str] = None  # set when output truncation happened
    fuel_consumed: Optional[int] = None

    model_config = ConfigDict(use_enum_values=True)


class CapacityError(Exception):
    """Raised when the execution concurrency cap is reached."""
