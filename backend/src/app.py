"""FastAPI entry — SSE chat endpoint + auth routes.

M0: POST /api/chat takes full message history (multi-turn context).
M1: /api/auth/{register,login,me} + chat endpoint requires Bearer JWT.
M3: POST /api/chat optionally takes kb_id; when set, the agent runs in
    KB-bound mode (search_kb only, no travel tools).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace as dc_replace
from typing import Any, AsyncGenerator, Literal

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from src.agent.graph import build_graph
from src.auth.middleware import CurrentUser
from src.auth.models import User
from src.auth.routes import router as auth_router
from src.conversations.routes import router as conversations_router
from src.infra.database import get_session, init_db
from src.infra.rate_limit import check as rate_check
from src.kb.models import KB
from src.kb.routes import invitations_router
from src.kb.routes import router as kb_router
from src.safety.input_filter import sanitize_user_input
from src.safety.output_filter import redact_pii
from src.settings import get_settings
from src.settings_user import (
    require_user_embedding,
    require_user_llm,
    resolve_user_embedding,
    resolve_user_llm,
    resolve_user_reranker,
)

logging.basicConfig(level=logging.INFO)
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    log.info("startup", env=get_settings().app_env)
    await init_db()
    log.info("db_ready")
    from src.kb.system_seed import seed_system_kbs

    await seed_system_kbs()
    log.info("system_kbs_seeded")

    from src.auth.admin_seed import seed_admins

    await seed_admins()
    log.info("admins_seeded")
    yield
    log.info("shutdown")


app = FastAPI(title="AnyKB", version="3.1.0", lifespan=lifespan)

# v2-M7 — optional Logfire monitoring. Enabled only when both:
#   1) `pip install -e '.[monitoring]'` (or pip install logfire)
#   2) LOGFIRE_TOKEN env is set (get one at https://logfire.pydantic.dev/)
# Falls through silently when either is missing — structlog still works.
_logfire_token = os.getenv("LOGFIRE_TOKEN")
if _logfire_token:
    try:
        import logfire  # type: ignore[import-not-found]

        logfire.configure(
            token=_logfire_token,
            service_name=os.getenv("LOGFIRE_SERVICE_NAME", "anykb-backend"),
        )
        logfire.instrument_fastapi(app, capture_headers=False)
        logfire.instrument_httpx()
        log.info("logfire_enabled")
    except ImportError:
        log.warning(
            "LOGFIRE_TOKEN set but logfire not installed; "
            "run `pip install -e '.[monitoring]'` to enable monitoring"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("logfire_init_failed", error=str(exc))

s = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in s.cors_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST", "DELETE", "PATCH"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(kb_router)
app.include_router(invitations_router)
app.include_router(conversations_router)

from src.conversations.memory_routes import router as memories_router  # noqa: E402

app.include_router(memories_router)

from src.settings_user.routes import router as settings_router  # noqa: E402

app.include_router(settings_router)

from src.admin.routes import router as admin_router  # noqa: E402

app.include_router(admin_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.3.0"}


# ---------------------------------------------------------------------------
# Chat schemas
# ---------------------------------------------------------------------------
class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1)
    kb_id: str | None = Field(default=None, description="Bind to a KB; agent uses search_kb only")
    # v3-M6: optional per-request LLM model override. Frontend reads
    # currentConv.llm_model (saved per-conversation in DB) and passes it here.
    # Server applies via dataclasses.replace() — no schema-level dependency.
    model: str | None = Field(default=None, max_length=128)
    # v3-M2 memory-optimization: optional source conversation id. Lets plan_node
    # read this conversation's early-summary (L4). Reads are scoped to the
    # authenticated user (Redis key embeds user_id; PG lookup checks ownership),
    # so a foreign id yields "" rather than leaking. Old frontends that don't
    # send it simply get no L4 layer (exact M1 behavior).
    conversation_id: str | None = Field(default=None, max_length=36)


# ---------------------------------------------------------------------------
# Shared session runner — used by both POST and the deprecated GET endpoint
# ---------------------------------------------------------------------------
def _run_chat_session(
    messages: list[dict[str, str]],
    rate_key: str,
    user_email: str | None = None,
    *,
    kb: KB | None = None,
    user: User | None = None,
    model_override: str | None = None,
    conversation_id: str | None = None,
) -> EventSourceResponse:
    settings = get_settings()
    allowed, remaining = rate_check(rate_key, settings.rate_limit_per_hour)
    if not allowed:
        raise HTTPException(status_code=429, detail="rate_limit_exceeded")

    if not messages or messages[-1]["role"] != "user":
        raise HTTPException(
            status_code=400,
            detail="messages must be non-empty and end with role='user'",
        )

    last_user_content = messages[-1]["content"]
    cleaned, blocked = sanitize_user_input(last_user_content)
    if blocked:
        raise HTTPException(status_code=400, detail=f"input_blocked: {blocked}")

    full_messages = messages[:-1] + [{"role": "user", "content": cleaned}]

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def emit(evt: dict[str, Any]) -> None:
        await queue.put(evt)

    # v2-M1: per-user LLM / embedding overrides. None = fall back to env.
    llm_cfg = resolve_user_llm(user) if user is not None else None
    # v3-M6: per-conversation model override — swap both default + complex on
    # the resolved cfg so pick_model() / plan_node both pick up the choice
    # without touching graph code.
    if model_override and llm_cfg is not None:
        llm_cfg = dc_replace(
            llm_cfg, default_model=model_override, complex_model=model_override
        )
    # v3-M7: embedding + reranker cfgs are KB-level when a KB is selected
    # (KB row carries its own creds → KB cfg wins; else fall back to user cfg).
    # For unbound chat (no KB), there's nothing to embed so user cfg is fine.
    if kb is not None and user is not None:
        from src.settings_user.kb_resolvers import (
            resolve_kb_embedding,
            resolve_kb_reranker,
        )
        embedding_cfg = resolve_kb_embedding(kb, user)
        reranker_cfg = resolve_kb_reranker(kb, user)
    else:
        embedding_cfg = resolve_user_embedding(user) if user is not None else None
        # v3-M4: per-user reranker override (opt-in, default None). Returned None
        # both when the user hasn't saved a config AND when the enable toggle is
        # off, so KBSearchTool can use this as the single skip-rerank signal.
        reranker_cfg = resolve_user_reranker(user) if user is not None else None
    # v2-M6: per-user KB-mode web_search opt-in flag.
    kb_web_search_enabled = bool(getattr(user, "kb_web_search_enabled", False))
    graph, cost = build_graph(
        emit=emit,
        kb=kb,
        llm_cfg=llm_cfg,
        embedding_cfg=embedding_cfg,
        reranker_cfg=reranker_cfg,
        kb_web_search_enabled=kb_web_search_enabled,
    )

    async def run_agent() -> None:
        try:
            initial_state: dict[str, Any] = {
                "messages": full_messages,
                "iterations": 0,
                "tool_call_log": [],
            }
            # v3-M2: thread the session identity into agent state so plan_node
            # can fetch this conversation's early-summary (L4). Only set when
            # both ids are present — a missing conversation_id (old frontend)
            # leaves L4 off, preserving M1 behavior.
            if conversation_id and user is not None:
                initial_state["conversation_id"] = conversation_id
                initial_state["user_id"] = user.id
            final_state = await graph.ainvoke(initial_state)
            report = redact_pii(final_state.get("final_report") or "")
            await queue.put({"event": "report_start"})
            for piece in _chunks(report, size=8):
                await queue.put({"event": "token", "text": piece})
                await asyncio.sleep(0.02)
            await queue.put(
                {
                    "event": "done",
                    "cost_usd": round(final_state.get("cost_usd", 0.0), 6),
                    "rate_remaining": remaining,
                    "kb_id": kb.id if kb else None,
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("agent_failed", user=user_email, kb_id=kb.id if kb else None)
            await queue.put({"event": "error", "message": str(exc)})
        finally:
            await queue.put(None)

    task = asyncio.create_task(run_agent())

    async def event_gen() -> AsyncGenerator[dict[str, str], None]:
        try:
            while True:
                evt = await queue.get()
                if evt is None:
                    break
                yield {"event": "message", "data": json.dumps(evt, ensure_ascii=False)}
        finally:
            if not task.done():
                task.cancel()

    return EventSourceResponse(event_gen(), ping=15)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/api/chat")
async def chat_post(
    req: ChatRequest,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> EventSourceResponse:
    """SSE stream with full message history. Requires Bearer JWT.

    If kb_id is set, validates ownership and runs the agent in KB-bound mode.

    BYOK gate (v2-M2): when settings.byok_required is True, user must have
    configured their own LLM cfg; if kb_id is set they also need embedding cfg
    (KB-mode chat embeds the query for similarity search).
    """
    require_user_llm(user)

    kb: KB | None = None
    if req.kb_id:
        kb = await session.get(KB, req.kb_id)
        if kb is None:
            raise HTTPException(status_code=404, detail="kb not found")
        # v2-M9: any role (owner / editor / viewer) grants read access. System
        # KB returns "viewer" for everyone. None role = caller has no access,
        # answer 404 to avoid leaking existence.
        role = await kb.role_for(session, user.id)
        if role is None:
            raise HTTPException(status_code=404, detail="kb not found")
        # KB-mode chat needs embedding cfg too (search_kb embeds the query).
        # Skip the check for system KBs (they're read-only and predate BYOK).
        # v3-M7: also skip when the KB carries its own embedding cfg — the
        # caller doesn't need user-level cfg to use a KB that brings its own.
        if not kb.is_system and not bool(getattr(kb, "embedding_provider", None)):
            require_user_embedding(user)

    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    return _run_chat_session(
        messages,
        rate_key=f"user:{user.id}",
        user_email=user.email,
        kb=kb,
        user=user,
        model_override=req.model,
        conversation_id=req.conversation_id,
    )


@app.get("/api/chat", deprecated=True)
async def chat_get(q: str, request: Request) -> EventSourceResponse:
    """Deprecated single-turn endpoint (anonymous, IP-rate-limited).

    Kept for backward-compat smoke tests. New clients must use POST with auth.
    """
    client_ip = (request.client.host if request.client else "unknown") or "unknown"
    return _run_chat_session(
        [{"role": "user", "content": q}], rate_key=f"ip:{client_ip}"
    )


def _chunks(s: str, *, size: int) -> list[str]:
    return [s[i : i + size] for i in range(0, len(s), size)]
