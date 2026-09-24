import math
import time

from spoke.ledger.graph import LENSES, load_graph, resolve_lens
from spoke.ledger.layout import MEMBER_RADIUS_MIN, layout
from tests.test_ledger_graph import FakeStore, _n   # reuse the helpers

# Measured on this machine (Apple silicon, CPython 3.12): 5,000 nodes lay out
# in 0.0016-0.0023s, worst of five runs in a cold process. The budget is ~20x
# the worst measurement rather than the 5-10x a tighter reading would give,
# because at this scale 5x is 0.01s -- close enough to a GC pause or a shared
# CI core to flake, and a flaky gate gets deleted. 0.05s still fails loudly on
# the regression it exists for: an O(n^2) placement over 5,000 nodes is tens of
# millions of Python operations, seconds not milliseconds, so it misses this by
# two orders of magnitude and fails in CI rather than quietly in use.
# Deliberately LOOSE, and this is the second value it has had.
#
# It was 0.05s: twenty times a measurement taken on the author's laptop.
# That is a budget calibrated on the wrong machine, and it went red on a
# shared CI runner at 0.078s -- on a commit that changed nothing about
# the algorithm. It then passed on the next run. Flaky is worse than red:
# a gate that cries wolf is a gate people learn to skip, and this project
# exists because skipped gates are how failures hide.
#
# What this test is FOR is catching a quadratic placement. Over 5,000
# nodes that is tens of millions of Python operations -- seconds, not
# milliseconds -- so it misses two seconds by two orders of magnitude
# just as surely as it missed fifty milliseconds. The tightness bought
# nothing and cost green builds.
BUDGET_SECONDS = 2.0


def _resolved(nodes, lens="product"):
    graph, _ = load_graph(FakeStore(nodes))
    return graph, LENSES[lens], resolve_lens(graph, LENSES[lens])


def _a_product_with_members(count=5, product="atlas"):
    return [_n(product, "product")] + [
        _n(f"i{i}", rels=[("touches", product)]) for i in range(count)
    ]


def test_every_rendered_node_gets_a_position():
    graph, lens, resolved = _resolved(
        _a_product_with_members() + [_n("nav", "product"), _n("unrelated")]
    )
    rendered = set(resolved) | {m for members in resolved.values() for m in members}

    pos = layout(resolved)

    assert set(pos) == rendered
    # Two nodes drawn at one point are one node to the reader.
    assert len(set(pos.values())) == len(pos)


def test_layout_is_deterministic():
    graph, lens, resolved = _resolved(_a_product_with_members())
    assert layout(resolved) == layout(resolved)

    # ...and independent of the order the nodes arrived in, which is what
    # decides dict insertion order and therefore set iteration order here.
    shuffled = list(reversed(_a_product_with_members()))
    other_graph, _, other_resolved = _resolved(shuffled)
    assert layout(other_resolved) == layout(resolved)


def test_hub_nodes_are_separated_from_their_members():
    graph, lens, resolved = _resolved(_a_product_with_members())
    pos = layout(resolved)

    sx, sy = pos["atlas"]
    for member in resolved["atlas"] - {"atlas"}:
        mx, my = pos[member]
        assert math.hypot(mx - sx, my - sy) >= MEMBER_RADIUS_MIN


def test_a_node_under_two_hubs_gets_exactly_one_position():
    # A lens is not a tree: `shared` is legitimately in both products' scope.
    # It is drawn once, in the cluster of the first hub by name -- so the
    # picture stays a map and not a duplicate of the same node twice.
    nodes = [
        _n("alpha", "product"),
        _n("beta", "product"),
        _n("shared", rels=[("touches", "alpha"), ("touches", "beta")]),
    ]
    graph, lens, resolved = _resolved(nodes)
    assert "shared" in resolved["alpha"] and "shared" in resolved["beta"]

    pos = layout(resolved)

    to_alpha = math.dist(pos["shared"], pos["alpha"])
    to_beta = math.dist(pos["shared"], pos["beta"])
    assert to_alpha < to_beta


def test_an_empty_resolved_lens_yields_an_empty_layout():
    graph, _ = load_graph(FakeStore([]))
    assert layout({}) == {}


def test_a_5000_node_graph_lays_out_within_the_budget():
    # Fails loudly in CI rather than quietly in use.
    nodes = []
    for p in range(50):
        nodes.append(_n(f"p{p:03d}", "product"))
        nodes.extend(
            _n(f"p{p:03d}-i{i:03d}", rels=[("touches", f"p{p:03d}")]) for i in range(99)
        )
    graph, lens, resolved = _resolved(nodes)
    assert len(graph.nodes) == 5000

    start = time.perf_counter()
    pos = layout(resolved)
    elapsed = time.perf_counter() - start

    assert len(pos) == 5000
    assert elapsed < BUDGET_SECONDS, f"{elapsed:.3f}s over budget {BUDGET_SECONDS}s"
