"""FastAPI application: validate requests, supervise execution, return results."""

import asyncio
import base64
from concurrent.futures import Future

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import SETTINGS, SandboxConfig
from .executor import Outcome
from .models import CapacityError, ExecuteRequest, ExecutionResult, ExecutionStatus
from .supervisor import ExecutionSupervisor
from .validation import RequestValidationError, validate_request


def create_app(settings: SandboxConfig = SETTINGS) -> FastAPI:
    supervisor = ExecutionSupervisor(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # Cancel and drain every live execution on server shutdown.
        supervisor.shutdown(wait=True)

    app = FastAPI(title="WASI Wasm Execution Sandbox", version="1.0.0",
                  lifespan=lifespan)
    app.state.supervisor = supervisor
    app.state.settings = settings

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/limits")
    async def limits():
        return {
            "max_concurrent_executions": settings.max_concurrent_executions,
            "fuel": {"default": settings.default_fuel,
                     "max": settings.max_fuel},
            "timeout_ms": {"default": settings.default_timeout_ms,
                           "max": settings.max_timeout_ms},
            "memory_bytes": {"default": settings.default_memory_bytes,
                             "max": settings.max_memory_bytes},
            "output_bytes": {"default": settings.default_output_bytes,
                             "max": settings.max_output_bytes},
            "max_module_bytes": settings.max_module_bytes,
            "max_stdin_bytes": settings.max_stdin_bytes,
        }

    @app.post("/execute")
    async def execute(request: Request):
        # 1) Parse + decode + validate fully before taking an execution slot.
        try:
            raw = await request.json()

            def _validate():
                payload = ExecuteRequest.model_validate(raw)
                return validate_request(payload, settings)

            # Compilation/validation are CPU-bound: keep the event loop free.
            validated = await asyncio.to_thread(_validate)
        except RequestValidationError as exc:
            return _result_response(Outcome(
                status=ExecutionStatus.INPUT_ERROR.value,
                detail=str(exc)))
        except ValidationError as exc:
            return _result_response(Outcome(
                status=ExecutionStatus.INPUT_ERROR.value,
                detail=f"request schema error: {exc.errors()[0]['msg'] if exc.errors() else exc}"))
        except Exception as exc:
            return _result_response(Outcome(
                status=ExecutionStatus.INPUT_ERROR.value,
                detail=f"malformed JSON request: {exc}"))

        # 2) Acquire a slot immediately or reject (no waiting queue).
        try:
            future, cancel = supervisor.submit(validated)
        except CapacityError as exc:
            return JSONResponse(status_code=503, content={
                "status": ExecutionStatus.RESOURCE_EXHAUSTED.value,
                "detail": str(exc)})

        # 3) Wait for the worker; abort it if the client goes away.
        try:
            outcome = await _wait_future(future, cancel, validated, supervisor)
        except asyncio.CancelledError:
            supervisor.cancel(cancel)
            # Drain the worker (bounded) so the slot is really released and no
            # execution keeps running in the background.  Race with a normal
            # completion is harmless: cancel() only sets an event.
            try:
                await asyncio.to_thread(future.result, 2.0)
            except Exception:
                pass
            raise
        return _result_response(outcome)

    return app


async def _wait_future(future: Future, cancel, validated,
                       supervisor: ExecutionSupervisor):
    """Bridge a concurrent Future to asyncio.

    Block in the default executor in short increments: when the client
    disconnects, asyncio cancels the awaiting coroutine without cancelling the
    worker Future (it finishes from its epoch trap), and the CancelledError
    handler marks the cancel event.
    """
    loop = asyncio.get_running_loop()
    while True:
        done = await loop.run_in_executor(None, future.done)
        if done:
            return future.result()
        await asyncio.sleep(0.02)


def _result_response(outcome: Outcome) -> JSONResponse:
    result = ExecutionResult(
        status=outcome.status,
        detail=outcome.detail,
        exit_code=outcome.exit_code,
        stdout=base64.b64encode(outcome.stdout).decode("ascii"),
        stderr=base64.b64encode(outcome.stderr).decode("ascii"),
        output_truncated=outcome.output_truncated,
        limit_reason=outcome.limit_reason,
        fuel_consumed=outcome.fuel_consumed)
    return JSONResponse(content=result.model_dump())


app = create_app()
