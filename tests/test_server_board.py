"""The board's HTTP surface.

The rule under test throughout: a layer being hidden must never make a
problem invisible. Everything else here (shapes, badges, positions,
skipped) exists to serve that.
"""
import subprocess

from fastapi.testclient import TestClient

from tests.conftest import build_store, seed_nodes
from spoke.config import Config
from spoke.ledger import Node, Relation
from spoke.ledger.store import LedgerStore
from spoke.server import create_app


def _node(**kw) -> Node:
    base = dict(
        name="", type="item", state="open", title="", body="body",
        relations=(), ruling=None, blocked_by=(), provenance=(),
        opened=None, updated=None, by=None, claimed_by=None, claimed_at=None,
    )
    base.update(kw)
    return Node(**base)


def _repo(tmp_path, *nodes, stylesheet=None) -> Config:
    build_store(tmp_path)
    seed_nodes(tmp_path, nodes)
    # keyword, not positional: Config gained a field between
    # llm_base_url and board_stylesheet and every positional call
    # site silently shifted.
    return Config(tmp_path, (), 30, "groq", None, board_stylesheet=stylesheet)


def _estate(tmp_path, **kw) -> Config:
    """A product whose only trouble is three layers down, plus a
    capability one product has and another does not."""
    return _repo(
        tmp_path,
        _node(name="some-product", type="product", state="open", title="A product"),
        _node(name="other-product", type="product", state="open", title="Another"),
        _node(name="the-gate", type="capability", state="open", title="A capability"),
        _node(name="blocker", state="open", title="The cause"),
        _node(name="deeply-nested-item", state="blocked", title="Deep work",
              blocked_by=("blocker",),
              relations=(Relation("touches", "some-product"),
                         Relation("blocked_by", "blocker"))),
        _node(name="wire-gate", state="open", title="Wires the gate",
              relations=(Relation("touches", "some-product"),
                         Relation("touches", "the-gate"))),
        **kw,
    )


def test_api_lens_returns_nodes_flags_and_positions(tmp_path):
    c = TestClient(create_app(_estate(tmp_path)))
    body = c.get("/api/lens?name=product").json()
    assert body["nodes"] and body["positions"] and "skipped" in body
    assert set(body["positions"]) == {n["name"] for n in body["nodes"]}


def test_api_lens_refuses_an_unknown_lens(tmp_path):
    c = TestClient(create_app(_estate(tmp_path)))
    r = c.get("/api/lens?name=nope")
    assert r.status_code == 422
    assert "known lenses" in r.text


def test_api_lens_refuses_an_unknown_zoom_or_flag(tmp_path):
    """A bad parameter must not fall back to a working default: a page
    answering a question nobody asked looks exactly like one that
    answered the question they did."""
    c = TestClient(create_app(_estate(tmp_path)))
    assert c.get("/api/lens?name=product&zoom=nope").status_code == 422
    assert c.get("/api/lens?name=product&flag=nope").status_code == 422


def test_api_matrix_returns_rows_cols_and_cells(tmp_path):
    c = TestClient(create_app(_estate(tmp_path)))
    body = c.get("/api/matrix?rows=product&cols=capability").json()
    assert "rows" in body and "cols" in body and "cells" in body
    covered = {(x["row"], x["col"]) for x in body["cells"] if x["covered"]}
    assert ("some-product", "the-gate") in covered
    assert ("other-product", "the-gate") not in covered


def test_api_matrix_refuses_an_unknown_lens(tmp_path):
    c = TestClient(create_app(_estate(tmp_path)))
    assert c.get("/api/matrix?rows=product&cols=nope").status_code == 422


def test_board_page_is_served_and_names_no_cdn(tmp_path):
    c = TestClient(create_app(_estate(tmp_path)))
    html = c.get("/board").text
    assert "board.js" in html
    assert "http://" not in html and "https://" not in html


def test_api_lens_labels_each_node_with_its_zoom_layer(tmp_path):
    """Semantic zoom is decided server-side so the browser only draws."""
    c = TestClient(create_app(_estate(tmp_path)))
    body = c.get("/api/lens?name=product&zoom=close").json()
    layers = {n["name"]: n["layer"] for n in body["nodes"]}
    assert layers["some-product"] < layers["deeply-nested-item"]


