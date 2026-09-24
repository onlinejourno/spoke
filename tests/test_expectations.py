"""Expectations and checklists: question 4 of the task ledger made
checkable by machine, and the four questions carried on a node until
each is ticked."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from spoke.checks.expect import check_expectations, check_one
from spoke.cli import FOUR_QUESTIONS, main
from spoke.clock import Clock
from spoke.config import Config
from spoke.ledger import Node
from spoke.ledger.flags import compute_flags
from spoke.ledger.graph import LENSES, load_graph
from spoke.ledger.schema import validate
from spoke.ledger.store import LedgerStore
from spoke.server import create_app
from tests.conftest import build_store, seed_nodes


class _Resp:
    def __init__(self, status=200, text="", headers=None):
        self.status_code = status; self.text = text; self.headers = headers or {}


class _Client:
    """A fake HTTP client: url -> _Resp (or an exception to raise)."""
    def __init__(self, routes): self.routes = routes; self.calls = []
    def get(self, url, follow_redirects=False):
        self.calls.append(url)
        r = self.routes.get(url)
        if isinstance(r, Exception): raise r
        return r or _Resp(404)


def _n(name, **kw):
    base = dict(name=name, type="item", state="open", title=name, body="", relations=(),
                ruling=None, blocked_by=(), provenance=(), opened=None, updated=None,
                by=None, claimed_by=None, claimed_at=None)
    return Node(**{**base, **kw})


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _guard_that_only_blocks_metadata(monkeypatch):
    """The real guard resolves hostnames, and the fake hosts here do not
    resolve. Keep the guard's shape -- it must still refuse a private
    address -- without a network."""
    monkeypatch.setattr("spoke.checks.expect.is_safe_url",
                        lambda url: "169.254.169.254" not in url and "://10." not in url)


def test_an_expectation_says_what_must_hold_and_the_probe_says_which_clause_failed():
    c = _Client({
        "https://ok.example/brief": _Resp(200, "Today's brief", {"last-modified": "Sat, 13 Sep 2026 06:00:00 GMT"}),
        "https://stale.example/brief": _Resp(200, "old", {"last-modified": "Mon, 01 Jun 2026 06:00:00 GMT"}),
        "https://undated.example/": _Resp(200, "x"),
        "https://down.example/": _Resp(503),
        "https://boom.example/": RuntimeError("connection refused"),
    })
    assert check_one({"url": "https://ok.example/brief", "contains": "brief", "fresh_within": "36h"}, c, NOW) == \
        (True, "HTTP 200; contains 'brief'; modified 6h ago")
    met, why = check_one({"url": "https://stale.example/brief", "fresh_within": "2d"}, c, NOW)
    assert not met and "expected within 2d" in why
    met, why = check_one({"url": "https://undated.example/", "fresh_within": "1d"}, c, NOW)
    assert not met and "no Last-Modified" in why, "no date is not fresh"
    assert check_one({"url": "https://down.example/", "status": 200}, c, NOW) == (False, "HTTP 503, expected 200")
    met, why = check_one({"url": "https://ok.example/brief", "contains": "tomorrow"}, c, NOW)
    assert not met and "does not contain" in why
    met, why = check_one({"url": "https://boom.example/"}, c, NOW)
    assert not met and "connection refused" in why
    # private addresses never get fetched: the SSRF guard the citation probe uses
    met, why = check_one({"url": "http://169.254.169.254/latest"}, c, NOW)
    assert not met and "SSRF" in why and "http://169.254.169.254/latest" not in c.calls


def test_the_probe_writes_what_it_found_back_and_the_flag_is_read_from_it(tmp_path):
    store = build_store(tmp_path / "store")
    seed_nodes(store, [_n("brief-appears-daily",
                          expects=({"url": "https://x.example/brief", "fresh_within": "36h"},))])
    ledger = LedgerStore(store)
    clock = Clock(date(2026, 9, 13), 30)

    # never checked: unmet, and says so
    g, _ = load_graph(ledger)
    flags = compute_flags(g, LENSES["time"], clock)["brief-appears-daily"]
    assert [f.kind for f in flags] == ["unmet"] and "never checked" in flags[0].detail

    # a failing probe: recorded on the node, reported as a finding
    c = _Client({"https://x.example/brief": _Resp(200, "old", {"last-modified": "Mon, 01 Jun 2026 06:00:00 GMT"})})
    findings = check_expectations(ledger, c, date(2026, 9, 13), now=NOW)
    assert [(f.kind, f.file) for f in findings] == [("unmet", "brief-appears-daily")]
    last = ledger.read("brief-appears-daily").expects[0]["last"]
    assert last["ok"] is False and "expected within 36h" in last["detail"] and last["by"] == "probe:spoke-doctor"
    g, _ = load_graph(ledger)
    flags = compute_flags(g, LENSES["time"], clock)["brief-appears-daily"]
    assert [f.kind for f in flags] == ["unmet"] and "expected within 36h" in flags[0].detail

    # a passing probe: no finding, no flag, and the check is still recorded
    c = _Client({"https://x.example/brief": _Resp(200, "new", {"last-modified": "Sat, 13 Sep 2026 06:00:00 GMT"})})
    assert check_expectations(ledger, c, date(2026, 9, 13), now=NOW) == []
    last = ledger.read("brief-appears-daily").expects[0]["last"]
    assert last["ok"] is True and last["at"].startswith("2026-09-13T12:00")
    g, _ = load_graph(ledger)
    assert compute_flags(g, LENSES["time"], clock).get("brief-appears-daily", ()) == ()


def test_an_expectation_with_no_clause_or_a_bad_url_is_refused_at_the_gate():
    bad = _n("x", expects=({"url": "https://a.example/"},))
    assert any("say what is expected" in r for r in validate(bad))
    bad = _n("x", expects=({"url": "ftp://a.example/", "status": 200},))
    assert any("http(s)" in r for r in validate(bad))
    bad = _n("x", expects=({"url": "https://a.example/", "fresh_within": "soon"},))
    assert any("36h" in r for r in validate(bad))
    ok = _n("x", expects=({"url": "https://a.example/", "status": 200, "fresh_within": "2d"},))
    assert validate(ok) == []


def test_a_node_cannot_close_with_an_unchecked_item_and_shows_the_flag_until_then():
    n = _n("task", state="done", checklist=({"text": "prove it", "done": False},))
    assert any("unchecked checklist item" in r for r in validate(n))
    n = _n("task", state="done", checklist=({"text": "prove it", "done": True},))
    assert validate(n) == []
    g, _ = load_graph(type("S", (), {"list_nodes": lambda self: [
        _n("task", checklist=({"text": "a", "done": True}, {"text": "b", "done": False}))]})())
    flags = compute_flags(g, LENSES["time"], Clock(date(2026, 9, 13), 30))["task"]
    assert [f.kind for f in flags] == ["unchecked"] and "1 of 2" in flags[0].detail


def test_the_cli_seeds_the_four_questions_ticks_them_and_states_an_expectation(tmp_path, monkeypatch, capsys):
    store = build_store(tmp_path / "store")
    monkeypatch.setenv("SPOKE_STORE_PATH", str(store))
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "reg.toml"))
    cfg = tmp_path / "config.toml"; cfg.write_text(f'[store]\npath = "{store}"\n')
    args = ["--config", str(cfg)]
    assert main(["ledger", "new", "fix-it", "--type", "item", "--state", "open", "--title", "Fix it",
                 "--body", "b", "--questions", "--check", "tell the founder", *args]) == 0
    node = LedgerStore(store).read("fix-it")
    assert [c["text"] for c in node.checklist] == list(FOUR_QUESTIONS) + ["tell the founder"]
    # cannot close it
    assert main(["ledger", "set", "fix-it", "--state", "done", *args]) != 0
    assert "unchecked" in capsys.readouterr().out
    for i in range(1, 6):
        assert main(["ledger", "tick", "fix-it", str(i), "--note", f"done {i}", *args]) == 0
    assert "all checked" in capsys.readouterr().out
    assert main(["ledger", "set", "fix-it", "--state", "done", *args]) == 0
    # an expectation, then shown
    assert main(["ledger", "expect", "fix-it", "--url", "https://x.example/", "--contains", "ok", *args]) == 0
    capsys.readouterr()
    assert main(["ledger", "show", "fix-it", *args]) == 0
    out = capsys.readouterr().out
    assert "[x] 1." in out and "expects: https://x.example/ (contains=ok) -- never checked" in out


def test_a_report_from_a_page_carries_the_four_questions(tmp_path):
    store = build_store(tmp_path / "store")
    c = TestClient(create_app(Config(store, (), 30, "groq", None)))
    name = c.post("/api/report", json={"page": "/board", "text": "the key is hidden"}).json()["name"]
    node = LedgerStore(store).read(name)
    assert [x["text"] for x in node.checklist] == list(FOUR_QUESTIONS)
    d = c.get("/api/lens?name=time").json()
    me = next(n for n in d["nodes"] if n["name"] == name)
    assert len(me["checklist"]) == 4 and any(f["kind"] == "unchecked" for f in me["flags"])


def test_freshness_can_be_read_from_a_date_the_page_states_after_a_marker():
    """None of the estate's pages sends Last-Modified; every Watch prints
    'Beat: tech · 2026-09-14' in the body. That is the brief's own date."""
    c = _Client({
        "https://w.example/": _Resp(200, "Daily Brief · tech  Beat: tech · 2026-09-13  20 shortlisted"),
        "https://old.example/": _Resp(200, "Beat: law · 2026-06-01"),
        "https://nodate.example/": _Resp(200, "Beat: law · soon"),
    })
    met, why = check_one({"url": "https://w.example/", "fresh_within": "36h", "dated_by": "Beat: tech · "}, c, NOW)
    assert met and why.endswith("dated 2026-09-13")
    met, why = check_one({"url": "https://old.example/", "fresh_within": "36h", "dated_by": "Beat: law · "}, c, NOW)
    assert not met and "expected within 36h" in why
    met, why = check_one({"url": "https://nodate.example/", "fresh_within": "36h", "dated_by": "Beat: law · "}, c, NOW)
    assert not met and "no date follows" in why
    assert any("both are needed" in r for r in validate(_n("x", expects=({"url": "https://a.example/", "dated_by": "Beat: "},))))


