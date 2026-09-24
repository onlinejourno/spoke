from datetime import date

from spoke.ledger.graph import LENSES, load_graph
from spoke.ledger.matrix import cross
from tests.test_ledger_graph import FakeStore, _n   # reuse the helpers
from spoke.clock import Clock

TODAY = date(2026, 9, 9)


def _graph():
    nodes = [
        _n("gate", "capability"), _n("nav", "capability"),
        _n("a", "product"), _n("b", "product"), _n("c", "product"),
        _n("i1", rels=[("touches", "a"), ("touches", "gate")]),
        _n("i2", rels=[("touches", "b"), ("touches", "nav")]),
    ]
    g, skipped = load_graph(FakeStore(nodes))
    assert skipped == []
    return g


def test_product_by_capability_reports_coverage_and_absence():
    m = cross(_graph(), LENSES["product"], LENSES["capability"], Clock(TODAY, 30))
    assert set(m.rows) == {"a", "b", "c"}
    assert set(m.cols) == {"gate", "nav"}

    assert m.cell("a", "gate").covered
    assert not m.cell("c", "gate").covered
    absent = [(r, c) for r in m.rows for c in m.cols
              if any(f.kind == "absent" for f in m.cell(r, c).flags)]
    assert ("c", "gate") in absent
    assert ("c", "nav") in absent
    assert ("a", "gate") not in absent


def test_a_covered_cell_can_be_asked_why():
    """Coverage that cannot name its evidence is a claim, not a finding."""
    m = cross(_graph(), LENSES["product"], LENSES["capability"], Clock(TODAY, 30))
    assert m.cell("a", "gate").shared == ("i1",)


def test_the_matrix_counts_match_the_lens():
    """No lens is the real one: crossing the other way must transpose,
    not disagree. A coverage number that depends on which lens you
    happened to put on the rows is not a measurement."""
    g = _graph()
    pc = cross(g, LENSES["product"], LENSES["capability"], Clock(TODAY, 30))
    cp = cross(g, LENSES["capability"], LENSES["product"], Clock(TODAY, 30))
    assert set(pc.rows) == set(cp.cols) and set(pc.cols) == set(cp.rows)
    for r in pc.rows:
        for c in pc.cols:
            assert pc.cell(r, c).covered == cp.cell(c, r).covered
            assert pc.cell(r, c).shared == cp.cell(c, r).shared


def test_coverage_counts_are_recomputed_not_counted_by_hand():
    """The Sec.4.4 case: "N of M" as a view."""
    m = cross(_graph(), LENSES["product"], LENSES["capability"], Clock(TODAY, 30))
    have_gate = [r for r in m.rows if m.cell(r, "gate").covered]
    assert have_gate == ["a"]                       # 1 of 3, computed


def test_a_cell_carries_only_flags_whose_origin_is_in_the_intersection():
    """A row's whole flag set belongs to the row, not to every column of
    it -- spreading one problem across a line of the grid would make the
    view useless for locating anything."""
    nodes = [
        _n("gate", "capability"), _n("nav", "capability"),
        _n("a", "product"),
        _n("i1", rels=[("touches", "a"), ("touches", "gate")]),
        _n("i2", "item", "blocked", rels=[("touches", "a"), ("touches", "nav")]),
    ]
    g, _ = load_graph(FakeStore(nodes))
    m = cross(g, LENSES["product"], LENSES["capability"], Clock(TODAY, 30))
    gate_origins = {f.origin for f in m.cell("a", "gate").flags}
    nav_origins = {f.origin for f in m.cell("a", "nav").flags}
    assert "i2" in nav_origins
    assert "i2" not in gate_origins


def test_an_empty_graph_yields_an_empty_matrix_not_a_crash():
    g, _ = load_graph(FakeStore([]))
    m = cross(g, LENSES["product"], LENSES["capability"], Clock(TODAY, 30))
    assert m.rows == () and m.cols == ()