def test_a_flag_from_a_hidden_layer_rolls_up_to_a_visible_ancestor(tmp_path):
    """The founding rule as a UI rule: hiding a layer must not hide a
    problem. At the far zoom the item is not drawn at all, and the
    product still shows the block AND names where it came from."""
    c = TestClient(create_app(_estate(tmp_path)))
    body = c.get("/api/lens?name=product&zoom=far").json()
    drawn = {n["name"] for n in body["nodes"]}
    assert "deeply-nested-item" not in drawn

    prod = next(n for n in body["nodes"] if n["name"] == "some-product")
    assert any(f["kind"] == "blocked" for f in prod["flags"])
    assert any(f["origin"] == "deeply-nested-item" for f in prod["flags"])


def test_every_flagged_node_carries_shape_and_badge_not_only_colour(tmp_path):
    c = TestClient(create_app(_estate(tmp_path)))
    body = c.get("/api/lens?name=product").json()
    flagged = [n for n in body["nodes"] if n["flags"]]
    assert flagged
    for n in flagged:
        assert n["shape"] and n["badge"]      # survives greyscale and colour-blindness
        assert n["severity"] in ("held", "warn", "bad")


def test_clients_aggregate_below_the_closest_zoom(tmp_path):
    """A client's identity is not what a map of the estate is for, and a
    hundred of them would drown everything else."""
    cfg = _repo(
        tmp_path,
        _node(name="some-product", type="product", state="open", title="A product"),
        _node(name="c1", type="client", state="open", title="client-aaa",
              relations=(Relation("serves", "some-product"),)),
        _node(name="c2", type="client", state="open", title="client-bbb",
              relations=(Relation("serves", "some-product"),)),
    )
    c = TestClient(create_app(cfg))

    close = c.get("/api/lens?name=client&zoom=close").json()
    names = {n["name"] for n in close["nodes"]}
    assert "c1" in names and "c2" in names   # clients hub their own lens

    prod = c.get("/api/lens?name=product&zoom=close").json()
    labels = [n["label"] for n in prod["nodes"] if n["type"] == "clients"]
    assert labels == ["2 clients · 0 flagged"]

    closest = c.get("/api/lens?name=product&zoom=closest").json()
    assert {"c1", "c2"} <= {n["name"] for n in closest["nodes"]}
    assert not [n for n in closest["nodes"] if n["type"] == "clients"]


def test_the_stylesheet_key_links_an_external_sheet_when_configured(tmp_path):
    c = TestClient(create_app(_estate(tmp_path, stylesheet="/static/app.css")))
    assert 'rel="stylesheet" href="/static/app.css"' in c.get("/board").text


def test_a_stylesheet_that_is_not_a_path_or_https_url_is_refused(tmp_path):
    """The value lands in an href on a page that talks to the store. A
    config file is not a trusted input just because it is local."""
    cfg = _estate(tmp_path)
    c = TestClient(create_app(cfg, config_path=tmp_path / "config.toml"))
    r = c.post("/api/config", json={
        "store_path": str(tmp_path), "accepted_absences": [], "stale_days": 30,
        "llm_provider": "groq",
        "board_stylesheet": '"><script>fetch("/api/records")</script>',
    })
    assert r.status_code == 422
    assert "board_stylesheet" in r.text


def test_skipped_nodes_reach_the_api(tmp_path):
    cfg = _repo(
        tmp_path,
        _node(name="some-product", type="product", state="open", title="A product"),
        _node(name="orphan", state="open", title="Points nowhere",
              relations=(Relation("touches", "ghost"),)),
    )
    c = TestClient(create_app(cfg))
    body = c.get("/api/lens?name=product").json()
    skipped = body["skipped"]
    assert any("ghost" in s["detail"] for s in skipped)
    # the KIND travels with it, and so does the one phrasing of that kind
    assert {s["kind"] for s in skipped} == {"dangling"}
    assert "pointed at a node that does not exist" in body["skip_headings"]["dangling"]


def test_a_flag_filter_narrows_the_hub(tmp_path):
    c = TestClient(create_app(_estate(tmp_path)))
    body = c.get("/api/lens?name=product&flag=blocked").json()
    names = {n["name"] for n in body["nodes"] if n["hub"]}
    assert names == {"some-product"}


