import asyncio
import base64
import threading
import time
from pathlib import Path

import httpx

from sandbox_service.app import create_app
from sandbox_service.config import SandboxConfig

EXAMPLES = Path(__file__).parent.parent / "examples"


def module_b64(name):
    return base64.b64encode((EXAMPLES / f"{name}.wasm").read_bytes()).decode()


def test_client_disconnect_cancels_execution_and_frees_slot():
    """Cancelling the in-flight request must terminate the guest promptly and
    release the concurrency slot (no orphan thread keeps running)."""
    settings = SandboxConfig(max_concurrent_executions=1)

    async def scenario():
        app = create_app(settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t", timeout=10) as http:
            slow = asyncio.create_task(http.post("/execute", json={
                "module": module_b64("busy_loop"),
                "fuel": 10 ** 9, "timeout_ms": 10_000}))
            deadline = time.monotonic() + 3.0
            while not app.state.supervisor._live and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert len(app.state.supervisor._live) == 1

            slow.cancel()  # client disconnects
            with __import__("pytest").raises(asyncio.CancelledError):
                await slow

            # Give the epoch trap + slot release a short grace period.
            deadline = time.monotonic() + 3.0
            while (app.state.supervisor._live or
                   app.state.supervisor._slots._value == 0) and \
                    time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert app.state.supervisor._live == set()
            assert app.state.supervisor._slots._value == 1

    asyncio.run(scenario())


def test_shutdown_drains_executions():
    settings = SandboxConfig(max_concurrent_executions=1)

    async def scenario():
        app = create_app(settings)
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://t") as h:
            slow = asyncio.create_task(h.post("/execute", json={
                "module": module_b64("busy_loop"),
                "fuel": 10 ** 9, "timeout_ms": 10_000}))
            deadline = time.monotonic() + 3.0
            while not app.state.supervisor._live and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert len(app.state.supervisor._live) == 1

            # What the FastAPI lifespan calls on server shutdown:
            await asyncio.to_thread(app.state.supervisor.shutdown, True)

        assert app.state.supervisor._live == set()
        assert app.state.supervisor._slots._value == 1
        try:
            slow.cancel()
            await slow
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
