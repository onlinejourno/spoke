import time
from dataclasses import replace
from datetime import date, timedelta

import pytest

from spoke.ledger import STATES, Node, Relation
from spoke.ledger.graph import LENSES, load_graph
from spoke.ledger.flags import (
    Flag, STALE_MULTIPLIER, STALE_NEVER, _stale_cutoff, compute_flags,
)
from tests.test_ledger_graph import FakeStore, _n   # reuse the helpers
from spoke.clock import Clock


def _flags(nodes, lens="product", today=date(2026, 9, 9), stale_days=30):
    g, _ = load_graph(FakeStore(nodes))
    return compute_flags(g, LENSES[lens], Clock(today, stale_days))


def _with_updated(node, updated):
    return replace(node, updated=updated)


def test_blocked_travels_to_what_it_blocks():
    f = _flags([_n("atlas", "product"), _n("i1", rels=[("touches", "atlas")]),
                _n("i2", state="blocked", rels=[("touches", "atlas"), ("blocked_by", "i1")])])
    assert any(x.kind == "blocked" for x in f.get("atlas", ()))


def test_a_hold_reaches_every_product_it_touches():
    f = _flags([_n("a", "product"), _n("b", "product"),
                _n("h", "decision", state="hold", rels=[("touches", "a"), ("touches", "b")])])
    assert any(x.kind == "held" for x in f.get("a", ()))
    assert any(x.kind == "held" for x in f.get("b", ()))


def test_an_open_item_governed_by_a_hold_deviates():
    # The Audit shape: a ruling says stop; an open item under it is a contradiction.
    nodes = [_n("verdict", "decision", state="hold"),
             _n("work", state="open", rels=[("governed_by", "verdict")])]
    f = _flags(nodes, lens="decision")
    assert any(x.kind == "deviates" and x.origin == "work" for x in f.get("verdict", ())), f


def test_a_closed_item_under_a_hold_does_not_deviate():
    nodes = [_n("verdict", "decision", state="hold"),
             _n("work", state="done", rels=[("governed_by", "verdict")])]
    f = _flags(nodes, lens="decision")
    assert not any(x.kind == "deviates" for x in f.get("verdict", ()))


def test_absent_reports_a_product_lacking_a_capability_others_have():
    nodes = [_n("gate", "capability"), _n("a", "product"), _n("b", "product"),
             _n("i", rels=[("touches", "a"), ("touches", "gate")])]
    f = _flags(nodes, lens="capability")
    assert any(x.kind == "absent" and "b" in x.detail for x in f.get("gate", ())), f


def test_flags_are_not_stored_on_the_node():
    nodes = [_n("a", "product"), _n("h", "decision", state="hold", rels=[("touches", "a")])]
    g, _ = load_graph(FakeStore(nodes))
    compute_flags(g, LENSES["product"], Clock(date(2026, 9, 9), 30))
    assert not hasattr(g.nodes["a"], "flags")


def test_a_cycle_terminates():
    nodes = [_n("a", state="blocked", rels=[("blocked_by", "b")]),
             _n("b", rels=[("derived_from", "a"), ("blocked_by", "a")])]
    assert _flags(nodes) is not None


def test_the_universal_flags_appear_in_every_lens():
    nodes = [_n("cap", "capability"), _n("prod", "product"),
             _n("h", "decision", state="hold", rels=[("touches", "prod"), ("touches", "cap")])]
    for lens in ("product", "capability", "decision"):
        f = _flags(nodes, lens=lens)
        assert any(any(x.kind == "held" for x in v) for v in f.values()), lens


# --- Finding 1: staleness is per-state (spec Sec.5's table), never one
# scalar applied to every node regardless of what its state means. -------

def test_a_hold_is_never_stale_however_old():
    # The decision lens surfaces "h" itself as a hub, which is what
    # makes this test meaningful rather than vacuous -- the product lens
    # (this file's default) would find no product hub at all here and
    # pass trivially with or without the fix.
    n = _n("h", "decision", state="hold")
    n = _with_updated(n, "2020-01-01")
    f = _flags([n], lens="decision")
    assert not any(x.kind == "stale" for x in f.get("h", ())), f
    # ...and it is still correctly flagged `held` -- the fix must not
    # have thrown out the flag that IS supposed to fire.
    assert any(x.kind == "held" for x in f.get("h", ())), f


# `superseded` used to be asserted ABSENT from STATES here, with the
# never-stale test parametrised over `abandoned` alone because a node
# could not carry the other. It is admitted now; both are covered.
@pytest.mark.parametrize("state", ["abandoned", "superseded"])
def test_a_closed_state_is_never_stale(state):
    n = _n("x", "decision", state=state)
    n = _with_updated(n, "2020-01-01")
    f = _flags([n], lens="decision")
    assert not any(x.kind == "stale" for x in f.get("x", ())), f


