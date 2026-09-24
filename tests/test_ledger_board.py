"""The board's view model, tested directly.

`board.py` composes graph, flags, layout, matrix and scale, and it
carries this project's founding rule in its own docstring: a hidden layer
must never hide a problem. Every module it composes had a direct unit
test; it had none. It was reachable only through `TestClient` against a
real git repository, which meant the rule was asserted through three
layers that could each have been the thing under test.

These reach the module itself. No HTTP, no git, no temporary directory.
"""
from dataclasses import replace
from datetime import date

import pytest

from spoke.ledger import Relation
from spoke.ledger.board import BoardError, build_matrix, build_scale, build_view
from spoke.ledger.scale import Axis, Score
from tests.test_ledger_graph import FakeStore, _n   # reuse the helpers
from spoke.clock import Clock

TODAY = date(2026, 9, 9)


def _estate():
    """A product whose only trouble is a layer down, plus a capability
    one product has and another does not."""
    return FakeStore([
        _n("editor", "product"),
        _n("archive", "product"),
        _n("the-gate", "capability"),
        _n("blocker", "item"),
        _n("deep-work", "item", "blocked", rels=[("touches", "editor")]),
        _n("wire-gate", "item", rels=[("touches", "editor"), ("touches", "the-gate")]),
    ])


def _view(store=None, **kw):
    return build_view(store or _estate(), kw.pop("lens", "product"), Clock(TODAY, 30), **kw)


# ── the founding rule ─────────────────────────────────────────────────

def test_a_flag_from_a_hidden_layer_rolls_up_and_names_its_origin():
    body = _view(zoom="far")
    drawn = {n["name"] for n in body["nodes"]}
    assert "deep-work" not in drawn          # the item is not on screen

    editor = next(n for n in body["nodes"] if n["name"] == "editor")
    assert any(f["kind"] == "blocked" for f in editor["flags"])
    assert any(f["origin"] == "deep-work" for f in editor["flags"])


def test_zooming_in_reveals_the_layer_without_changing_the_verdict():
    """The same problem, the same origin, at every zoom. Only the detail
    on screen changes."""
    far = _view(zoom="far")
    close = _view(zoom="close")
    def origins(body):
        n = next(x for x in body["nodes"] if x["name"] == "editor")
        return {(f["kind"], f["origin"]) for f in n["flags"]}
    assert origins(far) == origins(close)
    assert "deep-work" in {n["name"] for n in close["nodes"]}


@pytest.mark.parametrize("zoom,expected", [
    ("far", {"product"}),
    ("middle", {"product", "capability", "decision"}),
])
def test_each_zoom_draws_only_the_layers_it_should(zoom, expected):
    types = {n["type"] for n in _view(zoom=zoom)["nodes"]}
    assert types <= expected, (zoom, types)


def test_a_hub_is_always_drawn_because_it_anchors_the_view():
    for zoom in ("far", "middle", "close", "closest"):
        names = {n["name"] for n in _view(zoom=zoom)["nodes"] if n["hub"]}
        assert names == {"editor", "archive"}, zoom


# ── the guarantee that the payload did not used to carry ──────────────

def _clients():
    return FakeStore([
        _n("editor", "product"),
        _n("c1", "client", rels=[("serves", "editor")]),
        _n("c2", "client", rels=[("serves", "editor")]),
    ])


def test_every_drawn_node_has_a_position_and_no_two_share_one():
    """`layout` guarantees one position per node and is tested for it.
    This asserts the same property of the payload actually shipped, which
    is a different set of nodes: it includes the client aggregate, which
    `layout` never used to be told about."""
    for zoom in ("far", "middle", "close", "closest"):
        body = build_view(_clients(), "product", Clock(TODAY, 30), zoom=zoom)
        names = {n["name"] for n in body["nodes"]}
        assert set(body["positions"]) == names, zoom
        placed = [tuple(p) for p in body["positions"].values()]
        assert len(set(placed)) == len(placed), (zoom, placed)


def test_clients_aggregate_until_the_closest_zoom():
    close = build_view(_clients(), "product", Clock(TODAY, 30), zoom="close")
    labels = [n["label"] for n in close["nodes"] if n["type"] == "clients"]
    assert labels == ["2 clients \u00b7 0 flagged"]

    closest = build_view(_clients(), "product", Clock(TODAY, 30), zoom="closest")
    assert {"c1", "c2"} <= {n["name"] for n in closest["nodes"]}
    assert not [n for n in closest["nodes"] if n["type"] == "clients"]


# ── refusals, which must never be a silent fallback ───────────────────

@pytest.mark.parametrize("kw,fragment", [
    ({"lens": "nope"}, "known lenses"),
    ({"zoom": "nope"}, "known zooms"),
    ({"flag_filter": ("nope",)}, "known kinds"),
    ({"hops": 0}, "at least 1"),
])
def test_a_bad_request_is_refused_and_says_what_would_work(kw, fragment):
    """A page that answers a question nobody asked looks exactly like a
    page that answered the one they did."""
    with pytest.raises(BoardError) as e:
        _view(**kw)
    assert fragment in str(e.value)


