import base64
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sandbox_service.app import create_app
from sandbox_service.config import SandboxConfig

EXAMPLES = Path(__file__).parent.parent / "examples"


def _ensure_wasm():
    # Tests run against prebuilt artifacts; rebuild them if a source changed.
    from wasmtime import wat2wasm
    for wat in sorted(EXAMPLES.glob("*.wat")):
        out = wat.with_suffix(".wasm")
        if not out.exists() or out.stat().st_mtime < wat.stat().st_mtime:
            out.write_bytes(wat2wasm(wat.read_bytes()))


_ensure_wasm()


@pytest.fixture()
def settings():
    return SandboxConfig(max_concurrent_executions=2)


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def module_b64(name: str) -> str:
    return base64.b64encode((EXAMPLES / f"{name}.wasm").read_bytes()).decode()


def stdin_b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def decode(value: str) -> bytes:
    return base64.b64decode(value)
