"""Seed platform admins from the ADMIN_EMAILS allowlist on startup.

Idempotent and safe to call on every boot. Only flips already-registered users
to is_admin=True; emails not yet registered are logged and skipped (they get
promoted on a later boot once the account exists). This lets the operator
bootstrap the first admin without a manual DB edit:

    1. Register normally through the app.
    2. Add the email to ADMIN_EMAILS in backend/.env.
    3. Restart — the account is now an admin.

Runtime promotion/demotion happens through the admin API (PATCH user); the env
allowlist is just the startup floor, not a hard source of truth.
"""
from __future__ import annotations

import structlog
from sqlalchemy import select

from src.auth.models import User
from src.infra.database import get_session_factory
from src.settings import get_settings

log = structlog.get_logger()


async def seed_admins() -> None:
    """Promote every registered user whose email is in ADMIN_EMAILS."""
    emails = [e.strip().lower() for e in get_settings().admin_emails.split(",") if e.strip()]
    if not emails:
        return

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(select(User).where(User.email.in_(emails)))
        users = result.scalars().all()

        promoted = 0
        for u in users:
            if not u.is_admin:
                u.is_admin = True
                promoted += 1
        if promoted:
            await session.commit()

        found = {u.email for u in users}
        not_registered = [e for e in emails if e not in found]
        log.info(
            "admins_seeded",
            requested=len(emails),
            promoted=promoted,
            already_admin=len(users) - promoted,
            not_registered=not_registered,
        )
