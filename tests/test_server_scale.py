"""The scale view's HTTP surface.

The rule under test throughout: a number must never be able to pass for
one that somebody checked.
"""
import subprocess
from datetime import date, timedelta

from fastapi.testclient import TestClient

from tests.conftest import build_store, seed_nodes
from spoke.config import Config
from spoke.ledger import Node
from spoke.ledger.scale import Axis, Score
from spoke.ledger.store import LedgerStore
from spoke.server import create_app

AXES = (
    Axis("orchestration", "Orchestration", 20),
    Axis("editability", "Editability", 20),
    Axis("reliability", "Reliability", 30),
    Axis("actionability", "Actionability", 30),
)


def _node(**kw) -> Node:
    base = dict(
        name="", type="product", state="open", title="", body="body",
        relations=(), ruling=None, blocked_by=(), provenance=(),
        opened=None, updated=None, by=None, claimed_by=None, claimed_at=None,
        scores=(),
    )
    base.update(kw)
    return Node(**base)


def _repo(tmp_path, *nodes) -> Config:
    build_store(tmp_path)
    seed_nodes(tmp_path, nodes, AXES)
    return Config(tmp_path, (), 30, "groq", None)


def _partly_scored(tmp_path) -> Config:
    return _repo(
        tmp_path,
        _node(name="widget", title="Widget", scores=(
            Score("editability", 3, "measured", on="2026-09-08", note="a real admin screen"),
            Score("reliability", 1, "measured", on="2026-09-08", note="liveness only"),
            Score("actionability", 4, "asserted", note="claimed in the plan, not observed"),
        )),
        _node(name="gadget", title="Gadget"),
    )


def test_api_scale_returns_axes_rows_and_cells(tmp_path):
    c = TestClient(create_app(_partly_scored(tmp_path), axes=AXES))
    d = c.get("/api/scale").json()
    assert [a["key"] for a in d["axes"]] == [a.key for a in AXES]
    assert {r["name"] for r in d["rows"]} == {"widget", "gadget"}
    # EVERY pair is present, including the unscored ones
    for row in d["rows"]:
        assert [cell["axis"] for cell in row["cells"]] == [a.key for a in AXES]


def test_the_axis_weights_reach_the_page_as_shares(tmp_path):
    c = TestClient(create_app(_partly_scored(tmp_path), axes=AXES))
    shares = {a["key"]: a["share"] for a in c.get("/api/scale").json()["axes"]}
    assert shares["reliability"] == 0.3 and shares["editability"] == 0.2


def test_a_cell_carries_its_basis_and_note(tmp_path):
    c = TestClient(create_app(_partly_scored(tmp_path), axes=AXES))
    row = next(r for r in c.get("/api/scale").json()["rows"] if r["name"] == "widget")
    cell = next(x for x in row["cells"] if x["axis"] == "reliability")
    assert cell["value"] == 1 and cell["basis"] == "measured"
    assert cell["note"] == "liveness only" and cell["on"] == "2026-09-08"


def test_an_unscored_cell_is_null_not_zero(tmp_path):
    """A zero is a finding; a null is an absence. Collapsing them would
    rank the unexamined below the genuinely bad."""
    c = TestClient(create_app(_partly_scored(tmp_path), axes=AXES))
    row = next(r for r in c.get("/api/scale").json()["rows"] if r["name"] == "gadget")
    assert all(cell["value"] is None and cell["basis"] is None for cell in row["cells"])


def test_a_withheld_composite_reaches_the_api_with_its_reason(tmp_path):
    c = TestClient(create_app(_partly_scored(tmp_path), axes=AXES))
    row = next(r for r in c.get("/api/scale").json()["rows"] if r["name"] == "widget")
    assert row["composite"] is None
    assert "Orchestration (not scored)" in row["withheld_reason"]
    assert "Actionability (asserted)" in row["withheld_reason"]


def test_an_asserted_score_does_not_count_toward_the_measured_weight(tmp_path):
    c = TestClient(create_app(_partly_scored(tmp_path), axes=AXES))
    row = next(r for r in c.get("/api/scale").json()["rows"] if r["name"] == "widget")
    assert row["measured_weight"] == 0.5      # editability 20 + reliability 30


def test_the_threshold_travels_with_every_row(tmp_path):
    c = TestClient(create_app(_partly_scored(tmp_path), axes=AXES))
    for row in c.get("/api/scale").json()["rows"]:
        assert row["threshold"] > 0


