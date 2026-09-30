"""Execution supervision: bounded concurrency, worker threads, cancellation.

A counting semaphore caps *active* executions.  Acquiring it is non-blocking,
so a full server rejects immediately instead of building a wait queue.  Each
execution owns one short-lived worker thread plus its own Wasmtime engine; a
client disconnect or server shutdown cancels only that execution.
"""

import threading
from concurrent.futures import Future
from typing import Optional

from .config import SandboxConfig
from .executor import Outcome, run_wasm
from .models import CapacityError
from .validation import ValidatedRequest


class ExecutionSupervisor:
    def __init__(self, settings: SandboxConfig):
        self._settings = settings
        self._slots = threading.BoundedSemaphore(settings.max_concurrent_executions)
        # Registry of live cancel events, used to cancel all on shutdown.
        self._live = set()
        self._live_lock = threading.Lock()
        self._closed = False
        self._workers = []
        self._workers_lock = threading.Lock()

    def is_full(self) -> bool:
        return not self._slots.acquire(blocking=False)

    def submit(self, request: ValidatedRequest) -> "tuple[Future, object]":
        # One slot per execution; acquired before the thread starts so that the
        # count reflects accepted, actually-running work.
        if self._closed:
            raise CapacityError("service is shutting down")
        if not self._slots.acquire(blocking=False):
            raise CapacityError(
                f"execution limit {self._settings.max_concurrent_executions} "
                "reached; try again later")
        cancel = threading.Event()
        future: Future = Future()
        with self._live_lock:
            self._live.add(cancel)

        def worker():
            try:
                outcome = run_wasm(
                    request.module_bytes, request.stdin_bytes, request.budget,
                    self._settings, cancel_event=cancel)
                if not future.done():
                    future.set_result(outcome)
            except BaseException as exc:  # never kill the supervisor
                if not future.done():
                    future.set_exception(exc)
            finally:
                with self._live_lock:
                    self._live.discard(cancel)
                self._slots.release()

        thread = threading.Thread(
            target=worker, name="wasm-exec", daemon=True)
        with self._workers_lock:
            self._workers.append(thread)
        thread.start()
        return future, cancel

    def cancel(self, cancel: threading.Event) -> None:
        cancel.set()

    def shutdown(self, wait: bool = True) -> None:
        """Signal every live execution to trap and release its resources."""
        self._closed = True
        with self._live_lock:
            live = list(self._live)
        for cancel in live:
            cancel.set()
        if wait:
            with self._workers_lock:
                workers = list(self._workers)
            for thread in workers:
                thread.join(timeout=2.0)
