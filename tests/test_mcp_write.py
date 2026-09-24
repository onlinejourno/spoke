import pytest
from spoke.mcp_server import build_server, call_tool
from spoke.ledger.store import LedgerStore


def test_recording_an_item_writes_it(scratch_workspace):
    ws, name = scratch_workspace
    out = call_tool(build_server(ws), "spoke_record", {
        "name": "from-an-agent", "type": "item", "state": "open",
        "title": "Something an agent noticed", "body": "why"})
    assert "from-an-agent" in out
    assert LedgerStore(ws.config.store_path).read("from-an-agent").title.startswith("Something")


def test_an_agent_cannot_defer_without_a_ruling(scratch_workspace):
    # The gate must be identical for an agent and a person.
    ws, name = scratch_workspace
    out = call_tool(build_server(ws), "spoke_record", {
        "name": "sloppy", "type": "item", "state": "deferred",
        "title": "Deferred with no reason", "body": "why"})
    assert "ruling" in out.lower()
    with pytest.raises(Exception):
        LedgerStore(ws.config.store_path).read("sloppy")


def test_an_agent_cannot_block_without_a_blocker(scratch_workspace):
    # The gate must be identical for an agent and a person.
    ws, name = scratch_workspace
    out = call_tool(build_server(ws), "spoke_record", {
        "name": "unblocked", "type": "item", "state": "blocked",
        "title": "Blocked with no blocker", "body": "why"})
    assert "blocked_by" in out.lower()
    with pytest.raises(Exception):
        LedgerStore(ws.config.store_path).read("unblocked")


def test_update_appends_without_destroying_the_body(scratch_workspace):
    ws, name = scratch_workspace
    call_tool(build_server(ws), "spoke_update", {
        "name": "first-item", "append_body": "appended by an agent"})
    body = LedgerStore(ws.config.store_path).read("first-item").body
    assert "original" in body and "appended by an agent" in body


def test_a_refused_write_leaves_disk_byte_identical(scratch_workspace):
    ws, name = scratch_workspace
    before = (ws.config.store_path / "ledger" / "first-item.md").read_text()
    call_tool(build_server(ws), "spoke_update",
              {"name": "first-item", "state": "abandoned"})   # no ruling
    assert (ws.config.store_path / "ledger" / "first-item.md").read_text() == before


def test_a_write_records_llm_provenance_not_human(scratch_workspace):
    # A node an agent wrote must never read as a person's decision.
    ws, name = scratch_workspace
    call_tool(build_server(ws), "spoke_record", {
        "name": "agent-wrote-this", "type": "item", "state": "open",
        "title": "T", "body": "b", "by": "mcp:some-agent"})
    node = LedgerStore(ws.config.store_path).read("agent-wrote-this")
    assert any("mcp:" in str(p) or "llm:" in str(p) for p in node.provenance), node.provenance


def test_an_agents_write_is_validated_against_the_projects_axes(scratch_workspace):
    """The gap that could not be closed from where the MCP server stood.

    `build_server` used to take a project NAME and a `Config`. A name is
    not a project and `Config` carries no axes, so the agent-facing write
    path had no route to them -- `LedgerStore.write`'s axes invariant was
    not merely forgotten here, it was unreachable. Taking a Workspace
    makes an agent's write meet exactly the gate a human's write meets.
    """
    from dataclasses import replace as _replace
    from datetime import date
    from spoke.ledger.scale import Axis, Score
    from spoke.ledger.store import LedgerStore
    from spoke.projects import Project
    from spoke.workspace import Workspace

    ws, name = scratch_workspace
    store = LedgerStore(ws.config.store_path)
    scored = _replace(
        store.read("first-item"),
        scores=(Score("reliability", 1, "measured", on="2026-09-08"),),
    )
    assert store.write(scored, (Axis("reliability", "Reliability", 30),)).ok

    # the project no longer declares that axis
    narrowed = Workspace(
        config=ws.config,
        project=Project(name, (), axes=(Axis("editability", "Editability", 20),)),
        today=date(2026, 9, 10),
    )
    before = (ws.config.store_path / "ledger" / "first-item.md").read_text()
    out = call_tool(build_server(narrowed), "spoke_update", {
        "name": "first-item", "state": "done",
    })
    assert "refused" in out and "has not declared" in out, out
    assert (ws.config.store_path / "ledger" / "first-item.md").read_text() == before


def test_a_write_tool_reports_a_failed_push_in_its_text(tmp_path, monkeypatch):
    """The agent reads only the tool's text; 'wrote' alone would be the
    CLI's silence again, one surface over."""
    from tests.test_ledger_store import _repo_with_upstream
    from spoke.mcp_server import _after_write
    from spoke.ledger.store import WriteResult
    assert _after_write(WriteResult(ok=True, pushed=True), "wrote x") == "wrote x"
    text = _after_write(WriteResult(ok=True, pushed=False, advisories=["git-push-failed: no"]), "wrote x")
    assert text.startswith("wrote x\n") and "push failed - git-push-failed: no" in text and "local only" in text
