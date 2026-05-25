"""LLM client wrapper supporting Anthropic and OpenAI-compatible providers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from src.settings import get_settings

if TYPE_CHECKING:
    from src.settings_user import UserLLMConfig

# Cost per 1M tokens (USD). Update with vendor pricing.
PRICING = {
    "claude-haiku-4-5-20251001": {"in": 1.0, "out": 5.0},
    "claude-sonnet-4-6": {"in": 3.0, "out": 15.0},
    "claude-opus-4-7": {"in": 15.0, "out": 75.0},
    "deepseek-chat": {"in": 0.14, "out": 0.28},  # DeepSeek v3 pricing
}


@dataclass
class CostTracker:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    usd: float = 0.0
    calls: list[dict[str, Any]] = field(default_factory=list)

    def add(self, model: str, usage: Any) -> None:
        price = PRICING.get(model, {"in": 1.0, "out": 5.0})
        in_t = getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0)
        out_t = getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0)
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0

        self.input_tokens += in_t
        self.output_tokens += out_t
        self.cache_read_tokens += cache_read
        self.cache_creation_tokens += cache_create

        # Cache read costs 10% of normal input.
        call_cost = (
            in_t * price["in"] / 1_000_000
            + out_t * price["out"] / 1_000_000
            + cache_read * price["in"] * 0.1 / 1_000_000
            + cache_create * price["in"] * 1.25 / 1_000_000
        )
        self.usd += call_cost
        self.calls.append(
            {"model": model, "in": in_t, "out": out_t, "cache_read": cache_read, "usd": call_cost}
        )


def get_client(cfg: "UserLLMConfig | None" = None):
    """Return appropriate client. If cfg is given, use user creds; else fall back to env."""
    if cfg is not None:
        if cfg.provider == "anthropic":
            from anthropic import AsyncAnthropic
            return AsyncAnthropic(api_key=cfg.api_key, base_url=cfg.base_url)
        # openai-compat covers DeepSeek, OpenAI, vLLM, Together, Groq, LMStudio, etc.
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url)

    s = get_settings()
    if s.llm_provider == "deepseek":
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=s.deepseek_api_key, base_url=s.deepseek_base_url)
    from anthropic import AsyncAnthropic
    return AsyncAnthropic(api_key=s.anthropic_api_key, base_url=s.anthropic_base_url)


def pick_model(messages: list[dict], tools: list[dict], cfg: "UserLLMConfig | None" = None) -> str:
    """Route between default and complex model."""
    if cfg is not None:
        default_model = cfg.default_model
        complex_model = cfg.complex_model or cfg.default_model
    else:
        s = get_settings()
        default_model = s.llm_default_model
        complex_model = s.llm_complex_model

    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    text_len = 0
    if last_user:
        content = last_user.get("content", "")
        if isinstance(content, str):
            text_len = len(content)
        elif isinstance(content, list):
            text_len = sum(len(b.get("text", "")) for b in content if isinstance(b, dict))
    # Upgrade to complex model if many tools OR very long user input.
    if len(tools) > 5 or text_len > 2000:
        return complex_model
    return default_model


def with_cache_control(blocks: list[dict], cfg: "UserLLMConfig | None" = None) -> list[dict]:
    """Add cache_control to the last block for prompt caching (Anthropic only)."""
    provider = cfg.provider if cfg is not None else get_settings().llm_provider
    if provider != "anthropic" or not blocks:
        return blocks
    out = [dict(b) for b in blocks]
    out[-1]["cache_control"] = {"type": "ephemeral"}
    return out


def convert_to_openai_format(messages: list[dict], tools: list[dict]) -> tuple[str, list[dict], list[dict]]:
    """Convert Anthropic-style messages to OpenAI format."""
    # Extract system from first message if present
    system = ""
    openai_messages = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "system":
            if isinstance(content, list):
                system = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            else:
                system = content
            continue

        # Convert content blocks to OpenAI format
        if isinstance(content, list):
            text_parts = []
            tool_calls = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id"),
                            "type": "function",
                            "function": {
                                "name": block.get("name"),
                                "arguments": str(block.get("input", {}))
                            }
                        })
                    elif block.get("type") == "tool_result":
                        # Tool result becomes a tool message
                        openai_messages.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id"),
                            "content": block.get("content", "")
                        })
                        continue

            msg_dict = {"role": role, "content": " ".join(text_parts) if text_parts else ""}
            if tool_calls:
                msg_dict["tool_calls"] = tool_calls
            openai_messages.append(msg_dict)
        else:
            openai_messages.append({"role": role, "content": content})

    # Convert tools to OpenAI format
    openai_tools = []
    for tool in tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool.get("name"),
                "description": tool.get("description"),
                "parameters": tool.get("input_schema", {})
            }
        })

    return system, openai_messages, openai_tools
