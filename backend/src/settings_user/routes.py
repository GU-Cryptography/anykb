"""Per-user settings HTTP routes (v2-M1).

Endpoints (all require Bearer JWT):
  GET    /api/settings/me                    — current user's saved + effective config
  PUT    /api/settings/llm                   — save LLM block
  DELETE /api/settings/llm                   — clear LLM block (revert to env fallback)
  PUT    /api/settings/embedding             — save embedding block (with dim-conflict check)
  DELETE /api/settings/embedding             — clear embedding block
  POST   /api/settings/probe/llm             — probe a candidate provider's model list
  POST   /api/settings/probe/embedding       — same for embedding (+ optional live dim probe)

Saving the LLM block with `api_key=""` keeps the existing encrypted key (lets
users update only the model without re-entering the key). Saving the embedding
block follows the same convention.

Dim-conflict semantics: when a user changes their embedding dim while owning
KBs created with a different dim, the PUT returns 409 + `affected_kbs` list.
The user must delete those KBs (or accept the loss) before the change can land.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.middleware import CurrentUser
from src.auth.models import User
from src.infra.crypto import decrypt, encrypt
from src.infra.database import get_session
from src.kb.models import KB
from src.settings_user.probe import (
    EmbeddingProbeResult,
    ProbeError,
    probe_embedding,
    probe_llm_models,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
LLM_PROVIDERS = Literal["anthropic", "openai-compat"]
EMBEDDING_PROVIDERS = Literal["openai-compat", "ollama"]
RERANKER_PROVIDERS = Literal["siliconflow", "cohere", "openai-compat"]


class LLMBody(BaseModel):
    provider: LLM_PROVIDERS
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = Field(default="", max_length=512)  # "" = keep existing
    default_model: str = Field(min_length=1, max_length=128)
    complex_model: str = Field(default="", max_length=128)


class EmbeddingBody(BaseModel):
    provider: EMBEDDING_PROVIDERS
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = Field(default="", max_length=512)
    model: str = Field(min_length=1, max_length=128)
    dim: int = Field(gt=0, le=8192)


class RerankerBody(BaseModel):
    """v3-M4: per-user cross-encoder reranker config (opt-in, default off)."""
    provider: RERANKER_PROVIDERS
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = Field(default="", max_length=512)  # "" = keep existing
    model: str = Field(min_length=1, max_length=128)
    enabled: bool = True


class ProbeLLMBody(BaseModel):
    provider: LLM_PROVIDERS
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = Field(min_length=1, max_length=512)


class ProbeEmbeddingBody(BaseModel):
    provider: EMBEDDING_PROVIDERS
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = Field(default="", max_length=512)
    model: str = Field(default="", max_length=128)


class ProbeRerankerBody(BaseModel):
    """v3-M4: probe a candidate reranker provider — reuses the openai-compat
    /models lister so the UI dropdown can be populated. The list is unfiltered;
    the user picks the rerank-capable model name themselves.
    """
    provider: RERANKER_PROVIDERS
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = Field(default="", max_length=512)


class KbOptionsBody(BaseModel):
    """v2-M6: per-user KB-mode toggles. Currently just web_search opt-in."""
    kb_web_search_enabled: bool


# ---------------------------------------------------------------------------
# GET /me — current saved + effective view
# ---------------------------------------------------------------------------
def _to_public(user: User) -> dict:
    """Saved-side projection (user's persisted choices). Never reveal api_key."""
    return {
        "llm": {
            "provider": user.llm_provider,
            "base_url": user.llm_base_url,
            "default_model": user.llm_default_model,
            "complex_model": user.llm_complex_model,
            "has_key": bool(user.llm_api_key_enc),
            "configured": bool(
                user.llm_provider
                and user.llm_base_url
                and user.llm_api_key_enc
                and user.llm_default_model
            ),
        },
        "embedding": {
            "provider": user.embedding_provider,
            "base_url": user.embedding_base_url,
            "model": user.embedding_model,
            "dim": user.embedding_dim,
            "has_key": bool(user.embedding_api_key_enc),
            "configured": bool(
                user.embedding_provider
                and user.embedding_base_url
                and user.embedding_model
                and user.embedding_dim
            ),
        },
        # v3-M4: per-user cross-encoder reranker. `configured` = config fields
        # present; `enabled` = toggle on. Resolver requires both for the
        # reranker to actually fire at chat time.
        "reranker": {
            "provider": getattr(user, "reranker_provider", None),
            "base_url": getattr(user, "reranker_base_url", None),
            "model": getattr(user, "reranker_model", None),
            "has_key": bool(getattr(user, "reranker_api_key_enc", None)),
            "configured": bool(
                getattr(user, "reranker_provider", None)
                and getattr(user, "reranker_base_url", None)
                and getattr(user, "reranker_model", None)
            ),
            "enabled": bool(getattr(user, "reranker_enabled", False)),
        },
        # v2-M6: KB-mode toggles.
        "kb_options": {
            "kb_web_search_enabled": bool(getattr(user, "kb_web_search_enabled", False)),
        },
    }


@router.get("/me")
async def get_my_settings(user: CurrentUser) -> dict:
    return _to_public(user)


# ---------------------------------------------------------------------------
# PUT /llm — save LLM block
# ---------------------------------------------------------------------------
@router.put("/llm")
async def save_llm(
    body: LLMBody,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    user_row = await session.get(User, user.id)
    if user_row is None:
        raise HTTPException(status_code=404, detail="user not found")

    user_row.llm_provider = body.provider
    user_row.llm_base_url = body.base_url.rstrip("/")
    if body.api_key:
        user_row.llm_api_key_enc = encrypt(body.api_key)
    elif not user_row.llm_api_key_enc:
        raise HTTPException(
            status_code=400, detail="api_key required for first-time configuration"
        )
    user_row.llm_default_model = body.default_model
    user_row.llm_complex_model = body.complex_model or body.default_model
    await session.commit()
    await session.refresh(user_row)
    return _to_public(user_row)


@router.delete("/llm", status_code=status.HTTP_204_NO_CONTENT)
async def clear_llm(
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> None:
    user_row = await session.get(User, user.id)
    if user_row is None:
        return
    user_row.llm_provider = None
    user_row.llm_base_url = None
    user_row.llm_api_key_enc = None
    user_row.llm_default_model = None
    user_row.llm_complex_model = None
    await session.commit()


# ---------------------------------------------------------------------------
# PUT /embedding — save embedding block (with dim-conflict pre-check)
# ---------------------------------------------------------------------------
@router.put("/embedding")
async def save_embedding(
    body: EmbeddingBody,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    user_row = await session.get(User, user.id)
    if user_row is None:
        raise HTTPException(status_code=404, detail="user not found")

    # v3-M7: dim-conflict pre-check now ONLY flags KBs that fall back to the
    # user-level embedding cfg. KBs that carry their own embedding_provider
    # are unaffected by user-cfg changes (they keep using their own creds).
    result = await session.execute(
        select(KB).where(KB.user_id == user.id, KB.is_system.is_(False))
    )
    owned_kbs = result.scalars().all()
    affected = [
        {"id": kb.id, "name": kb.name, "vector_size": kb.vector_size}
        for kb in owned_kbs
        if kb.vector_size != body.dim
        and not getattr(kb, "embedding_provider", None)
    ]
    if affected:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "embedding_dim_conflict",
                "message": (
                    f"切换到 {body.dim} 维 embedding 会让你已有的 {len(affected)} 个 KB（未单独配置 embedding 的）失效。"
                    "请先为这些 KB 单独配置 embedding，或删除它们。"
                ),
                "new_dim": body.dim,
                "affected_kbs": affected,
            },
        )

    user_row.embedding_provider = body.provider
    user_row.embedding_base_url = body.base_url.rstrip("/")
    if body.api_key:
        user_row.embedding_api_key_enc = encrypt(body.api_key)
    elif not user_row.embedding_api_key_enc and body.provider != "ollama":
        raise HTTPException(
            status_code=400, detail="api_key required for first-time configuration"
        )
    user_row.embedding_model = body.model
    user_row.embedding_dim = body.dim
    await session.commit()
    await session.refresh(user_row)
    return _to_public(user_row)


@router.delete("/embedding", status_code=status.HTTP_204_NO_CONTENT)
async def clear_embedding(
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> None:
    user_row = await session.get(User, user.id)
    if user_row is None:
        return
    # Same conflict check applies — clearing user cfg means falling back to env dim.
    # If env dim differs from owned KBs, downgrade still corrupts. Block.
    from src.infra.embedding import get_vector_size

    try:
        env_dim = get_vector_size()
    except Exception:
        env_dim = None
    if env_dim:
        result = await session.execute(
            select(KB).where(KB.user_id == user.id, KB.is_system.is_(False))
        )
        affected = [
            {"id": kb.id, "name": kb.name, "vector_size": kb.vector_size}
            for kb in result.scalars().all()
            if kb.vector_size != env_dim
            and not getattr(kb, "embedding_provider", None)
        ]
        if affected:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "embedding_dim_conflict",
                    "message": (
                        f"清空配置会回到 env 默认 ({env_dim} 维)，但你已有 {len(affected)} 个 KB（未单独配置 embedding 的）"
                        "维度不同。请先为这些 KB 单独配置 embedding，或删除它们。"
                    ),
                    "new_dim": env_dim,
                    "affected_kbs": affected,
                },
            )
    user_row.embedding_provider = None
    user_row.embedding_base_url = None
    user_row.embedding_api_key_enc = None
    user_row.embedding_model = None
    user_row.embedding_dim = None
    await session.commit()


# ---------------------------------------------------------------------------
# POST /probe/* — discover what models a candidate config exposes
# ---------------------------------------------------------------------------
@router.post("/probe/llm")
async def probe_llm(
    body: ProbeLLMBody,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    # v3-M6: when api_key is empty AND provider/base_url match the stored cfg,
    # fall back to the user's saved decrypted key. Lets the frontend re-probe
    # (e.g. on ModelSelect first render) without re-entering credentials.
    api_key = body.api_key
    if not api_key:
        u = await session.get(User, user.id)
        if (
            u is not None
            and u.llm_provider == body.provider
            and (u.llm_base_url or "").rstrip("/") == body.base_url.rstrip("/")
            and u.llm_api_key_enc
        ):
            api_key = decrypt(u.llm_api_key_enc)
    try:
        models = await probe_llm_models(body.provider, body.base_url, api_key)
    except ProbeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"models": models}


@router.post("/probe/embedding")
async def probe_embedding_route(
    body: ProbeEmbeddingBody,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    # v3-M8: empty api_key + matching provider/base_url → fall back to user's
    # stored decrypted key. Same pattern as probe_llm. Lets the KB creation
    # form re-probe without forcing the user to re-enter creds every time.
    api_key = body.api_key
    if not api_key:
        u = await session.get(User, user.id)
        if (
            u is not None
            and u.embedding_provider == body.provider
            and (u.embedding_base_url or "").rstrip("/") == body.base_url.rstrip("/")
            and u.embedding_api_key_enc
        ):
            api_key = decrypt(u.embedding_api_key_enc)
    try:
        result: EmbeddingProbeResult = await probe_embedding(
            body.provider,
            body.base_url,
            api_key,
            body.model or None,
        )
    except ProbeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"models": result.models, "dim": result.dim}


# ---------------------------------------------------------------------------
# PUT /kb-options — KB-mode toggles (v2-M6)
# ---------------------------------------------------------------------------
@router.put("/kb-options")
async def save_kb_options(
    body: KbOptionsBody,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    user_row = await session.get(User, user.id)
    if user_row is None:
        raise HTTPException(status_code=404, detail="user not found")
    user_row.kb_web_search_enabled = bool(body.kb_web_search_enabled)
    await session.commit()
    await session.refresh(user_row)
    return _to_public(user_row)


# ---------------------------------------------------------------------------
# PUT / DELETE / probe /reranker (v3-M4)
# ---------------------------------------------------------------------------
@router.put("/reranker")
async def save_reranker(
    body: RerankerBody,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Save reranker config block (with optional enable toggle).

    `api_key=""` keeps the existing encrypted key — lets users toggle enable
    or update the model without re-entering the key. First-time configuration
    requires an api_key except for `openai-compat` (self-hosted endpoints may
    not enforce auth).
    """
    user_row = await session.get(User, user.id)
    if user_row is None:
        raise HTTPException(status_code=404, detail="user not found")

    user_row.reranker_provider = body.provider
    user_row.reranker_base_url = body.base_url.rstrip("/")
    if body.api_key:
        user_row.reranker_api_key_enc = encrypt(body.api_key)
    elif not user_row.reranker_api_key_enc and body.provider != "openai-compat":
        raise HTTPException(
            status_code=400, detail="api_key required for first-time configuration"
        )
    user_row.reranker_model = body.model
    user_row.reranker_enabled = bool(body.enabled)
    await session.commit()
    await session.refresh(user_row)
    return _to_public(user_row)


@router.delete("/reranker", status_code=status.HTTP_204_NO_CONTENT)
async def clear_reranker(
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> None:
    user_row = await session.get(User, user.id)
    if user_row is None:
        return
    user_row.reranker_provider = None
    user_row.reranker_base_url = None
    user_row.reranker_api_key_enc = None
    user_row.reranker_model = None
    user_row.reranker_enabled = False
    await session.commit()


@router.post("/probe/reranker")
async def probe_reranker(
    body: ProbeRerankerBody,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Probe the candidate provider's model list.

    Reuses the openai-compat /models lister — most rerank providers (SiliconFlow,
    Cohere via /v1/models, self-hosted) expose this. We return the full list
    unfiltered; the UI tells the user to pick a rerank-capable model id (e.g.
    one containing "rerank" or "bge-reranker").

    v3-M8: empty api_key + matching provider/base_url → fall back to user's
    stored decrypted key (same pattern as probe_llm / probe_embedding).
    """
    from src.settings_user.probe import _probe_openai_compat_models

    base_url = (body.base_url or "").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="base_url 不能为空")
    api_key = body.api_key
    if not api_key:
        u = await session.get(User, user.id)
        if (
            u is not None
            and u.reranker_provider == body.provider
            and (u.reranker_base_url or "").rstrip("/") == base_url
            and u.reranker_api_key_enc
        ):
            api_key = decrypt(u.reranker_api_key_enc)
    # Cohere / SiliconFlow / self-hosted all want a Bearer key for /models;
    # openai-compat without a key is theoretically possible (anonymous TEI)
    # but rare — surface a clearer error if the upstream rejects.
    if not api_key and body.provider != "openai-compat":
        raise HTTPException(status_code=400, detail=f"{body.provider}: api_key 不能为空")
    try:
        models = await _probe_openai_compat_models(base_url, api_key)
    except ProbeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"models": models}
