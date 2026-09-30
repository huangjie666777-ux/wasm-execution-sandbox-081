# WASI WebAssembly Execution Sandbox

Pure-backend service that runs an untrusted third-party **data-conversion
program** compiled to **WASI preview 1**. You upload the `.wasm` module, its
stdin bytes and an (optional, tighter) execution budget over HTTP; the service
runs the guest `_start` in a fresh isolated Wasmtime instance and returns
stdout, stderr, the exit code and an explicit machine-readable status.

There is no frontend and no module registry: every request carries its own
module. No Docker or external services are required.

## Run

```bash
.venv/bin/python -m uvicorn sandbox_service.app:app --host 127.0.0.1 --port 8081
```

Health and the current server-side caps:

```bash
curl -s http://127.0.0.1:8081/health
curl -s http://127.0.0.1:8081/limits
```

## HTTP protocol

`POST /execute`, `Content-Type: application/json`. All binary fields are
**Base64** strings.

Request fields:

| field          | required | meaning |
|----------------|----------|---------|
| `module`       | yes      | Base64 encoded `.wasm` bytes |
| `stdin`        | no       | Base64 bytes fed to guest fd 0 (default empty) |
| `fuel`         | no       | instruction fuel budget; `1..max_fuel` |
| `timeout_ms`   | no       | wall-clock budget; `1..max_timeout_ms` |
| `memory_bytes` | no       | linear memory cap; `1..max_memory_bytes` |
| `output_bytes` | no       | combined stdout+stderr retention cap |

Per-request budgets are **whitelist-tightening only**: any value above the
server cap (see `GET /limits`) is rejected as an input error before execution;
omitted fields use the server defaults.

Response fields:

| field              | meaning |
|--------------------|---------|
| `status`           | `exited` / `trapped` / `resource_exhausted` / `input_error` |
| `detail`           | human-readable reason |
| `exit_code`        | WASI exit code, present only for `exited` (incl. `proc_exit`) |
| `stdout`,`stderr`  | Base64 captured bytes (truncated to the output budget) |
| `output_truncated` | true if the combined output hit the byte cap |
| `limit_reason`     | explanation when output was truncated |
| `fuel_consumed`    | fuel actually spent |

Status semantics:

- `exited` — `_start` returned or called `proc_exit` (code reported).
- `trapped` — the guest faulted (`unreachable`, out-of-bounds, integer trap).
- `resource_exhausted` — fuel, wall-clock, linear memory or output limit hit.
- `input_error` — bad Base64/JSON, invalid or disallowed module, budget over cap.

When the concurrency cap is reached the request is rejected **immediately**
with HTTP `503` (same status/error JSON body); requests are never queued.

### Example

```bash
MODULE=$(base64 -w0 examples/uppercase.wasm)
STDIN=$(printf 'Hello, World!' | base64 -w0)
curl -s -X POST http://127.0.0.1:8081/execute \
  -H 'Content-Type: application/json' \
  -d "{\"module\":\"$MODULE\", \"stdin\":\"$STDIN\"}"
# stdout SEVMTE8sIFdPUkxEIQ== decodes to "HELLO, WORLD!"
```

## Isolation guarantees

- A **new Engine/Module/Store/Instance** is built per request; nothing is
  shared between guests.
- Only WASI preview-1 stdio is wired: stdin (staged via a temp file that is
  never preopened), stdout and stderr (captured in memory).
- The environment is explicitly **empty**; the host environment is not
  inherited. `argv` is a single fixed program name.
- **No directories are preopened** and no sockets are provided, so the guest
  has no filesystem or network access.
- Before execution the module is parsed and checked: it must export a
  `_start` function, must not import memory, and every import must be one of the
  known `wasi_snapshot_preview1` functions. Anything else
  (e.g. `env.do_evil`) is rejected as an input error.

## Enforced limits

Defaults/caps are centralized in `sandbox_service/config.py` (`SETTINGS`):

| limit | default | maximum (hard cap) |
|-------|---------|--------------------|
| instruction fuel | 100,000,000 | 1,000,000,000 |
| wall-clock time | 1,000 ms | 10,000 ms |
| linear memory | 16 MiB | 64 MiB |
| stdout+stderr bytes | 64 KiB | 1 MiB |
| module upload | 8 MiB | — |
| stdin upload | 2 MiB | — |
| concurrent executions | 4 | — |

How each limit terminates *real* execution:

- **Fuel** is consumed by compiled code; exhaustion raises the Wasmtime
  out-of-fuel trap inside the guest. Infinite loops cannot spin forever.
- **Wall clock** uses a 10 ms engine-epoch watchdog plus an epoch deadline.
  Timeout, client disconnect and server shutdown increment the epoch so the
  guest traps at the next loop back-edge — execution is actually aborted, not
  merely left unawaited.
- **Linear memory** uses Wasmtime store limits: `memory.grow` beyond the cap
  fails and continued growth attempts are reported as memory exhaustion.
- **Output** is counted across stdout+stderr; on overflow the bytes already
  produced are retained, `output_truncated`/`limit_reason` are set, and the
  epoch is kicked so an endlessly printing program is killed.

Concurrency and lifecycle:

- A bounded semaphore caps active executions; a full server returns `503`
  immediately (no waiting queue).
- Each execution runs on its own short-lived worker thread and is independent;
  a trap or failure in one request cannot affect another.
- Client disconnect cancels just that request; server shutdown (FastAPI
  lifespan) cancels and joins every live worker, freeing slots. Completion,
  timeout and cancellation races release the slot exactly once.

## Code layout

- `sandbox_service/config.py` — central defaults and hard caps.
- `sandbox_service/models.py` — request/response models and status enum.
- `sandbox_service/validation.py` — Base64/budget/module/import validation.
- `sandbox_service/executor.py` — isolated WASI instance plus fuel/memory/
  timeout/output enforcement and trap classification.
- `sandbox_service/supervisor.py` — bounded concurrency, worker threads,
  cancellation and shutdown draining.
- `sandbox_service/app.py` — FastAPI routes, request lifecycle, JSON protocol.

## Example programs

`examples/` contains WAT sources (hand-written, no toolchain needed) covering
every outcome. Build them with the Wasmtime API already installed:

```bash
.venv/bin/python examples/build_examples.py
```

| file | demonstrates |
|------|--------------|
| `uppercase.wat` | data conversion: ASCII lower-to-upper on stdout, marker on stderr |
| `exit42.wat` | `proc_exit(42)` reported with the code |
| `trap.wat` | program trap (`unreachable`) |
| `busy_loop.wat` | killed by fuel or wall-clock limit |
| `memory_bomb.wat` | blocked by the linear-memory cap |
| `spam.wat` | endless output truncated then killed |
| `env_isolation.wat` | verifies the guest sees zero environment entries |
| `bad_import.wat` | non-WASI import rejected before execution |

## Tests

```bash
.venv/bin/python -m pytest
```

The suite covers the conversion program, `proc_exit`, traps, fuel/timeout/
memory/output exhaustion, import and Base64 rejection, budget caps, immediate
`503` under load, per-request isolation, and disconnect/shutdown slot cleanup.
