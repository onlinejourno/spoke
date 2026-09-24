"""What one surface can do, the others can do too -- or say why not.

Three surfaces render the same facts: the CLI, the HTTP API and the MCP
server. They had drifted into different CAPABILITIES, not just different
wording: the browser could widen a matrix and the terminal could not, an
unknown node type was refused by one and answered by another, and an
agent could write to the ledger but could not ask it anything.

Wording is deliberately NOT asserted here. Two ledger decisions
(`hub-not-spine`, `no-renames-of-shipped-names`) hold the vocabulary
still, and pinning prose would make those holds harder to keep, not
easier.
"""
import subprocess

from tests.conftest import build_store
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spoke.cli import main
from spoke.config import Config
from spoke.ledger import NODE_TYPES
from spoke.mcp_server import build_server, call_tool
from spoke.server import create_app


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _store(root: Path) -> Path:
    return build_store(root)


@pytest.fixture
def surfaces(tmp_path, monkeypatch):
    """One store, reachable through all three surfaces."""
    from datetime import date
    from spoke.workspace import Workspace

    store = _store(tmp_path / "store")
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    monkeypatch.delenv("SPOKE_STORE_PATH", raising=False)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f'[store]\npath = "{store}"\n\n[checks]\nstale_days = 30\n')

    for name, type_ in (("widget", "product"), ("gate", "capability")):
        assert main(["ledger", "new", name, "--type", type_, "--state", "open",
                     "--title", name, "--body", "why", "--config", str(cfg_file)]) == 0
    assert main(["ledger", "new", "wire", "--type", "item", "--state", "open",
                 "--title", "wire", "--body", "why", "--rel", "touches:widget",
                 "--rel", "touches:gate", "--config", str(cfg_file)]) == 0

    cfg = Config(store, (), 30, "groq", None)
    ws = Workspace(config=cfg, project=None, today=date(2026, 9, 10))
    return cfg_file, TestClient(create_app(cfg)), build_server(ws)


# ── capabilities that existed on one surface only ─────────────────────

def test_the_terminal_can_widen_a_matrix_like_the_browser_can(surfaces, capsys):
    """`/api/matrix` has always taken `hops`; `ledger matrix` had no flag
    for it, so the browser could ask a question the terminal could not."""
    cfg_file, http, _srv = surfaces
    capsys.readouterr()
    assert main(["ledger", "matrix", "product", "capability", "--hops", "2",
                 "--config", str(cfg_file)]) == 0
    assert "gate: 1 of 1" in capsys.readouterr().out
    assert http.get("/api/matrix?rows=product&cols=capability&hops=2").status_code == 200


def test_an_agent_can_ask_what_it_is_about_to_change(surfaces):
    """Six tools against twenty-three commands, and the six were the ones
    that write or list -- so the surface that most needed to check itself
    before acting was the one that could not."""
    _cfg, _http, srv = surfaces
    out = call_tool(srv, "spoke_lens", {"name": "product"})
    assert "hub widget" in out and "spokes: wire" in out

    out = call_tool(srv, "spoke_matrix", {"rows": "product", "cols": "capability"})
    assert "gate: 1 of 1" in out

    out = call_tool(srv, "spoke_scale", {})
    assert "no capability axes" in out          # a refusal IS the answer here


def test_every_surface_refuses_an_unknown_lens(surfaces, capsys):
    cfg_file, http, srv = surfaces
    capsys.readouterr()
    assert main(["ledger", "lens", "nope", "--config", str(cfg_file)]) != 0
    assert "known lenses" in capsys.readouterr().out
    assert http.get("/api/lens?name=nope").status_code == 422
    assert "refused" in call_tool(srv, "spoke_lens", {"name": "nope"})


def test_every_surface_refuses_an_unknown_node_type(surfaces, capsys):
    """`spoke scale --type` used `in` on a string -- a SUBSTRING test that
    agreed with the server only because no member of NODE_TYPES happens
    to be a substring of another. `/api/scale` answered 200 with an empty
    grid, so a typo was indistinguishable from an unscored project."""
    cfg_file, http, srv = surfaces
    capsys.readouterr()
    assert main(["scale", "--type", "nope", "--config", str(cfg_file)]) != 0
    assert "known types" in capsys.readouterr().out
    assert http.get("/api/scale?type=nope").status_code == 422
    assert "refused" in call_tool(srv, "spoke_scale", {"type": "nope"})


def test_a_comma_separated_type_is_not_quietly_accepted(surfaces, capsys):
    """The substring test made `--type "product,capability"` return both
    types in the terminal and none over HTTP."""
    cfg_file, http, _srv = surfaces
    capsys.readouterr()
    assert main(["scale", "--type", "product,capability", "--config", str(cfg_file)]) != 0
    assert http.get("/api/scale?type=product,capability").status_code == 422


def test_the_scale_page_is_told_the_node_types_rather_than_hardcoding_them(surfaces):
    _cfg, http, _srv = surfaces
    assert http.get("/api/scale").json()["node_types"] == list(NODE_TYPES)