def test_board_assets_are_cache_busted_by_their_mtime(tmp_path):
    """An upgraded Spoke serving last week's CSS against this week's
    markup looks like a broken page, not a stale cache."""
    c = TestClient(create_app(_estate(tmp_path)))
    html = c.get("/board").text
    assert "<!--ASSET_V-->" not in html
    assert "board.css?v=" in html and "board.js?v=" in html


def test_one_client_is_not_reported_as_1_clients(tmp_path):
    cfg = _repo(
        tmp_path,
        _node(name="some-product", type="product", state="open", title="A product"),
        _node(name="c1", type="client", state="open", title="client-aaa",
              relations=(Relation("serves", "some-product"),)),
    )
    c = TestClient(create_app(cfg))
    body = c.get("/api/lens?name=product&zoom=close").json()
    labels = [n["label"] for n in body["nodes"] if n["type"] == "clients"]
    assert labels == ["1 client · 0 flagged"]


def test_the_board_names_hubs_and_spokes(tmp_path):
    """The metaphor has to be on the page, not only in the design docs."""
    c = TestClient(create_app(_estate(tmp_path)))
    page = c.get("/board").text
    assert "hub" in page.lower() and "spoke" in page.lower()

    body = c.get("/api/lens?name=product&zoom=close").json()
    prod = next(n for n in body["nodes"] if n["name"] == "some-product")
    item = next(n for n in body["nodes"] if n["name"] == "wire-gate")
    assert prod["hub"] is True and item["hub"] is False


def test_the_board_shows_the_projects_own_word_for_a_type(tmp_path):
    """Criterion 4: a project that calls its units something else must
    never be shown the engine's word by a tool that has been told the
    word it actually uses."""
    c = TestClient(create_app(_estate(tmp_path), vocabulary={"product": "app"}))
    body = c.get("/api/lens?name=product").json()
    prod = next(n for n in body["nodes"] if n["name"] == "some-product")
    assert prod["kind"] == "app"
    assert prod["type"] == "product"     # the engine's type is unchanged


def test_without_a_vocabulary_the_kind_is_the_engine_type(tmp_path):
    c = TestClient(create_app(_estate(tmp_path)))
    body = c.get("/api/lens?name=product").json()
    prod = next(n for n in body["nodes"] if n["name"] == "some-product")
    assert prod["kind"] == "product"


def test_the_canvas_centre_is_a_label_and_not_a_node(tmp_path):
    """The ledger has no canonical root by design. A real node at the
    middle would re-impose the single hierarchy the model removes, so the
    centre is drawn, not stored."""
    c = TestClient(create_app(_estate(tmp_path)))
    js = c.get("/static/board.js").text
    assert "drawCentre" in js
    assert '"Hub"' in js                       # the word is on the canvas
    body = c.get("/api/lens?name=product").json()
    assert not any(n["name"].lower() == "hub" for n in body["nodes"])
    assert "centre" not in {n["type"] for n in body["nodes"]}


def test_the_board_legend_says_the_centre_is_not_a_node(tmp_path):
    page = TestClient(create_app(_estate(tmp_path))).get("/board").text
    assert "not a node" in page.lower()


def test_two_different_defects_are_not_reported_as_one_kind(tmp_path):
    """The defect this typing exists to remove.

    A dangling relation and a file that could not be read are different
    conditions. They used to be concatenated into one list of strings,
    and with the kind gone each page guessed it back: the board told the
    reader every entry was a dangling relation, the scale told the reader
    every entry could not be read, about the identical field. Each was
    wrong half the time and nothing caught it, because the only test
    asserted the field ARRIVED.
    """
    cfg = _repo(
        tmp_path,
        _node(name="some-product", type="product", state="open", title="A product"),
        _node(name="orphan", state="open", title="Points nowhere",
              relations=(Relation("touches", "ghost"),)),
    )
    # a file inside ledger/ that cannot be parsed
    (tmp_path / "ledger" / "broken.md").write_text("---\nname: [unclosed\n---\n\nbody\n")

    c = TestClient(create_app(cfg))
    for path in ("/api/lens?name=product", "/api/scale"):
        body = c.get(path).json()
        kinds = {s["kind"] for s in body["skipped"]}
        assert kinds == {"dangling", "unreadable"}, (path, body["skipped"])
        # and each page renders a heading it was GIVEN, per kind
        for kind in kinds:
            assert body["skip_headings"][kind]
        assert body["skip_headings"]["dangling"] != body["skip_headings"]["unreadable"]


