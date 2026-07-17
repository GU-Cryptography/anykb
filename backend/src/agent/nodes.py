"""LangGraph nodes: plan, call_tools, skill_report."""
from __future__ import annotations

import asyncio
import json
from typing import Any, TYPE_CHECKING

from src.agent.prompts import SYSTEM_PROMPT
from src.agent.state import AgentState
from src.infra.llm import CostTracker, get_client, pick_model, with_cache_control, convert_to_openai_format
from src.safety.tool_guard import is_tool_allowed
from src.skills.loader import invoke_skill
from src.tools.base import ToolRegistry

if TYPE_CHECKING:
    from src.settings_user import UserLLMConfig

MAX_ITERATIONS = 10


async def plan_node(
    state: AgentState,
    *,
    registry: ToolRegistry,
    cost: CostTracker,
    system_prompt: str = SYSTEM_PROMPT,
    include_travel_skill: bool = True,
    include_kb_skill: bool = False,
    llm_cfg: "UserLLMConfig | None" = None,
) -> AgentState:
    """LLM decides next action: call tools, call skill, or finish.

    The agent's prompt and the schema for the optional "skill" tools are
    injected by build_graph. KB-mode conversations get a different
    system_prompt + the generic `generate_kb_report` skill (v2-M8); travel
    KB gets `generate_travel_report`. Unbound chat mounts neither.

    v3-M1 (memory-optimization): constructs a layered prompt via
    ``build_context_sections`` + ``build_layered_prompt`` instead of
    passing ``system_prompt`` and ``messages`` directly to the LLM.

    v3-M2: injects the L4 early-summary layer when the session has compressed
    history. Requires ``conversation_id`` + ``user_id`` in state; when either
    is missing (old frontend) or short-term memory is off, L4 is simply
    omitted → identical to M1.
    """
    from src.agent.context_builder import build_layered_prompt
    from src.agent.prompts import build_context_sections
    from src.settings import get_settings

    # Early exit if final_report already set (by skill_report from prev tool wave)
    if state.get("final_report"):
        return {**state, "pending_tool_calls": []}

    iters = state.get("iterations", 0)
    if iters >= MAX_ITERATIONS:
        return {**state, "final_report": "超出最大推理轮数限制。", "pending_tool_calls": []}

    messages = state.get("messages", [])
    extra: list[dict[str, Any]] = []
    if include_travel_skill:
        extra.append(_skill_tool_schema())
    if include_kb_skill:
        extra.append(_kb_skill_tool_schema())
    tools_schema = registry.all_schemas() + extra
    model = pick_model(messages, tools_schema, llm_cfg)
    client = get_client(llm_cfg)

    s = get_settings()

    # v3-M2: fetch the early-history summary (L4) for this conversation. Only
    # when short-term memory is on AND the session id flowed in; any failure or
    # missing id degrades to "" → L4 omitted (M1 behavior). Best-effort read
    # never blocks the plan step. Fetched at most ONCE per request: the result
    # (even "") is cached into agent state so later plan iterations (tool
    # rounds, up to MAX_ITERATIONS) don't re-hit Redis/PG.
    early_summary = state.get("early_summary")
    if early_summary is None:
        early_summary = ""
        conv_id = state.get("conversation_id")
        user_id = state.get("user_id")
        if conv_id and user_id:
            try:
                from src.conversations.short_term_memory import get_context_summary

                early_summary = await get_context_summary(user_id, conv_id) or ""
            except Exception:  # noqa: BLE001 — L4 is best-effort, never break planning.
                early_summary = ""

    # v3-M3: fetch the L1 user profile + L2 long-term memories ONCE per request
    # (keyed off "long_term_memory" not yet being in state), then cache both into
    # agent state so tool-loop iterations skip the Redis/Milvus/PG reads — same
    # once-per-request pattern as early_summary above. Requires user_id in state;
    # anonymous / old-frontend requests (no user_id) skip L1+L2 entirely, which
    # keeps their prompt identical to pre-M3. Any failure degrades to empty →
    # the corresponding layer is simply omitted, never a broken plan step.
    user_profile = state.get("user_profile")
    long_term_memory = state.get("long_term_memory")
    if "long_term_memory" not in state:
        user_profile = {}
        long_term_memory = []
        user_id = state.get("user_id")
        if user_id:
            from src.conversations.long_term_memory import (
                get_user_profile,
                retrieve_long_term_memories,
            )

            try:
                user_profile = await get_user_profile(user_id)
            except Exception:  # noqa: BLE001 — L1 best-effort.
                user_profile = {}
            try:
                # L2 query is the current user input (last plain user message).
                long_term_memory = (
                    await retrieve_long_term_memories(user_id, _last_user_text(messages))
                    or []
                )
            except Exception:  # noqa: BLE001 — L2 best-effort.
                long_term_memory = []

    # Build layered context (M1: L0 + L5; M2 adds L4; M3 adds L1 + L2 when present).
    sections = build_context_sections(
        system_prompt_text=system_prompt,
        recent_messages=messages,
        memory_window_size=s.memory_window_size,
        early_summary=early_summary,
        user_profile=user_profile,
        long_term_memories=long_term_memory,
    )
    layered = build_layered_prompt(
        sections,
        total_budget=s.context_total_budget,
    )

    # Decide API shape: anthropic vs openai-compat. User cfg wins; env fallback otherwise.
    if llm_cfg is not None:
        is_anthropic = llm_cfg.provider == "anthropic"
    else:
        is_anthropic = s.llm_provider == "anthropic"

    if not is_anthropic:
        # OpenAI-compatible (DeepSeek, OpenAI, vLLM, Together, Groq, LMStudio, etc.)
        _, openai_messages, openai_tools = convert_to_openai_format(
            layered.messages, tools_schema,
        )
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": layered.system_text}] + openai_messages,
            tools=openai_tools if openai_tools else None,
            max_tokens=2048,
        )
        cost.add(model, resp.usage)

        choice = resp.choices[0]
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        if choice.message.content:
            text_parts.append(choice.message.content)

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": json.loads(tc.function.arguments) if tc.function.arguments else {}
                })

        # Build assistant message for history
        assistant_content = []
        if text_parts:
            assistant_content.append({"type": "text", "text": " ".join(text_parts)})
        for tc in tool_calls:
            assistant_content.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc["input"]
            })
    else:
        # Anthropic API
        system_blocks = with_cache_control(layered.system_blocks, llm_cfg)
        resp = await client.messages.create(
            model=model,
            max_tokens=2048,
            system=system_blocks,
            messages=layered.messages,
            tools=tools_schema,
        )
        cost.add(model, resp.usage)

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "input": block.input})

        assistant_content = [
            b.model_dump() if hasattr(b, "model_dump") else dict(b) for b in resp.content
        ]

    new_messages = messages + [{"role": "assistant", "content": assistant_content}]
    final_report: str | None = state.get("final_report")

    # Stop condition: model returns text only AND no pending tools.
    if not tool_calls and text_parts and not final_report:
        final_report = "\n".join(text_parts)

    return {
        **state,
        "messages": new_messages,
        "pending_tool_calls": tool_calls,
        "iterations": iters + 1,
        "final_report": final_report,
        "cost_usd": cost.usd,
        # v3-M2: persist the (possibly empty) L4 summary so subsequent plan
        # iterations within this request skip the Redis/PG re-read.
        "early_summary": early_summary,
        # v3-M3: persist the (possibly empty) L1 profile + L2 memories so
        # subsequent plan iterations skip the re-read (once-per-request cache).
        "user_profile": user_profile,
        "long_term_memory": long_term_memory,
    }


