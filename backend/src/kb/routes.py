"""KB + Document HTTP routes.

Authorization model:
  - All routes require a Bearer JWT (via CurrentUser).
  - v2-M9: per-KB role model. Each call resolves the caller's effective role
    via KB.role_for(session, user_id) which returns one of:
      - "owner": kbs.user_id == caller (full control)
      - "editor": (kb_id, caller) ∈ kb_members WHERE role="editor"
                  (read + upload/delete docs; CAN'T delete KB or manage members)
      - "viewer": system KB (anyone), or (kb_id, caller) ∈ kb_members WHERE role="viewer"
                  (read only)
      - None: no access → 404 (don't leak existence)

Lifecycle of an upload:
  POST /api/kbs/{id}/documents
    → 201 + Document(status="pending")
    → BackgroundTask spawned to do parse/chunk/embed/upsert
    → Client polls GET /api/kbs/{id} (or /documents) for status transitions
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

import httpx
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel, EmailStr, Field, HttpUrl
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.middleware import CurrentUser
from src.auth.models import User
from src.infra.database import get_session
from src.infra.embedding import _resolve_config, get_vector_size, probe_vector_size
from src.infra.vector_store import get_store
from src.kb.ingest import (
    delete_document_chunks,
    delete_kb_uploads,
    delete_uploaded_file,
    ingest_document,
    save_uploaded_file,
)
from src.kb.models import KB, Document, KBInvitation, KBMember
from src.kb.parsers import SUPPORTED_EXTS
from src.settings_user import require_user_embedding, resolve_user_embedding
from src.settings_user.kb_resolvers import resolve_kb_embedding

router = APIRouter(prefix="/api/kbs", tags=["kbs"])

# v2-M9: invitations live under their own top-level prefix because the
# accept endpoint identifies the KB via token, not via kb_id in the path.
invitations_router = APIRouter(prefix="/api/invitations", tags=["invitations"])


# Max single-upload size: 50 MB. Bigger files should be split or moved to a
# dedicated worker (out of scope for v1).
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class CreateKBRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)

    # v3-M7: optional per-KB embedding override. When provided, the KB persists
    # these creds and ingest / search use them; NULL on all = fall back to
    # user-level cfg.
    embedding_provider: Optional[Literal["openai-compat", "ollama"]] = None
    embedding_base_url: Optional[str] = Field(default=None, max_length=255)
    embedding_api_key: str = Field(default="", max_length=512)
    embedding_model: Optional[str] = Field(default=None, max_length=128)
    # Optional dim hint when caller already probed it; server still validates.
    embedding_dim: Optional[int] = Field(default=None, ge=1, le=8192)

    # v3-M7: optional per-KB reranker override. opt-in (default off). When
    # reranker_enabled=True AND the four cfg fields populated, KBSearchTool
    # reranks search hits for this KB.
    reranker_provider: Optional[Literal["siliconflow", "cohere", "openai-compat"]] = None
    reranker_base_url: Optional[str] = Field(default=None, max_length=255)
    reranker_api_key: str = Field(default="", max_length=512)
    reranker_model: Optional[str] = Field(default=None, max_length=128)
    reranker_enabled: bool = False


class PatchKBRequest(BaseModel):
    """v3-M3: KB owner-only PATCH. Only allows toggling `grouping_enabled`
    for now — name/description editing is intentionally not in this round."""

    grouping_enabled: Optional[bool] = None


class CreateURLDocRequest(BaseModel):
    url: HttpUrl
    filename: str = Field(default="", max_length=255)  # optional display label


class InviteMemberRequest(BaseModel):
    email: EmailStr
    role: str = Field(pattern="^(editor|viewer)$")


class PatchMemberRequest(BaseModel):
    role: str = Field(pattern="^(editor|viewer)$")


class CreateInvitationRequest(BaseModel):
    role: str = Field(pattern="^(editor|viewer)$")
    expires_at: Optional[datetime] = None
    max_uses: Optional[int] = Field(default=None, ge=1, le=1000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _resolve_role(
    session: AsyncSession, kb_id: str, user_id: str
) -> tuple[KB, str]:
    """Load KB + compute caller's effective role. Returns (kb, role).

    Raises 404 if KB doesn't exist OR caller has no access (don't leak existence).
    """
    kb = await session.get(KB, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="kb not found")
    role = await kb.role_for(session, user_id)
    if role is None:
        raise HTTPException(status_code=404, detail="kb not found")
    return kb, role


async def _load_readable_kb(session: AsyncSession, kb_id: str, user_id: str) -> KB:
    """For read paths: any role (owner / editor / viewer)."""
    kb, _ = await _resolve_role(session, kb_id, user_id)
    return kb


async def _load_writable_kb(session: AsyncSession, kb_id: str, user_id: str) -> KB:
    """For doc write paths (upload, delete doc): owner or editor; system KB 403."""
    kb, role = await _resolve_role(session, kb_id, user_id)
    if kb.is_system:
        raise HTTPException(status_code=403, detail="system kb is read-only")
    if role not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="editor or owner role required")
    return kb


async def _load_owner_kb(session: AsyncSession, kb_id: str, user_id: str) -> KB:
    """For owner-only paths (delete KB, manage members, manage invitations)."""
    kb, role = await _resolve_role(session, kb_id, user_id)
    if role != "owner":
        raise HTTPException(status_code=403, detail="owner role required")
    return kb


# Backwards-compat alias for any callers expecting the pre-v2-M9 name.
_load_owned_kb = _load_writable_kb


async def _resolve_vector_size(user_embedding_cfg=None) -> int:
    """Vector size for new KB collections.

    User cfg.dim (if configured) wins. Otherwise: env override → known model
    table → live probe. Probing costs one embed call, but only on first use of
    an unknown model.
    """
    if user_embedding_cfg is not None and user_embedding_cfg.dim:
        return user_embedding_cfg.dim
    try:
        return get_vector_size()
    except RuntimeError:
        return await probe_vector_size()


# v3-M7: KB-level cfg helper — used by create_kb to derive a cfg dataclass
# from the request body (after Fernet-encrypting api_key for DB storage).
# v3-M8: when body.api_key is empty AND provider/base_url match the user's
# stored cfg, transparently reuse the user-level decrypted key. This is the
# "暗中记忆" mechanism that lets the KB creation form prefill prior creds
# without re-prompting for the api_key on every new KB.
async def _kb_embedding_cfg_from_body(
    req: "CreateKBRequest",
    session: AsyncSession,
    user_id: str,
):
    """Return (UserEmbeddingConfig | None) for vector-size probing if body
    carries a full KB-level embedding override; else None."""
    from src.settings_user.models import UserEmbeddingConfig
    from src.infra.crypto import decrypt
    if not (req.embedding_provider and req.embedding_base_url and req.embedding_model):
        return None
    api_key = req.embedding_api_key or ""
    if not api_key:
        u = await session.get(User, user_id)
        if (
            u is not None
            and u.embedding_api_key_enc
            and u.embedding_provider == req.embedding_provider
            and (u.embedding_base_url or "").rstrip("/") == req.embedding_base_url.rstrip("/")
        ):
            api_key = decrypt(u.embedding_api_key_enc)
            # Mutate body so the persisted KB row also gets the resolved key
            req.embedding_api_key = api_key
    return UserEmbeddingConfig(
        provider=req.embedding_provider,
        base_url=req.embedding_base_url.rstrip("/"),
        api_key=api_key,
        model=req.embedding_model,
        dim=int(req.embedding_dim or 0),
    )


# ---------------------------------------------------------------------------
# KB CRUD
# ---------------------------------------------------------------------------
@router.post("", status_code=status.HTTP_201_CREATED)
async def create_kb(
    req: CreateKBRequest,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    # v3-M7: KB-level embedding cfg takes precedence over the user-level gate.
    # If the body carries a full embedding override, we accept it even if the
    # user hasn't configured `/settings` yet — this is the whole point of
    # per-KB cfg (a user can have multiple KBs each with different providers).
    kb_ecfg = await _kb_embedding_cfg_from_body(req, session, user.id)
    if kb_ecfg is None:
        require_user_embedding(user)
        ecfg = resolve_user_embedding(user)
    else:
        ecfg = kb_ecfg

    if ecfg is not None:
        embedding_model = ecfg.model
    else:
        embedding_model = _resolve_config()["model"]

    # v3-M8.2: actually probe the embedding before persisting the KB row. The
    # frontend can already test the connection on its end (via /api/settings/
    # probe/embedding), but a defense-in-depth probe here closes the race
    # where the user clicks "save" before retesting after editing the key /
    # url. Without this, a wrong api_key results in a perfectly-created KB
    # row that 403s on first upload — confusing and hard to recover from.
    if ecfg is not None:
        try:
            actual_dim = await probe_vector_size(ecfg)
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"embedding 连接测试失败（{exc.response.status_code}）："
                    f"请检查 base_url 与 api_key 是否正确。"
                ),
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=f"embedding 连接测试失败：{exc}",
            ) from exc
        # Probe authoritative — overrides any client-supplied dim hint.
        vector_size = actual_dim
    else:
        # No user/KB cfg → use env-resolved size (legacy path, e.g. tests).
        vector_size = await _resolve_vector_size(ecfg)

    # v3-M8: same fall-back logic for reranker — empty api_key + matching
    # provider/base_url reuses the user-level decrypted key.
    if req.reranker_provider and not req.reranker_api_key:
        u = await session.get(User, user.id)
        if (
            u is not None
            and u.reranker_api_key_enc
            and u.reranker_provider == req.reranker_provider
            and (u.reranker_base_url or "").rstrip("/") == (req.reranker_base_url or "").rstrip("/")
        ):
            from src.infra.crypto import decrypt as _dec
            req.reranker_api_key = _dec(u.reranker_api_key_enc)

    # v3-M7: encrypt KB-level api_keys before persistence.
    from src.infra.crypto import encrypt
    enc_embedding_key = (
        encrypt(req.embedding_api_key)
        if (kb_ecfg is not None and req.embedding_api_key)
        else None
    )
    enc_reranker_key = (
        encrypt(req.reranker_api_key)
        if (req.reranker_provider and req.reranker_api_key)
        else None
    )

    kb = KB(
        id=str(uuid.uuid4()),
        user_id=user.id,
        name=req.name.strip(),
        description=req.description.strip(),
        embedding_model=embedding_model,
        vector_size=vector_size,
        # v3-M7: persist KB-level embedding override (only when caller opted in)
        embedding_provider=req.embedding_provider if kb_ecfg is not None else None,
        embedding_base_url=(req.embedding_base_url.rstrip("/") if kb_ecfg is not None and req.embedding_base_url else None),
        embedding_api_key_enc=enc_embedding_key,
        embedding_model_override=req.embedding_model if kb_ecfg is not None else None,
        # v3-M7: persist KB-level reranker override (opt-in)
        reranker_provider=req.reranker_provider,
        reranker_base_url=(req.reranker_base_url.rstrip("/") if req.reranker_base_url else None),
        reranker_api_key_enc=enc_reranker_key,
        reranker_model=req.reranker_model,
        reranker_enabled=bool(req.reranker_enabled and req.reranker_provider),
    )
    session.add(kb)
    await session.commit()
    await session.refresh(kb)

    # Create Qdrant collection. If this fails we roll back the KB so the user
    # doesn't get a half-broken record.
    store = get_store()
    if not hasattr(store, "create_collection"):
        await session.delete(kb)
        await session.commit()
        raise HTTPException(
            status_code=500,
            detail="KB requires VECTOR_STORE=qdrant or milvus; current backend doesn't support multi-collection",
        )
    try:
        await store.create_collection(kb.collection_name, vector_size)
    except Exception as exc:  # noqa: BLE001
        await session.delete(kb)
        await session.commit()
        raise HTTPException(status_code=502, detail=f"qdrant create failed: {exc}") from exc

    return kb.to_public_dict(my_role="owner")


@router.get("")
async def list_kbs(
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """List the user's own KBs + all system (read-only) KBs + member-of KBs.

    System KBs are pinned to the top; user KBs follow by created_at desc.
    Each KB carries `my_role` so the UI knows what controls to show.
    """
    own_or_sys = select(KB).where(or_(KB.user_id == user.id, KB.is_system.is_(True)))
    member_of = (
        select(KB)
        .join(KBMember, KBMember.kb_id == KB.id)
        .where(KBMember.user_id == user.id)
    )
    stmt = own_or_sys.union(member_of).order_by(
        KB.is_system.desc(), KB.created_at.desc()
    )
    # `union` returns rows shaped like KB columns; rehydrate via id lookup so
    # SQLAlchemy gives us full ORM instances (with relationship lazy-load).
    rows = (await session.execute(stmt)).all()
    ids = [r[0] for r in rows]
    kbs = (await session.execute(select(KB).where(KB.id.in_(ids)))).scalars().all()
    # Preserve union ordering by id.
    by_id = {k.id: k for k in kbs}
    ordered = [by_id[i] for i in ids if i in by_id]

    out: list[dict] = []
    for kb in ordered:
        role = await kb.role_for(session, user.id)
        out.append(kb.to_public_dict(my_role=role))
    return out


@router.get("/{kb_id}")
async def get_kb(
    kb_id: str,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    kb, role = await _resolve_role(session, kb_id, user.id)
    docs_sorted = sorted(kb.documents, key=lambda d: d.created_at)
    return {
        **kb.to_public_dict(my_role=role),
        "documents": [d.to_public_dict() for d in docs_sorted],
    }


async def purge_kb(session: AsyncSession, kb: KB) -> None:
    """Drop a KB's vector collection, delete its row (cascades documents /
    members / invitations) and remove uploaded files from disk.

    Shared by the owner delete route (DELETE /api/kbs/{id}) and the admin
    delete endpoint (DELETE /api/admin/kbs/{id}); callers authorize first.
    """
    collection_name = kb.collection_name
    kb_id = kb.id

    # Drop the vector collection first — it's idempotent so leaks here are
    # recoverable, but a dangling DB row pointing at a missing collection would
    # 500 on search.
    store = get_store()
    if hasattr(store, "delete_collection"):
        await store.delete_collection(collection_name)

    await session.delete(kb)  # cascade deletes Document / member / invitation rows
    await session.commit()

    # Clean up uploaded files on disk.
    delete_kb_uploads(kb_id)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb(
    kb_id: str,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> None:
    kb = await _load_owner_kb(session, kb_id, user.id)
    await purge_kb(session, kb)


# ---------------------------------------------------------------------------
# v3-M3: KB settings PATCH + rebuild
# ---------------------------------------------------------------------------
@router.patch("/{kb_id}")
async def patch_kb(
    kb_id: str,
    body: PatchKBRequest,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Owner-only. Currently scoped to `grouping_enabled` toggle (v3-M3).
    System KBs are rejected — owner sentinel can't be hit via auth anyway,
    but we belt-and-braces here for clarity."""
    kb = await _load_owner_kb(session, kb_id, user.id)
    if kb.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System KBs cannot be modified",
        )
    if body.grouping_enabled is not None:
        kb.grouping_enabled = body.grouping_enabled
    await session.commit()
    await session.refresh(kb)
    return kb.to_public_dict(my_role="owner")