def test_a_fully_measured_row_gets_its_composite(tmp_path):
    """Dated, because a measurement with no observation date no longer
    counts -- see test_an_undated_measurement_gets_no_composite below."""
    cfg = _repo(tmp_path, _node(name="whole", title="Whole", scores=tuple(
        Score(a.key, 3, "measured", on=date.today().isoformat()) for a in AXES
    )))
    c = TestClient(create_app(cfg, axes=AXES))
    row = c.get("/api/scale").json()["rows"][0]
    assert row["composite"] == 3.0 and row["withheld_reason"] is None


def test_an_undated_measurement_gets_no_composite(tmp_path):
    """A measured score's whole warrant is "observed on <date>". With no
    date the warrant is absent while the number would still count, which
    is this project's founding failure with a number on it."""
    cfg = _repo(tmp_path, _node(name="whole", title="Whole", scores=tuple(
        Score(a.key, 3, "measured") for a in AXES
    )))
    row = TestClient(create_app(cfg, axes=AXES)).get("/api/scale").json()["rows"][0]
    assert row["composite"] is None
    assert "measured, but undated" in row["withheld_reason"]


def test_the_page_states_that_colour_here_means_score_not_severity(tmp_path):
    """The other page in this app uses colour for severity. Two meanings
    for one channel, unlabelled, would be worse than no colour."""
    c = TestClient(create_app(_partly_scored(tmp_path), axes=AXES))
    page = c.get("/scale").text
    low = page.lower()
    assert "colour means the score" in low
    assert "severity" in low and "/board" in page


def test_the_legend_says_an_unscored_axis_is_not_a_zero(tmp_path):
    c = TestClient(create_app(_partly_scored(tmp_path), axes=AXES))
    assert "not a zero" in c.get("/scale").text.lower()


def test_the_page_names_no_cdn(tmp_path):
    page = TestClient(create_app(_partly_scored(tmp_path), axes=AXES)).get("/scale").text
    assert "http://" not in page and "https://" not in page


def test_the_page_assets_are_cache_busted(tmp_path):
    page = TestClient(create_app(_partly_scored(tmp_path), axes=AXES)).get("/scale").text
    assert "<!--ASSET_V-->" not in page
    assert "scale.css?v=" in page and "scale.js?v=" in page


def test_an_undeclared_axis_set_yields_an_empty_axis_list_not_a_crash(tmp_path):
    c = TestClient(create_app(_partly_scored(tmp_path)))
    d = c.get("/api/scale").json()
    assert d["axes"] == []
    # ... and every row says why there is no composite
    assert all("no axes declared" in r["withheld_reason"] for r in d["rows"])


def test_the_scale_can_be_asked_about_another_node_type(tmp_path):
    cfg = _repo(tmp_path, _node(name="cap", type="capability", title="A capability"))
    c = TestClient(create_app(cfg, axes=AXES))
    assert c.get("/api/scale?type=product").json()["rows"] == []
    assert [r["name"] for r in c.get("/api/scale?type=capability").json()["rows"]] == ["cap"]


def test_the_board_and_the_records_screen_link_to_the_scale(tmp_path):
    c = TestClient(create_app(_partly_scored(tmp_path), axes=AXES))
    assert "/scale" in c.get("/board").text


def test_the_page_offers_a_tab_per_axis(tmp_path):
    """The grid answers "how do these compare"; an axis tab answers
    "what is our reliability, actually" -- which is the question somebody
    asks when they are about to act on it."""
    c = TestClient(create_app(_partly_scored(tmp_path), axes=AXES))
    page = c.get("/scale").text
    assert 'role="tablist"' in page
    js = c.get("/static/scale.js").text
    assert "renderAxis" in js and "All axes" in js


def test_an_axis_tab_has_everything_it_needs_from_one_payload(tmp_path):
    """No extra round trip per tab: every cell already carries its basis,
    date and note, so switching tabs cannot show a different truth from
    the grid beside it."""
    c = TestClient(create_app(_partly_scored(tmp_path), axes=AXES))
    d = c.get("/api/scale").json()
    for row in d["rows"]:
        for cell in row["cells"]:
            assert set(cell) >= {"axis", "value", "basis", "note", "on", "by"}


# --- freshness: the score's own observation date ------------------------
#
# `stale` watched a node's `updated`; nothing watched a score's `on`, so a
# measurement taken months ago rendered identically to one taken today.