async def call_tools_node(
    state: AgentState,
    *,
    registry: ToolRegistry,
    emit,
    llm_cfg: "UserLLMConfig | None" = None,
) -> AgentState:
    """Execute all pending tool calls concurrently.

    v2-M8: `llm_cfg` flows through to `invoke_skill` so the report skill
    uses the user's own LLM (v2-M1) instead of always env defaults.
    """
    pending = state.get("pending_tool_calls", [])
    if not pending:
        return state

    async def _run(tc: dict[str, Any]) -> dict[str, Any]:
        name = tc["name"]
        args = tc.get("input") or {}
        ok, reason = is_tool_allowed(
            name,
            registry.names() + ["generate_travel_report", "generate_kb_report"],
        )
        if not ok:
            await emit({"event": "tool_blocked", "name": name, "reason": reason})
            return {
                "type": "tool_result",
                "tool_use_id": tc["id"],
                "content": f"[blocked by safety] {reason}",
                "is_error": True,
            }
        await emit({"event": "tool_start", "name": name, "input": args})

        if name == "generate_travel_report":
            text = await invoke_skill("travel_report", args, llm_cfg=llm_cfg)
            await emit({"event": "tool_end", "name": name, "latency_ms": 0, "ok": True})
            return {"type": "tool_result", "tool_use_id": tc["id"], "content": text}

        if name == "generate_kb_report":
            text = await invoke_skill("general_report", args, llm_cfg=llm_cfg)
            await emit({"event": "tool_end", "name": name, "latency_ms": 0, "ok": True})
            return {"type": "tool_result", "tool_use_id": tc["id"], "content": text}

        result = await registry.call(name, args)
        await emit(
            {
                "event": "tool_end",
                "name": name,
                "latency_ms": result.latency_ms,
                "ok": result.error is None,
                "error": result.error,
            }
        )
        return {
            "type": "tool_result",
            "tool_use_id": tc["id"],
            "content": result.text if result.error is None else f"[tool error] {result.error}",
            "is_error": result.error is not None,
        }

    results = await asyncio.gather(*[_run(tc) for tc in pending])

    log = list(state.get("tool_call_log") or [])
    for tc, r in zip(pending, results, strict=False):
        log.append(
            {
                "id": tc["id"],
                "name": tc["name"],
                "input": tc.get("input") or {},
                "result": r["content"],
                "latency_ms": 0,
                "error": "yes" if r.get("is_error") else None,
            }
        )

    messages = list(state.get("messages") or [])
    messages.append({"role": "user", "content": results})

    # If a report skill was called, treat its result as final_report.
    skill_names = {"generate_travel_report", "generate_kb_report"}
    skill_call = next((p for p in pending if p["name"] in skill_names), None)
    final_report = state.get("final_report")
    if skill_call:
        for r in results:
            if r["tool_use_id"] == skill_call["id"] and not r.get("is_error"):
                final_report = r["content"]
                break

    return {
        **state,
        "messages": messages,
        "pending_tool_calls": [],
        "tool_call_log": log,
        "final_report": final_report,
    }


