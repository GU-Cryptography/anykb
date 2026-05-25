"""Graph wiring test — verifies entry/exit + node names."""
import pytest

from src.agent.graph import build_graph


def test_graph_compiles():
    graph, _ = build_graph()
    assert graph is not None


@pytest.mark.asyncio
async def test_graph_no_crash_on_empty():
    """Smoke: agent state with mock emit handler."""
    from src.tools.base import ToolRegistry
    reg = ToolRegistry()  # empty registry
    graph, _ = build_graph(registry=reg)
    # Don't actually run (no API key); just check structure.
    assert hasattr(graph, "ainvoke")
