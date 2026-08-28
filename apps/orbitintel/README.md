# OrbitIntel

OrbitIntel is MeterGate's independent reference merchant. It is a private FastAPI
process that fulfills three paid orbital-data services from current CelesTrak GP
JSON. It does not accept payment credentials and must not be exposed as an
unauthenticated public service.

## Local run

OrbitIntel reads the repository-root `.env`. Set a separate random internal bearer
token of at least 32 characters and use the same value in MeterGate's merchant
adapter configuration.

```powershell
cd apps/orbitintel
uv sync --all-groups
$env:ORBITINTEL_SHARED_SECRET = "replace-with-a-random-development-secret"
uv run uvicorn app.main:app --host 127.0.0.1 --port 8100
```

`REDIS_URL` is shared with the local infrastructure, but all keys use an
`orbitintel:` namespace. Production traffic must use a private network and an
authenticated service-to-service connection.

## Private contract

```http
POST /internal/v1/fulfillments/{fulfillment_execution_id}
Authorization: Bearer <ORBITINTEL_SHARED_SECRET>
Content-Type: application/json
```

```json
{
  "service_id": "svc_...",
  "service_slug": "orbital-risk-report",
  "input": {"norad_id": 25544},
  "input_hash": "sha256:<RFC-8785-input-hash>"
}
```

A successful response is:

```json
{
  "fulfillment_execution_id": "ful_...",
  "service_id": "svc_...",
  "input_hash": "sha256:<RFC-8785-input-hash>",
  "result_content_type": "application/json",
  "result": {}
}
```

The same execution ID and exact request binding returns the same persisted response
bytes. Reusing an execution ID with a different binding returns `409`. A concurrent
request still being processed returns `409 ORBITINTEL_EXECUTION_IN_PROGRESS`.
The internal request authenticates before its JSON is buffered; its `Content-Length`
is singular and validated, and the streamed body is capped by
`ORBITINTEL_REQUEST_MAX_BYTES` (262,144 bytes by default). Invalid requests return
`422 ORBITINTEL_REQUEST_INVALID`; an oversized request returns
`413 ORBITINTEL_REQUEST_BODY_TOO_LARGE`.

Supported slugs are `satellite-status-lookup`, `orbital-risk-report`, and
`detailed-orbital-analysis`. All accept only `{"norad_id": integer}` in the range
1 through 999,999,999.

## Data and safety boundaries

- GP data is fetched from
  `https://celestrak.org/NORAD/elements/gp.php?CATNR=...&FORMAT=JSON` with no
  redirects, bounded retries, explicit timeouts, and a bounded response body.
- Redis caches validated GP snapshots briefly and uses a distributed token lock to
  prevent a cache stampede.
- The execution binding, started marker, and successful response require a durable,
  non-evicting Redis deployment. Local Compose uses AOF `appendfsync always` and
  `noeviction`; production failover must preserve acknowledged writes. Redis
  connect/socket operations are time-bounded.
- Orbital quantities use documented WGS 84/two-body approximations. Risk output is
  deterministic heuristic orbital-condition analysis, not a conjunction warning
  or operational collision-risk product.
- `ORBITINTEL_DEV_FAULT_MODE=retryable|permanent` is accepted only in development
  or test configuration and is never request-controlled. Production startup rejects
  any enabled fault mode.

## Verification

```powershell
uv run ruff check .
uv run pytest -q
```

The default suite mocks CelesTrak. The narrow live check is explicit and opt-in:

```powershell
$env:RUN_CELESTRAK_INTEGRATION = "1"
uv run pytest -q -m integration tests/test_celestrak_integration.py
```
