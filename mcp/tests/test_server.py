"""Smoke test for MCP tool listing."""

from bvmcp.scopes import scope_for_tool
from bvmcp.server import list_tools


async def test_list_tools_empty() -> None:
    tools = await list_tools()
    # The exact count grows whenever a new tool family lands; instead of
    # hard-coding it (which made the test stale every sprint), assert the
    # invariants: non-empty surface, unique names, and every tool has a
    # scope catalog entry — the dispatcher fail-closes on tools missing
    # from the catalog, so a missing entry is a regression.
    assert len(tools) > 0
    names = [t.name for t in tools]
    assert len(set(names)) == len(names), f"duplicate tool names: {names}"
    missing_scope = [n for n in names if scope_for_tool(n) is None]
    assert not missing_scope, f"tools missing scope catalog entry: {missing_scope}"


async def test_list_tools_includes_folder_surface() -> None:
    """Folder navigation + tree reshape must be exposed to MCP agents.

    The user explicitly requires that LLMs can list folders, create /
    rename / delete them, and add or remove items so the assistant can
    reorganise a fascicolo end-to-end via MCP. This test guards against
    the family being accidentally dropped from a future refactor of
    ``_TOOL_MODULES``.
    """
    tools = await list_tools()
    names = {t.name for t in tools}
    expected = {
        "list_folders",
        "get_folder",
        "create_folder",
        "update_folder",
        "delete_folder",
        "add_item_to_folder",
        "remove_item_from_folder",
    }
    missing = expected - names
    assert not missing, f"missing folder MCP tools: {sorted(missing)}"
