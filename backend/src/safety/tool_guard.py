"""L2 — tool whitelist. Block calls outside the registry."""
from __future__ import annotations

DANGEROUS_TOOL_NAMES = {"execute_shell", "delete_file", "read_file", "write_file"}


def is_tool_allowed(name: str, registry_names: list[str]) -> tuple[bool, str | None]:
    if name in DANGEROUS_TOOL_NAMES:
        return False, f"危险工具 {name} 已被阻止"
    if name not in registry_names:
        return False, f"工具 {name} 不在白名单"
    return True, None
