"""The first minute of an install, through the surface an installer sees.

Before this, `spoke serve` with no store refused to start and printed a
ConfigError; the first thing an installer met was the terminal telling
them to edit a file. Now the server starts unconfigured, every page
sends them to /setup, and four steps -- each an action on the page --
bring the map from nothing to something the next session is handed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from spoke.config import Config, load_config_file
from spoke.server import create_app
from tests.conftest import build_repo, build_store


def _bare_app(tmp_path, claude_projects: Path | None = None):
    """A server as `serve` builds it with no config file at all."""
    cfg = load_config_file(tmp_path / "config.toml")  # store_path == Path("")
    app = create_app(cfg, config_path=tmp_path / "config.toml",
                     registry_path=tmp_path / "projects.toml")
    return TestClient(app)


def test_an_unconfigured_server_starts_and_sends_every_page_to_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(tmp_path / "claude"))
    c = _bare_app(tmp_path)
    assert c.get("/api/health").json() == {"app": "spoke", "store": None, "first_run": True}
    for path in ("/", "/board", "/scale"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/setup", path
    assert c.get("/settings").status_code == 200, "settings stays reachable"
    page = c.get("/setup").text
    assert 'id="step-store"' in page and "<!--MARK-->" not in page
    # The install's name, not the package's, leads the page; and the bar
    # reaches every page, as on the other four.
    assert "<!--BRAND-->" not in page and 'href="/board"' in page and 'href="/scale"' in page
    js = c.get("/static/setup.js").text
    assert "Spoke is set up" not in js, "the headline takes the brand from the bar"
    d = c.get("/api/setup").json()
    assert d["next"] == "store"
    assert d["store"] == {"path": None, "ok": False, "error": "no memory store configured"}


def test_the_page_offers_the_stores_already_on_this_machine(tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    build_store(claude / "-one" / "memory")
    (claude / "-empty" / "memory").mkdir(parents=True)  # no records: not offered
    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(claude))
    c = _bare_app(tmp_path)
    cands = c.get("/api/setup").json()["store_candidates"]
    assert [x["project_dir"] for x in cands] == ["-one"]
    assert cands[0]["records"] >= 1


def test_the_four_steps_in_order_through_the_surface(tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    store = build_store(claude / "-demo" / "memory")
    repo = build_repo(tmp_path / "repo", {
        "pyproject.toml": '[project]\nname = "demo"\n',
        "docs/adr/0001-one.md": "# One\n\n**Status:** accepted\n",
    })
    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(claude))
    c = _bare_app(tmp_path)

    # 1. store -- through the ONE writer of config.toml
    r = c.post("/api/config", json={"store_path": str(store), "accepted_absences": [],
                                    "stale_days": 30, "llm_provider": "groq"})
    assert r.status_code == 200, r.text
    d = c.get("/api/setup").json()
    assert d["store"]["ok"] and d["next"] == "project"
    assert c.get("/board", follow_redirects=False).status_code == 200, "no longer redirected"
    assert c.get("/api/health").json()["first_run"] is False

    # 2. project -- and it CARRIES the store, or the very next terminal
    #    step ("projects axes") fails to resolve it
    r = c.post("/api/projects", json={"name": "demo", "repos": [str(repo)]})
    assert r.status_code == 200, r.text
    assert r.json()["verb"] == "registered"
    from spoke.projects import load_registry
    proj = load_registry(tmp_path / "projects.toml")["demo"]
    assert proj.memory_store == store
    assert c.get("/api/setup").json()["next"] == "content"

    # 3. content -- preview writes nothing; write writes
    r = c.post("/api/scan", json={"write": False})
    assert r.status_code == 200, r.text
    assert r.json()["counts"]["proposals"] >= 2
    assert c.get("/api/setup").json()["ledger_nodes"] == 0, "a preview writes nothing"
    r = c.post("/api/scan", json={"write": True})
    assert r.status_code == 200, r.text
    assert r.json()["counts"]["written"] >= 2
    d = c.get("/api/setup").json()
    assert d["ledger_nodes"] >= 2 and d["next"] == "axes"

    # 4. axes -- declared on the terminal, noticed by the page
    subprocess.run(
        # sys.executable, not a hardcoded <repo>/.venv/bin/python: that path is a
        # local dev layout. CI installs with `pip install -e .` into the runner's
        # own interpreter and has no .venv, so this step raised FileNotFoundError
        # on every CI run while passing locally -- a green local suite over a red
        # gate nobody was reading.
        [sys.executable, "-m", "spoke.cli",
         "projects", "axes", "--project", "demo", "--set", "reliability=Reliability:30"],
        check=True, capture_output=True, env={"SPOKE_REGISTRY": str(tmp_path / "projects.toml"),
                                               "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        cwd=tmp_path,
    )
    d = c.get("/api/setup").json()
    assert d["axes"] == 1 and d["next"] == "done"
    # and the scale draws the axis just declared, in the same process
    assert [a["key"] for a in c.get("/api/scale").json()["axes"]] == ["reliability"]


def test_register_refuses_what_is_not_a_directory_and_grows_an_existing_project(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(tmp_path / "claude"))
    store = build_store(tmp_path / "store")
    c = TestClient(create_app(Config(store, (), 30, "groq", None),
                              config_path=tmp_path / "config.toml",
                              registry_path=tmp_path / "projects.toml"))
    assert c.post("/api/projects", json={"name": "x", "repos": [str(tmp_path / "nope")]}).status_code == 422
    assert c.post("/api/projects", json={"name": "", "repos": [str(tmp_path)]}).status_code == 422
    a = tmp_path / "a"; a.mkdir(); b = tmp_path / "b"; b.mkdir()
    assert c.post("/api/projects", json={"name": "x", "repos": [str(a)]}).json()["verb"] == "registered"
    assert c.post("/api/projects", json={"name": "x", "repos": [str(b)]}).json()["verb"] == "grown"
    assert c.post("/api/projects", json={"name": "x", "repos": [str(b)]}).json()["verb"] == "unchanged"


def test_scan_refuses_without_a_store_or_a_project(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(tmp_path / "claude"))
    c = _bare_app(tmp_path)
    assert c.post("/api/scan", json={"write": True}).status_code == 409
    store = build_store(tmp_path / "store")
    c.post("/api/config", json={"store_path": str(store), "accepted_absences": [],
                                "stale_days": 30, "llm_provider": "groq"})
    r = c.post("/api/scan", json={"write": True})
    assert r.status_code == 409 and "register one first" in r.json()["detail"]


def test_the_board_says_why_it_is_blank_and_tells_three_reasons_apart(tmp_path):
    """A blank canvas that does not say why is indistinguishable from a
    page that failed. The server sends the counts that tell an empty
    ledger, a type nobody has recorded, and a filter that excludes
    everything apart; the page only phrases them."""
    from tests.conftest import seed_nodes
    from spoke.ledger import Node

    store = build_store(tmp_path / "store")
    c = TestClient(create_app(Config(store, (), 30, "groq", None)))
    page = c.get("/board").text
    assert 'id="empty"' in page and 'id="empty-why"' in page
    js = c.get("/static/board.js").text
    assert "function renderEmpty" in js and '"/setup"' in js

    # empty ledger
    d = c.get("/api/lens?name=product").json()
    assert d["ledger_total"] == 0 and d["hub_type_count"] == 0 and d["nodes"] == []

    # a product exists; the client lens has nothing of its type
    seed_nodes(store, [Node(name="p", type="product", state="open", title="p", body="",
                            relations=(), ruling=None, blocked_by=(), provenance=(),
                            opened=None, updated=None, by=None, claimed_by=None, claimed_at=None)])
    d = c.get("/api/lens?name=client").json()
    assert d["ledger_total"] == 1 and d["hub_type_count"] == 0 and d["hub_types"] == ["client"]
    # the product lens has its type but a filter excludes it
    d = c.get("/api/lens?name=product&flag=blocked").json()
    assert d["ledger_total"] == 1 and d["hub_type_count"] == 1 and d["nodes"] == []


def test_axes_are_declared_on_the_settings_screen(tmp_path, monkeypatch):
    """The last thing only a terminal could reach. A full-set write in
    reading order, validated as `projects axes` validates, read back by
    the scale in the same process."""
    from spoke.projects import add_project, load_registry

    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(tmp_path / "claude"))
    store = build_store(tmp_path / "store")
    reg = tmp_path / "projects.toml"
    add_project(reg, "p", [tmp_path])
    c = TestClient(create_app(Config(store, (), 30, "groq", None),
                              config_path=tmp_path / "config.toml", registry_path=reg))

    d = c.get("/api/axes").json()
    assert d["project"] == "p" and d["axes"] == [] and d["scale_max"] == 5

    body = {"project": "p", "scale_max": 4, "axes": [
        {"key": "coverage", "label": "Coverage", "weight": 25},
        {"key": "reliability", "label": "", "weight": 8},   # label defaults to the key
    ]}
    r = c.post("/api/axes", json=body)
    assert r.status_code == 200, r.text
    assert [a["key"] for a in r.json()["axes"]] == ["coverage", "reliability"]
    assert r.json()["axes"][1]["label"] == "reliability"
    assert r.json()["axes"][0]["share"] == round(25 / 33, 4)

    got = load_registry(reg)["p"]
    assert [a.key for a in got.axes] == ["coverage", "reliability"] and got.scale_max == 4
    # the scale reads them in the same process
    s = c.get("/api/scale").json()
    assert [a["key"] for a in s["axes"]] == ["coverage", "reliability"] and s["max_value"] == 4
    # reversed order is kept as sent -- reading order is the project's
    body["axes"].reverse()
    c.post("/api/axes", json=body)
    assert [a.key for a in load_registry(reg)["p"].axes] == ["reliability", "coverage"]

    # refusals: duplicate key, bad key, negative weight, unknown project, max < 1
    bad = lambda **kw: c.post("/api/axes", json={**body, **kw}).status_code
    assert bad(axes=[{"key": "a", "weight": 1}, {"key": "a", "weight": 1}]) == 422
    assert bad(axes=[{"key": "not ok", "weight": 1}]) == 422
    assert bad(axes=[{"key": "a", "weight": -1}]) == 422
    assert bad(project="nope") == 422
    assert bad(scale_max=0) == 422
    assert [a.key for a in load_registry(reg)["p"].axes] == ["reliability", "coverage"], "a refused save changes nothing"

    page = c.get("/settings").text
    assert 'id="axes-form"' in page and 'id="axes-rows"' in page
    setup = c.get("/setup").text
    assert 'href="/settings#axes"' in setup


# -- the third door: sample data ------------------------------------------
#
# The two ways in both assume the installer already has something to point
# at -- a store, or repos worth scanning. Someone who has just cloned Spoke
# has neither, and the honest answer to "what does this actually do" was a
# CLI command in a README. `spoke demo` builds a complete map from two real
# public repos; this puts it on the page.

def test_the_demo_can_be_built_from_the_setup_page(tmp_path, monkeypatch):
    """The whole point: no terminal, no store of their own, nothing of
    theirs touched -- and it hands back the one command that serves it."""
    from spoke import demo as demo_mod
    from tests.conftest import build_repo

    # Real repos, locally: the endpoint must not reach the network in a test,
    # and `build` already takes local paths for exactly this reason.
    tare = build_repo(tmp_path / "tare", {"README.md": "# Tare\n", "package.json": '{"name":"tare"}'})
    forage = build_repo(tmp_path / "forage", {"README.md": "# Forage\n", "pyproject.toml": "[project]\nname='forage'\n"})
    real_build = demo_mod.build
    monkeypatch.setattr(demo_mod, "build",
                        lambda dest, repos=None, today=None: real_build(
                            dest, {"tare": tare, "forage": forage}, today))

    c = _bare_app(tmp_path)
    dest = tmp_path / "demo"
    r = c.post("/api/demo", json={"dir": str(dest)})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["ok"] is True
    assert body["nodes"] > 0, "a demo with nothing on the map is not a demo"
    # It carries the demo's own registry in the environment, so it cannot
    # pick up the user's: "SPOKE_REGISTRY=<demo>/projects.toml spoke serve --config ..."
    assert "spoke serve" in body["serve_command"]
    assert "SPOKE_REGISTRY=" in body["serve_command"]
    assert str(dest) in body["serve_command"], "the command must name the demo, not the user's own store"
    assert Path(body["store"]).is_dir()

    # Nothing of the installer's was touched: this server is still unconfigured.
    assert not (tmp_path / "config.toml").exists()
    assert c.get("/api/setup").json()["store"]["ok"] is False


def test_a_demo_that_cannot_be_built_says_why_and_writes_nothing(tmp_path, monkeypatch):
    """A failed clone is the likely case (no network on a fresh machine),
    and it must read as a refusal with a reason, not a 500."""
    from spoke import demo as demo_mod

    def boom(dest, repos=None, today=None):
        raise demo_mod.DemoError("could not clone tare: network is unreachable")
    monkeypatch.setattr(demo_mod, "build", boom)

    c = _bare_app(tmp_path)
    r = c.post("/api/demo", json={"dir": str(tmp_path / "demo")})
    assert r.status_code == 409, r.text
    assert "network is unreachable" in r.json()["detail"]


def test_the_setup_page_offers_all_three_doors(tmp_path):
    """The page itself must name the third way in -- an endpoint nothing
    links to is a CLI command with extra steps."""
    c = _bare_app(tmp_path)
    html = c.get("/setup").text
    assert 'id="step-demo"' in html
    assert "sample data" in html.lower()
    # and the two questions a first-timer actually asks are answered in place
    assert html.count("<details") >= 2, "the longer explanations must be on the page, not a docs link"
