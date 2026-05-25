"""KB vector search tool — generic per-KB RAG.

Construction:
    KBSearchTool(kb=<KB row>, embedding_cfg=<UserEmbeddingConfig | None>,
                 reranker_cfg=<UserRerankerConfig | None>)
    binds the tool to one KB collection so its description can include the KB
    name/description (helps the LLM decide when to invoke it) and `execute(query)`
    doesn't need a kb_id arg. embedding_cfg routes query embedding through the
    user's configured provider (None = env fallback). reranker_cfg (v3-M4) opts
    the user into a second-stage cross-encoder rerank pass — when set, search
    over-fetches 4x candidates and the reranker picks the final top-K. System
    KBs (the curated travel demo) always bypass the reranker regardless of
    user setting, to keep demo behavior stable.

Returned text format is one chunk per block, separated by `---`, with filename
and similarity score inline so the agent can cite sources.
"""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from src.infra.embedding import embed
from src.infra.reranker import rerank
from src.infra.vector_store import QdrantStore, get_store
from src.kb.models import KB
from src.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from src.settings_user import UserEmbeddingConfig, UserRerankerConfig


log = logging.getLogger(__name__)

# v3-M4: when reranker is enabled we over-fetch candidates to give the
# cross-encoder more material to choose from. 4x is the industry default;
# capped at 30 so a misbehaving caller can't blow up the upstream quota.
_RERANK_OVERFETCH_MULTIPLIER = 4
_RERANK_OVERFETCH_CAP = 30


class KBSearchTool(Tool):
    name = "search_kb"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "用于在知识库中检索的查询字符串。具体、关键词清晰最好。",
            },
            "limit": {
                "type": "integer",
                "description": "返回 top-k 数，默认 5。",
                "default": 5,
            },
        },
        "required": ["query"],
    }

    def __init__(
        self,
        kb: KB,
        embedding_cfg: "UserEmbeddingConfig | None" = None,
        reranker_cfg: "UserRerankerConfig | None" = None,
    ) -> None:
        self.kb_id = kb.id
        self.kb_name = kb.name
        self.kb_description = kb.description or ""
        self.collection_name = kb.collection_name
        self.embedding_cfg = embedding_cfg
        # v3-M4: System KBs (curated travel demo) bypass reranker — their
        # 20-chunk dataset has been hand-tuned via MMR + city filter in v2-M7
        # and we want demo behavior stable regardless of user reranker setting.
        self.reranker_cfg = None if bool(getattr(kb, "is_system", False)) else reranker_cfg
        # v3-M3: owner-controlled. Passed through to hybrid_search as
        # group_by_field="doc_id" so each document contributes at most one
        # chunk to top-k. No-op if the collection doesn't support hybrid.
        self.grouping_enabled = bool(getattr(kb, "grouping_enabled", False))
        # Compose a description that tells the LLM what's in this KB so it
        # knows when calling search_kb makes sense.
        desc_part = f"。{self.kb_description}" if self.kb_description else ""
        self.description = (
            f"在用户的知识库「{self.kb_name}」中做向量检索{desc_part}。"
            f"任何用户问题如果可能在该 KB 里能找到答案，都应优先调用此工具。"
            f"传 query (中文 / 英文均可)，返回 top-k 相关文本 chunks，"
            f"基于 chunks 内容作答，不要编造。"
        )

    async def execute(self, query: str, limit: int = 5) -> ToolResult:
        if not query or not query.strip():
            return ToolResult(text="", latency_ms=0, error="query is empty")

        try:
            vec = await embed(query.strip(), cfg=self.embedding_cfg)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(text="", latency_ms=0, error=f"embed failed: {exc}")

        try:
            store = get_store()
            if not hasattr(store, "search") or not self.collection_name:
                return ToolResult(
                    text="", latency_ms=0, error="KB search requires a multi-collection backend (qdrant or milvus)"
                )
            # v3-M3: prefer hybrid (dense + BM25) if the collection was built
            # with the hybrid schema. Falls back to dense-only for legacy
            # collections and Qdrant. RRF picks the top-N members; per-chunk
            # `score` is still cosine similarity so the v2-M6 prompt's 3-tier
            # threshold logic continues to work unchanged.
            original_limit = max(1, min(int(limit) if limit else 5, 20))
            # v3-M4: when reranker is enabled, over-fetch so the cross-encoder
            # has more candidates to discriminate over.
            fetch_limit = (
                min(original_limit * _RERANK_OVERFETCH_MULTIPLIER, _RERANK_OVERFETCH_CAP)
                if self.reranker_cfg
                else original_limit
            )
            supports_hybrid = (
                hasattr(store, "hybrid_search")
                and hasattr(store, "collection_supports_hybrid")
                and await store.collection_supports_hybrid(self.collection_name)
            )
            if supports_hybrid:
                hits = await store.hybrid_search(
                    query_vector=vec,
                    query_text=query.strip(),
                    collection_name=self.collection_name,
                    limit=fetch_limit,
                    group_by="doc_id" if self.grouping_enabled else None,
                )
            else:
                hits = await store.search(
                    vec,
                    collection_name=self.collection_name,
                    limit=fetch_limit,
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(text="", latency_ms=0, error=f"vector search failed: {exc}")

        # v3-M4: cross-encoder rerank pass. Reorders top-N candidates; on
        # failure, fall back to first-stage order so chat doesn't break.
        # IMPORTANT: hit["score"] stays cosine — reranker only reorders.
        if self.reranker_cfg and len(hits) >= 2:
            texts = [(h.get("payload") or {}).get("text", "") or "" for h in hits]
            try:
                reordered = await rerank(
                    query.strip(),
                    texts,
                    top_n=original_limit,
                    cfg=self.reranker_cfg,
                )
                if reordered:
                    hits = [hits[idx] for idx, _ in reordered if 0 <= idx < len(hits)]
                    log.info(
                        "kb_search.reranked",
                        extra={
                            "kb_id": self.kb_id,
                            "fetch_limit": fetch_limit,
                            "top_n": len(hits),
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "kb_search.rerank_failed kb_id=%s err=%s — falling back to dense order",
                    self.kb_id,
                    exc,
                )

        # Trim to caller's requested limit (defensive: the rerank path already
        # returns at most top_n, but over-fetched hits without rerank need it).
        hits = hits[:original_limit]

        if not hits:
            return ToolResult(
                text=f"知识库「{self.kb_name}」中没有找到与「{query}」相关的内容。",
                latency_ms=0,
                raw={"hits": 0, "kb_id": self.kb_id},
            )

        # Format: per-chunk block with source filename + score for citation.
        blocks: list[str] = []
        for i, c in enumerate(hits, start=1):
            p = c.get("payload", {}) or {}
            filename = p.get("filename", "(unknown)")
            text = (p.get("text") or "").strip()
            score = c.get("score", 0.0)
            blocks.append(
                f"[chunk {i}] 来源: {filename}  相关度: {score:.3f}\n{text}"
            )

        return ToolResult(
            text="\n\n---\n\n".join(blocks),
            latency_ms=0,
            raw={"hits": len(hits), "kb_id": self.kb_id},
        )