def test_every_surface_refuses_the_same_radius(surfaces, capsys):
    """`/api/lens?hops=0` was a 422 and `/api/matrix?hops=0` a 200 on the
    same server in the same second."""
    cfg_file, http, srv = surfaces
    assert http.get("/api/lens?name=product&hops=0").status_code == 422
    assert http.get("/api/matrix?rows=product&cols=capability&hops=0").status_code == 422
    capsys.readouterr()
    assert main(["ledger", "matrix", "product", "capability", "--hops", "0",
                 "--config", str(cfg_file)]) != 0
    assert "at least 1" in capsys.readouterr().out
    assert "at least 1" in call_tool(srv, "spoke_matrix",
                                     {"rows": "product", "cols": "capability", "hops": 0})


def test_the_matrix_tool_reports_what_could_not_be_read(surfaces, tmp_path):
    """An agent asking for coverage was told a number and never told what
    could not be read -- the matrix tool rendered no skips at all."""
    cfg_file, _http, srv = surfaces
    store = tmp_path / "store"
    (store / "ledger" / "broken.md").write_text("---\nname: [unclosed\n---\n\nbody\n")
    out = call_tool(srv, "spoke_matrix", {"rows": "product", "cols": "capability"})
    assert "could not be read" in out and "broken.md" in out


def test_a_dangling_relation_reaches_the_terminal_scale_too(surfaces, tmp_path, capsys):
    """`spoke scale` sourced its nodes from `list_nodes()` while the deep
    module uses `load_graph`, so it never saw a dangling relation at all
    -- the HTTP and MCP scale payloads carried them and the terminal did
    not. Same store, same command, less truth.

    Asserted with NO axes declared, deliberately: an unreadable file is
    worth knowing whether or not anyone has got round to declaring axes,
    and the early return on "no axes" used to swallow it."""
    cfg_file, http, _srv = surfaces
    (tmp_path / "store" / "ledger" / "orphan.md").write_text(
        "---\nname: orphan\ntype: item\nstate: open\ntitle: Orphan\n"
        "relations:\n  - rel: touches\n    to: ghost\n---\n\nbody\n")
    capsys.readouterr()
    main(["scale", "--config", str(cfg_file)])
    out = capsys.readouterr().out
    assert "ghost" in out and "pointed at a node that does not exist" in out


def test_the_terminal_and_an_agent_get_the_identical_lens(surfaces, capsys):
    """The query was derived twice and rendered twice, so the two could
    disagree -- and did. Byte-for-byte the same now."""
    cfg_file, _http, srv = surfaces
    capsys.readouterr()
    assert main(["ledger", "lens", "product", "--config", str(cfg_file)]) == 0
    terminal = [l for l in capsys.readouterr().out.splitlines()
                if not l.startswith("project:")]
    agent = call_tool(srv, "spoke_lens", {"name": "product"}).splitlines()
    assert terminal == agent, (terminal, agent)


def test_an_agent_can_filter_by_flag_like_the_terminal_can(surfaces):
    """The terminal grew `--flag`; the agent-facing surface never got it,
    so the surface that most needs to narrow to what is blocked could
    not."""
    _cfg, _http, srv = surfaces
    out = call_tool(srv, "spoke_lens", {"name": "product", "flag": ["blocked"]})
    assert "no product nodes matching" in out
    assert "refused" not in out


def test_an_unknown_flag_kind_is_refused_on_both_text_surfaces(surfaces, capsys):
    cfg_file, _http, srv = surfaces
    capsys.readouterr()
    assert main(["ledger", "lens", "product", "--flag", "nope",
                 "--config", str(cfg_file)]) != 0
    assert "known kinds" in capsys.readouterr().out
    assert "known kinds" in call_tool(srv, "spoke_lens",
                                      {"name": "product", "flag": ["nope"]})


# -- the chrome is shared, so its gaps are shared too -----------------------

def _static(name: str) -> str:
    from spoke import server as _srv
    return (Path(_srv.__file__).parent / "static" / name).read_text()


def test_every_page_with_a_masthead_loads_the_shared_chrome():
    """chrome.css exists because the masthead rules used to live in two
    page stylesheets and the page loading neither rendered the brand
    wrong. A page that grows a masthead without the sheet re-opens that;
    this counts them so the next one cannot be added quietly."""
    from spoke import server as _srv
    static = Path(_srv.__file__).parent / "static"
    pages = sorted(p.name for p in static.glob("*.html"))
    with_masthead = [n for n in pages if 'class="masthead"' in (static / n).read_text()]
    with_chrome = [n for n in pages if "chrome.css" in (static / n).read_text()]
    assert with_masthead == with_chrome, (
        "these pages disagree about the shared chrome: "
        f"masthead={with_masthead} chrome.css={with_chrome}")
    assert len(with_chrome) == 5, f"expected five chrome pages, found {with_chrome}"


def test_the_tagline_stands_down_on_a_narrow_screen():
    """At 375px the tagline wrapped one word per line and the bar grew to
    286px of an 812px screen -- a third of the phone spent on a sentence
    the brand already implies. Measured on all five pages before this
    rule existed; the rule lives in the shared sheet so no page can be
    fixed while the others stay broken."""
    css = _static("chrome.css")
    assert "@media" in css, "the shared chrome has no narrow-screen rule at all"
    narrow = css[css.index("@media"):]
    assert ".tagline" in narrow and "display: none" in narrow
