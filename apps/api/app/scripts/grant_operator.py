"""Explicit local operator-role bootstrap; never exposed over HTTP."""

import argparse
import asyncio

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import create_database
from app.models import Account, OperatorRole


async def grant(account_id: str, role: str, confirmed: bool) -> None:
    if not confirmed:
        raise SystemExit("Refusing role mutation without --confirm")
    database = create_database(get_settings())
    try:
        async with database.session() as session:
            account = await session.get(Account, account_id)
            if account is None or str(account.status) != "active":
                raise SystemExit("Account must exist and be active")
            existing = await session.scalar(
                select(OperatorRole).where(OperatorRole.account_id == account_id)
            )
            if existing is None:
                session.add(
                    OperatorRole(
                        account_id=account_id,
                        role=role,
                        status="active",
                        created_by="local-bootstrap",
                    )
                )
            elif existing.status == "disabled":
                raise SystemExit("Disabled assignment requires separate privileged administration")
            elif existing.role != role:
                raise SystemExit("Existing role differs; refusing implicit mutation")
            await session.commit()
            print(f"Granted {role} to {account_id}")
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("account_id")
    parser.add_argument("--role", choices=("operator", "admin"), default="operator")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    asyncio.run(grant(args.account_id, args.role, args.confirm))


if __name__ == "__main__":
    main()