@pytest.mark.parametrize("state", ["blocked", "open", "deferred", "done"])
def test_a_state_goes_stale_only_past_its_own_threshold(state):
    # One row per spec Sec.5's staleness table: blocked=short, open=medium,
    # deferred=long, done=long (re-verification, not decay). The threshold
    # is read from flags.STALE_MULTIPLIER itself so this test tracks the
    # documented mapping rather than duplicating its numbers.
    stale_days = 30
    threshold = int(stale_days * STALE_MULTIPLIER[state])
    today = date(2026, 9, 9)
    just_within = (today - timedelta(days=threshold)).isoformat()
    just_past = (today - timedelta(days=threshold + 1)).isoformat()

    fresh = [_n("atlas", "product"),
             _with_updated(_n("i", state=state, rels=[("touches", "atlas")]), just_within)]
    aged = [_n("atlas", "product"),
            _with_updated(_n("i", state=state, rels=[("touches", "atlas")]), just_past)]

    f_fresh = _flags(fresh, today=today, stale_days=stale_days)
    f_aged = _flags(aged, today=today, stale_days=stale_days)
    assert not any(x.kind == "stale" for x in f_fresh.get("atlas", ())), (state, f_fresh)
    assert any(x.kind == "stale" for x in f_aged.get("atlas", ())), (state, f_aged)


# --- Finding 2: flag propagation is one multi-source BFS per (kind,
# relation set), not one BFS per seed -- O(N + E), not O(seeds x N). -----

def _synthetic_graph(n_items=5000, n_products=50, n_capabilities=20):
    """A hub-shaped graph sized like the reviewer's benchmark: every item
    touches a couple of products and a capability, so products and
    capabilities act as hubs a `held` seed's propagation fans out through
    -- the shape that made the old per-seed BFS in `_propagate` cost 3.8s
    on 5,000 nodes with several hundred `hold` seeds. `held` travels along touches
    (spec Sec.5, "in every lens"), and flags recompute on every render, so
    this is real per-request cost, not a synthetic worst case.
    """
    nodes = [_n(f"product-{i}", "product") for i in range(n_products)]
    nodes += [_n(f"capability-{i}", "capability") for i in range(n_capabilities)]
    for i in range(n_items):
        state = "hold" if i % 5 == 0 else "open"
        rels = [
            ("touches", f"product-{i % n_products}"),
            ("touches", f"product-{(i * 7) % n_products}"),
            ("touches", f"capability-{i % n_capabilities}"),
        ]
        nodes.append(_n(f"item-{i}", "item", state=state, rels=rels))
    g, _ = load_graph(FakeStore(nodes))
    return g


# Loose on purpose -- see the same reasoning in test_ledger_layout.py.
# The old per-seed BFS took ~0.35-0.4s on this synthetic graph here and
# 3.8s on a larger, denser one; the multi-source fix takes ~0.11-0.13s.
# A regression to the old shape is a MULTIPLE, not a few milliseconds, so
# a ceiling that a loaded shared runner can never trip still catches it.
# The earlier 0.25s value could not tell a regression from a busy runner,
# and went red on CI for the second reason.
BUDGET_SECONDS = 2.0


def test_flag_propagation_stays_within_budget_on_a_large_graph():
    # 5000 nodes, hub-shaped fan-out. The board recomputes on every render,
    # so this is per-request cost, not a one-off.
    g = _synthetic_graph(n_items=5000, n_products=50, n_capabilities=20)
    t0 = time.perf_counter()
    compute_flags(g, LENSES["product"], Clock(date(2026, 9, 9), 30))
    assert time.perf_counter() - t0 < BUDGET_SECONDS


# --- Finding 3 (resolve_lens filtering every hop) is covered directly in
# tests/test_ledger_graph.py::test_a_lens_stays_within_its_relations_at_every_hop,
# since resolve_lens lives in graph.py, not this module. Repeated here as
# a flags-level check that lens-scoped flags (stale/unruled) don't leak
# through a non-lens relation either, now that resolve_lens is fixed. ---

def test_lens_scoped_flags_do_not_leak_through_a_non_lens_relation():
    nodes = [_n("atlas", "product"),
             _n("i1", rels=[("touches", "atlas")]),
             _with_updated(_n("i2", state="deferred", rels=[("blocked_by", "i1")]), "2000-01-01")]
    f = _flags(nodes, lens="product", stale_days=30)
    atlas_flags = f.get("atlas", ())
    assert not any(x.origin == "i2" and x.kind in ("stale", "unruled") for x in atlas_flags), atlas_flags


