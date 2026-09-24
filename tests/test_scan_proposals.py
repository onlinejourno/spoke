from datetime import date
from pathlib import Path
import subprocess

from tests.conftest import build_repo

from spoke.ledger.schema import validate
from spoke.scan import ADR_STATES, scan_repos

TODAY = date(2026, 9, 10)


def _repo(root: Path, files: dict[str, str]) -> Path:
    return build_repo(root, files)



def _demo(tmp_path) -> list[Path]:
    a = _repo(tmp_path / "mono", {
        "apps/alpha/package.json": '{"name": "alpha"}',
        "apps/beta/package.json": '{"name": "beta"}',
        "docs/adr/0001-do-it.md": "# Do it\n\nStatus: Accepted\n",
        "docs/adr/0002-not-that.md": "# Not that\n\nStatus: Rejected\n",
        "README.md": "# mono\n",
        "vendor/nav.js": "shared\n" * 60,
    })
    b = _repo(tmp_path / "solo", {
        "pyproject.toml": '[project]\nname = "solo"\n',
        "lib/nav.js": "shared\n" * 60,
    })
    return [a, b]


def test_every_proposed_node_carries_probe_provenance_and_a_date(tmp_path):
    res = scan_repos(_demo(tmp_path), TODAY)
    assert res.proposals
    for p in res.proposals:
        assert p.node.provenance, p.node.name
        entry = p.node.provenance[0]
        assert entry["by"].startswith("probe:"), entry
        assert entry["on"] == "2026-09-10"


def test_no_proposed_node_reads_as_human_authored(tmp_path):
    res = scan_repos(_demo(tmp_path), TODAY)
    for p in res.proposals:
        assert not any(e["by"].startswith("human:") for e in p.node.provenance)


def test_every_proposed_node_passes_the_gate_the_scan_would_write_it_through(tmp_path):
    """A scan whose own output validate() refuses is not a scan."""
    res = scan_repos(_demo(tmp_path), TODAY)
    for p in res.proposals:
        assert validate(p.node) == [], (p.node.name, validate(p.node))


def test_an_abandoned_decision_always_carries_a_ruling(tmp_path):
    res = scan_repos(_demo(tmp_path), TODAY)
    abandoned = [p for p in res.proposals if p.node.state == "abandoned"]
    assert abandoned
    for p in abandoned:
        assert p.node.ruling and "rejected" in p.node.ruling


def test_an_unmapped_adr_status_is_reported_not_guessed(tmp_path):
    r = _repo(tmp_path / "p", {
        "pyproject.toml": '[project]\nname = "p"\n',
        "docs/adr/0001-x.md": "# X\n\nStatus: Marinating\n",
    })
    res = scan_repos([r], TODAY)
    node = next(p.node for p in res.proposals if p.node.type == "decision")
    assert node.state == "open"
    assert any("marinating" in n.lower() for n in res.notes), res.notes


def test_the_label_comes_from_the_repo_not_from_this_module(tmp_path):
    marker = _repo(tmp_path / "m", {"crates/one/Cargo.toml": '[package]\nname = "one"\n'})
    res = scan_repos([marker], TODAY)
    products = [p for p in res.proposals if p.node.type == "product"]
    assert [p.label for p in products] == ["crate"]
    assert all(p.accepted_label is False for p in products)


def test_an_accepted_vocabulary_is_used_and_not_re_proposed(tmp_path):
    res = scan_repos(_demo(tmp_path), TODAY, vocabulary={"product": "service"})
    products = [p for p in res.proposals if p.node.type == "product"]
    assert {p.label for p in products} == {"service"}
    assert all(p.accepted_label for p in products)
    assert "product" not in res.proposed_vocabulary


def test_two_runs_over_the_same_repos_produce_identical_proposals(tmp_path):
    repos = _demo(tmp_path)
    assert scan_repos(repos, TODAY) == scan_repos(list(reversed(repos)), TODAY)


def test_duplication_becomes_one_shared_node_touching_both_repos_units(tmp_path):
    res = scan_repos(_demo(tmp_path), TODAY)
    shared = [p for p in res.proposals if p.node.type == "capability"]
    assert [p.node.name for p in shared] == ["shared-nav-js"]
    assert {r.to for r in shared[0].node.relations} == {"alpha", "beta", "solo"}