def test_a_flag_filter_narrows_hubs_not_nodes():
    body = _view(flag_filter=("blocked",))
    assert {n["name"] for n in body["nodes"] if n["hub"]} == {"editor"}


# ── the project's own words ───────────────────────────────────────────

def test_the_kind_is_the_projects_word_and_the_type_is_the_engines():
    body = _view(vocabulary={"product": "app"})
    editor = next(n for n in body["nodes"] if n["name"] == "editor")
    assert editor["kind"] == "app" and editor["type"] == "product"


def test_without_a_vocabulary_the_kind_falls_back_to_the_type():
    editor = next(n for n in _view()["nodes"] if n["name"] == "editor")
    assert editor["kind"] == "product"


# ── what could not be read ────────────────────────────────────────────

def test_a_dangling_relation_reaches_the_payload_with_its_kind():
    store = FakeStore([
        _n("editor", "product"),
        _n("orphan", "item", rels=[("touches", "ghost")]),
    ])
    body = build_view(store, "product", Clock(TODAY, 30))
    assert [s["kind"] for s in body["skipped"]] == ["dangling"]
    assert "ghost" in body["skipped"][0]["detail"]
    assert body["skip_headings"]["dangling"]


def test_three_channels_on_every_flagged_node():
    """Colour is never the signal on its own: shape carries the node type
    and the badge carries the count and kind, so the flag survives
    greyscale, a screenshot and a colour-blind reader."""
    flagged = [n for n in _view()["nodes"] if n["flags"]]
    assert flagged
    for n in flagged:
        assert n["shape"] and n["badge"] and n["severity"] in ("held", "warn", "bad")


# ── the scale ─────────────────────────────────────────────────────────

AXES = (
    Axis("orchestration", "Orchestration", 20),
    Axis("editability", "Editability", 20),
    Axis("reliability", "Reliability", 30),
    Axis("actionability", "Actionability", 30),
)


def _scored():
    editor = replace(
        _n("editor", "product"),
        scores=(
            Score("editability", 3, "measured", on="2026-09-08", note="a real admin screen"),
            Score("reliability", 1, "measured", on="2026-09-08", note="liveness only"),
            Score("actionability", 4, "asserted", note="claimed, not observed"),
        ),
    )
    return FakeStore([editor, _n("archive", "product")])


def test_the_scale_withholds_a_composite_and_says_why():
    row = next(r for r in build_scale(_scored(), AXES, Clock(TODAY, 30))["rows"] if r["name"] == "editor")
    assert row["composite"] is None
    assert "Orchestration (not scored)" in row["withheld_reason"]
    assert "Actionability (asserted)" in row["withheld_reason"]
    assert row["measured_weight"] == 0.5     # an assertion counts for nothing


def test_the_scale_emits_every_cell_including_the_unscored_ones():
    """A payload that omitted them would render as a gap the reader has
    to notice, and noticing an absence is exactly what people do not do."""
    body = build_scale(_scored(), AXES, Clock(TODAY, 30))
    for row in body["rows"]:
        assert [c["axis"] for c in row["cells"]] == [a.key for a in AXES]
    archive = next(r for r in body["rows"] if r["name"] == "archive")
    assert all(c["value"] is None and c["basis"] is None for c in archive["cells"])


def test_no_axes_declared_is_a_stated_reason_not_an_empty_grid():
    body = build_scale(_scored(), (), Clock(TODAY, 30))
    assert body["axes"] == []
    assert all("no axes declared" in r["withheld_reason"] for r in body["rows"])


# ── the matrix ────────────────────────────────────────────────────────

def test_the_matrix_reports_coverage_and_absence():
    body = build_matrix(_estate(), "product", "capability", Clock(TODAY, 30))
    covered = {(c["row"], c["col"]) for c in body["cells"] if c["covered"]}
    assert ("editor", "the-gate") in covered
    assert ("archive", "the-gate") not in covered


def test_the_matrix_refuses_an_unknown_lens_on_either_axis():
    for rows, cols in (("product", "nope"), ("nope", "product")):
        with pytest.raises(BoardError):
            build_matrix(_estate(), rows, cols, Clock(TODAY, 30))


def test_this_module_invents_no_coordinates():
    """The real defect behind the aggregate's position, stated directly.

    It used to be placed at `hub + (90, 90)` -- MEMBER_RADIUS_MIN copied
    by VALUE into the module that does not own positioning. In practice
    it almost certainly never collided with a real node: I reverted the
    fix and this file's distinctness test still passed, so the collision
    was latent, not observed, and it would be dishonest to claim
    otherwise.

    What WAS wrong is the coupling. A geometry constant lived in two
    modules, and `layout` was computing positions for a set of nodes that
    was not the set being drawn -- so its guarantee, however well tested,
    was a guarantee about the wrong thing. This test pins the property
    that fixes: every coordinate in the payload comes from `layout`.
    """
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "spoke" / "ledger" / "board.py"
    text = source.read_text()
    body = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )
    # any float literal is a coordinate or a threshold this module should
    # not be deciding for itself
    assert not re.findall(r"\b\d+\.\d+\b", body), re.findall(r"\b\d+\.\d+\b", body)