# -- local expectations: a repo's worktree count ---------------------------
#
# Sessions isolate into a worktree per task and walk away; nothing reaps
# them, so the count only grows and nothing turns red. An expectation
# about the count is the telling.

def _repo_with_worktrees(root, n):
    import subprocess
    repo = root / "repo"
    repo.mkdir()
    def git(*a, cwd=repo):
        subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@example", "commit", "-q", "--allow-empty", "-m", "root")
    for i in range(n):
        git("worktree", "add", "-q", "--detach", str(root / f"wt-{i}"))
    return repo


def test_a_repo_can_expect_a_ceiling_on_its_linked_worktrees(tmp_path):
    repo = _repo_with_worktrees(tmp_path, 3)
    met, why = check_one({"path": str(repo), "worktrees_max": 5}, None, NOW)
    assert met and why == "3 linked worktrees (at most 5)"
    met, why = check_one({"path": str(repo), "worktrees_max": 2}, None, NOW)
    assert not met and why == "3 linked worktrees, expected at most 2"


def test_a_worktree_expectation_that_cannot_be_read_is_unmet_not_skipped(tmp_path):
    met, why = check_one({"path": str(tmp_path / "gone"), "worktrees_max": 5}, None, NOW)
    assert not met and "no such directory" in why
    plain = tmp_path / "plain"; plain.mkdir()
    met, why = check_one({"path": str(plain), "worktrees_max": 5}, None, NOW)
    assert not met and "not a git repository" in why.lower()