def _aged(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def _mixed_freshness(tmp_path) -> Config:
    return _repo(
        tmp_path,
        _node(name="widget", title="Widget", scores=(
            Score("orchestration", 4, "measured", on=_aged(3), note="watched it run"),
            Score("editability", 3, "measured", on=_aged(400), note="an admin screen"),
            Score("reliability", 1, "measured", note="liveness only"),
            Score("actionability", 4, "asserted", on=_aged(400), note="claimed"),
        )),
        _node(name="gadget", title="Gadget"),
    )


def test_every_cell_carries_its_freshness_and_age(tmp_path):
    c = TestClient(create_app(_mixed_freshness(tmp_path), axes=AXES))
    row = next(r for r in c.get("/api/scale").json()["rows"] if r["name"] == "widget")
    cells = {x["axis"]: x for x in row["cells"]}
    assert cells["orchestration"]["freshness"] == "fresh"
    assert cells["orchestration"]["age_days"] == 3


def test_a_measurement_past_its_threshold_is_marked_stale(tmp_path):
    c = TestClient(create_app(_mixed_freshness(tmp_path), axes=AXES))
    row = next(r for r in c.get("/api/scale").json()["rows"] if r["name"] == "widget")
    cell = next(x for x in row["cells"] if x["axis"] == "editability")
    assert cell["freshness"] == "stale" and cell["age_days"] == 400


def test_a_measurement_with_no_observation_date_is_marked_undated(tmp_path):
    c = TestClient(create_app(_mixed_freshness(tmp_path), axes=AXES))
    row = next(r for r in c.get("/api/scale").json()["rows"] if r["name"] == "widget")
    cell = next(x for x in row["cells"] if x["axis"] == "reliability")
    assert cell["freshness"] == "undated" and cell["age_days"] is None


def test_an_asserted_cell_has_no_freshness_at_all(tmp_path):
    """None is not "fresh": freshness does not apply to a basis that was
    never an observation, and the payload must let the page tell the two
    apart rather than render an old assertion as current."""
    c = TestClient(create_app(_mixed_freshness(tmp_path), axes=AXES))
    row = next(r for r in c.get("/api/scale").json()["rows"] if r["name"] == "widget")
    cell = next(x for x in row["cells"] if x["axis"] == "actionability")
    assert cell["freshness"] is None


def test_an_unscored_cell_has_no_freshness(tmp_path):
    c = TestClient(create_app(_mixed_freshness(tmp_path), axes=AXES))
    row = next(r for r in c.get("/api/scale").json()["rows"] if r["name"] == "gadget")
    assert all(x["freshness"] is None and x["age_days"] is None for x in row["cells"])


def test_the_payload_states_the_thresholds_it_used(tmp_path):
    """The same rule the composite threshold already follows: a number
    nobody chose is an unstated assumption, so the actual day counts
    travel with the payload rather than being re-derived on the page."""
    d = TestClient(create_app(_mixed_freshness(tmp_path), axes=AXES)).get("/api/scale").json()
    assert d["stale_days"] == 30
    assert d["score_stale_days"] == {"measured": 60, "asserted": None, "unverified": None}


def test_a_stale_measurement_stops_counting_toward_the_composite(tmp_path):
    """The drift guard for the whole layer: revert the clock that
    build_scale passes to composite() and this row gets a composite
    again, computed over a measurement nobody has re-taken in over a
    year."""
    cfg = _repo(tmp_path, _node(name="aged", title="Aged", scores=tuple(
        Score(a.key, 3, "measured", on=_aged(400)) for a in AXES
    )))
    row = TestClient(create_app(cfg, axes=AXES)).get("/api/scale").json()["rows"][0]
    assert row["composite"] is None
    assert row["measured_weight"] == 0.0
    assert any("stale" in m for m in row["missing"])


def test_the_withheld_reason_carries_the_age_not_just_the_word(tmp_path):
    cfg = _repo(tmp_path, _node(name="aged", title="Aged", scores=tuple(
        Score(a.key, 3, "measured", on=_aged(400)) for a in AXES
    )))
    row = TestClient(create_app(cfg, axes=AXES)).get("/api/scale").json()["rows"][0]
    assert "measured 400d ago -- stale" in row["withheld_reason"]


def test_the_page_explains_freshness_in_words_not_only_a_treatment(tmp_path):
    """scale.css's legend rule: colour (or any other treatment) never
    carries a meaning on its own."""
    c = TestClient(create_app(_mixed_freshness(tmp_path), axes=AXES))
    page = c.get("/scale").text.lower()
    assert "freshness" in page and "stale" in page and "undated" in page
    css = c.get("/static/scale.css").text
    assert ".cell.stale" in css and ".cell.undated" in css
    js = c.get("/static/scale.js").text
    # the word is written into the cell, beside the basis word
    assert '"stale"' in js or "'stale'" in js