# ── the envelope no build may forget to fill ──────────────────────────

def _store_with_two_defects(tmp_path):
    """A dangling relation (the graph knows) AND an unreadable file (only
    the store knows). The two kinds come from different places, which is
    why one build could report the first and not the second."""
    import subprocess

    root = tmp_path / "store"
    (root / "ledger").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "MEMORY.md").write_text("- [a](a.md) - hook\n")
    (root / "ledger" / "widget.md").write_text(
        "---\nname: widget\ntype: product\nstate: open\ntitle: Widget\n---\n\nbody\n")
    (root / "ledger" / "orphan.md").write_text(
        "---\nname: orphan\ntype: item\nstate: open\ntitle: Orphan\n"
        "relations:\n  - rel: touches\n    to: ghost\n---\n\nbody\n")
    (root / "ledger" / "broken.md").write_text("---\nname: [unclosed\n---\n\nbody\n")
    from spoke.ledger.store import LedgerStore
    return LedgerStore(root)


@pytest.mark.parametrize("build", ["view", "matrix", "scale"])
def test_every_build_reports_both_kinds_of_unread_thing(tmp_path, build):
    """`build_matrix` passed `store=None` to the skip payload, so it was
    structurally incapable of reporting a file it could not read -- only
    a dangling relation. The matrix is the coverage view; a file it never
    read is exactly the thing that would make a coverage count wrong."""
    store = _store_with_two_defects(tmp_path)
    if build == "view":
        body = build_view(store, "product", Clock(TODAY, 30))
    elif build == "matrix":
        body = build_matrix(store, "product", "capability", Clock(TODAY, 30))
    else:
        body = build_scale(store, AXES, Clock(TODAY, 30))
    kinds = {s["kind"] for s in body["skipped"]}
    assert kinds == {"dangling", "unreadable"}, (build, body["skipped"])
    assert body["skip_headings"] and body["skipped_lines"]


def test_the_skip_lines_are_counted_once_not_per_entry(tmp_path):
    """The MCP scale tool formatted these itself and printed "1 entry"
    for each, so three unreadable files claimed to be three separate
    single entries. One renderer, one count."""
    store = _store_with_two_defects(tmp_path)
    lines = build_matrix(store, "product", "capability", Clock(TODAY, 30))["skipped_lines"]
    assert any(l.startswith("1 entry") for l in lines)
    assert not [l for l in lines if l.startswith("1 entry") and "," in l]


@pytest.mark.parametrize("build", ["view", "matrix"])
def test_a_radius_below_one_is_refused_by_every_build(tmp_path, build):
    """Measured on a real store before this: /api/lens?hops=0 was a 422
    and /api/matrix?hops=0 was a 200 with 480 cells, 0 covered and 480
    `absent` flags -- a healthy estate rendered as "nothing is connected
    to anything", by the view whose purpose is that a coverage count
    stops being a number somebody had to trust."""
    store = _store_with_two_defects(tmp_path)
    with pytest.raises(BoardError) as e:
        if build == "view":
            build_view(store, "product", Clock(TODAY, 30), hops=0)
        else:
            build_matrix(store, "product", "capability", Clock(TODAY, 30), hops=0)
    assert "at least 1" in str(e.value)


def test_the_time_lens_places_a_node_by_when_it_was_opened_not_when_it_was_last_touched():
    """A doctor run writes `updated` on every node it probes. If the axis
    read `updated`, each morning's probe would drag those decisions to
    today and the lens would show activity, not chronology."""
    store = FakeStore([
        replace(_n("early", "decision"), opened="2026-07-01", updated="2026-09-13"),
        replace(_n("late", "decision"), opened="2026-09-01", updated="2026-07-02"),
        _n("nodate", "decision"),
    ])
    v = _view(store, lens="time")
    pos = v["positions"]
    assert pos["early"][0] < pos["late"][0], "opened decides the order, updated does not"
    ticks = {t["label"] for t in v["axis"]}
    assert "2026-07-01" in ticks and "2026-09-01" in ticks
    assert pos["nodate"][0] < pos["early"][0], "the undated block sits before the axis"


def test_the_scale_ships_the_projects_words_for_its_type_selector():
    """The header said 'package' while the selector said 'product': the
    selector was four hardcoded options. It now draws from the engine's
    list and the project's vocabulary, both shipped in the payload."""
    body = build_scale(_scored(), AXES, Clock(TODAY, 30), vocabulary={"product": "package"})
    assert body["kind"] == "package"
    assert body["vocabulary"] == {"product": "package"}
    assert "product" in body["node_types"] and "capability" in body["node_types"]