def test_a_worktree_expectation_is_shaped_at_the_gate():
    ok = _n("x", expects=({"path": "/somewhere/repo", "worktrees_max": 5},))
    assert validate(ok) == []
    bad = _n("x", expects=({"path": "/somewhere/repo"},))
    assert any("worktrees_max" in r for r in validate(bad))
    bad = _n("x", expects=({"path": "/somewhere/repo", "worktrees_max": "many"},))
    assert any("whole number" in r for r in validate(bad))
    bad = _n("x", expects=({"path": "/somewhere/repo", "url": "https://a.example/", "status": 200},))
    assert any("one or the other" in r for r in validate(bad))
    bad = _n("x", expects=({"path": "relative/repo", "worktrees_max": 5},))
    assert any("absolute" in r for r in validate(bad))


def test_the_worktree_probe_writes_back_and_the_flag_names_the_path(tmp_path):
    repo = _repo_with_worktrees(tmp_path, 2)
    store = build_store(tmp_path / "store")
    seed_nodes(store, [_n("some-repo", expects=({"path": str(repo), "worktrees_max": 1},))])
    ledger = LedgerStore(store)
    findings = check_expectations(ledger, _Client({}), date(2026, 9, 13), now=NOW)
    assert [(f.kind, f.file) for f in findings] == [("unmet", "some-repo")]
    assert str(repo) in findings[0].detail and "expected at most 1" in findings[0].detail
    g, _ = load_graph(ledger)
    flags = compute_flags(g, LENSES["time"], Clock(date(2026, 9, 13), 30))["some-repo"]
    assert [f.kind for f in flags] == ["unmet"] and str(repo) in flags[0].detail


