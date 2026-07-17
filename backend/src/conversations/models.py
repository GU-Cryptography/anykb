"""Conversation + Message SQLAlchemy models (v2-M3).

Mirrors the proven kbs + documents pattern (`src/kb/models.py:49-147`):
- Soft FK to users (no ON DELETE) — we never expose user deletion.
- Strong FK + ON DELETE CASCADE from messages → conversations so deleting
  a conversation transparently drops its messages without manual cleanup.
- `tool_call_log` stored as JSON-encoded text (not a JSON column type) to
  keep SQLite / Postgres parity without dialect-specific work.

Schema decisions:
- Conversation.id is server-generated UUID4 (not client-provided) so bulk
  imports can't collide with future server-generated rows.
- Conversation.title is auto-derived from the first user message at append
  time (`routes.append_message`), letting the frontend stay dumb.
- Conversation.kb_id is a soft string FK so re-binding to a KB that gets
  deleted later doesn't break this row (chat endpoint already validates
  KB existence at request time).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.infra.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Conversation(Base):
    """One chat thread — owned by exactly one user."""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(128), default="新对话", nullable=False)
    kb_id: Mapped[str | None] = mapped_column(String(36), nullable=True, default=None)

    # v3-M6: per-conversation LLM model override. NULL = fall back to the
    # user's default LLM cfg (settings.user.llm_default_model / complex_model).
    # When set, _run_chat_session uses dataclasses.replace() to swap both
    # default_model and complex_model to this value.
    llm_model: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)

    # v3-M2 memory-optimization: short-term memory compression bookkeeping.
    # compressed_count   — rounds already folded into context_summary.
    # compression_watermark — last message id compressed (dedup guard / audit).
    # context_summary    — accumulated early-history summary (L4). PG is the
    #   durable fallback; Redis Hash meta mirrors it as the hot copy. NULL/0 =
    #   nothing compressed yet, so plan_node simply omits the L4 layer.
    compressed_count: Mapped[int] = mapped_column(default=0, nullable=False)
    compression_watermark: Mapped[str | None] = mapped_column(
        String(36), nullable=True, default=None
    )
    context_summary: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
        lazy="selectin",
    )

    def to_summary_dict(self) -> dict:
        """Sidebar / list payload — no messages array."""
        return {
            "id": self.id,
            "title": self.title,
            "kb_id": self.kb_id,
            "llm_model": self.llm_model,
            "message_count": len(self.messages) if self.messages is not None else 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_dict_with_messages(self) -> dict:
        return {
            **self.to_summary_dict(),
            "messages": [m.to_public_dict() for m in self.messages] if self.messages else [],
        }


class Message(Base):
    """One message in a conversation — either user input or assistant output."""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # "user" | "assistant" — kept as plain string for cross-DB simplicity.
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Assistant-only fields. tool_call_log is JSON-encoded ToolEvent[].
    tool_call_log: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    def to_public_dict(self) -> dict:
        import json

        tools: list | None = None
        if self.tool_call_log:
            try:
                tools = json.loads(self.tool_call_log)
            except (ValueError, TypeError):
                tools = None
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content or "",
            # Frontend expects `tools` (matches ChatEvent / ToolEvent[]).
            "tools": tools if tools is not None else ([] if self.role == "assistant" else None),
            "cost_usd": self.cost_usd,
            "error": self.error or None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class UserMemory(Base):
    """v3-M3: a single long-term memory extracted about a user (PRD §5.1).

    Cross-session, user-scoped facts / preferences / skills / task notes that
    survive the short-term (Redis window + L4 summary) memory. Rows are the
    durable source of truth; the parallel ``user_memory_vectors`` collection
    (see ``infra/memory_vector.py``) is a best-effort ANN index over ``content``
    for L2 semantic recall — when it's unavailable (embedding unset, local
    vector backend, or any failure) retrieval falls back to importance-ranked
    rows here, so the feature degrades but never breaks.

    ``memory_type`` is an application-level enum kept as a plain string for
    SQLite/Postgres parity (same convention as ``Document.status`` /
    ``Message.role``): profile | preference | fact | task | skill | explicit.

    ``user_id`` is a soft FK (no ON DELETE) — ``auth.routes.purge_user`` clears
    these rows (plus the vector chain) explicitly, mirroring how KBs and
    conversations are purged.

    Type portability: ``Float`` → REAL (SQLite) / double precision (Postgres),
    ``DateTime(timezone=True)`` → tz-aware on both; no dialect-specific DDL, so
    the table is born via ``init_db``'s ``create_all`` (no ALTER needed — it's a
    brand-new table, registered on ``Base.metadata`` through this module's
    import in ``infra/database.py:init_db``).
    """

    __tablename__ = "user_memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_conversation_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, default=None
    )
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_user_memories_user", "user_id", "memory_type"),
    )

    def to_public_dict(self) -> dict:
        """Payload for the M4 ``GET /api/memories`` listing (defined now so the
        model is the single source of truth for the memory shape)."""
        return {
            "id": self.id,
            "type": self.memory_type,
            "content": self.content or "",
            "importance": self.importance,
            "source_conversation_id": self.source_conversation_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
