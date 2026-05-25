"""Amap POI fallback — used when restaurant_kb returns 0 hits."""
from __future__ import annotations

import httpx

from src.settings import get_settings
from src.tools.base import Tool, ToolResult


class AmapFallbackTool(Tool):
    name = "amap_search"
    description = (
        "高德地图 POI 搜索兜底。仅在 search_restaurant_kb 返回 0 条时使用。"
        "返回 3 个 POI (名字 / 地址 / 类型)。注意：高德结果偏大众，质量不如本地库。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "keyword": {"type": "string", "description": "搜索关键词 (餐厅名或菜系)"},
        },
        "required": ["city", "keyword"],
    }

    async def execute(self, city: str, keyword: str) -> ToolResult:
        s = get_settings()
        if not s.amap_api_key:
            return ToolResult(
                text=f"[mock amap] 在 {city} 搜索 '{keyword}': "
                f"POI-A (X 路 1 号, 中餐), POI-B (Y 路 2 号, 中餐), POI-C (Z 路 3 号, 中餐) "
                f"— 配置 AMAP_API_KEY 启用真实查询",
                latency_ms=0,
                raw={"mock": True},
            )
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://restapi.amap.com/v3/place/text",
                params={
                    "key": s.amap_api_key,
                    "keywords": keyword,
                    "city": city,
                    "citylimit": "true",
                    "offset": 5,
                    "page": 1,
                    "extensions": "base",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        pois = data.get("pois") or []
        if not pois:
            return ToolResult(text=f"高德也未找到 '{city} {keyword}' 相关 POI。", latency_ms=0)
        top = pois[:3]
        lines = [f"[{p.get('name')}] 地址:{p.get('address')} 类型:{p.get('type')}" for p in top]
        return ToolResult(text="\n".join(lines), latency_ms=0, raw={"count": len(pois)})
