"""Shared test harness for DB-backed / HTTP tests (introduced 06-01 admin-dashboard).

The pre-existing suite is pure-unit (no DB, no HTTP). Admin features need a real
app DB + authenticated requests, so this conftest provides:

  - `db`     : an isolated temp-SQLite app DB with tables + additive migration
               applied (resets the cached settings + engine globals so the
               temp DATABASE_URL actually takes effect, and disposes on teardown).
  - `client` : an httpx AsyncClient bound to the FastAPI app via ASGITransport.
               NOTE: httpx does not run ASGI lifespan, so `db` does init_db()
               explicitly — startup seeds (system KB / admins) are invoked
               directly by the tests that need them.
  - `create_user` : factory to insert a user (with is_admin / is_active) directly.

Local SQLite mirrors prod PostgreSQL closely enough for these paths; the one
real divergence (boolean DDL defaults) is covered by an explicit migration test.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """Isolated temp-SQLite app DB with schema + additive migration applied."""
    db_file = tmp_path / "test_app.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("BYOK_REQUIRED", "false")
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    # Local vector store: no network, and it has no delete_collection so
    # purge_kb cleanly skips vector cleanup in tests (see kb.routes.purge_kb).
    monkeypatch.setenv("VECTOR_STORE", "local")

    from src.settings import get_settings
    import src.infra.database as database
    import src.infra.vector_store as vector_store

    # Drop cached Settings + engine + store so the temp env is picked up.
    get_settings.cache_clear()
    database._engine = None
    database._session_factory = None
    vector_store._store = None

    await database.init_db()

    yield database

    if database._engine is not None:
        await database._engine.dispose()
    database._engine = None
    database._session_factory = None
    vector_store._store = None
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(db):  # noqa: ARG001 — depends on db for setup/teardown ordering
    from httpx import ASGITransport, AsyncClient

    from src.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def create_user():
    """Return an async factory that inserts a user straight into the app DB."""

    async def _create(
        email: str,
        password: str = "password123",
        *,
        is_admin: bool = False,
        is_active: bool = True,
        display_name: str = "",
    ):
        from src.auth.models import User
        from src.auth.password import hash_password
        from src.infra.database import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            user = User(
                id=str(uuid.uuid4()),
                email=email.lower(),
                password_hash=hash_password(password),
                display_name=display_name or email.split("@")[0],
                is_admin=is_admin,
                is_active=is_active,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            return user

    return _create


@pytest.fixture
def create_kb():
    """Return an async factory that inserts a KB row straight into the app DB."""

    async def _create(owner_id: str, name: str = "KB", *, is_system: bool = False):
        from src.infra.database import get_session_factory
        from src.kb.models import KB

        factory = get_session_factory()
        async with factory() as session:
            kb = KB(
                id=str(uuid.uuid4()),
                user_id=owner_id,
                name=name,
                description="",
                is_system=is_system,
            )
            session.add(kb)
            await session.commit()
            await session.refresh(kb)
            return kb

    return _create