def test_a_hand_edited_unquoted_date_does_not_crash_the_computation():
    """The store quotes `updated`, so its own nodes round-trip as
    strings -- but the store is markdown a human is meant to edit, and
    an unquoted `updated: 2000-01-01` is what a human writes. YAML types
    that as a `date`, which used to raise TypeError inside
    `_stale_cutoff` and take the whole flag computation down with it.
    """
    from datetime import date as _date
    assert _stale_cutoff(Clock(_date(2026, 9, 9), 30), "open", _date(2000, 1, 1)) is True
    assert _stale_cutoff(Clock(_date(2026, 9, 9), 30), "open", _date(2026, 9, 8)) is False
    assert _stale_cutoff(Clock(_date(2026, 9, 9), 30), "open", 12345) is False


def test_a_hold_marks_what_it_names_and_not_what_those_things_touch():
    """A hold is a SCOPE, not a chain.

    Found by running the board on a real estate: one hold declaring six
    `touches` coloured all twelve products, because a repo's
    documentation node touches every unit in that repo and bridged the
    hold to units the decision had never named. At six nodes it was
    invisible; at sixty-two everything was held, and a flag that marks
    everything marks nothing.
    """
    nodes = [
        _n("shelved", "decision", "hold", rels=[("touches", "alpha")]),
        _n("alpha", "product"),
        _n("beta", "product"),
        # the bridge: a docs node touching every unit in one repo
        _n("repo-docs", "item", rels=[("touches", "alpha"), ("touches", "beta")]),
    ]
    g, _ = load_graph(FakeStore(nodes))
    # hops=1, the default and the case the board actually renders.
    flags = compute_flags(g, LENSES["product"], Clock(date(2026, 9, 9), 30))
    held_on = {name for name, fs in flags.items() if any(f.kind == "held" for f in fs)}
    assert "alpha" in held_on          # named by the decision
    assert "beta" not in held_on, held_on   # merely adjacent to something named


def test_widening_the_lens_still_rolls_a_hold_up_to_a_neighbour():
    """The counterpart, and it is NOT a contradiction. Asking for a wider
    radius is asking what is near this hub, and something held two hops
    away IS near it. The roll-up is deliberate: a hidden layer must never
    hide a problem. What changed is that the HOLD itself no longer
    travels the graph on its own -- the reader widened the view."""
    nodes = [
        _n("shelved", "decision", "hold", rels=[("touches", "alpha")]),
        _n("alpha", "product"),
        _n("beta", "product"),
        _n("repo-docs", "item", rels=[("touches", "alpha"), ("touches", "beta")]),
    ]
    g, _ = load_graph(FakeStore(nodes))
    flags = compute_flags(g, LENSES["product"], Clock(date(2026, 9, 9), 30), hops=2)
    assert any(f.kind == "held" for f in flags["beta"])


def test_blocked_still_travels_the_whole_chain():
    """The counterpart. If the thing blocking you is itself blocked, you
    are too -- that IS transitive, and must stay so."""
    nodes = [
        _n("far", "item", "blocked", rels=[("blocked_by", "middle")]),
        _n("middle", "item", rels=[("blocked_by", "near")]),
        _n("near", "item", rels=[("touches", "widget")]),
        _n("widget", "product"),
    ]
    g, _ = load_graph(FakeStore(nodes))
    flags = compute_flags(g, LENSES["product"], Clock(date(2026, 9, 9), 30), hops=3)
    origins = {f.origin for f in flags["widget"] if f.kind == "blocked"}
    assert "far" in origins, flags["widget"]


def test_absence_is_asked_by_the_lens_not_guessed_from_its_types():
    """`absent` used to be gated on `lens.hub_types == ("capability",)`,
    a fact about the lens living in its consumer. It is declared on the
    Lens now. Two lenses whose hubs are the same type must be able to
    differ on whether they ask it -- which the old check made impossible."""
    from dataclasses import replace

    from spoke.clock import Clock
    from spoke.ledger.graph import LENSES, Lens, load_graph

    nodes = [
        _n("cap", "capability"),
        _n("has", "product", rels=[("touches", "cap")]),
        _n("lacks", "product"),
    ]
    g, _ = load_graph(FakeStore(nodes))
    clock = Clock(date(2026, 9, 9), 30)

    asks = LENSES["capability"]
    assert asks.reports_absence
    assert any(f.kind == "absent" for f in compute_flags(g, asks, clock)["cap"])

    same_hubs_but_silent = replace(asks, name="quiet", reports_absence=False)
    assert not any(f.kind == "absent" for f in compute_flags(g, same_hubs_but_silent, clock)["cap"])

    # and the time lens, which hubs on capabilities too, does not ask it
    assert not LENSES["time"].reports_absence
