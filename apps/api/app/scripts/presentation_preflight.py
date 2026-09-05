"""Read-only, secret-free local presentation checks. Does not claim payment acceptance."""

import argparse
import asyncio
import json
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import create_database
from app.models import WorkerHeartbeat
from app.services.worker_health import WORKER_TYPES


async def check(api_origin: str, require_refunds: bool) -> bool:
    settings = get_settings()
    checks: dict[str, bool] = {}
    checks["test_mode_payments_configured"] = (
        settings.payments_enabled and settings.razorpay_mode == "test"
    )
    checks["fulfillment_configured"] = settings.fulfillment_enabled
    if require_refunds:
        checks["refunds_enabled_in_this_process"] = settings.refunds_enabled
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        for label, url in (
            ("api_database_redis_ready", f"{api_origin.rstrip('/')}/health/ready"),
            (
                "merchant_process_live",
                f"{str(settings.orbitintel_base_url).rstrip('/')}/health/live",
            ),
        ):
            try:
                response = await client.get(url)
                checks[label] = response.status_code == 200
            except httpx.HTTPError:
                checks[label] = False
        try:
            catalog = await client.get(f"{api_origin.rstrip('/')}/api/v1/catalog")
            merchants = catalog.json().get("merchants", [])
            checks["three_reference_services"] = catalog.status_code == 200 and any(
                m.get("slug") == "orbitintel" and len(m.get("services", [])) == 3 for m in merchants
            )
            response = await client.post(
                f"{api_origin.rstrip('/')}/api/v1/resources/orbitintel/orbital-risk-report/execute",
                json={"norad_id": 25544},
            )
            checks["resource_returns_402"] = (
                response.status_code == 402
                and response.json().get("type") == "metergate_payment_required"
            )
            # Unauthorized empty writes must be denied before validation or mutation.
            response = await client.patch(
                f"{api_origin.rstrip('/')}/api/v1/services/svc_preflight", json={}
            )
            checks["anonymous_catalog_write_denied"] = response.status_code == 401
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            checks["catalog_and_boundary_checks"] = False
    database = create_database(settings)
    try:
        async with database.session() as session:
            for worker_type in WORKER_TYPES:
                heartbeat = await session.scalar(
                    select(WorkerHeartbeat)
                    .where(WorkerHeartbeat.worker_type == worker_type)
                    .order_by(WorkerHeartbeat.last_heartbeat.desc())
                    .limit(1)
                )
                checks[f"{worker_type}_heartbeat"] = (
                    heartbeat is not None
                    and (
                        datetime.now(UTC) - heartbeat.last_heartbeat.astimezone(UTC)
                    ).total_seconds()
                    <= settings.worker_heartbeat_stale_seconds
                )
    except Exception:
        checks["worker_heartbeat_read"] = False
    finally:
        await database.dispose()
    print(
        json.dumps(
            {
                "checked_at": datetime.now(UTC).isoformat(),
                "checks": checks,
                "refunds_enabled_in_this_process": settings.refunds_enabled,
                "remaining_acceptance": [
                    "browser/passkey",
                    "real checkout and delivery",
                    "webhook delivery",
                    "processed refund",
                    "recording and submission",
                ],
            },
            indent=2,
        )
    )
    return all(checks.values())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-origin", default="http://localhost:8000")
    parser.add_argument("--require-refunds", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(check(args.api_origin, args.require_refunds)) else 1)


if __name__ == "__main__":
    main()
