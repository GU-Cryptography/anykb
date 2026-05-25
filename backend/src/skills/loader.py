"""Skill loader — loads SKILL.md and invokes via a second LLM call (no tools)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.infra.llm import get_client, pick_model, with_cache_control
from src.settings import get_settings

if TYPE_CHECKING:
    from src.settings_user import UserLLMConfig

SKILLS_DIR = Path(__file__).parent
_SKILL_CACHE: dict[str, str] = {}


def load_skill_md(skill_name: str) -> str:
    if skill_name in _SKILL_CACHE:
        return _SKILL_CACHE[skill_name]
    path = SKILLS_DIR / skill_name / "SKILL.md"
    if not path.exists():
        raise FileNotFoundError(f"Skill not found: {skill_name} at {path}")
    content = path.read_text(encoding="utf-8")
    _SKILL_CACHE[skill_name] = content
    return content


async def invoke_skill(
    skill_name: str,
    args: dict[str, Any],
    *,
    llm_cfg: "UserLLMConfig | None" = None,
) -> str:
    """Invoke skill by sending SKILL.md + args to LLM (no tools, text only).

    v2-M8: prompt is generic (no longer travel-specific). Templates carry
    their own heading conventions in SKILL.md so the LLM picks them up.
    v2-M8: also accepts `llm_cfg` so per-user LLM (v2-M1) routes through
    the skill call too, not just the planner step.
    """
    s = get_settings()
    skill_md = load_skill_md(skill_name)
    client = get_client(llm_cfg)
    model = pick_model([], [], llm_cfg)

    user_prompt = (
        "请根据下方的输入数据，按下方模板格式直接输出最终的 Markdown 报告。\n\n"
        "重要规则：\n"
        "1. 直接输出最终报告内容，不要输出模板本身\n"
        "2. 不要输出 SKILL 元数据 (---name: xxx---)\n"
        "3. 不要解释你在做什么，直接输出 `## ` 开头的报告\n"
        "4. 用真实数据填充模板里所有 {{xxx}} 形式的占位符\n"
        "5. {{#each xxx}} ... {{/each}} 循环段全部展开列出，不要保留模板语法\n\n"
        f"=== 模板格式参考 ===\n{skill_md}\n=== 模板结束 ===\n\n"
        f"=== 输入数据 ===\n{json.dumps(args, ensure_ascii=False, indent=2)}\n"
        "=== 数据结束 ===\n\n"
        "现在请直接输出最终的 Markdown 报告："
    )

    system_msg = (
        "你是 Markdown 报告生成助手。你的任务是把结构化数据填入模板，"
        "直接输出最终的 Markdown 报告。绝对不要原样输出模板内容或元数据。"
    )

    # Provider routing — user cfg wins; env fallback otherwise.
    if llm_cfg is not None:
        is_anthropic = llm_cfg.provider == "anthropic"
    else:
        is_anthropic = s.llm_provider == "anthropic"

    if not is_anthropic:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=1500,
        )
        return resp.choices[0].message.content or ""
    resp = await client.messages.create(
        model=model,
        max_tokens=1500,
        system=with_cache_control([{"type": "text", "text": system_msg}], llm_cfg),
        messages=[{"role": "user", "content": user_prompt}],
    )
    return resp.content[0].text if resp.content else ""
