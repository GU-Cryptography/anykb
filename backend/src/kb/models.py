"""KB + Document SQLAlchemy models.

A KB owns a Qdrant collection named `kb_{kb.id}` and any number of Document
rows describing the upload sources. Vectors themselves live in Qdrant, not
SQL — these tables are just metadata + ingest bookkeeping.

Schema decisions:
- `KB.user_id` is a soft FK (string UUID) — we don't add ON DELETE CASCADE
  because we never expose a "delete user" path, and accidentally deleting all
  of a user's KBs via SQL would be very expensive (Qdrant collections leak).
  Use the explicit DELETE /api/kbs/{id} route which handles both sides.
- `KB.embedding_model` records the model used at create-time so a future model
  swap doesn't silently corrupt search (we re-create the collection then).
- `Document.status` is a string enum maintained at the application layer; we
  don't use SQL ENUM to keep migrations painless on SQLite.
- `KB.is_system` (M4) marks built-in read-only KBs that all users can read
  but only the seeder can write. The travel demo KB lives here.
- `KBMember` / `KBInvitation` (v2-M9) carry collaboration state:
    * KBMember = established (kb_id, user_id) → role
    * KBInvitation = pending share-link tokens (n→m link)
  Both cascade-delete with the parent KB.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.infra.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


DocStatus = Literal["pending", "ingesting", "done", "failed"]
SourceType = Literal["file", "url"]
Role = Literal["owner", "editor", "viewer"]
MemberRole = Literal["editor", "viewer"]

# ---------------------------------------------------------------------------
# System KBs — well-known UUIDs that map to pre-existing Qdrant collections.
# Adding a new built-in KB = add a constant here + register in system_seed.py.
# ---------------------------------------------------------------------------
SYSTEM_TRAVEL_KB_ID = "00000000-0000-4000-8000-000000000001"
SYSTEM_TRAVEL_COLLECTION = "restaurants"  # legacy curated 4-city dataset

# user_id for any system KB. Sentinel value that no real user can have
# (real users get random uuid4 ids).
SYSTEM_USER_ID = "00000000-0000-0000-0000-000000000000"


class KB(Base):
    """Knowledge Base — top-level container."""

    __tablename__ = "kbs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(512), default="")

    # Embedding lineage: which model produced the vectors. Vector dim is implied.
    embedding_model: Mapped[str] = mapped_column(String(128), default="")
    vector_size: Mapped[int] = mapped_column(Integer, default=0)

    # Denormalized count maintained by ingest pipeline (Σ over docs.chunks_count).
    chunks_count: Mapped[int] = mapped_column(Integer, default=0)

    # Built-in / read-only flag. M4 seeds the travel demo KB with this true.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # v3-M3: owner-controlled toggle for Milvus grouping_search.
    # When True, KBSearchTool passes group_by_field="doc_id" so each document
    # contributes at most one chunk to top-k results. Helps when one long
    # document otherwise dominates retrieval. Only affects user KBs (system
    # travel KB has its own MMR diversity path).
    grouping_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # v3-M7: per-KB embedding override (independent from user-level cfg).
    # When set, this KB's documents were ingested with these credentials and
    # KBSearchTool re-embeds queries with the same model. NULL on all four =
    # fall back to the user-level resolve_user_embedding().
    embedding_provider:       Mapped[Optional[str]] = mapped_column(String(32),  nullable=True, default=None)
    embedding_base_url:       Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)
    embedding_api_key_enc:    Mapped[Optional[str]] = mapped_column(String(1024), nullable=True, default=None)
    embedding_model_override: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, default=None)

    # v3-M7: per-KB reranker override (opt-in, default off). reranker_enabled
    # MUST be True AND the four config cols populated for KBSearchTool to
    # consult the configured /rerank endpoint. NULL on all = fall back to
    # user-level resolve_user_reranker() (which may also be off → no rerank).
    # System KBs strictly skip reranker (handled in KBSearchTool).
    reranker_provider:    Mapped[Optional[str]] = mapped_column(String(32),  nullable=True, default=None)
    reranker_base_url:    Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)
    reranker_api_key_enc: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True, default=None)
    reranker_model:       Mapped[Optional[str]] = mapped_column(String(128), nullable=True, default=None)
    reranker_enabled:     Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    documents: Mapped[list["Document"]] = relationship(
        back_populates="kb",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    members: Mapped[list["KBMember"]] = relationship(
        back_populates="kb",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    invitations: Mapped[list["KBInvitation"]] = relationship(
        back_populates="kb",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def collection_name(self) -> str:
        """Qdrant collection name for this KB.

        System KBs may point at a pre-existing collection that wasn't created
        via `kb_{uuid}` convention (e.g., the travel demo KB reuses the legacy
        `restaurants` collection so we don't have to re-ingest 20 curated rows).
        """
        if self.id == SYSTEM_TRAVEL_KB_ID:
            return SYSTEM_TRAVEL_COLLECTION
        return f"kb_{self.id.replace('-', '')}"

    async def role_for(
        self, session: AsyncSession, user_id: str
    ) -> Optional[Role]:
        """Return the highest role this user has for this KB, or None.

        Precedence: owner > editor > viewer > None.
        System KBs return 'viewer' for everyone (read-only, no member rows).

        v2-M9: replaces the old `user_id == owner OR is_system` two-state
        check. Used by `kb/routes.py` helpers and `app.py:chat_post`.
        """
        if self.user_id == user_id:
            return "owner"
        if self.is_system:
            return "viewer"
        m = (
            await session.execute(
                select(KBMember).where(
                    KBMember.kb_id == self.id, KBMember.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        return m.role if m else None  # type: ignore[return-value]

    def to_public_dict(self, my_role: Optional[Role] = None) -> dict:
        out = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "embedding_model": self.embedding_model,
            "vector_size": self.vector_size,
            "chunks_count": self.chunks_count,
            "documents_count": len(self.documents) if self.documents is not None else 0,
            "is_system": bool(self.is_system),
            "grouping_enabled": bool(self.grouping_enabled),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if my_role is not None:
            out["my_role"] = my_role
        return out


class Document(Base):
    """One uploaded file / URL within a KB."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kbs.id", ondelete="CASCADE"), index=True, nullable=False
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime: Mapped[str] = mapped_column(String(128), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    # "file" or "url" — drives parser selection in ingest.py
    source_type: Mapped[str] = mapped_column(String(16), default="file")
    source_url: Mapped[str] = mapped_column(String(2048), default="")

    # Lifecycle: pending → ingesting → done | failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    chunks_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    kb: Mapped[KB] = relationship(back_populates="documents")

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "kb_id": self.kb_id,
            "filename": self.filename,
            "mime": self.mime,
            "size_bytes": self.size_bytes,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "status": self.status,
            "chunks_count": self.chunks_count,
            "error": self.error or None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class KBMember(Base):
    """Established (kb_id, user_id) → role mapping. v2-M9.

    Composite PK (kb_id, user_id) gives natural uniqueness — a user can only
    have one role per KB. Owner is NOT in this table (owner = kbs.user_id);
    members are strictly editor / viewer.

    user_id is soft FK (same convention as kbs.user_id) — we don't cascade on
    user deletion since we don't expose a delete-user path. kb_id cascades so
    member rows go away when the KB is deleted.
    """

    __tablename__ = "kb_members"

    kb_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("kbs.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # "editor" | "viewer"
    invited_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    kb: Mapped[KB] = relationship(back_populates="members")

    def to_public_dict(self) -> dict:
        return {
            "kb_id": self.kb_id,
            "user_id": self.user_id,
            "role": self.role,
            "invited_by": self.invited_by or None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class KBInvitation(Base):
    """Pending share-link token. v2-M9.

    `id` is also the URL token (UUID4). The link can be:
      - bounded in time (expires_at)
      - bounded in uses (max_uses + uses_count)
      - manually revoked (revoked=True)

    On accept, a KBMember row is created and uses_count incremented. If the
    user is already a member, accept is idempotent (no bump).
    """

    __tablename__ = "kb_invitations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("kbs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # "editor" | "viewer"
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    max_uses: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    uses_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    kb: Mapped[KB] = relationship(back_populates="invitations")

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "kb_id": self.kb_id,
            "role": self.role,
            "created_by": self.created_by,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "max_uses": self.max_uses,
            "uses_count": self.uses_count,
            "revoked": bool(self.revoked),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
