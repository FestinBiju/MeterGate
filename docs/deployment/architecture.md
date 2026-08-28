# Staging Deployment

The supported topology is a static/Node Next.js frontend, public HTTPS MeterGate API, managed PostgreSQL, managed Redis, private OrbitIntel service, and three independent workers. PostgreSQL and Redis must not be publicly reachable. OrbitIntel must accept traffic only from MeterGate and its URL/credential never belongs in catalog output.

Milestone 11 remains Razorpay Test Mode only. `APP_ENV=staging`, `RAZORPAY_MODE=test`, HTTPS WebAuthn/CORS origins, `AUTH_COOKIE_SECURE=true`, and an HTTPS `MCP_FRONTEND_BASE_URL` are startup requirements. The application rejects Live Mode configuration.

Build examples are in the three application Dockerfiles. [`infrastructure/docker-compose.staging.yml`](../../infrastructure/docker-compose.staging.yml) is a reproducible single-host reference, not a recommendation to colocate managed data services in a final deployment.

Bootstrap a clean database:

```powershell
Set-Location apps/api
uv run alembic upgrade head
uv run python -m app.scripts.seed_dev
uv run python -m app.scripts.grant_operator acct_… --confirm
```

Configure the Razorpay Test Mode webhook as `https://<api-host>/api/v1/webhooks/razorpay` with the payment and refund events used by MeterGate. Use the exact configured webhook secret. zrok remains a local-only option and is unnecessary in staging.

`GET /health` proves only process liveness. `GET /health/ready` reports PostgreSQL and Redis separately. Razorpay and CelesTrak are deliberately not liveness dependencies.
