import pytest
from spoke.ledger import Node, Relation
from spoke.ledger.graph import Graph, Lens, LENSES, load_graph, resolve_lens


def _n(name, type_="item", state="open", rels=()):
    return Node(name=name, type=type_, state=state, title=name, body="",
                relations=tuple(Relation(r, t) for r, t in rels), ruling=None,
                blocked_by=(), provenance=(), opened=None, updated=None, by=None,
                claimed_by=None, claimed_at=None)


class FakeStore:
    def __init__(self, nodes): self._n = list(nodes); self.skipped = []
    def list_nodes(self): return list(self._n)


def test_relations_are_navigable_in_both_directions():
    g, _ = load_graph(FakeStore([_n("a", rels=[("touches", "b")]), _n("b", "product")]))
    assert g.out("a") == (Relation("touches", "b"),)
    assert g.inbound("b") == (("a", Relation("touches", "b")),)


def test_a_relation_to_a_missing_node_is_reported_not_dropped():
    g, skipped = load_graph(FakeStore([_n("a", rels=[("touches", "ghost")])]))
    assert any("ghost" in s for s in skipped)
    assert "a" in g.nodes


def test_the_product_lens_hubs_on_products():
    nodes = [_n("atlas", "product"), _n("i1", rels=[("touches", "atlas")]),
             _n("i2", rels=[("touches", "atlas")]), _n("unrelated")]
    g, _ = load_graph(FakeStore(nodes))
    got = resolve_lens(g, LENSES["product"])
    assert set(got) == {"atlas"}
    assert got["atlas"] == {"atlas", "i1", "i2"}


def test_the_capability_lens_hubs_on_capabilities_over_the_same_nodes():
    nodes = [_n("entitlement", "capability"), _n("atlas", "product"),
             _n("i1", rels=[("touches", "atlas"), ("touches", "entitlement")])]
    g, _ = load_graph(FakeStore(nodes))
    prod = resolve_lens(g, LENSES["product"])
    cap = resolve_lens(g, LENSES["capability"])
    assert set(prod) == {"atlas"} and set(cap) == {"entitlement"}
    assert "i1" in prod["atlas"] and "i1" in cap["entitlement"]


def test_no_lens_is_canonical_every_lens_sees_the_same_nodes():
    nodes = [_n("entitlement", "capability"), _n("atlas", "product"), _n("adr", "decision"),
             _n("i1", rels=[("touches", "atlas"), ("touches", "entitlement"),
                            ("governed_by", "adr")])]
    g, _ = load_graph(FakeStore(nodes))
    seen = {ln: {m for s in resolve_lens(g, LENSES[ln]).values() for m in s}
            for ln in ("product", "capability", "decision")}
    assert seen["product"] & {"i1"} and seen["capability"] & {"i1"} and seen["decision"] & {"i1"}


def test_one_hop_is_the_boundary():
    nodes = [_n("atlas", "product"), _n("i1", rels=[("touches", "atlas")]),
             _n("i2", rels=[("touches", "i1")])]
    g, _ = load_graph(FakeStore(nodes))
    assert "i2" not in resolve_lens(g, LENSES["product"])["atlas"]
    assert "i2" in resolve_lens(g, LENSES["product"], hops=2)["atlas"]


def test_a_lens_stays_within_its_relations_at_every_hop():
    # atlas(product) <-touches- i1 <-blocked_by- i2 <-touches- i3: i2 and
    # i3 only reach the product hub through a blocked_by hop, which is
    # not the product lens's relation -- they must never appear in the
    # product lens, at any hop count. (blocked/held still travel to them
    # regardless -- that's flags._propagate, deliberately not lens-scoped.)
    nodes = [_n("atlas", "product"),
             _n("i1", rels=[("touches", "atlas")]),
             _n("i2", rels=[("blocked_by", "i1")]),
             _n("i3", rels=[("touches", "i2")])]
    g, _ = load_graph(FakeStore(nodes))
    for hops in (1, 2, 3, 5):
        got = resolve_lens(g, LENSES["product"], hops=hops)["atlas"]
        assert "i2" not in got, f"blocked_by is not a product-lens relation (hops={hops})"
        assert "i3" not in got, f"blocked_by is not a product-lens relation (hops={hops})"