def test_both_pages_are_handed_the_same_phrasing(tmp_path):
    """Neither page authors a sentence about this list any more."""
    c = TestClient(create_app(_estate(tmp_path)))
    lens = c.get("/api/lens?name=product").json()["skip_headings"]
    scale = c.get("/api/scale").json()["skip_headings"]
    assert lens == scale
    # ONE renderer for the envelope, loaded by both pages -- not one copy
    # per page that reads the headings identically today and drifts
    # tomorrow. The test used to look for `skip_headings` in each page's
    # own script, which was satisfied by two copies of the same code.
    shared = c.get("/static/envelope.js").text
    assert "skip_headings" in shared
    for page, js in (("board", "board.js"), ("scale", "scale.js")):
        markup = c.get(f"/{page}").text
        assert "/static/envelope.js" in markup
        source = c.get(f"/static/{js}").text
        assert "function renderSkipped" not in source
        # the old hand-authored claims are gone from everywhere
        assert "point at a node that does not exist" not in source
        assert "could not be read:" not in source
        assert "point at a node that does not exist" not in shared
        assert "could not be read:" not in shared


def test_the_scale_page_leads_with_the_command_when_there_are_no_axes(tmp_path):
    """Reported from the scale page, through the scale page: with no axes
    the page was a legend for a grid that did not exist, followed by one
    'No composite. no axes declared' card per node, and the one command
    that changes any of it sat between them. Now the empty state is the
    page, and the command is its first line of code."""
    c = TestClient(create_app(_estate(tmp_path)))  # no axes passed
    page = c.get("/scale").text
    assert 'id="noaxes"' in page
    assert "spoke projects axes --set" in page
    assert page.index('id="noaxes"') < page.index('id="legend"')
    js = c.get("/static/scale.js").text
    # the JS hides the legend, grid and composites when bare, and the old
    # in-grid copy of the message is gone so there is one empty state
    assert '$("legend").hidden = bare' in js
    assert '$("composites").hidden = bare' in js
    assert "declared no capability axes" not in js


def test_the_freshness_legend_stacks_label_over_prose(tmp_path):
    """The widest chip claimed the `auto` column and left the prose a
    strip twelve words wide."""
    c = TestClient(create_app(_estate(tmp_path)))
    css = c.get("/static/scale.css").text
    rule = css[css.index("#freshness {"):css.index("}", css.index("#freshness {"))]
    assert "grid-template-columns: 1fr" in rule
    assert "auto 1fr" not in rule


def test_the_scale_page_renders_the_projects_own_top_not_the_packages(tmp_path):
    """A 0-4 project on a 0-5 ramp shows a full mark as 80%."""
    from spoke.ledger.scale import Axis

    axes = (Axis("reliability", "Reliability", 1.0),)
    c = TestClient(create_app(_estate(tmp_path / "four"), axes=axes, scale_max=4))
    d = c.get("/api/scale").json()
    assert d["max_value"] == 4
    c5 = TestClient(create_app(_estate(tmp_path / "five"), axes=axes))
    assert c5.get("/api/scale").json()["max_value"] == 5


def test_the_scale_applies_the_same_standing_rule_as_the_lens(tmp_path):
    """An abandoned product lost its hub on the product lens and kept a
    row on the scale. One rule, declared once on the Lens, read by both."""
    from spoke.ledger.scale import Axis

    cfg = _estate(tmp_path)
    gone = _node(name="gone-product", type="product", state="abandoned", title="gone",
                 ruling="withdrawn")
    seed_nodes(tmp_path, [gone])
    c = TestClient(create_app(cfg, axes=(Axis("r", "R", 1.0),)))
    rows = [r["name"] for r in c.get("/api/scale").json()["rows"]]
    assert "gone-product" not in rows
    assert rows, "the live products are still there"


def test_the_view_ships_the_tables_the_key_is_drawn_from(tmp_path):
    """A legend authored by hand is a second statement of what the
    colours mean, and two statements drift. The page draws its key from
    these, with the same classes the nodes use."""
    from spoke.ledger import NODE_TYPES
    from spoke.ledger.board import SEVERITY

    c = TestClient(create_app(_estate(tmp_path)))
    d = c.get("/api/lens?name=product").json()
    assert d["severities"] == SEVERITY
    assert set(d["severities"]) == set(d["flag_kinds"]), "every kind has a colour"
    assert d["severity_order"] == ["held", "warn", "bad"]
    assert d["node_types"] == list(NODE_TYPES)


