import asyncio
import base64
import time

import httpx

from sandbox_service.app import create_app

from tests.conftest import decode, module_b64, stdin_b64


def test_health_and_limits(client):
    assert client.get("/health").json()["status"] == "ok"
    limits = client.get("/limits").json()
    assert limits["max_concurrent_executions"] == 2
    assert limits["fuel"]["max"] >= limits["fuel"]["default"]


def test_transform_program_runs(client):
    resp = client.post("/execute", json={
        "module": module_b64("uppercase"),
        "stdin": stdin_b64(b"Hello, World!")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "exited"
    assert body["exit_code"] == 0
    assert decode(body["stdout"]) == b"HELLO, WORLD!"
    assert decode(body["stderr"]) == b"uppercase: done\n"
    assert body["fuel_consumed"] is not None


def test_nonzero_proc_exit(client):
    body = client.post("/execute", json={"module": module_b64("exit42")}).json()
    assert body["status"] == "exited"
    assert body["exit_code"] == 42


def test_trap_is_reported_as_trapped(client):
    body = client.post("/execute", json={"module": module_b64("trap")}).json()
    assert body["status"] == "trapped"
    assert "unreachable" in body["detail"]


def test_infinite_loop_hits_fuel(client):
    body = client.post("/execute", json={
        "module": module_b64("busy_loop"),
        "fuel": 200_000, "timeout_ms": 10_000}).json()
    assert body["status"] == "resource_exhausted"
    assert "fuel" in body["detail"]


def test_infinite_loop_hits_timeout(client):
    body = client.post("/execute", json={
        "module": module_b64("busy_loop"),
        "fuel": 10 ** 9, "timeout_ms": 200}).json()
    start = time.monotonic()
    assert body["status"] == "resource_exhausted"
    assert "wall-clock" in body["detail"]
    assert time.monotonic() - start < 3.0


def test_memory_growth_capped(client):
    body = client.post("/execute", json={
        "module": module_b64("memory_bomb"),
        "memory_bytes": 256 * 1024,
        "fuel": 10 ** 9, "timeout_ms": 5_000}).json()
    assert body["status"] == "resource_exhausted"
    assert "memory" in body["detail"]


def test_endless_output_truncated_and_killed(client):
    body = client.post("/execute", json={
        "module": module_b64("spam"),
        "output_bytes": 100,
        "fuel": 10 ** 9, "timeout_ms": 5_000}).json()
    assert body["status"] == "resource_exhausted"
    assert body["output_truncated"] is True
    assert "byte limit" in body["limit_reason"]
    assert len(decode(body["stdout"])) <= 100


def test_disallowed_import_rejected(client):
    body = client.post("/execute", json={"module": module_b64("bad_import")}).json()
    assert body["status"] == "input_error"
    assert "disallowed import" in body["detail"]


def test_bad_base64_rejected(client):
    body = client.post("/execute", json={"module": "not base64!!"}).json()
    assert body["status"] == "input_error"


def test_malformed_wasm_rejected(client):
    body = client.post("/execute", json={
        "module": base64.b64encode(b"\x00notwasm").decode()}).json()
    assert body["status"] == "input_error"
    assert "invalid wasm" in body["detail"]


def test_budget_cannot_exceed_cap(client):
    limits = client.get("/limits").json()
    body = client.post("/execute", json={
        "module": module_b64("trap"),
        "fuel": limits["fuel"]["max"] + 1}).json()
    assert body["status"] == "input_error"
    assert "exceeds server cap" in body["detail"]


def test_concurrency_cap_rejects_immediately(settings):
    import asyncio
    import httpx

    async def scenario():
        app = create_app(settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as http:
            # Occupy both slots with a long-running busy loop...
            payload = {"module": module_b64("busy_loop"),
                       "fuel": 10 ** 9, "timeout_ms": 3_000}
            slow1 = asyncio.create_task(http.post("/execute", json=payload))
            slow2 = asyncio.create_task(http.post("/execute", json=payload))
            await asyncio.sleep(0.3)  # let both acquire their slots

            # ...the third request must be rejected immediately with 503.
            rejected = await http.post("/execute", json=payload)
            assert rejected.status_code == 503
            assert rejected.json()["status"] == "resource_exhausted"

            results = await asyncio.gather(slow1, slow2)
            assert all(r.status_code == 200 for r in results)

            # Slots are released after completion: server accepts work again.
            follow = await http.post("/execute",
                                    json={"module": module_b64("trap")})
            assert follow.status_code == 200

    asyncio.run(scenario())


def test_executions_are_independent(client):
    # A trap in one request must not corrupt the next.
    for _ in range(3):
        assert client.post("/execute",
                           json={"module": module_b64("trap")}).json()[
                   "status"] == "trapped"
    body = client.post("/execute", json={
        "module": module_b64("uppercase"),
        "stdin": stdin_b64(b"abc")}).json()
    assert decode(body["stdout"]) == b"ABC"


def test_no_host_environment_inherited(client):
    body = client.post("/execute",
                       json={"module": module_b64("env_isolation")}).json()
    assert body["status"] == "exited"
    assert decode(body["stdout"]).strip() == b"ENV_OK"
