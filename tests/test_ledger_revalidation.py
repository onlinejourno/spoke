"""A ledger node is checked on write AND afterwards.

It used to be checked only on write, which is exactly backwards for this
store: it is a directory of markdown files whose whole point is that a
human can edit them, and a hand edit goes nowhere near
`LedgerStore.write`.

The asymmetry was arbitrary and easy to miss. `check_wikilinks`
deliberately scans both the memory tree and the ledger tree, so a broken
wikilink INSIDE a ledger node was reported -- while the node itself being
malformed was not.
"""
import subprocess
from pathlib import Path

import pytest

from spoke.checks.structural import check_all, check_ledger_schema
from spoke.ledger.scale import Axis


def _store(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "MEMORY.md").write_text("- [alpha](alpha.md) - hook\n")
    (tmp_path / "alpha.md").write_text("---\nname: alpha\ndescription: d\n---\n\nbody\n")
    (tmp_path / "ledger").mkdir()
    return tmp_path


def _node_file(store: Path, name: str, frontmatter: str, body: str = "why") -> None:
    (store / "ledger" / f"{name}.md").write_text(f"---\n{frontmatter}---\n\n{body}\n")


def _kinds(findings):
    return {f.kind for f in findings}


# ── what a hand edit can put on disk ──────────────────────────────────

def test_a_deferral_hand_edited_to_have_no_ruling_is_reported(tmp_path):
    """The condition the gate exists to refuse, sitting on disk. Every
    surface used to render it as an ordinary node."""
    store = _store(tmp_path / "store")
    _node_file(store, "dropped",
               "name: dropped\ntype: item\nstate: deferred\ntitle: A deferral\n")
    findings = check_ledger_schema(store)
    assert _kinds(findings) == {"ledger-invalid"}
    assert any("requires a non-empty ruling" in f.detail for f in findings)
    assert findings[0].file == "ledger/dropped.md"


def test_an_unknown_type_is_reported(tmp_path):
    store = _store(tmp_path / "store")
    _node_file(store, "odd", "name: odd\ntype: widget\nstate: open\ntitle: Odd\n")
    assert any("unknown type" in f.detail for f in check_ledger_schema(store))


def test_a_client_node_carrying_a_real_name_is_reported(tmp_path):
    """A client node's title must be an opaque id -- that is a privacy
    rule, and a hand edit could put a customer's name in the store."""
    store = _store(tmp_path / "store")
    _node_file(store, "cust",
               "name: cust\ntype: client\nstate: open\ntitle: Acme Newspapers Ltd\n")
    assert any("opaque id" in f.detail for f in check_ledger_schema(store))


def test_a_node_that_cannot_be_parsed_is_reported_separately(tmp_path):
    """Two kinds because they need different work: one cannot be read at
    all, the other reads fine and fails the gate."""
    store = _store(tmp_path / "store")
    (store / "ledger" / "broken.md").write_text("---\nname: [unclosed\n---\n\nbody\n")
    assert _kinds(check_ledger_schema(store)) == {"ledger-unreadable"}


def test_a_score_on_an_undeclared_axis_is_reported_when_the_axes_are_known(tmp_path):
    store = _store(tmp_path / "store")
    _node_file(
        store, "widget",
        "name: widget\ntype: product\nstate: open\ntitle: Widget\n"
        "scores:\n  - axis: vibes\n    value: 3\n    basis: measured\n",
    )
    assert check_ledger_schema(store) == []          # no axis list, no membership check
    findings = check_ledger_schema(store, (Axis("reliability", "Reliability", 30),))
    assert any("has not declared" in f.detail for f in findings)


def test_a_well_formed_node_produces_nothing(tmp_path):
    store = _store(tmp_path / "store")
    _node_file(store, "fine", "name: fine\ntype: item\nstate: open\ntitle: Fine\n")
    assert check_ledger_schema(store) == []


def test_a_store_with_no_ledger_is_not_an_error(tmp_path):
    store = _store(tmp_path / "store")
    (store / "ledger").rmdir()
    assert check_ledger_schema(store) == []


# ── it reaches the surfaces, and does not brick the store ─────────────

def test_check_all_includes_it(tmp_path):
    store = _store(tmp_path / "store")
    _node_file(store, "dropped",
               "name: dropped\ntype: item\nstate: deferred\ntitle: A deferral\n")
    assert "ledger-invalid" in _kinds(check_all(store, accepted=()))


def test_a_bad_ledger_node_does_not_block_an_unrelated_memory_write(tmp_path):
    """This is the risk of revalidating: a hand-edited node that has been
    sitting there for months must not make the store unwritable.

    `Store.write` blocks only on findings whose file it is touching, so a
    pre-existing ledger defect surfaces as an advisory. Asserted rather
    than assumed -- getting this wrong would turn a report into an
    outage.
    """
    from spoke.store import Store

    store = _store(tmp_path / "store")
    subprocess.run(["git", "init", "-q", str(store)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(store), "config", k, v], check=True)
    _node_file(store, "dropped",
               "name: dropped\ntype: item\nstate: deferred\ntitle: A deferral\n")
    subprocess.run(["git", "-C", str(store), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(store), "commit", "-qm", "seed"], check=True)

    res = Store(store).write("alpha.md", "edited\n")
    assert res.ok, res.findings
    assert "edited" in (store / "alpha.md").read_text()
    assert any(f.kind == "ledger-invalid" for f in res.advisories), res.advisories
