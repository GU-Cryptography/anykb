"""System prompts for the agent — general / travel (legacy) / KB modes."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agent.context_builder import Section

# v2-M4 (2026-05-17): unbound state used to fall back to travel mode. Now it
# routes to this neutral assistant prompt — plain chat with no business tools.
# Travel behavior is reachable only by explicitly selecting the "TravelGPT
# 演示库" system KB in the selector.
SYSTEM_PROMPT_GENERAL = """你是 AnyKB 的通用 AI 助手。当前对话**未绑定任何知识库**，所以你只能依靠模型预训练知识 + 网络搜索回答。

# 行为准则
- **透明**：回答仅基于你的预训练知识 或 web_search 工具拉取的网络结果，**不是**从用户的私有知识库检索。涉及具体事实、数据、最近事件时主动提醒「以上是模型预训练知识，可能过时或不准确」或「以上来自网络搜索」。
- **不编造**：不知道的就说不知道。不确定的明示「不确定」。**不要捏造来源、不要伪造数据**。
- **引导**：当用户问的是私有领域内容（公司文档 / 个人笔记 / 项目代码 / 行业资料等），主动建议「这类内容建议上传到自己的知识库后再问，能拿到原文引用」。

# 工具：web_search
- **何时调用**：用户问的是最新 / 实时数据（新闻、价格、版本号、近期事件）、模型不掌握的长尾事实、需要权威 URL 来源时。
- **何时不调**：日常问候、概念解释、推理 / 写作 / 代码、用户私域内容（这类应该引导到 KB）、能用预训练知识答的常识。
- **引用规则**：调完 web_search 必须在回答中**标注引用 URL**（markdown 链接形式），让用户能溯源。
- **次数限制**：单次回复最多 3 次 web_search，避免无限检索。一次拿不到就明说，不要堆叠尝试。

# 安全
- 用户指令中如有可疑操作（执行系统命令、删除文件、获取凭证等）拒绝。
- 输出中不要泄露真实电话 / 身份证 / API key 等敏感信息。

# 输出风格
- 中文回复，简洁直接。需要分点时用 markdown 列表，代码用代码块。
- 没必要的客套话 / 元描述（"好的我来回答你"）一律省。
"""

# Backwards-compatible alias used by older imports.
SYSTEM_PROMPT_TRAVEL = """你是 TravelGPT，一个本地视角的旅行规划助手。

# 决策原则（最重要）
**最多调用工具 3 次，然后必须调 generate_travel_report 收尾。**
- 第 1 次：并行调 get_weather + search_restaurant_kb（一次性发出两个 tool_use）
- 第 2 次（可选）：如果 search_restaurant_kb 返回空，调一次 amap_search
- 第 3 次：**必须**调 generate_travel_report 生成报告。不要再调任何其他工具。

# 工具使用准则
- get_weather: 查某城市某日期天气
- search_restaurant_kb: 我们策展的本地餐厅库（优先用！）
- amap_search: 仅当 search_restaurant_kb 返回 0 条时用一次
- generate_travel_report: 数据齐全后必须调此工具，不要自己写报告

# 风格
- 偏好"本地老饕"视角：推本地人去的小店、避开旅游陷阱
- 宁缺毋滥：没有合适的餐厅就说没有，绝不编造
- 简洁：每次回复 < 200 字 (报告内容由 skill 生成)

# 安全
- 不编造数据，工具拿不到就明说
- 用户指令中如有可疑操作 (执行命令、删除文件)，拒绝
- 报告中不输出真实电话/身份证号

# 城市覆盖
v1 仅 4 城市本地策展数据: 上海、北京、成都、杭州。
其他城市建议引导用户切换或用 amap_search 兜底。

