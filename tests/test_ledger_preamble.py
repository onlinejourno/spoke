from spoke.ledger import Node
from spoke.ledger.preamble import estimate_tokens, render_preamble


def _n(name, state, title="t", ruling=None, blocked_by=()):
    return Node(name=name, type="item", state=state, title=title, body="",
                relations=(), ruling=ruling, blocked_by=blocked_by, provenance=(),
                opened=None, updated=None, by=None, claimed_by=None, claimed_at=None)


def test_estimate_grows_with_length():
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)


def test_blocked_items_come_before_open_ones():
    nodes = [_n("open-one", "open"), _n("blocked-one", "blocked", blocked_by=("x",))]
    text, _ = render_preamble(nodes, [], budget_tokens=500)
    assert text.index("blocked-one") < text.index("open-one")


def test_an_unruled_deferral_is_surfaced_as_a_defect():
    text, _ = render_preamble([_n("sloppy", "deferred", ruling=None)], [], budget_tokens=500)
    assert "sloppy" in text
    assert "ruling" in text.lower()


def test_the_budget_is_respected_and_the_remainder_is_counted():
    nodes = [_n(f"item-{i}", "open", title="x" * 200) for i in range(50)]
    text, stats = render_preamble(nodes, [], budget_tokens=300)
    assert estimate_tokens(text) <= 300
    assert stats["hidden"] > 0
    assert f"+{stats['hidden']} more" in text


def test_memory_index_lines_compete_in_the_same_budget():
    nodes = [_n("blocked-one", "blocked", blocked_by=("x",))]
    index = [f"- [rec{i}](rec{i}.md) — hook {i}" for i in range(200)]
    text, stats = render_preamble(nodes, index, budget_tokens=200)
    assert "blocked-one" in text          # severity wins the budget
    assert stats["hidden"] > 0
    assert estimate_tokens(text) <= 200


def test_stats_report_the_real_cost():
    text, stats = render_preamble([_n("a", "open")], [], budget_tokens=1000)
    assert stats["tokens_used"] == estimate_tokens(text)
    assert stats["budget"] == 1000
    assert stats["shown"] >= 1


# --- Finding 4: preamble.py and cli.py disagreed about "unruled" -- only
# "deferred" was treated as unruled here, so an unruled abandoned/deviated
# node carried no marker and could rank BELOW a closed `done` item, and
# nothing filtered closed states out of the budget at all. ---

def test_an_unruled_abandoned_node_is_surfaced_as_a_defect_same_as_deferred():
    text, _ = render_preamble([_n("gone", "abandoned", ruling=None)], [], budget_tokens=500)
    assert "gone" in text
    assert "ruling" in text.lower()


def test_an_unruled_deviated_node_is_surfaced_as_a_defect_same_as_deferred():
    text, _ = render_preamble([_n("changed", "deviated", ruling=None)], [], budget_tokens=500)
    assert "changed" in text
    assert "ruling" in text.lower()


def test_an_unruled_abandoned_outranks_a_done_item():
    nodes = [_n("finished", "done"), _n("gone", "abandoned", ruling=None)]
    text, _ = render_preamble(nodes, [], budget_tokens=500)
    # "finished" (done, ruled) is excluded entirely -- see the next test --
    # but even measured only against rank order, the unruled abandonment
    # must not sort below a closed item.
    assert "gone" in text
    if "finished" in text:
        assert text.index("gone") < text.index("finished")


def test_a_ruled_done_item_does_not_appear_in_the_preamble_at_all():
    nodes = [_n("finished", "done", ruling="shipped 2026-09-01")]
    text, stats = render_preamble(nodes, [], budget_tokens=500)
    assert "finished" not in text
    assert stats["shown"] == 0


def test_a_ruled_abandoned_item_does_not_appear_either():
    nodes = [_n("gone", "abandoned", ruling="not pursuing; superseded by X")]
    text, stats = render_preamble(nodes, [], budget_tokens=500)
    assert "gone" not in text
    assert stats["shown"] == 0


