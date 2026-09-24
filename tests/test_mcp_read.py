import pytest
from spoke.mcp_server import build_server, call_tool


def test_open_items_returns_the_ledger(scratch_workspace):
    ws, name = scratch_workspace          # fixture: a store with 2 open items
    out = call_tool(build_server(ws), "spoke_open_items", {})
    assert "first-item" in out and "second-item" in out


def test_ledger_show_returns_one_node(scratch_workspace):
    ws, name = scratch_workspace
    out = call_tool(build_server(ws), "spoke_ledger_show", {"name": "first-item"})
    assert "first-item" in out


def test_ledger_show_refuses_a_name_that_escapes_the_ledger(scratch_workspace):
    ws, name = scratch_workspace
    out = call_tool(build_server(ws), "spoke_ledger_show", {"name": "../MEMORY"})
    assert "refused" in out.lower() or "not a safe" in out.lower()


def test_check_reports_defects(scratch_workspace_with_a_broken_record):
    ws, name = scratch_workspace_with_a_broken_record
    out = call_tool(build_server(ws), "spoke_check", {})
    assert "broken" in out


def test_project_names_the_active_project_and_store(scratch_workspace):
    ws, name = scratch_workspace
    out = call_tool(build_server(ws), "spoke_project", {})
    assert name in out and str(ws.config.store_path) in out


def test_no_tool_result_contains_a_credential(scratch_workspace, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-should-never-appear")
    ws, name = scratch_workspace
    srv = build_server(ws)
    for tool in ("spoke_open_items", "spoke_check", "spoke_project"):
        assert "sk-should-never-appear" not in call_tool(srv, tool, {})
