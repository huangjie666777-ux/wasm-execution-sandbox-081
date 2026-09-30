"""Synchronous WASI preview-1 execution in a fresh, isolated Wasmtime store.

Each call builds its own ``Engine``/``Module``/``Store``/``Instance`` so no
state can leak between requests.  Only stdin/stdout/stderr are wired up; the
guest gets an empty environment, no argv beyond a program name, no preopened
directories and no network.
"""

import os
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Optional

from wasmtime import (Config, Engine, ExitTrap, Linker, Module, Store, Trap,
                      TrapCode, WasiConfig, WasmtimeError)

from .config import SandboxConfig
from .validation import Budget

@dataclass
class Outcome:
    status: str                    # exited | trapped | resource_exhausted | input_error
    detail: str
    exit_code: Optional[int] = None
    stdout: bytes = b""
    stderr: bytes = b""
    output_truncated: bool = False
    limit_reason: Optional[str] = None
    fuel_consumed: Optional[int] = None


@dataclass
class _OutputCapture:
    cap: int
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    total: int = 0
    truncated: bool = False
    # Callback forces a real trap by exhausting the epoch deadline once the
    # combined output budget is reached; this label explains why on the way out.
    kill_reason: Optional[str] = None
    store: Optional[Store] = None

    def write(self, stream: str, chunk: bytes):
        if self.truncated:
            # Already over the cap: keep forcing execution to stop.  Returning
            # -EIO would just let the guest spin on retries, so kick the epoch.
            self.store.engine.increment_epoch()
            return len(chunk)
        remaining = self.cap - self.total
        taken = chunk[:remaining]
        getattr(self, stream).extend(taken)
        self.total += len(taken)
        if len(chunk) > remaining:
            self.truncated = True
            self.kill_reason = (
                f"combined stdout+stderr exceeded {self.cap} byte limit; "
                "output truncated")
            if self.store is not None:
                # Epoch interrupt traps at the next loop back-edge, which is
                # enough to stop even a program producing output in a tight loop.
                self.store.engine.increment_epoch()
        return len(chunk)


def _engine_config() -> Config:
    config = Config()
    config.consume_fuel = True
    config.epoch_interruption = True
    return config