def test_the_map_owns_the_board_page(tmp_path):
    """The canvas used to hide behind a 'Draw the whole lens' button and
    a paragraph, and at a narrow viewport was the smallest thing on the
    map page. It is always drawn now; the prose is a disclosure; the key
    is a <dl> the script fills."""
    c = TestClient(create_app(_estate(tmp_path)))
    page = c.get("/board").text
    assert 'id="canvas"' in page and 'id="key"' in page
    assert "showall" not in page and 'id="legend"' not in page
    assert "<details id=\"howto\">" in page
    js = c.get("/static/board.js").text
    assert "function renderKey" in js
    assert "showall" not in js


def test_the_mark_is_the_installs_logo_when_configured_and_the_prism_when_not(tmp_path):
    from spoke.config import Config
    store = _estate(tmp_path).store_path
    bare = TestClient(create_app(Config(store, (), 30, "groq", None))).get("/board").text
    assert 'class="prism"' in bare and 'class="logo"' not in bare
    assert "<!--MARK-->" not in bare

    branded = Config(store, (), 30, "groq", None, brand_logo_url="https://acme.example/mark.png")
    page = TestClient(create_app(branded)).get("/board").text
    assert '<img class="logo" src="https://acme.example/mark.png" alt="">' in page
    assert 'class="prism"' not in page

    # a hand-edited URL that is not http(s)/same-origin gets the prism, not a src
    bad = Config(store, (), 30, "groq", None, brand_logo_url="javascript:alert(1)")
    page = TestClient(create_app(bad)).get("/board").text
    assert "javascript:" not in page and 'class="prism"' in page


def test_the_logo_url_is_refused_on_save_unless_same_origin_or_https(tmp_path):
    from spoke.config import Config
    store = _estate(tmp_path).store_path
    c = TestClient(create_app(Config(store, (), 30, "groq", None), config_path=tmp_path / "config.toml"))
    body = {"store_path": str(store), "accepted_absences": [], "stale_days": 30, "llm_provider": "groq"}
    assert c.post("/api/config", json={**body, "brand_logo_url": "http://plain.example/x.png"}).status_code == 422
    assert c.post("/api/config", json={**body, "brand_logo_url": "javascript:x"}).status_code == 422
    assert c.post("/api/config", json={**body, "brand_logo_url": "https://acme.example/m.png"}).status_code == 200
    assert c.get("/api/config").json()["file"]["brand_logo_url"] == "https://acme.example/m.png"


def test_the_view_names_every_flag_origin_and_what_each_level_of_detail_adds(tmp_path):
    """The rail groups by origin -- eight hubs each showing '1 held' are
    one hold reaching eight -- and needs what the origin IS. The detail
    control says what each level adds, in the project's words, rather
    than 'middle'."""
    c = TestClient(create_app(_estate(tmp_path), vocabulary={"item": "documentation"}))
    d = c.get("/api/lens?name=product").json()
    origins = {f["origin"] for n in d["nodes"] for f in n["flags"]}
    assert origins, "the fixture has flags"
    assert set(d["origins"]) == origins
    for o in d["origins"].values():
        assert set(o) == {"name", "kind", "state", "title", "ruling"}
    assert d["zoom_adds"] == {
        "middle": ["capability", "decision"],
        "close": ["documentation"],
        "closest": ["client"],
    }
    assert "zoom_adds" in d and "far" not in d["zoom_adds"], "the first level adds nothing"


def test_the_rail_and_detail_control_are_drawn_from_the_payload(tmp_path):
    c = TestClient(create_app(_estate(tmp_path)))
    js = c.get("/static/board.js").text
    assert "function renderDetail" in js and "zoom_adds" in js
    assert "groups.get(f.origin)" in js, "the rail groups by origin"
    page = c.get("/board").text
    assert 'type="range"' not in page, "detail is a segmented control, not a slider"
    assert 'id="howto"' in page and 'class="gloss"' in page
    for term in ("hub", "spoke", "lens", "detail", "blocked", "deviates", "held", "unruled", "stale", "absent"):
        assert f">{term}</dt>" in page or f'></span> {term}</dt>' in page, term
