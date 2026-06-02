"""PR1 foundation tests for the admin dashboard.

Covers the platform-role plumbing without any admin endpoints yet (those land
in PR2): the additive column migration, the require_admin dependency, ban
enforcement at login + protected endpoints, the ADMIN_EMAILS startup seed, and
the public-dict surfacing of the new flags.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Migration — the cross-dialect gotcha this whole feature hinges on.
# ---------------------------------------------------------------------------
def test_migration_adds_admin_columns_and_is_idempotent(tmp_path):
    """A legacy `users` table missing is_admin/is_active gets them backfilled,
    existing rows take the DEFAULT, and re-running the migration is a no-op.
    """
    from sqlalchemy import create_engine, inspect, text

    from src.infra.database import _migrate_additive_columns

    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    # Simulate an old DB: users table predating every additive column.
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, email VARCHAR(255))")
        )
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u1', 'old@x.com')"))

    # Two separate "startups" — must be idempotent (no error on the second).
    for _ in range(2):
        with engine.begin() as conn:
            _migrate_additive_columns(conn)

    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("users")}
    assert "is_admin" in cols
    assert "is_active" in cols

    # Existing row backfilled with the column DEFAULTs (admin=false, active=true).
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT is_admin, is_active FROM users WHERE id = 'u1'")
        ).one()
    assert row[0] in (0, False)
    assert row[1] in (1, True)


# ---------------------------------------------------------------------------
# require_admin dependency (pure — no DB needed).
# ---------------------------------------------------------------------------
async def test_require_admin_allows_admin():
    from src.auth.middleware import require_admin

    class _U:
        is_admin = True

    user = _U()
    assert await require_admin(user) is user


async def test_require_admin_rejects_non_admin():
    from src.auth.middleware import require_admin

    class _U:
        is_admin = False

    with pytest.raises(HTTPException) as exc:
        await require_admin(_U())
    assert exc.value.status_code == 403
    assert exc.value.detail == "admin only"


def test_to_public_dict_exposes_admin_flags():
    from src.auth.models import User

    u = User(id="x", email="a@b.c", password_hash="!", is_admin=True, is_active=False)
    d = u.to_public_dict()
    assert d["is_admin"] is True
    assert d["is_active"] is False


# ---------------------------------------------------------------------------
# Ban enforcement — login + protected endpoint (HTTP).
# ---------------------------------------------------------------------------
async def test_login_rejects_banned_user(client, create_user):
    await create_user("banned@x.com", "password123", is_active=False)
    r = await client.post(
        "/api/auth/login", json={"email": "banned@x.com", "password": "password123"}
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "account disabled"


async def test_login_success_returns_admin_flag(client, create_user):
    await create_user("la@x.com", "password123", is_admin=True)
    r = await client.post(
        "/api/auth/login", json={"email": "la@x.com", "password": "password123"}
    )
    assert r.status_code == 200
    assert r.json()["user"]["is_admin"] is True


async def test_banned_user_blocked_on_protected_endpoint(client, create_user):
    u = await create_user("banned2@x.com", is_active=False)
    from src.auth.tokens import issue_token

    token = issue_token(u.id, u.email)
    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert r.json()["detail"] == "account disabled"


async def test_active_user_me_includes_admin_flags(client, create_user):
    u = await create_user("ok@x.com", is_admin=True)
    from src.auth.tokens import issue_token

    token = issue_token(u.id, u.email)
    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["is_admin"] is True
    assert body["is_active"] is True


# ---------------------------------------------------------------------------
# ADMIN_EMAILS startup seed.
# ---------------------------------------------------------------------------
async def test_seed_admins_promotes_only_listed_registered_emails(
    db, create_user, monkeypatch  # noqa: ARG001 — db sets up the engine
):
    from sqlalchemy import select

    from src.auth.admin_seed import seed_admins
    from src.auth.models import User
    from src.infra.database import get_session_factory
    from src.settings import get_settings

    await create_user("boss@x.com")
    await create_user("worker@x.com")

    # boss is registered → promote; ghost isn't → silently skipped.
    monkeypatch.setenv("ADMIN_EMAILS", "boss@x.com, ghost@x.com")
    get_settings.cache_clear()

    await seed_admins()
    await seed_admins()  # idempotent — second run promotes nobody new

    factory = get_session_factory()
    async with factory() as s:
        boss = (await s.execute(select(User).where(User.email == "boss@x.com"))).scalar_one()
        worker = (
            await s.execute(select(User).where(User.email == "worker@x.com"))
        ).scalar_one()

    assert boss.is_admin is True
    assert worker.is_admin is False


# ===========================================================================
# PR2 — Admin API endpoints (/api/admin/*)
# ===========================================================================
def _bearer(user) -> dict:
    from src.auth.tokens import issue_token

    return {"Authorization": f"Bearer {issue_token(user.id, user.email)}"}


# --- guard ---------------------------------------------------------------
async def test_admin_endpoint_requires_auth(client):
    r = await client.get("/api/admin/stats")
    assert r.status_code == 401


async def test_admin_endpoint_forbidden_for_non_admin(client, create_user):
    u = await create_user("plain@x.com")
    r = await client.get("/api/admin/stats", headers=_bearer(u))
    assert r.status_code == 403
    assert r.json()["detail"] == "admin only"


async def test_admin_endpoint_ok_for_admin(client, create_user):
    a = await create_user("admin@x.com", is_admin=True)
    r = await client.get("/api/admin/stats", headers=_bearer(a))
    assert r.status_code == 200


async def test_banned_admin_cannot_use_admin_api(client, create_user):
    # current_user rejects banned accounts before require_admin even runs.
    a = await create_user("a@x.com", is_admin=True, is_active=False)
    r = await client.get("/api/admin/stats", headers=_bearer(a))
    assert r.status_code == 403


# --- stats ---------------------------------------------------------------
async def test_stats_counts(client, create_user, create_kb):
    admin = await create_user("a@x.com", is_admin=True)
    u1 = await create_user("u1@x.com")
    await create_user("banned@x.com", is_active=False)
    await create_kb(u1.id, "U1 KB")

    body = (await client.get("/api/admin/stats", headers=_bearer(admin))).json()
    assert body["users"]["total"] == 3
    assert body["users"]["admins"] == 1
    assert body["users"]["banned"] == 1
    assert body["users"]["active"] == 2
    assert body["kbs"]["total"] == 1
    assert body["documents"] == 0
    assert body["conversations"] == 0
    assert body["messages"] == 0


# --- users list / detail -------------------------------------------------
async def test_list_users_includes_counts_and_no_content(client, create_user, create_kb):
    admin = await create_user("a@x.com", is_admin=True)
    u1 = await create_user("u1@x.com")
    await create_kb(u1.id, "K1")
    await create_kb(u1.id, "K2")

    body = (await client.get("/api/admin/users", headers=_bearer(admin))).json()
    assert body["total"] == 2
    by_email = {u["email"]: u for u in body["users"]}
    assert by_email["u1@x.com"]["kb_count"] == 2
    assert by_email["u1@x.com"]["conversation_count"] == 0
    assert by_email["u1@x.com"]["byok_configured"] is False
    # privacy boundary: no message / chunk content fields leak through
    assert "messages" not in by_email["u1@x.com"]
    assert "content" not in by_email["u1@x.com"]


async def test_get_user_404(client, create_user):
    admin = await create_user("a@x.com", is_admin=True)
    r = await client.get("/api/admin/users/does-not-exist", headers=_bearer(admin))
    assert r.status_code == 404


# --- patch: ban / unban / promote / demote -------------------------------
async def test_patch_ban_then_unban(client, create_user):
    admin = await create_user("a@x.com", is_admin=True)
    u = await create_user("u@x.com")

    r = await client.patch(
        f"/api/admin/users/{u.id}", json={"is_active": False}, headers=_bearer(admin)
    )
    assert r.status_code == 200 and r.json()["is_active"] is False
    # banned → login blocked
    rl = await client.post(
        "/api/auth/login", json={"email": "u@x.com", "password": "password123"}
    )
    assert rl.status_code == 403
    # unban → login works again
    r2 = await client.patch(
        f"/api/admin/users/{u.id}", json={"is_active": True}, headers=_bearer(admin)
    )
    assert r2.json()["is_active"] is True
    rl2 = await client.post(
        "/api/auth/login", json={"email": "u@x.com", "password": "password123"}
    )
    assert rl2.status_code == 200


async def test_patch_promote_then_demote(client, create_user):
    admin = await create_user("a@x.com", is_admin=True)
    u = await create_user("u@x.com")
    r = await client.patch(
        f"/api/admin/users/{u.id}", json={"is_admin": True}, headers=_bearer(admin)
    )
    assert r.json()["is_admin"] is True
    r2 = await client.patch(
        f"/api/admin/users/{u.id}", json={"is_admin": False}, headers=_bearer(admin)
    )
    assert r2.json()["is_admin"] is False


async def test_patch_nothing_to_update(client, create_user):
    admin = await create_user("a@x.com", is_admin=True)
    u = await create_user("u@x.com")
    r = await client.patch(f"/api/admin/users/{u.id}", json={}, headers=_bearer(admin))
    assert r.status_code == 400


# --- self-protection (the operative guard) -------------------------------
async def test_cannot_ban_self(client, create_user):
    admin = await create_user("a@x.com", is_admin=True)
    r = await client.patch(
        f"/api/admin/users/{admin.id}", json={"is_active": False}, headers=_bearer(admin)
    )
    assert r.status_code == 400


async def test_cannot_demote_self(client, create_user):
    admin = await create_user("a@x.com", is_admin=True)
    r = await client.patch(
        f"/api/admin/users/{admin.id}", json={"is_admin": False}, headers=_bearer(admin)
    )
    assert r.status_code == 400


async def test_cannot_delete_self(client, create_user):
    admin = await create_user("a@x.com", is_admin=True)
    r = await client.delete(f"/api/admin/users/{admin.id}", headers=_bearer(admin))
    assert r.status_code == 400


async def test_can_demote_another_admin_when_not_last(client, create_user):
    admin = await create_user("a@x.com", is_admin=True)
    other = await create_user("b@x.com", is_admin=True)
    # acting admin remains → demoting `other` is allowed
    r = await client.patch(
        f"/api/admin/users/{other.id}", json={"is_admin": False}, headers=_bearer(admin)
    )
    assert r.status_code == 200 and r.json()["is_admin"] is False


# --- reset password ------------------------------------------------------
async def test_reset_password(client, create_user):
    admin = await create_user("a@x.com", is_admin=True)
    await create_user("u@x.com", "oldpassword")
    target = (await client.get("/api/admin/users", headers=_bearer(admin))).json()
    uid = next(u["id"] for u in target["users"] if u["email"] == "u@x.com")

    r = await client.post(
        f"/api/admin/users/{uid}/reset-password",
        json={"new_password": "brandnewpw"},
        headers=_bearer(admin),
    )
    assert r.status_code == 200
    assert (
        await client.post(
            "/api/auth/login", json={"email": "u@x.com", "password": "oldpassword"}
        )
    ).status_code == 401
    assert (
        await client.post(
            "/api/auth/login", json={"email": "u@x.com", "password": "brandnewpw"}
        )
    ).status_code == 200


# --- delete user (reuses purge_user) -------------------------------------
async def test_delete_user_purges_owned_kbs(client, create_user, create_kb):
    admin = await create_user("a@x.com", is_admin=True)
    u1 = await create_user("u1@x.com")
    await create_kb(u1.id, "K1")

    r = await client.delete(f"/api/admin/users/{u1.id}", headers=_bearer(admin))
    assert r.status_code == 204
    assert (
        await client.get(f"/api/admin/users/{u1.id}", headers=_bearer(admin))
    ).status_code == 404
    # their KB is gone too
    assert (await client.get("/api/admin/kbs", headers=_bearer(admin))).json()["total"] == 0


# --- KBs -----------------------------------------------------------------
async def test_list_kbs_across_users(client, create_user, create_kb):
    admin = await create_user("a@x.com", is_admin=True)
    u1 = await create_user("u1@x.com")
    u2 = await create_user("u2@x.com")
    await create_kb(u1.id, "K1")
    await create_kb(u2.id, "K2")

    body = (await client.get("/api/admin/kbs", headers=_bearer(admin))).json()
    assert body["total"] == 2
    assert {k["owner_email"] for k in body["kbs"]} == {"u1@x.com", "u2@x.com"}


async def test_delete_kb(client, create_user, create_kb):
    admin = await create_user("a@x.com", is_admin=True)
    u1 = await create_user("u1@x.com")
    kb = await create_kb(u1.id, "K1")
    r = await client.delete(f"/api/admin/kbs/{kb.id}", headers=_bearer(admin))
    assert r.status_code == 204
    assert (await client.get("/api/admin/kbs", headers=_bearer(admin))).json()["total"] == 0


async def test_cannot_delete_system_kb(client, create_user, create_kb):
    admin = await create_user("a@x.com", is_admin=True)
    kb = await create_kb(admin.id, "SysKB", is_system=True)
    r = await client.delete(f"/api/admin/kbs/{kb.id}", headers=_bearer(admin))
    assert r.status_code == 400
