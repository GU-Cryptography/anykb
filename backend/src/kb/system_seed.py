"""Seed built-in system KBs on app startup.

Idempotent: if a system KB row already exists (matched by its fixed UUID),
we leave it alone. The vector data lives in pre-existing Qdrant collections
(populated by `data/ingest.py` for the travel demo) — we don't touch Qdrant
from here.

To add a new built-in KB:
  1. Pick a stable UUID (constants in src.kb.models)
  2. If it should override the default `kb_{uuid}` collection name, add a
     branch to `KB.collection_name`
  3. Add an `await _seed_one(...)` call in `seed_system_kbs()` below
"""
from __future__ import annotations

import structlog

from src.infra.database import get_session_factory
from src.kb.models import KB, SYSTEM_TRAVEL_KB_ID, SYSTEM_USER_ID

log = structlog.get_logger()


async def seed_system_kbs() -> None:
    """Ensure built-in read-only KBs exist. Safe to call on every startup."""
    factory = get_session_factory()
    async with factory() as session:
        await _seed_one(
            session,
            kb_id=SYSTEM_TRAVEL_KB_ID,
            name="TravelGPT 演示库",
            description=(
                "本地老饕策展的 4 城（上海 / 北京 / 成都 / 杭州）餐厅库，"
                "仅供演示。绑定此知识库会启用天气查询 / 高德 POI 兜底 / "
                "旅行报告生成等专用工具，体验完整 TravelGPT 流程。"
            ),
            embedding_model="BAAI/bge-m3",
            vector_size=1024,
        )


async def _seed_one(
    session,
    *,
    kb_id: str,
    name: str,
    description: str,
    embedding_model: str,
    vector_size: int,
) -> None:
    existing = await session.get(KB, kb_id)
    if existing is not None:
        # Refresh metadata so updates to name/description here flow through
        # without requiring a manual DB edit.
        changed = False
        if existing.name != name:
            existing.name = name
            changed = True
        if existing.description != description:
            existing.description = description
            changed = True
        if not existing.is_system:
            existing.is_system = True
            changed = True
        if changed:
            await session.commit()
            log.info("system_kb_seed_updated", kb_id=kb_id)
        else:
            log.info("system_kb_seed_skip", kb_id=kb_id)
        return

    kb = KB(
        id=kb_id,
        user_id=SYSTEM_USER_ID,
        name=name,
        description=description,
        embedding_model=embedding_model,
        vector_size=vector_size,
        is_system=True,
    )
    session.add(kb)
    await session.commit()
    log.info("system_kb_seed_created", kb_id=kb_id, name=name)