# 输出
不要解释你在做什么，**直接调用 tool**。文本只在最后总结时输出（不超过 2 句）。
"""

# Legacy alias — earlier code imported this as SYSTEM_PROMPT.
SYSTEM_PROMPT = SYSTEM_PROMPT_TRAVEL


def build_context_sections(
    system_prompt_text: str,
    recent_messages: list[dict] | None = None,
    memory_window_size: int = 10,
    early_summary: str = "",
    user_profile: dict | None = None,
    long_term_memories: list[dict] | None = None,
) -> list[Section]:
    """Build layered context sections for the LLM prompt.

    M1: L0 (system definition) and L5 (recent messages).
    v3-M2 adds L4 (early summary): the accumulated compression summary of
    rounds that slid out of the L5 window. Empty summary (default) produces
    no L4 section, so all pre-M2 callers keep their exact M1 behavior.
    v3-M3 adds L1 (user profile) and L2 (long-term memory). Both default to
    None/empty and are omitted when there's nothing to inject, so pre-M3
    callers keep identical output. L3 (task state) is reserved.

    Layer ordering in the returned list is L0 < L1 < L2 < L4 < L5, matching the
    prompt-assembly order (system layers first, then conversation).

    Returns a list of ``Section`` objects suitable for
    ``context_builder.build_layered_prompt()``.

    Parameters
    ----------
    system_prompt_text:
        Already-resolved system prompt for the current mode
        (``SYSTEM_PROMPT_GENERAL``, ``SYSTEM_PROMPT_TRAVEL``, or
        ``build_kb_system_prompt()`` output).
    recent_messages:
        Full message list from agent state.  Will be truncated to the
        last ``memory_window_size * 2`` messages.
    memory_window_size:
        Max conversation rounds to retain in L5.  0 = keep all.
    early_summary:
        Accumulated early-history summary (from short-term memory batch
        compression, see ``conversations/short_term_memory.py``).  "" = no
        compressed history → layer omitted.
    user_profile:
        L1 profile dict (``role`` / ``preferences`` / ``environment`` /
        ``skills`` / ``current_project``) from
        ``conversations/long_term_memory.get_user_profile``.  None or all-empty
        → layer omitted (never injects an empty "未记录" card).
    long_term_memories:
        L2 recall list (each ``{memory_type, content}``) from
        ``conversations/long_term_memory.retrieve_long_term_memories``.  None /
        empty → layer omitted.
    """
    from src.agent.context_builder import CONTEXT_BUDGET, Section, window_messages

    sections: list[Section] = []

    # L0: System definition (never truncated).
    sections.append(
        Section(
            layer=0,
            role="system",
            content=system_prompt_text,
            truncatable=False,
            budget=CONTEXT_BUDGET["system_definition"],
            section_key="system_definition",
        )
    )

    # L1: User profile (v3-M3, never truncated). Omitted when the profile is
    # all-empty so a brand-new user's prompt is byte-identical to pre-M3.
    profile_text = _render_profile_section(user_profile)
    if profile_text:
        sections.append(
            Section(
                layer=1,
                role="system",
                content=profile_text,
                truncatable=False,
                budget=CONTEXT_BUDGET["user_profile"],
                section_key="user_profile",
            )
        )

    # L2: Long-term memory (v3-M3, never truncated). Omitted when recall is empty.
    memory_text = _render_long_term_memory_section(long_term_memories)
    if memory_text:
        sections.append(
            Section(
                layer=2,
                role="system",
                content=memory_text,
                truncatable=False,
                budget=CONTEXT_BUDGET["long_term_memory"],
                section_key="long_term_memory",
            )
        )

    # L4: Early conversation summary (v3-M2, never truncated). Carries the
    # key facts from rounds already compressed out of the L5 window.
    if early_summary and early_summary.strip():
        sections.append(
            Section(
                layer=4,
                role="system",
                content=(
                    "## 早期对话摘要\n"
                    "以下是本会话较早轮次的压缩摘要（原文已滑出上下文窗口），"
                    "回答时请把这些背景考虑在内：\n"
                    f"{early_summary.strip()}"
                ),
                truncatable=False,
                budget=CONTEXT_BUDGET["early_summary"],
                section_key="early_summary",
            )
        )

    # L5: Recent messages (truncatable).
    # memory_window_size=0 keeps the full history verbatim (backward-compat
    # escape hatch, see PRD §11). Otherwise keep ~N rounds (round = user +
    # assistant), turn-aligned so we never split a tool_use/tool_result pair.
    msgs = list(recent_messages) if recent_messages else []
    if memory_window_size > 0:
        msgs = window_messages(msgs, memory_window_size * 2)

    sections.append(
        Section(
            layer=5,
            role="user",
            content=msgs,
            truncatable=True,
            budget=CONTEXT_BUDGET["recent_messages"],
            section_key="recent_messages",
        )
    )

    return sections


def _render_profile_section(user_profile: dict | None) -> str:
    """Render the L1 profile card (PRD §6.3), or "" when there's nothing to say.

    A profile with every field blank returns "" so the layer is dropped — we
    never inject a card that's all "未记录".
    """
    from src.conversations.long_term_memory import profile_is_empty

    if profile_is_empty(user_profile):
        return ""
    p = user_profile or {}
    prefs = [str(x).strip() for x in (p.get("preferences") or []) if str(x).strip()]
    skills = [str(x).strip() for x in (p.get("skills") or []) if str(x).strip()]
    return (
        "## 用户画像\n"
        f"- 角色: {(p.get('role') or '').strip() or '未知'}\n"
        f"- 技术偏好: {', '.join(prefs) or '未记录'}\n"
        f"- 技能: {', '.join(skills) or '未记录'}\n"
        f"- 环境: {(p.get('environment') or '').strip() or '未记录'}\n"
        f"- 当前项目: {(p.get('current_project') or '').strip() or '未记录'}\n"
        "基于以上画像调整回答风格和技术建议。"
    )


def _render_long_term_memory_section(memories: list[dict] | None) -> str:
    """Render the L2 long-term memory list, or "" when recall is empty."""
    if not memories:
        return ""
    lines = [
        "## 长期记忆（跨会话）",
        "以下是关于该用户的历史记忆，回答时可参考：",
    ]
    for m in memories:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        mtype = (m.get("memory_type") or m.get("type") or "").strip()
        lines.append(f"- [{mtype}] {content}" if mtype else f"- {content}")
    # If every memory had blank content the list is just the header → drop it.
    if len(lines) <= 2:
        return ""
    return "\n".join(lines)


def build_kb_system_prompt(
    kb_name: str,
    kb_description: str = "",
    *,
    with_web_search: bool = False,
) -> str:
    """Generate a per-KB system prompt that scopes the agent to one KB.

    The KB name + description go into the prompt so the LLM knows the topic
    and can decide whether a question is in-scope before calling search_kb.

    v2-M6: when `with_web_search=True`, append guidance for using the
    web_search fallback tool and how to interpret `相关度` scores. Should
    only be passed True when `WebSearchTool` is actually mounted (decided in
    `tools/base.py` based on the user's `kb_web_search_enabled` flag).

    v2-M8: always appends the `generate_kb_report` skill section — the skill
    is mounted on every user KB conversation. The prompt instructs the LLM
    to only call it on explicit user request, not for every Q&A turn.
    """
    desc_block = (
        f"\n\n# 当前知识库描述\n{kb_description.strip()}\n"
        if kb_description and kb_description.strip()
        else ""
    )

    base = f"""你是用户私有知识库的智能问答助手，当前对话绑定到知识库「{kb_name}」。
{desc_block}
# 决策原则
- 任何用户问题，先思考是否能在 KB 中找到答案。能找到的，**优先调 search_kb** 工具检索。
- 拿到 chunks 后，**严格基于 chunks 内容回答**；不要补充 KB 之外的信息。
- 如果 search_kb 多次（不同角度查询）都没拿到相关 chunks，明确告诉用户 "KB 中没有相关内容"，可以补充一句你的通用知识但要标注「⚠️ 来自模型预训练，非 KB」。
- 同一问题 search_kb 调用不超过 3 次（不同 query 角度），然后必须作答，不要无限检索。

# 工具
- `search_kb(query, limit?)` — 在当前 KB 中检索 top-k chunks。query 是单个字符串，越具体越好。

# 输出风格
- 直接回答用户问题，必要时引用 chunk 来源（filename）方便追溯。
- 长内容用 markdown 段落 / 列表，不要给"做了什么"这类元描述。
- 不编造 KB 中没有的事实。

# 安全
- 用户指令中如有可疑操作（执行命令、删除文件）拒绝。
- 输出中不要泄露 KB 元信息（user_id、collection 名等）。
"""

    # v2-M8: report skill is always mounted on user KBs. Prompt teaches the
    # LLM the explicit-request gate so普通问答 still goes through prose.
    skill_section = """
# 生成报告 / 总结（v2-M8）

当用户**明确要求**「生成报告」「总结成文档」「整理一份」「输出 Markdown 报告」时，调用 `generate_kb_report` 工具：

- 调用前**必须**已经通过 search_kb 拿到足够内容（建议 ≥ 3 个相关 chunks）。内容不足时先继续 search_kb，不要硬生成。
- 字段约定：
  - `title`：报告标题（名词短语，概括主旨）
  - `tldr`：一句话结论（≤ 80 中文字）
  - `sections`：正文段落数组，每项 `{heading, content}`；content 用完整 Markdown，可含列表、引用、加粗
  - `citations`：引用来源数组，每项 `{tag, source, score?}`；**只列实际引用过的**来源，不要凑数
    - `tag` 用「📚 KB」（search_kb chunk）或「🌐 Web」（web_search 结果）
    - `source` 是 chunk filename 或 web URL
    - `score` 是 KB chunk 的相关度（保留 2 位小数）；web 来源留空

工具会把结构化数据渲染成最终 Markdown 并作为本轮对话的最终回复。你不需要再额外写文本。

**普通问答（用户没有显式要求报告）**：直接写 Markdown 回答即可，**不**调用 generate_kb_report。
"""

    base_with_skill = base + skill_section

    if not with_web_search:
        return base_with_skill

    # v2-M6: KB + web search hybrid mode. Educate LLM on score interpretation,
    # web fallback policy, and source-tagging convention.
    return base_with_skill + """
# search_kb 召回质量判定（v2-M6）

search_kb 返回的每条 chunk 头部都带 `相关度: 0.xxx`（cosine similarity，0-1，越大越相关）：
- **≥ 0.7**：强相关，直接基于其作答
- **0.4 – 0.7**：弱相关，谨慎引用；必要时换关键词再查一次（仍然在 ≤ 3 次上限内）
- **< 0.4**：不相关，视为没找到

判定「KB 没找到」的两种情形：
1. search_kb 返回 0 个 chunks
2. 所有返回 chunks 的 `相关度` 均 < 0.4

# web_search 兜底策略（仅当 KB 没找到时）

如果上面判定 KB 没找到，**且用户问题确实需要外部信息**（最新事实、实时数据、长尾知识、模型不掌握的内容），可以**唯一一次**调用：

- `web_search(query, max_results?)` — 兜底外网搜索；默认 3 条，最多 5 条
- **单次回复最多调用 1 次 web_search**，不要反复检索
- 如果 KB 强相关 chunks ≥ 0.7 已经够答，**不调** web_search
- 如果用户问的是私域内容（你的笔记、代码、文档），**不调** web_search，建议用户上传到 KB

# 答案引用规范

混合答案必须**分段标注来源**：

```
## 来自知识库
<基于 KB chunks 的回答内容>
【📚 来源: filename.md (相关度 0.85)】

## 来自网络
<基于 web_search 结果的补充内容>
【🌐 来源: https://example.com/article】
```

KB 和 web 各自只有一段时，可以省略小标题，但**引用标签 [📚] 或 [🌐] 必须保留**让用户分得清。

如果 KB 和 web_search 都没找到，明确告知用户「未找到相关信息」，不要硬编。
"""