def test_a_cycle_does_not_hang():
    nodes = [_n("a", rels=[("blocked_by", "b")]), _n("b", rels=[("derived_from", "a")])]
    g, _ = load_graph(FakeStore(nodes))
    assert resolve_lens(g, Lens("t", ("item",), ("blocked_by", "derived_from")), hops=5)


# --- former names ------------------------------------------------------

def _named(name, type_="item", state="open", rels=(), aliases=()):
    from dataclasses import replace
    return replace(_n(name, type_, state, rels), aliases=tuple(aliases))


def test_a_relation_to_a_former_name_resolves_and_says_so():
    """A shipped name does not get erased. Every reference written before
    a rename still points at the old one, and calling those broken
    reports the residue of a rename as a defect."""
    g, notes = load_graph(FakeStore([
        _named("spoke", "product", aliases=["memory-doctor"]),
        _n("a-canary", rels=[("touches", "memory-doctor")]),
    ]))
    assert g.out("a-canary") == (Relation("touches", "spoke"),)
    assert g.inbound("spoke") == (("a-canary", Relation("touches", "spoke")),)
    assert [s.kind for s in notes] == ["renamed"]
    assert "is now 'spoke'" in notes[0]


def test_a_former_name_that_is_a_live_nodes_name_is_refused():
    """Silently preferring one would make a relation point somewhere its
    author never wrote."""
    g, notes = load_graph(FakeStore([
        _named("spoke", "product", aliases=["almanac"]),
        _n("almanac", "product"),
        _n("x", rels=[("touches", "almanac")]),
    ]))
    assert g.out("x") == (Relation("touches", "almanac"),)   # the live node wins
    assert any(s.kind == "unreadable" and "live node" in s for s in notes)


def test_one_former_name_claimed_twice_is_used_by_neither():
    g, notes = load_graph(FakeStore([
        _named("alpha", "product", aliases=["old"]),
        _named("beta", "product", aliases=["old"]),
        _n("x", rels=[("touches", "old")]),
    ]))
    assert any(s.kind == "unreadable" and "both" in s for s in notes)
    assert any(s.kind == "dangling" for s in notes)     # still unresolved, still reported


def test_a_node_may_not_alias_its_own_name():
    from spoke.ledger.schema import validate
    node = _named("spoke", "product", aliases=["spoke"])
    assert node.aliases == ("spoke",), node
    assert any("own name" in r for r in validate(node)), validate(node)


def test_an_abandoned_product_is_not_a_hub_but_is_still_a_spoke():
    """A scan-withdrawn phantom unit kept a slot on the product lens
    because it was a product by type. An abandoned product is not a
    product anyone ships; it stays in the ledger, and stays a spoke
    wherever something live still points at it."""
    nodes = [
        _n("live", "product"),
        _n("gone", "product", state="abandoned", rels=[("serves", "live")]),
        _n("i1", rels=[("touches", "gone")]),
    ]
    g, _ = load_graph(FakeStore(nodes))
    got = resolve_lens(g, LENSES["product"])
    assert set(got) == {"live"}, "abandoned does not hub"
    assert "gone" in got["live"], "but is still a spoke of a live hub"


def test_a_rejected_decision_still_hubs_on_the_decision_lens():
    """Decided-against is a decision. The exclusion is the product
    lens's, not every lens's."""
    nodes = [_n("adr-7", "decision", state="abandoned")]
    g, _ = load_graph(FakeStore(nodes))
    assert set(resolve_lens(g, LENSES["decision"])) == {"adr-7"}
