"""Conversations + Messages HTTP routes (v2-M3).

Authorization model mirrors `kb/routes.py`:
- All routes require Bearer JWT.
- Every conversation is scoped to its owner. Cross-user requests return 404
  (not 403) to avoid leaking the existence of other users' resources.

Frontend orchestration:
- `POST /messages` is called twice per chat turn (once before SSE for the user
  message, once after `done`/`error` for the assistant message). The chat
  endpoint itself (`src/app.py`) stays stateless — it doesn't know about
  conversation_id.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.middleware import CurrentUser
from src.conversations.models import Conversation, Message
from src.infra.database import get_session

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class CreateConversationRequest(BaseModel):
    kb_id: str | None = Field(default=None, max_length=36)
    title: str | None = Field(default=None, max_length=128)


class PatchConversationRequest(BaseModel):
    title: str | None = Field(default=None, max_length=128)
    kb_id: str | None = Field(default=None, max_length=36)
    # v3-M6: per-conversation LLM model override. NULL = use user's default cfg.
    llm_model: str | None = Field(default=None, max_length=128)
    # Sentinel: pass {"kb_id": null} (or {"llm_model": null}) to unbind/reset.
    # Pydantic exposes this as `kb_id=None` whether the field was omitted or
    # set to null, so we use `model_fields_set` in the handler to disambiguate.


class AppendMessageRequest(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(default="")
    # Frontend passes ToolEvent[] as `tools`; stored as JSON text in DB.
    tools: list[dict[str, Any]] | None = Field(default=None)
    cost_usd: float | None = Field(default=None)
    error: str | None = Field(default=None, max_length=4096)


class ImportMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = ""
    tools: list[dict[str, Any]] | None = None
    cost_usd: float | None = None
    error: str | None = None
    # Epoch ms (client). Server converts → datetime to preserve original time.
    created_at: int | None = None


class ImportConversation(BaseModel):
    title: str = "新对话"
    kb_id: str | None = None
    created_at: int | None = None  # epoch ms
    updated_at: int | None = None  # epoch ms
    messages: list[ImportMessage] = Field(default_factory=list)


class ImportRequest(BaseModel):
    conversations: list[ImportConversation] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _load_owned_conversation(
    session: AsyncSession, conv_id: str, user_id: str
) -> Conversation:
    """Fetch a conversation owned by `user_id`, else 404 (no existence leak)."""
    conv = await session.get(Conversation, conv_id)
    if conv is None or conv.user_id != user_id:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


def _derive_title(content: str) -> str:
    cleaned = " ".join(content.strip().split())
    if not cleaned:
        return "新对话"
    return cleaned[:24] + "…" if len(cleaned) > 24 else cleaned


def _is_default_title(title: str) -> bool:
    return not title or title in {"新对话", "Untitled", ""}


def _ms_to_dt(ms: int | None) -> datetime | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("")
async def list_conversations(
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """List the user's conversations, newest update first.

    Returns summaries only — call GET /api/conversations/{id} to fetch the
    full messages array for a single conversation.
    """
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(desc(Conversation.updated_at))
    )
    return [c.to_summary_dict() for c in result.scalars().all()]


# ---------------------------------------------------------------------------
# v3-M5: bulk delete + export
# ---------------------------------------------------------------------------
@router.delete("", status_code=status.HTTP_200_OK)
async def delete_all_conversations(
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Hard-delete every conversation owned by the current user.

    ON DELETE CASCADE on messages.conversation_id handles message cleanup.
    """
    await session.execute(delete(Conversation).where(Conversation.user_id == user.id))
    await session.commit()
    return {"ok": True}