def test_a_missing_repo_is_a_note_not_a_crash(tmp_path):
    res = scan_repos([tmp_path / "nope"], TODAY)
    assert res.proposals == ()
    assert any("does not exist" in n for n in res.notes)


def test_a_repo_with_no_unit_says_so(tmp_path):
    r = _repo(tmp_path / "prose", {"README.md": "# just prose\n"})
    res = scan_repos([r], TODAY)
    assert any("no deployable unit" in n for n in res.notes), res.notes
    # ... and still contributes its documentation
    assert [p.node.name for p in res.proposals] == ["prose-docs"]


def test_the_units_last_commit_date_becomes_the_nodes_updated(tmp_path):
    """So `stale` works without anybody maintaining a field."""
    res = scan_repos(_demo(tmp_path), TODAY)
    product = next(p.node for p in res.proposals if p.node.type == "product")
    assert product.updated and len(product.updated) == 10


def test_only_the_four_conventional_adr_words_are_mapped():
    assert set(ADR_STATES) == {
        "proposed", "draft", "accepted", "rejected", "deprecated", "superseded",
    }


def test_the_scan_withdraws_what_it_owns_and_no_longer_derives():
    """A scan that writes what it finds and never retracts what it stops
    finding leaves the map asserting things no repo says any more. Fix
    a heuristic and every node the old one produced lingers."""
    from dataclasses import replace
    from datetime import date

    from spoke.ledger import Node
    from spoke.scan import _prov, ownership, withdrawn

    body = "Derived by spoke-scan on 2026-09-01 from somewhere.\n"
    owned = Node(
        name="gone", type="decision", state="open", title="gone", body=body,
        relations=(), ruling=None, blocked_by=(),
        provenance=_prov(date(2026, 9, 1), "somewhere", body),
        opened=None, updated=None, by=None, claimed_by=None, claimed_at=None,
    )
    assert ownership(owned) == "owned"

    w = withdrawn(owned, date(2026, 9, 13))
    assert w.state == "abandoned"
    assert "2026-09-13" in w.ruling and "no longer derived" in w.ruling
    assert w.body == body, "withdrawal changes standing, not content"
    # still owned afterwards: a later scan that derives it again can
    # revive it without a human in the loop
    assert ownership(w) == "owned"

    # a node a human touched is not the scan's to withdraw
    edited = replace(owned, body=body + "\nA human wrote this.\n")
    assert ownership(edited) == "diverged"


def _owned(name, body, on, **kw):
    from spoke.ledger import Node
    from spoke.scan import _prov
    base = dict(name=name, type="product", state="open", title=name, body=body,
                relations=(), ruling=None, blocked_by=(), provenance=_prov(on, "x", body),
                opened=None, updated=None, by=None, claimed_by=None, claimed_at=None)
    base.update(kw)
    return Node(**base)


def test_a_scan_on_a_new_day_is_unchanged_and_keeps_what_it_did_not_derive(tmp_path):
    """On the estate, 143 of 218 nodes 'would update' on a new day because
    the body's 'Derived ... on <date>' stamp moved -- and the rewrite
    dropped the expectations a person had put on them. The stamp is not
    content; the expectations are not the scan's to lose."""
    from dataclasses import replace
    from spoke.ledger.store import LedgerStore
    from spoke.scan import Proposal, ScanResult, apply
    from tests.conftest import build_store

    store = LedgerStore(build_store(tmp_path / "s"))
    d1, d2 = date(2026, 9, 13), date(2026, 9, 14)
    body1 = f"Derived by spoke-scan on {d1.isoformat()} from x.\n"
    body2 = f"Derived by spoke-scan on {d2.isoformat()} from x.\n"
    expects = ({"url": "https://a.example/", "status": 200},)
    from spoke.ledger.scale import Axis, Score
    axes = (Axis("coverage", "Coverage", 100),)
    scores = (Score("coverage", 3, "measured", "2026-09-08", "seen", "human:x"),)
    r = store.write(replace(_owned("alpha", body1, d1), expects=expects, scores=scores,
                            opened="2026-09-13"), axes, 5)
    assert r.ok, r.reasons

    out = apply(ScanResult(proposals=(Proposal(node=_owned("alpha", body2, d2), label="product",
                                               evidence=("x",)),), notes=(), proposed_vocabulary={}),
                store, axes, 5, d2, write=True)
    assert out.unchanged == 1 and out.written == 0, out.lines
    kept = store.read("alpha")
    assert kept.expects == expects and kept.opened == "2026-09-13"
    assert kept.scores == scores

    # and when the body DOES change, the re-derive still carries them
    body3 = body2 + "\nA new line the repo now says.\n"
    out = apply(ScanResult(proposals=(Proposal(node=_owned("alpha", body3, d2), label="product",
                                               evidence=("x",)),), notes=(), proposed_vocabulary={}),
                store, axes, 5, d2, write=True)
    assert out.written == 1, out.lines
    kept = store.read("alpha")
    assert kept.scores == scores and kept.expects == expects and "new line" in kept.body