@router.post("/{kb_id}/rebuild")
async def rebuild_kb(
    kb_id: str,
    user: CurrentUser,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Owner-only. Drops the vector collection and re-ingests every Document.

    Purpose (v3-M3): upgrade a pre-v3-M3 dense-only Milvus collection to the
    hybrid schema (dense + BM25). Since Milvus doesn't support adding a
    sparse field to an existing collection, the only path is drop+recreate
    +re-embed. Document SQLite rows survive — original files on disk
    (data/uploads/{kb_id}/{doc_id}.{ext}) drive re-ingest.

    URL-sourced documents re-fetch from source_url. File-sourced documents
    need the original upload still on disk; if missing, that doc is marked
    failed but other docs proceed.

    During the rebuild window (~30-90s for typical KB) chat against this KB
    will see empty hits — acceptable trade-off for a one-time owner action.
    """
    kb = await _load_owner_kb(session, kb_id, user.id)
    if kb.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System KBs cannot be rebuilt",
        )

    store = get_store()
    if not hasattr(store, "delete_collection") or not hasattr(
        store, "create_collection"
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "KB rebuild requires a multi-collection backend "
                "(qdrant or milvus)"
            ),
        )

    collection_name = kb.collection_name
    vector_size = kb.vector_size or await _resolve_vector_size(
        resolve_user_embedding(user)
    )

    # Reset all documents back to pending; zero the KB's chunks_count so the
    # ingest pipeline's delta math (kb.chunks_count -= prev + new) produces
    # the right total. error/chunks_count cleared per doc.
    docs = sorted(kb.documents, key=lambda d: d.created_at)
    for d in docs:
        d.status = "pending"
        d.chunks_count = 0
        d.error = ""
    kb.chunks_count = 0
    await session.commit()
    doc_ids = [d.id for d in docs]

    # Drop + recreate the collection with the current schema (v3-M3 hybrid
    # for Milvus, dense for Qdrant). create_collection is idempotent so
    # crash-recovery is safe.
    await store.delete_collection(collection_name)
    await store.create_collection(collection_name, vector_size)

    # Spawn one ingest task per document; they run sequentially in FastAPI's
    # BackgroundTasks queue. The user can watch GET /documents to track.
    ecfg = resolve_user_embedding(user)
    for did in doc_ids:
        background.add_task(ingest_document, did, ecfg)

    return {
        "rebuilding": True,
        "doc_count": len(doc_ids),
        "collection": collection_name,
    }


# ---------------------------------------------------------------------------
# Document upload
# ---------------------------------------------------------------------------
@router.post("/{kb_id}/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(
    kb_id: str,
    user: CurrentUser,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    file: Optional[UploadFile] = File(default=None),
    url: Annotated[str, Form()] = "",
) -> dict:
    """Upload a file (multipart) OR a URL (form field `url=...`).

    Exactly one of `file` / `url` must be provided.
    """
    kb = await _load_writable_kb(session, kb_id, user.id)
    # v3-M8.2: BYOK gate is conditional — KB with its own embedding cfg is
    # self-sufficient (the whole point of v3-M7 per-KB cfg). Only enforce the
    # user-level gate when the KB has no own embedding cfg.
    if not (kb.embedding_provider and kb.embedding_base_url and kb.embedding_model_override):
        require_user_embedding(user)

    if (file is None) == (not url):
        raise HTTPException(
            status_code=400,
            detail="provide exactly one of `file` (multipart) or `url` (form field)",
        )

    doc_id = str(uuid.uuid4())

    if file is not None:
        ext = (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ""
        if ext not in SUPPORTED_EXTS:
            raise HTTPException(
                status_code=415,
                detail=f"unsupported file type '.{ext}'. supported: {sorted(SUPPORTED_EXTS)}",
            )
        content = await file.read()
        if len(content) == 0:
            raise HTTPException(status_code=400, detail="empty file")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"file too large ({len(content)} > {MAX_UPLOAD_BYTES})",
            )
        save_uploaded_file(kb_id, doc_id, file.filename or f"upload.{ext}", content)
        doc = Document(
            id=doc_id,
            kb_id=kb_id,
            filename=file.filename or f"upload.{ext}",
            mime=file.content_type or "",
            size_bytes=len(content),
            source_type="file",
            source_url="",
            status="pending",
        )
    else:
        # URL upload: display label = filename if given, else URL itself
        url_str = url.strip()
        doc = Document(
            id=doc_id,
            kb_id=kb_id,
            filename=url_str[:255],  # display as-is; ingest will fetch + parse
            mime="text/html",
            size_bytes=0,  # filled in by ingest if needed
            source_type="url",
            source_url=url_str,
            status="pending",
        )

    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    # v3-M8.2: derive the ingest cfg from the KB row first (KB-level wins),
    # fall back to user-level only when KB has no own cfg. Without this the
    # ingest would always use the user-level cfg even when the KB was created
    # with its own embedding creds — leading to 403s when user-level api_key
    # is empty / wrong while the KB-level one is correct.
    ecfg = resolve_kb_embedding(kb, user)
    if ecfg is None:
        # Neither KB-level nor user-level cfg available — surface a clear
        # error now instead of letting ingest die silently in the background.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "embedding_not_configured",
                "message": "知识库未配置 embedding，且当前用户也未配置默认 embedding；请在创建知识库时填写 embedding 凭据。",
                "settings_url": "/settings",
            },
        )
    background.add_task(ingest_document, doc_id, ecfg)

    return doc.to_public_dict()


@router.get("/{kb_id}/documents")
async def list_documents(
    kb_id: str,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    kb = await _load_readable_kb(session, kb_id, user.id)
    docs_sorted = sorted(kb.documents, key=lambda d: d.created_at)
    return [d.to_public_dict() for d in docs_sorted]


@router.delete(
    "/{kb_id}/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_document(
    kb_id: str,
    doc_id: str,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> None:
    kb = await _load_writable_kb(session, kb_id, user.id)
    doc = await session.get(Document, doc_id)
    if doc is None or doc.kb_id != kb_id:
        raise HTTPException(status_code=404, detail="document not found")

    chunks_to_subtract = doc.chunks_count or 0
    filename_snap = doc.filename
    doc_id_snap = doc.id

    # Drop chunks from Qdrant (idempotent), then DB row, then on-disk file.
    await delete_document_chunks(kb.collection_name, doc_id_snap)
    await session.delete(doc)
    if chunks_to_subtract:
        kb.chunks_count = max(0, (kb.chunks_count or 0) - chunks_to_subtract)
    await session.commit()
    delete_uploaded_file(kb_id, doc_id_snap, filename_snap)


# ---------------------------------------------------------------------------
# v2-M9: Members management
# ---------------------------------------------------------------------------
async def _email_map(session: AsyncSession, user_ids: list[str]) -> dict[str, dict]:
    """Bulk lookup user_id → {email, display_name}. Missing ids absent from result."""
    ids = [u for u in user_ids if u]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(User.id, User.email, User.display_name).where(User.id.in_(ids))
        )
    ).all()
    return {
        r.id: {"email": r.email, "display_name": r.display_name or None} for r in rows
    }


@router.get("/{kb_id}/members")
async def list_members(
    kb_id: str,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List owner + members of a KB. Any role can see (transparency).

    Returns:
      {
        "owner": {"user_id", "email", "display_name"} | None (system KB has no real owner),
        "members": [{"user_id", "email", "display_name", "role", "invited_by_email", "created_at"}]
      }
    """
    kb = await _load_readable_kb(session, kb_id, user.id)
    members = list(kb.members) if kb.members is not None else []

    # Bulk lookup user info for owner + members + invited_by
    ids = {kb.user_id}
    for m in members:
        ids.add(m.user_id)
        if m.invited_by:
            ids.add(m.invited_by)
    info = await _email_map(session, list(ids))

    owner_info = info.get(kb.user_id)
    out_owner = (
        {"user_id": kb.user_id, **owner_info}
        if owner_info and not kb.is_system
        else None
    )

    members_out = []
    for m in sorted(members, key=lambda x: x.created_at):
        u = info.get(m.user_id, {"email": "(unknown)", "display_name": None})
        inviter = info.get(m.invited_by, {"email": None}) if m.invited_by else {}
        members_out.append(
            {
                "user_id": m.user_id,
                "email": u["email"],
                "display_name": u.get("display_name"),
                "role": m.role,
                "invited_by_email": inviter.get("email"),
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
        )
    return {"owner": out_owner, "members": members_out}


@router.post("/{kb_id}/members", status_code=status.HTTP_201_CREATED)
async def invite_member(
    kb_id: str,
    req: InviteMemberRequest,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Invite an existing user by email + role. Owner only."""
    kb = await _load_owner_kb(session, kb_id, user.id)

    # Find target user by email (case-insensitive on most SQLite collations).
    target = (
        await session.execute(select(User).where(User.email == req.email.lower()))
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="user with that email not found")
    if target.id == kb.user_id:
        raise HTTPException(status_code=400, detail="owner cannot be invited as member")

    # Upsert: if member row exists, update role; else insert.
    existing = (
        await session.execute(
            select(KBMember).where(
                KBMember.kb_id == kb.id, KBMember.user_id == target.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.role = req.role
    else:
        session.add(
            KBMember(
                kb_id=kb.id,
                user_id=target.id,
                role=req.role,
                invited_by=user.id,
            )
        )
    await session.commit()
    return {
        "user_id": target.id,
        "email": target.email,
        "display_name": target.display_name or None,
        "role": req.role,
    }


@router.patch("/{kb_id}/members/{user_id}")
async def patch_member(
    kb_id: str,
    user_id: str,
    req: PatchMemberRequest,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Change a member's role. Owner only."""
    kb = await _load_owner_kb(session, kb_id, user.id)
    m = (
        await session.execute(
            select(KBMember).where(
                KBMember.kb_id == kb.id, KBMember.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail="member not found")
    m.role = req.role
    await session.commit()
    return m.to_public_dict()


@router.delete(
    "/{kb_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_member(
    kb_id: str,
    user_id: str,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Remove a member. Either the owner (any user_id) or the member themselves
    (user_id == caller) can call this. Editor cannot remove peers."""
    kb, role = await _resolve_role(session, kb_id, user.id)
    is_owner = role == "owner"
    is_self = user_id == user.id
    if not (is_owner or is_self):
        raise HTTPException(status_code=403, detail="owner or self only")
    m = (
        await session.execute(
            select(KBMember).where(
                KBMember.kb_id == kb.id, KBMember.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail="member not found")
    await session.delete(m)
    await session.commit()


# ---------------------------------------------------------------------------
# v2-M9: Share-link invitations management
# ---------------------------------------------------------------------------
@router.get("/{kb_id}/invitations")
async def list_invitations(
    kb_id: str,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """List all share-link invitations for a KB. Owner only."""
    kb = await _load_owner_kb(session, kb_id, user.id)
    invs = list(kb.invitations) if kb.invitations is not None else []
    invs.sort(key=lambda i: i.created_at, reverse=True)
    return [i.to_public_dict() for i in invs]


@router.post("/{kb_id}/invitations", status_code=status.HTTP_201_CREATED)
async def create_invitation(
    kb_id: str,
    req: CreateInvitationRequest,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a share-link invitation token. Owner only."""
    kb = await _load_owner_kb(session, kb_id, user.id)
    inv = KBInvitation(
        id=str(uuid.uuid4()),
        kb_id=kb.id,
        role=req.role,
        created_by=user.id,
        expires_at=req.expires_at,
        max_uses=req.max_uses,
    )
    session.add(inv)
    await session.commit()
    await session.refresh(inv)
    return inv.to_public_dict()


@router.delete(
    "/{kb_id}/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_invitation(
    kb_id: str,
    invitation_id: str,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Revoke (soft-delete) a share-link invitation. Owner only.

    Sets `revoked=True` rather than DELETE so accept attempts get 404 (not
    confused with 'never existed'). Old rows can be GC'd separately if needed.
    """
    kb = await _load_owner_kb(session, kb_id, user.id)
    inv = await session.get(KBInvitation, invitation_id)
    if inv is None or inv.kb_id != kb.id:
        raise HTTPException(status_code=404, detail="invitation not found")
    inv.revoked = True
    await session.commit()


# ---------------------------------------------------------------------------
# v2-M9: Accept invitation (independent router — no kb_id in path)
# ---------------------------------------------------------------------------
@invitations_router.get("/{token}")
async def peek_invitation(
    token: str,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Preview an invitation without accepting (for the /invite/[token] page
    to show KB name + role before the user clicks confirm)."""
    inv = await session.get(KBInvitation, token)
    if inv is None or inv.revoked:
        raise HTTPException(status_code=404, detail="invitation invalid or revoked")
    if inv.expires_at and inv.expires_at < _utcnow():
        raise HTTPException(status_code=410, detail="invitation expired")
    if inv.max_uses is not None and inv.uses_count >= inv.max_uses:
        raise HTTPException(status_code=410, detail="invitation exhausted")
    kb = await session.get(KB, inv.kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="kb no longer exists")
    return {
        "kb_id": kb.id,
        "kb_name": kb.name,
        "role": inv.role,
        "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
        "max_uses": inv.max_uses,
        "uses_count": inv.uses_count,
    }


@invitations_router.post("/{token}/accept")
async def accept_invitation(
    token: str,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Accept a share-link invitation. Any authenticated user can call.

    Returns `{kb_id, role}`. Idempotent: if already a member, returns the
    existing role without incrementing uses_count. Owner calling on their
    own KB is a no-op returning role='owner'.
    """
    inv = await session.get(KBInvitation, token)
    if inv is None or inv.revoked:
        raise HTTPException(status_code=404, detail="invitation invalid or revoked")
    if inv.expires_at and inv.expires_at < _utcnow():
        raise HTTPException(status_code=410, detail="invitation expired")
    if inv.max_uses is not None and inv.uses_count >= inv.max_uses:
        raise HTTPException(status_code=410, detail="invitation exhausted")

    kb = await session.get(KB, inv.kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="kb no longer exists")

    # Owner accepting own KB's invitation is a no-op.
    if kb.user_id == user.id:
        return {"kb_id": kb.id, "role": "owner"}

    # Idempotent on existing membership.
    existing = (
        await session.execute(
            select(KBMember).where(
                KBMember.kb_id == kb.id, KBMember.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"kb_id": kb.id, "role": existing.role}

    session.add(
        KBMember(
            kb_id=kb.id,
            user_id=user.id,
            role=inv.role,
            invited_by=inv.created_by,
        )
    )
    inv.uses_count += 1
    await session.commit()
    return {"kb_id": kb.id, "role": inv.role}
