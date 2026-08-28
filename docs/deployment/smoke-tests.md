# Staging Smoke Tests

1. Confirm `/health` returns process liveness and `/health/ready` reports PostgreSQL and Redis ready.
2. Read `/api/v1/catalog` and confirm the three OrbitIntel services and integer prices.
3. POST `{"norad_id":25544}` to `/api/v1/resources/orbitintel/orbital-risk-report/execute` without authorization and require machine-readable `402`.
4. Sign up/sign in with a physical passkey at the deployed RP and Origin.
5. Create a PoHP challenge for an MCP session and verify it with the passkey.
6. Create and revoke an MCP session; confirm the secret appears once.
7. Request a ₹5 quote, create a ₹10 one-time policy, and evaluate it to ALLOW/human approval required.
8. Confirm a normal buyer receives `403` from operator APIs.

Ordinary smoke tests must not create external payments. Record physical passkey and Razorpay Test Mode acceptance separately.
