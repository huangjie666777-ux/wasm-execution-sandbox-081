"""Central server-side limits and defaults for the Wasm execution sandbox."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SandboxConfig:
    # Execution concurrency: requests beyond this are rejected immediately.
    max_concurrent_executions: int = 4

    # Fuel budget for executed WebAssembly instructions.
    default_fuel: int = 100_000_000
    max_fuel: int = 1_000_000_000

    # Wall-clock budget, in milliseconds.
    default_timeout_ms: int = 1_000
    max_timeout_ms: int = 10_000

    # Linear memory limit, in bytes.
    default_memory_bytes: int = 16 * 1024 * 1024
    max_memory_bytes: int = 64 * 1024 * 1024

    # Combined stdout + stderr bytes that are retained and returned.
    default_output_bytes: int = 64 * 1024
    max_output_bytes: int = 1 * 1024 * 1024

    # Largest accepted module payload (decoded bytes) and stdin payload.
    max_module_bytes: int = 8 * 1024 * 1024
    max_stdin_bytes: int = 2 * 1024 * 1024

    # How often (ms) the engine epoch advances; epoch traps kill real execution.
    epoch_tick_ms: int = 10

    # Single WebAssembly instance/memory/table per guest module.
    max_instances: int = 1
    max_memories: int = 1
    max_tables: int = 4
    max_table_elements: int = 10_000


SETTINGS = SandboxConfig()
