"""Guarded demo preparation that preserves immutable commerce history."""

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import create_database
from app.models import Account, McpAgentSession
from app.scripts.seed_dev import seed_development_data


async def prepare(account_id: str, confirmed: bool) -> None:
    settings = get_settings()
    if not settings.demo_mode or not confirmed:
        raise SystemExit("Refusing demo preparation without DEMO_MODE=true and --confirm")
    database = create_database(settings)
    try:
        await seed_development_data(database, orbitintel_base_url=settings.orbitintel_base_url)
        async with database.session() as session:
            account = await session.get(Account, account_id)
            if account is None or str(account.status) != "active":
                raise SystemExit("Dedicated demo account must exist and be active")
            sessions = list(
                (
                    await session.scalars(
                        select(McpAgentSession)
                        .where(
                            McpAgentSession.account_id == account_id,
                            McpAgentSession.revoked_at.is_(None),
                        )
                        .with_for_update()
                    )
                ).all()
            )
            now = datetime.now(UTC)
            for agent_session in sessions:
                agent_session.revoked_at = now
            await session.commit()
            print(
                f"Demo account prepared; revoked {len(sessions)} active agent session(s). Immutable commerce evidence was preserved."
            )
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("account_id")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    asyncio.run(prepare(args.account_id, args.confirm))


if __name__ == "__main__":
    main()