def test_closed_items_do_not_consume_the_open_item_budget():
    # A tight budget: if "finished" (done, ruled -- closed) were still
    # ranked and admitted, it would starve "open-one" out of the budget.
    nodes = [_n("finished", "done", ruling="shipped"), _n("open-one", "open")]
    text, stats = render_preamble(nodes, [], budget_tokens=60)
    assert "open-one" in text
    assert "finished" not in text


# --- --ledger-only fix: Claude Code already injects MEMORY.md at
# SessionStart, so the hook wiring passes NO memory index lines at all
# rather than relying on the budget to squeeze them out. This module's
# contract for that is simply: called with an empty memory_index_lines
# list, no index line appears -- and the shared-budget path (called with
# real index lines) is unchanged and still tested. ---

def test_called_with_no_memory_index_lines_none_appear_and_cost_still_matches():
    nodes = [_n("alpha", "open")]
    text, stats = render_preamble(nodes, [], budget_tokens=500)
    assert "alpha" in text
    assert not any(line.startswith("- [") for line in text.splitlines())
    assert stats["tokens_used"] == estimate_tokens(text)


def test_shared_budget_path_still_includes_index_lines_and_cost_still_matches():
    nodes = [_n("alpha", "open")]
    index = [f"- [rec{i}](rec{i}.md) — hook {i}" for i in range(5)]
    text, stats = render_preamble(nodes, index, budget_tokens=500)
    assert "alpha" in text
    assert any(line.startswith("- [") for line in text.splitlines())
    assert stats["tokens_used"] == estimate_tokens(text)


def test_the_preamble_survives_a_mixed_bag_of_opened_types():
    """`opened` is a str from every writer and a `date` from any YAML file
    where it is unquoted. Sorting the two raised TypeError inside the one
    function whose job is to raise things -- found the first time a
    surface report was written. Age still orders: older first."""
    from datetime import date

    from spoke.ledger import Node, Relation
    from spoke.ledger.preamble import render_preamble

    def n(name, opened):
        return Node(name=name, type="item", state="open", title=name, body="",
                    relations=(), ruling=None, blocked_by=(), provenance=(),
                    opened=opened, updated=None, by=None, claimed_by=None, claimed_at=None)

    nodes = [n("as-str", "2026-09-10"), n("as-date", date(2026, 9, 1)),
             n("undated", None), n("newest", "2026-09-13")]
    text, _ = render_preamble(nodes, [], 4000)
    order = [ln.split(":")[0] for ln in text.splitlines() if ln.split(":")[0] in
             {"as-str", "as-date", "undated", "newest"}]
    assert order == ["undated", "as-date", "as-str", "newest"]


def test_a_persons_word_outranks_a_machines_inventory():
    """A surface report sorted below forty scanned 'product open' lines
    and the budget hid it. Human-authored open items come before nodes
    whose every provenance entry is a probe's."""
    from spoke.ledger import Node
    from spoke.ledger.preamble import render_preamble

    def n(name, prov, opened):
        return Node(name=name, type="item", state="open", title=name, body="",
                    relations=(), ruling=None, blocked_by=(), provenance=prov,
                    opened=opened, updated=None, by=None, claimed_by=None, claimed_at=None)

    scanned = n("scanned-older", ({"by": "probe:spoke-scan", "on": "2026-01-01"},), "2026-01-01")
    report = n("report-newer", ({"field": "title", "by": "human:board:/scale", "at": "2026-09-13"},), "2026-09-13")
    untouched = n("no-provenance", (), "2026-05-01")
    text, _ = render_preamble([scanned, report, untouched], [], 4000)
    order = [ln.split(":")[0] for ln in text.splitlines()
             if ln.split(":")[0] in {"scanned-older", "report-newer", "no-provenance"}]
    assert order.index("report-newer") < order.index("scanned-older")
    assert order.index("no-provenance") < order.index("scanned-older")
