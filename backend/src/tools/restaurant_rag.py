"""Restaurant RAG tool — vector search via configurable backend + MMR diversity."""
from __future__ import annotations

import math
from typing import Any

from src.infra.embedding import embed
from src.tools.base import Tool, ToolResult


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-9)


def _mmr_select(
    candidates: list[dict[str, Any]],
    k: int = 3,
    lambda_: float = 0.85,
    min_score: float = 0.0,
) -> list[dict[str, Any]]:
    """Maximal Marginal Relevance for diversity. candidates must have 'vector' and 'score'.

    - lambda_: 相关度 vs 多样性权重 (1.0 = 纯相关度，0.0 = 纯多样性)。
    - min_score: 丢弃 score < min_score 的候选 (cosine 阈值)。
    """
    filtered = [c for c in candidates if c.get("score", 0.0) >= min_score]
    if len(filtered) <= k:
        return filtered
    selected: list[dict[str, Any]] = []
    remaining = list(filtered)
    while remaining and len(selected) < k:
        best, best_idx, best_score = None, -1, -math.inf
        for i, cand in enumerate(remaining):
            relevance = cand["score"]
            diversity = (
                max((_cosine(cand["vector"], s["vector"]) for s in selected), default=0.0)
                if selected
                else 0.0
            )
            mmr = lambda_ * relevance - (1 - lambda_) * diversity
            if mmr > best_score:
                best, best_idx, best_score = cand, i, mmr
        if best is None:
            break
        selected.append(best)
        remaining.pop(best_idx)
    return selected


class RestaurantRagTool(Tool):
    name = "search_restaurant_kb"
    description = (
        "在本地策展的'本地老饕'餐厅库搜索。优先使用此工具，命中 0 条时再用 amap_search。"
        "返回 3 家最相关餐厅 (名字 / 评分 / 地址 / 推荐菜 / 推荐理由)。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "中文城市名"},
            "cuisine": {"type": "string", "description": "菜系或菜名 (如 '酸菜鱼')"},
        },
        "required": ["city", "cuisine"],
    }

    async def execute(self, city: str, cuisine: str) -> ToolResult:
        query_text = f"{city} {cuisine}"
        query_vec = await embed(query_text)

        from src.infra.vector_store import get_store

        try:
            store = get_store()
            hits = await store.search(query_vec, city=city, limit=10)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(text="", latency_ms=0, error=f"Vector search failed: {exc}")

        if not hits:
            return ToolResult(
                text=f"本地餐厅库无 '{city} {cuisine}' 相关结果，建议调用 amap_search 兜底。",
                latency_ms=0,
                raw={"hits": 0},
            )

        selected = _mmr_select(hits, k=3, lambda_=0.85, min_score=0.2)
        if not selected:
            return ToolResult(
                text=(
                    f"本地餐厅库 '{city} {cuisine}' 召回都太弱 (cosine < 0.2)，"
                    "建议调用 amap_search 兜底。"
                ),
                latency_ms=0,
                raw={"hits": len(hits), "selected": 0},
            )
        lines = []
        for c in selected:
            p = c["payload"]
            lines.append(
                f"[{p.get('name')}] 评分:{p.get('local_score')}\n"
                f"地址:{p.get('addr')}\n"
                f"推荐菜:{', '.join(p.get('signature_dishes', []))}\n"
                f"推荐理由:{p.get('why_recommended', '')}"
            )
        return ToolResult(
            text="\n\n".join(lines), latency_ms=0, raw={"hits": len(hits), "selected": len(selected)}
        )
