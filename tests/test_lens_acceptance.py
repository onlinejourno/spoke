"""The acceptance case for slice 2c: the decision lens surfaces work
that contradicts a ruling.

Read the docstring on the test before changing anything here. This
asserts TRAVERSAL, not detection -- see the plan's "What this slice can
and cannot do".
"""
from datetime import date
from pathlib import Path
import subprocess

from tests.conftest import build_store

from spoke.ledger import Node, Relation
from spoke.ledger.graph import LENSES, load_graph
from spoke.ledger.flags import compute_flags
from spoke.ledger.store import LedgerStore
from spoke.clock import Clock


def _repo(tmp_path: Path) -> LedgerStore:
    root = tmp_path / "store"
    root.mkdir()
    build_store(root)
    return LedgerStore(root)


def _node(**kw) -> Node:
    base = dict(
        name="", type="item", state="open", title="", body="body",
        relations=(), ruling=None, blocked_by=(), provenance=(),
        opened=None, updated=None, by=None, claimed_by=None, claimed_at=None,
    )
    base.update(kw)
    return Node(**base)


def test_the_decision_lens_surfaces_work_that_contradicts_a_ruling(tmp_path):
    """A verdict shelves a layer; work continues on exactly that layer.

    Day one: a verdict shelves a layer of a product, naming its
    disqualifiers -- a decision in state `hold`.
    Day two: several commits patch exactly that layer -- open items
    governed by that decision.

    Both facts existed and nothing connected them. This asserts the
    traversal: once the work IS recorded and related, the decision lens
    reports the contradiction. It does NOT assert detection -- the real
    work of that kind is typically never recorded as ledger items at
    all, and making that automatic is the repo scan, a separate plan.
    """
    s = _repo(tmp_path)
    s.write(_node(name="prose-layer-shelved", type="decision", state="hold",
                  title="Prose and scoring layers are shelved"))
    for i in range(3):
        s.write(_node(name=f"grounding-patch-{i}", state="open",
                      title=f"Grounding: patch class {i}",
                      relations=(Relation("governed_by", "prose-layer-shelved"),)))

    graph, skipped = load_graph(s)
    assert skipped == [], skipped
    flags = compute_flags(graph, LENSES["decision"], Clock(date(2026, 9, 9), 30))

    dev = [f for f in flags.get("prose-layer-shelved", ()) if f.kind == "deviates"]
    assert len(dev) == 3, flags
    assert {f.origin for f in dev} == {f"grounding-patch-{i}" for i in range(3)}


def test_closing_the_contradicting_work_clears_the_deviation(tmp_path):
    """The counterpart: the flag is computed, so it goes away on its own
    when the contradiction does. A stored roll-up would not."""
    s = _repo(tmp_path)
    s.write(_node(name="prose-layer-shelved", type="decision", state="hold",
                  title="Prose and scoring layers are shelved"))
    s.write(_node(name="grounding-patch-0", state="abandoned",
                  title="Grounding: patch class 0",
                  ruling="stopped -- the layer is shelved",
                  relations=(Relation("governed_by", "prose-layer-shelved"),)))

    graph, _ = load_graph(s)
    flags = compute_flags(graph, LENSES["decision"], Clock(date(2026, 9, 9), 30))
    assert not [f for f in flags.get("prose-layer-shelved", ()) if f.kind == "deviates"]