def should_continue(state: AgentState) -> str:
    if state.get("final_report"):
        return "end"
    if state.get("pending_tool_calls"):
        return "tools"
    return "end"


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """Extract the current user query for L2 memory recall (v3-M3).

    Returns the text of the most recent plain user turn — a ``user`` message
    whose content is a string, OR the concatenated ``text`` blocks of a
    list-form user message. Tool-result user messages (content is a list of
    ``tool_result`` blocks, no free text) contribute nothing, so this reliably
    returns the human's question even mid tool-loop. "" when none found.
    """
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                return content.strip()
            continue
        if isinstance(content, list):
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            joined = " ".join(t for t in texts if t).strip()
            if joined:
                return joined
    return ""


def _skill_tool_schema() -> dict[str, Any]:
    return {
        "name": "generate_travel_report",
        "description": (
            "调用 travel_report skill 生成结构化 Markdown 旅行报告。"
            "数据齐全后调用此工具，传入收集到的所有信息。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "date": {"type": "string"},
                "weather": {"type": "string"},
                "restaurants": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "餐厅列表，每项含 name/addr/signature_dishes/why_recommended",
                },
                "user_intent": {"type": "string", "description": "用户原始诉求摘要"},
            },
            "required": ["city", "date"],
        },
    }


def _kb_skill_tool_schema() -> dict[str, Any]:
    """v2-M8: generic report skill for user KBs.

    Mounted on KB-bound conversations (non-travel). The LLM should call this
    only when the user explicitly asks for a report / summary / structured
    document, not for every Q&A turn — KB chat default behavior is still
    direct prose answers grounded in search_kb chunks.
    """
    return {
        "name": "generate_kb_report",
        "description": (
            "把当前对话基于知识库 chunks（必要时含 web_search 结果）整理成一份"
            "结构化 Markdown 报告。**仅当用户明确要求**「生成报告」/「总结成文档」/"
            "「整理一份」时调用；普通问答**不要**调用本工具，直接基于 chunks 作答即可。"
            "调用前你必须已经通过 search_kb 拿到足够内容；citations 字段必须如实引用使用过的来源。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "报告标题（名词短语，概括主旨）",
                },
                "tldr": {
                    "type": "string",
                    "description": "一句话结论，≤80 中文字",
                },
                "sections": {
                    "type": "array",
                    "description": "正文段落列表，按逻辑顺序排",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string"},
                            "content": {
                                "type": "string",
                                "description": "完整段落 Markdown，可含列表 / 引用 / 加粗",
                            },
                        },
                        "required": ["heading", "content"],
                    },
                },
                "citations": {
                    "type": "array",
                    "description": "引用来源列表，按引用顺序排",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tag": {
                                "type": "string",
                                "enum": ["📚 KB", "🌐 Web"],
                                "description": "📚 KB = search_kb chunk，🌐 Web = web_search 结果",
                            },
                            "source": {
                                "type": "string",
                                "description": "KB chunk 的 filename，或 web 结果的 URL",
                            },
                            "score": {
                                "type": "number",
                                "description": "KB chunk 的相关度（0-1）；web 来源留空",
                            },
                        },
                        "required": ["tag", "source"],
                    },
                },
            },
            "required": ["title", "tldr", "sections", "citations"],
        },
    }