def run_wasm(module_bytes: bytes, stdin_bytes: bytes, budget: Budget,
             settings: SandboxConfig,
             cancel_event: Optional[threading.Event] = None) -> Outcome:
    """Compile and run ``_start``. Never raises: failures become Outcome."""
    engine = Engine(_engine_config())
    capture = _OutputCapture(cap=budget.output_bytes)
    stdin_path: Optional[str] = None
    ticker_stop = threading.Event()
    initial_fuel = budget.fuel
    store: Optional[Store] = None
    instance = None

    try:
        try:
            module = Module(engine, module_bytes)
        except WasmtimeError as exc:
            return Outcome(status="input_error",
                           detail=f"invalid wasm module: {exc}")

        # Set the epoch deadline before any tick can arrive: the engine epoch
        # starts at 0, so without this the first tick would trap instantly.
        store = Store(engine)
        capture.store = store
        ticks = max(1, budget.timeout_ms // settings.epoch_tick_ms) + 2
        store.set_epoch_deadline(ticks)
        store.set_fuel(budget.fuel)
        store.set_limits(
            memory_size=budget.memory_bytes,
            table_elements=settings.max_table_elements,
            instances=settings.max_instances,
            tables=settings.max_tables,
            memories=settings.max_memories,
        )

        # stdin is provided through a temporary host file opened on fd 0.  The
        # path is never preopened, so the guest cannot reopen or browse it.
        if stdin_bytes:
            fd, stdin_path = tempfile.mkstemp(prefix="wasm-stdin-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(stdin_bytes)
            except Exception:
                return Outcome(status="input_error",
                               detail="failed to stage stdin")

        wasi = WasiConfig()
        wasi.argv = ["program"]
        wasi.env = []  # explicit empty: no host environment is inherited
        if stdin_path is not None:
            wasi.stdin_file = stdin_path
        wasi.stdout_custom = lambda data: capture.write("stdout", data)
        wasi.stderr_custom = lambda data: capture.write("stderr", data)
        store.set_wasi(wasi)

        linker = Linker(engine)
        linker.define_wasi()
        instance = linker.instantiate(store, module)
        start = instance.exports(store)["_start"]

        def ticker():
            interval = settings.epoch_tick_ms / 1000.0
            while not ticker_stop.wait(interval):
                if cancel_event is not None and cancel_event.is_set():
                    engine.increment_epoch()
                    return
                engine.increment_epoch()

        watchdog = threading.Thread(target=ticker, daemon=True)
        watchdog.start()
        try:
            start(store)
        finally:
            ticker_stop.set()
            watchdog.join(timeout=1.0)

        return _success(store, capture, initial_fuel)

    except ExitTrap as exc:
        # WASI proc_exit: a normal process exit, possibly with nonzero code.
        code = getattr(exc, "code", 0) or 0
        return _success(store, capture, initial_fuel, exit_code=int(code))
    except Trap as exc:
        code = _trap_code(exc)
        if capture.kill_reason is not None:
            reason = capture.kill_reason
        elif _memory_at_cap(instance, store, budget.memory_bytes):
            reason = (f"linear memory budget of {budget.memory_bytes} bytes "
                      "exhausted (memory.grow denied)")
        elif code is TrapCode.OUT_OF_FUEL:
            reason = f"fuel budget of {budget.fuel} exhausted"
        elif code is TrapCode.INTERRUPT:
            if cancel_event is not None and cancel_event.is_set():
                reason = "execution cancelled (client disconnected or shutdown)"
            else:
                reason = f"wall-clock budget of {budget.timeout_ms} ms exceeded"
        else:
            return Outcome(
                status="trapped", detail=_trap_message(exc),
                stdout=bytes(capture.stdout), stderr=bytes(capture.stderr),
                output_truncated=capture.truncated,
                limit_reason=capture.kill_reason,
                fuel_consumed=_remaining_fuel(store, initial_fuel))
        return Outcome(
            status="resource_exhausted", detail=reason,
            stdout=bytes(capture.stdout), stderr=bytes(capture.stderr),
            output_truncated=capture.truncated, limit_reason=capture.kill_reason,
            fuel_consumed=_remaining_fuel(store, initial_fuel))
    except WasmtimeError as exc:
        # Instantiation/linking problems that survived pre-validation.
        return Outcome(
            status="input_error", detail=f"module could not run: {exc}",
            stdout=bytes(capture.stdout), stderr=bytes(capture.stderr),
            output_truncated=capture.truncated, limit_reason=capture.kill_reason)
    finally:
        ticker_stop.set()
        # Explicit teardown avoids a runtime panic when several per-request
        # engines are dropped in one process.  The watchdog is already joined.
        try:
            if store is not None:
                store.close()
        finally:
            engine.close()
        if stdin_path is not None:
            try:
                os.unlink(stdin_path)
            except OSError:
                pass


def _trap_code(exc: Trap):
    try:
        return exc.trap_code
    except Exception:
        return None


def _memory_at_cap(instance, store: Optional[Store], cap: int) -> bool:
    if instance is None or store is None:
        return False
    try:
        mem = instance.exports(store).get("memory")
        return mem is not None and mem.data_len(store) >= cap
    except WasmtimeError:
        return False


def _trap_message(exc: Trap) -> str:
    message = str(exc).splitlines()
    # Keep the human-readable tail (the actual trap reason), drop backtrace.
    for line in reversed(message):
        line = line.strip().lstrip("0123456789: ").strip()
        if line.startswith("wasm trap:"):
            return f"guest trap: {line}"
    return f"guest trap: {exc}"


def _remaining_fuel(store: Optional[Store], initial: int) -> Optional[int]:
    if store is None:
        return None
    try:
        return max(0, initial - store.get_fuel())
    except WasmtimeError:
        return None


def _success(store: Store, capture: _OutputCapture, initial_fuel: int,
             exit_code: int = 0) -> Outcome:
    return Outcome(
        status="exited", detail=f"program exited with code {exit_code}",
        exit_code=exit_code,
        stdout=bytes(capture.stdout), stderr=bytes(capture.stderr),
        output_truncated=capture.truncated, limit_reason=capture.kill_reason,
        fuel_consumed=_remaining_fuel(store, initial_fuel))