@router.get("/export")
async def export_conversations(
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
):
    """Stream a JSON dump of all conversations + messages for download."""
    convs_result = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(desc(Conversation.updated_at))
    )
    convs = convs_result.scalars().all()
    out: list[dict] = []
    for c in convs:
        msgs_result = await session.execute(
            select(Message)
            .where(Message.conversation_id == c.id)
            .order_by(Message.created_at)
        )
        msgs = msgs_result.scalars().all()
        out.append({
            "id": c.id,
            "title": c.title,
            "kb_id": c.kb_id,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            "messages": [m.to_public_dict() for m in msgs],
        })
    return JSONResponse(
        out,
        headers={"Content-Disposition": 'attachment; filename="anykb-export.json"'},
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    req: CreateConversationRequest,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    conv = Conversation(
        id=str(uuid.uuid4()),
        user_id=user.id,
        title=(req.title or "新对话").strip()[:128] or "新对话",
        kb_id=req.kb_id,
    )
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    return conv.to_dict_with_messages()


@router.get("/{conv_id}")
async def get_conversation(
    conv_id: str,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    conv = await _load_owned_conversation(session, conv_id, user.id)
    return conv.to_dict_with_messages()


@router.patch("/{conv_id}")
async def patch_conversation(
    conv_id: str,
    req: PatchConversationRequest,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    conv = await _load_owned_conversation(session, conv_id, user.id)

    fields_set = req.model_fields_set
    if "title" in fields_set and req.title is not None:
        title = req.title.strip()[:128]
        if title:
            conv.title = title
    if "kb_id" in fields_set:
        # Explicit set, including null (unbind).
        conv.kb_id = req.kb_id
    if "llm_model" in fields_set:
        # v3-M6: explicit set, including null (reset to user default).
        conv.llm_model = req.llm_model

    await session.commit()
    await session.refresh(conv)
    return conv.to_summary_dict()


@router.delete("/{conv_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conv_id: str,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> None:
    conv = await _load_owned_conversation(session, conv_id, user.id)
    await session.delete(conv)  # cascade drops messages
    await session.commit()


@router.post("/{conv_id}/messages", status_code=status.HTTP_201_CREATED)
async def append_message(
    conv_id: str,
    req: AppendMessageRequest,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Append a message to a conversation.

    Side effect: if this is the first user message AND the conversation title
    is still a default, derive a title from the content. Saves a round trip
    for the frontend.
    """
    conv = await _load_owned_conversation(session, conv_id, user.id)

    msg = Message(
        id=str(uuid.uuid4()),
        conversation_id=conv.id,
        role=req.role,
        content=req.content or "",
        tool_call_log=json.dumps(req.tools, ensure_ascii=False) if req.tools else None,
        cost_usd=req.cost_usd,
        error=req.error or None,
    )
    session.add(msg)

    # Auto-derive title on the first user message if user hasn't renamed yet.
    if req.role == "user" and _is_default_title(conv.title) and req.content.strip():
        conv.title = _derive_title(req.content)

    # Touch updated_at so the conversation bubbles to the top of the sidebar.
    # onupdate=_utcnow fires automatically since we're mutating `conv`.
    conv.updated_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(msg)

    # v3-M2 memory-optimization: after archiving to PG, feed the message into
    # the short-term memory path (Redis hot window + async batch compression).
    # v3-M3: also fire real-time keyword-triggered long-term extraction.
    # Both are fire-and-forget — never block the response. The session LLM cfg
    # (BYOK + per-conversation model override) is resolved once and shared,
    # matching app._run_chat_session's resolution.
    from src.conversations.long_term_memory import schedule_keyword_extraction
    from src.conversations.short_term_memory import (
        resolve_session_llm,
        schedule_memory_update,
        short_term_memory_enabled,
    )
    from src.settings import get_settings

    stm_on = short_term_memory_enabled()
    auto_extract = get_settings().memory_auto_extract
    if stm_on or auto_extract:
        llm_cfg = resolve_session_llm(user, conv)
        if stm_on:
            schedule_memory_update(user.id, conv.id, req.role, req.content or "", llm_cfg)
        # Gated inside on memory_auto_extract + role=="user" + a regex hit (a
        # cheap pre-check), so non-user / non-matching turns cost only one regex
        # pass and spawn no task.
        schedule_keyword_extraction(user.id, conv.id, req.role, req.content or "", llm_cfg)

    return msg.to_public_dict()


@router.post("/{conv_id}/finalize")
async def finalize_conversation(
    conv_id: str,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Mark a conversation finished → extract long-term memories (PRD §8).

    Idempotent AND concurrency-safe: the ``finalized_at`` stamp is applied with
    an atomic compare-and-swap (``UPDATE ... WHERE finalized_at IS NULL``), never
    a read-modify-write. Two finalize requests that race — both seeing the row
    open — cannot both extract: exactly one UPDATE matches (``rowcount == 1``),
    the loser gets ``rowcount == 0`` and returns ``already_finalized`` without
    re-extracting (background-tasks-guidelines: CAS gate, not blind read-modify-
    write, or duplicate memories get written under concurrent finalize).

    First (winning) call: run ``extract_conversation_memories`` — AWAITED, since
    this is a user-initiated action whose count the caller wants (the extractor
    owns its own sessions, reuses the SESSION LLM, defensively parses, and never
    raises), clear the conversation's Redis hot keys (best-effort), and report
    the count.

    Extraction is best-effort by design: ``memory_auto_extract=false`` or an LLM
    failure yields 0 WITHOUT rolling back ``finalized_at`` — the semantics are
    "the session has ended; extraction was attempted." ``profile_updated`` is
    ``True`` iff at least one memory was stored (any new memory may shift the L1
    aggregate, whose cache the persist path already invalidates).
    """
    # Atomic finalize gate (CAS): flip finalized_at NULL→now for exactly one
    # caller. Owner-scoped in the same predicate so a foreign id can never stamp.
    result = await session.execute(
        update(Conversation)
        .where(
            Conversation.id == conv_id,
            Conversation.user_id == user.id,
            Conversation.finalized_at.is_(None),
        )
        .values(finalized_at=datetime.now(timezone.utc))
    )
    await session.commit()

    # Re-read owner-scoped: a foreign/missing id 404s (the CAS matched 0 rows for
    # those too, but 404 must win over already_finalized — no existence leak).
    # For an owned row this also supplies the per-conversation LLM override below.
    conv = await _load_owned_conversation(session, conv_id, user.id)

    if result.rowcount == 0:
        # Already finalized (or a concurrent finalize won the race) → no re-extract.
        return {
            "memory_extracted": 0,
            "profile_updated": False,
            "already_finalized": True,
        }

    # We won the CAS. Resolve the session LLM once (BYOK + per-conversation
    # override), exactly like append_message — passing it in avoids a redundant
    # id-based re-resolve inside the extractor.
    from src.conversations.long_term_memory import extract_conversation_memories
    from src.conversations.short_term_memory import (
        clear_conversation_hot_state,
        resolve_session_llm,
    )

    llm_cfg = resolve_session_llm(user, conv)
    extracted = await extract_conversation_memories(conv_id, user.id, llm_cfg)

    # Best-effort hot-key cleanup: the session is over, so drop its Redis window
    # + meta. The durable PG copy survives; failure never affects the response.
    await clear_conversation_hot_state(user.id, conv_id)

    return {
        "memory_extracted": extracted,
        "profile_updated": extracted > 0,
        "already_finalized": False,
    }


@router.post("/import")
async def import_conversations(
    req: ImportRequest,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Bulk-import conversations from frontend localStorage shape.

    Each conversation gets a fresh server UUID — client IDs are not reused
    (avoids any collision with future server-generated IDs). Client-supplied
    timestamps are preserved so old conversations don't all show "now".
    No dedup logic — frontend uses a migrated flag to prevent re-imports.
    """
    imported = 0
    for c in req.conversations:
        c_created = _ms_to_dt(c.created_at) or datetime.now(timezone.utc)
        c_updated = _ms_to_dt(c.updated_at) or c_created

        # Pick a sensible title if the imported one is the default and we have
        # a user message to derive from.
        title = (c.title or "新对话").strip()[:128] or "新对话"
        if _is_default_title(title):
            for m in c.messages:
                if m.role == "user" and m.content.strip():
                    title = _derive_title(m.content)
                    break

        conv = Conversation(
            id=str(uuid.uuid4()),
            user_id=user.id,
            title=title,
            kb_id=c.kb_id,
            created_at=c_created,
            updated_at=c_updated,
        )
        session.add(conv)
        await session.flush()  # populate conv.id for FK

        for m in c.messages:
            m_created = _ms_to_dt(m.created_at) or c_created
            session.add(
                Message(
                    id=str(uuid.uuid4()),
                    conversation_id=conv.id,
                    role=m.role,
                    content=m.content or "",
                    tool_call_log=json.dumps(m.tools, ensure_ascii=False) if m.tools else None,
                    cost_usd=m.cost_usd,
                    error=m.error or None,
                    created_at=m_created,
                )
            )
        imported += 1

    await session.commit()
    return {"imported": imported}
