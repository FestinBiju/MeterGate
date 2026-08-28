# Staging Configuration

Copy `.env.example` to an untracked secret store and replace every placeholder. Required staging differences include:

```env
APP_ENV=staging
RAZORPAY_MODE=test
AUTH_COOKIE_SECURE=true
AUTH_COOKIE_SAMESITE=lax
WEBAUTHN_RP_ID=app.example.com
WEBAUTHN_EXPECTED_ORIGINS=https://app.example.com
CORS_ALLOWED_ORIGINS=https://app.example.com
MCP_FRONTEND_BASE_URL=https://app.example.com
ORBITINTEL_BASE_URL=https://orbitintel.internal.example.com
```

Use independent high-entropy values for PostgreSQL, Redis, Razorpay Test Mode, webhook verification, entitlement capabilities, and OrbitIntel. Only `NEXT_PUBLIC_API_URL` is safe for the frontend build. Run `python scripts/secret_scan.py` before deployment.

After deployment run the non-payment smoke sequence documented in `docs/deployment/smoke-tests.md`.