def test_apply_dates_a_node_by_first_seen_and_a_decision_by_its_own_date(tmp_path):
    from spoke.ledger.store import LedgerStore
    from spoke.scan import Proposal, ScanResult, apply
    from tests.conftest import build_store

    store = LedgerStore(build_store(tmp_path / "s"))
    d1, d2 = date(2026, 9, 13), date(2026, 9, 14)
    body = f"Derived by spoke-scan on {d1.isoformat()} from x.\n"
    # a node the scan wrote before it dated anything: opened=None, provenance says 09-13
    assert store.write(_owned("legacy", body, d1), (), 5).ok
    props = [
        Proposal(node=_owned("legacy", body, d2), label="product", evidence=("x",)),
        Proposal(node=_owned("fresh", body, d2), label="product", evidence=("x",)),
        Proposal(node=_owned("adr", body, d2, type="decision", opened="2026-07-20"),
                 label="decision", evidence=("x",)),
    ]
    out = apply(ScanResult(proposals=tuple(props), notes=(), proposed_vocabulary={}), store, (), 5, d2, write=True)
    assert out.written == 3, out.lines
    assert store.read("legacy").opened == "2026-09-13", "first seen, not today"
    assert store.read("fresh").opened == "2026-09-14"
    assert store.read("adr").opened == "2026-07-20"
    # and the day after: nothing moves
    out = apply(ScanResult(proposals=tuple(props), notes=(), proposed_vocabulary={}), store, (), 5, date(2026, 9, 15), write=True)
    assert out.written == 0 and out.unchanged == 3, out.lines


def test_a_shared_package_is_one_capability_not_one_per_file(tmp_path):
    """A vendored engine of nine files became nine capabilities, and every
    product without the engine was reported absent nine times. And a copy
    inside ONE part was recorded as touching every part of its repo."""
    engine = {f"packages/engine/pyproject.toml": '[project]\nname = "engine"\n',
              "packages/engine/src/engine/score.py": "def s():\n    return 1\n" * 20,
              "packages/engine/src/engine/advise.py": "def a():\n    return 2\n" * 20}
    mono = _repo(tmp_path / "watches", {
        "one/pyproject.toml": '[project]\nname = "one"\n',
        "two/pyproject.toml": '[project]\nname = "two"\n',
        **{f"one/{k}": v for k, v in engine.items()},
        "one/static/favicon.ico": "\x00ICO" * 100,
        "docs/agents/triage.md": "# triage\n" * 40,
    })
    other = _repo(tmp_path / "other", {
        "pyproject.toml": '[project]\nname = "other"\n',
        **engine,
        "static/favicon.ico": "\x00ICO" * 100,
        "docs/agents/triage.md": "# triage\n" * 40,
    })
    res = scan_repos([mono, other], TODAY)
    shared = {p.node.name: p.node for p in res.proposals if p.node.type == "capability"}
    assert set(shared) == {"shared-engine", "shared-agents"}, set(shared)
    eng = shared["shared-engine"]
    assert eng.title == "Shared: packages/engine (2 files)"
    # `other` holds its engine as a part of its own (packages/engine is a
    # unit), so that copy touches the part; the copy inside `one` touches
    # `one`. `two`, which holds no copy, is not touched.
    assert {r.to for r in eng.relations} == {"one", "engine"}
    assert {r.to for r in shared["shared-agents"].relations} == {"one", "two", "engine"}, \
        "a copy outside every part touches all of that repo's parts"
