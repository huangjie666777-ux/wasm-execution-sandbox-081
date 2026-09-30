"""Per-request validation: payload decoding, budgets, and module safety checks.

Everything here runs *before* an execution slot is acquired or any guest code
runs.  The user may only tighten the server-side caps.
"""

import base64
import binascii
from dataclasses import dataclass
from typing import Optional

from wasmtime import Engine, Module

from .config import SandboxConfig


class RequestValidationError(Exception):
    """Client-side problem: malformed payload or disallowed module."""


# The complete set of WASI preview-1 (wasi_snapshot_preview1) functions.  The
# linker provides exactly these; anything else the module imports is rejected
# up front so a missing host function can never surface as a runtime surprise.
WASI_PREVIEW1_IMPORTS = frozenset({
    "args_get", "args_sizes_get", "environ_get", "environ_sizes_get",
    "clock_res_get", "clock_time_get", "fd_advise", "fd_allocate",
    "fd_close", "fd_datasync", "fd_fdstat_get", "fd_fdstat_set_flags",
    "fd_fdstat_set_rights", "fd_filestat_get", "fd_filestat_set_size",
    "fd_filestat_set_times", "fd_fsync", "fd_pread", "fd_prestat_dir_name",
    "fd_prestat_get", "fd_pwrite", "fd_read", "fd_readdir", "fd_renumber",
    "fd_seek", "fd_sync", "fd_tell", "fd_write", "path_create_directory",
    "path_filestat_get", "path_filestat_set_times", "path_link",
    "path_open", "path_readlink", "path_remove_directory", "path_rename",
    "path_symlink", "path_unlink_file", "poll_oneoff", "proc_exit",
    "proc_raise", "random_get", "sched_yield", "sock_accept", "sock_recv",
    "sock_send", "sock_shutdown",
})


@dataclass(frozen=True)
class Budget:
    fuel: int
    timeout_ms: int
    memory_bytes: int
    output_bytes: int


def _b64decode(field: str, value: str, max_bytes: int) -> bytes:
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise RequestValidationError(
            f"field '{field}' is not valid Base64: {exc}") from exc
    if len(raw) > max_bytes:
        raise RequestValidationError(
            f"field '{field}' is {len(raw)} bytes; server cap is {max_bytes}")
    return raw


def resolve_budget(settings: SandboxConfig, *, fuel: Optional[int],
                   timeout_ms: Optional[int], memory_bytes: Optional[int],
                   output_bytes: Optional[int]) -> Budget:
    def pick(name: str, requested: Optional[int], default: int, cap: int) -> int:
        value = default if requested is None else requested
        if requested is not None and requested > cap:
            raise RequestValidationError(
                f"{name}={requested} exceeds server cap {cap}")
        return value

    return Budget(
        fuel=pick("fuel", fuel, settings.default_fuel, settings.max_fuel),
        timeout_ms=pick("timeout_ms", timeout_ms, settings.default_timeout_ms,
                        settings.max_timeout_ms),
        memory_bytes=pick("memory_bytes", memory_bytes,
                         settings.default_memory_bytes,
                         settings.max_memory_bytes),
        output_bytes=pick("output_bytes", output_bytes,
                         settings.default_output_bytes,
                         settings.max_output_bytes),
    )


@dataclass
class ValidatedRequest:
    module_bytes: bytes
    stdin_bytes: bytes
    budget: Budget


def validate_request(payload, settings: SandboxConfig) -> ValidatedRequest:
    module_bytes = _b64decode("module", payload.module, settings.max_module_bytes)
    if not module_bytes:
        raise RequestValidationError("module decodes to empty bytes")
    stdin_bytes = _b64decode("stdin", payload.stdin or "",
                             settings.max_stdin_bytes)
    budget = resolve_budget(
        settings, fuel=payload.fuel, timeout_ms=payload.timeout_ms,
        memory_bytes=payload.memory_bytes, output_bytes=payload.output_bytes)

    engine = Engine(_engine_config())
    try:
        module = Module(engine, module_bytes)
    except Exception as exc:  # malformed/invalid wasm
        raise RequestValidationError(f"invalid wasm module: {exc}") from exc

    _check_imports(module)
    _check_start_export(module)
    _check_memory(module, budget.memory_bytes)

    return ValidatedRequest(module_bytes=module_bytes,
                           stdin_bytes=stdin_bytes, budget=budget)


def _engine_config():
    from wasmtime import Config
    config = Config()
    config.consume_fuel = True
    config.epoch_interruption = True
    return config


def _check_imports(module: Module) -> None:
    for imp in module.imports:
        module_name = imp.module
        field_name = imp.name
        if module_name != "wasi_snapshot_preview1":
            raise RequestValidationError(
                f"disallowed import: '{module_name}.{field_name}' "
                "(only wasi_snapshot_preview1 is provided)")
        if field_name not in WASI_PREVIEW1_IMPORTS:
            raise RequestValidationError(
                f"unknown wasi import: 'wasi_snapshot_preview1.{field_name}'")


def _check_start_export(module: Module) -> None:
    from wasmtime import FuncType
    start = next((exp for exp in module.exports if exp.name == "_start"), None)
    if start is None:
        raise RequestValidationError("module does not export '_start'")
    if not isinstance(start.type, FuncType):
        raise RequestValidationError("export '_start' is not a function")


def _check_memory(module: Module, memory_cap: int) -> None:
    from wasmtime import MemoryType
    for imp in module.imports:
        if isinstance(imp.type, MemoryType):
            raise RequestValidationError("imported linear memory is not allowed")
    for exp in module.exports:
        if isinstance(exp.type, MemoryType):
            minimum_bytes = exp.type.limits.min * exp.type.page_size
            if minimum_bytes > memory_cap:
                raise RequestValidationError(
                    f"memory minimum {minimum_bytes} bytes exceeds budget "
                    f"{memory_cap} bytes")
    # Modules with no memory cannot do stdio, but they are still valid wasm;
    # instantiation/start will simply have no effect.