def test_the_cli_states_a_worktree_expectation(tmp_path, monkeypatch, capsys):
    repo = _repo_with_worktrees(tmp_path, 4)
    store = build_store(tmp_path / "store")
    seed_nodes(store, [_n("some-repo")])
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "reg.toml"))
    cfg = tmp_path / "config.toml"; cfg.write_text(f'[store]\npath = "{store}"\n')
    # a path needs a clause, and url/path is one or the other
    assert main(["ledger", "expect", "some-repo", "--path", str(repo), "--config", str(cfg)]) != 0
    assert main(["ledger", "expect", "some-repo", "--path", str(repo), "--url", "https://x.example/",
                 "--worktrees-max", "3", "--config", str(cfg)]) != 0
    capsys.readouterr()
    assert main(["ledger", "expect", "some-repo", "--path", str(repo), "--worktrees-max", "3",
                 "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert f"some-repo expects {repo} (worktrees_max=3)" in out
    assert main(["ledger", "show", "some-repo", "--config", str(cfg)]) == 0
    assert f"expects: {repo} (worktrees_max=3) -- never checked" in capsys.readouterr().out


# -- the watcher itself, watched -------------------------------------------
#
# compute_flags asked whether the last probe passed, never when it ran.
# So if the scheduled doctor stops -- billing, a broken plist, a laptop
# shut for a month -- every expectation keeps reading "met" on the
# strength of one old success, and nothing goes red. That is the exact
# silence expectations exist to remove, one level up.

def test_a_probe_timestamp_is_a_date_the_clock_understands():
    """The prober writes '2026-09-13T12:00:00+00:00'. A parser that
    returns None for the format this system writes would have made the
    age check below silently never fire."""
    from spoke.ledger import parse_iso_date
    assert parse_iso_date("2026-09-13T12:00:00+00:00") == date(2026, 9, 13)
    assert parse_iso_date("2026-09-13T12:00:00Z") == date(2026, 9, 13)
    assert parse_iso_date("2026-09-13") == date(2026, 9, 13)
    assert parse_iso_date("nonsense") is None and parse_iso_date(None) is None


def _flags_for(expect, today=date(2026, 9, 13)):
    g, _ = load_graph(type("S", (), {"list_nodes": lambda self: [
        _n("watched", expects=(expect,))]})())
    return compute_flags(g, LENSES["time"], Clock(today, 30)).get("watched", ())


def _probed(ok, at):
    return {"url": "https://x.example/brief", "fresh_within": "36h",
            "last": {"at": at, "ok": ok, "detail": "HTTP 200", "by": "probe:spoke-doctor"}}


def test_a_met_expectation_goes_unverified_once_the_probe_stops():
    """Not 'unmet' in the sense of the thing having failed -- unverified.
    The detail has to say which, or a reader chases the wrong problem."""
    fresh = _flags_for(_probed(True, "2026-09-13T06:00:00+00:00"))
    assert fresh == (), "a probe from this morning is not stale"

    old = _flags_for(_probed(True, "2026-08-14T06:00:00+00:00"))
    assert [f.kind for f in old] == ["unmet"]
    detail = old[0].detail
    assert "30d ago" in detail, detail
    assert "last passed" in detail or "not been checked" in detail, detail


def test_the_probe_window_follows_what_the_expectation_asks_for():
    """An expectation that wants a page fresh within 36h cannot be
    answered by a probe from last week; one with no freshness clause
    falls back to the constant."""
    from spoke.clock import PROBE_WINDOW_DAYS

    within = {"url": "https://x.example/", "status": 200,
              "last": {"at": "2026-09-12T06:00:00+00:00", "ok": True, "detail": "HTTP 200"}}
    assert _flags_for(within) == (), f"a probe {1}d old is inside the {PROBE_WINDOW_DAYS}d default"

    beyond = dict(within, last={"at": "2026-09-01T06:00:00+00:00", "ok": True, "detail": "HTTP 200"})
    assert [f.kind for f in _flags_for(beyond)] == ["unmet"]


def test_a_probe_whose_date_cannot_be_read_is_not_taken_as_current():
    """Unreadable is not recent. The alternative is a single malformed
    timestamp buying an expectation permanent silence."""
    bad = _probed(True, "some time last week")
    flags = _flags_for(bad)
    assert [f.kind for f in flags] == ["unmet"]
    assert "when" in flags[0].detail.lower() or "unreadable" in flags[0].detail.lower()


def test_a_failing_probe_still_reports_the_failure_not_its_age():
    """A probe that ran today and said no must keep saying no -- the age
    check must not overwrite the reason."""
    failed = {"url": "https://x.example/", "status": 200,
              "last": {"at": "2026-09-13T06:00:00+00:00", "ok": False, "detail": "HTTP 503, expected 200"}}
    flags = _flags_for(failed)
    assert [f.kind for f in flags] == ["unmet"]
    assert "503" in flags[0].detail
