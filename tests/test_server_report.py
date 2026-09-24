"""A problem found IN a surface, reported FROM it, becomes an open item
the next session is handed -- through the same gate everything else
meets, and named back to the reporter."""
from __future__ import annotations

from fastapi.testclient import TestClient

from spoke.config import Config
from spoke.ledger.store import LedgerStore
from spoke.ledger.preamble import render_preamble
from spoke.server import create_app
from tests.conftest import build_store


def _client(tmp_path):
    store = build_store(tmp_path / "store")  # git-backed: a write commits
    return TestClient(create_app(Config(store, (), 30, "groq", None))), store


def test_a_report_becomes_an_open_item_named_back_to_the_reporter(tmp_path):
    c, store = _client(tmp_path)
    r = c.post("/api/report", json={
        "page": "/scale", "text": "Masthead reads 'Spokeby' with no gap.\nSecond line.",
        "context": {"type": "product", "axis": "(grid)"},
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["name"].startswith("surface-masthead-reads-spokeby-with-no-gap-")
    assert d["title"] == "Surface: Masthead reads 'Spokeby' with no gap."
    assert "next session" in d["raised"]

    node = LedgerStore(store).read(d["name"])
    assert node.type == "item" and node.state == "open"
    assert node.relations == (), "related to nothing: nothing is assumed about the ledger"
    assert "Second line." in node.body
    assert "Reported from /scale on" in node.body
    assert "- type: product" in node.body and "- axis: (grid)" in node.body
    assert node.by == "board:/scale"
    assert all(p["by"] == "human:board:/scale" for p in node.provenance)


def test_the_preamble_raises_a_report_without_anyone_looking_for_it(tmp_path):
    c, store = _client(tmp_path)
    c.post("/api/report", json={"page": "/board", "text": "The centre label overlaps a node"})
    text, _ = render_preamble(LedgerStore(store).list_nodes(), [], 4000)
    assert "Surface: The centre label overlaps a node" in text, text


def test_two_reports_with_the_same_words_get_distinct_names(tmp_path):
    c, _ = _client(tmp_path)
    a = c.post("/api/report", json={"page": "/", "text": "same words"}).json()["name"]
    b = c.post("/api/report", json={"page": "/", "text": "same words"}).json()["name"]
    assert a != b and b == a + "-2"


def test_a_report_is_validated_like_any_other_input(tmp_path):
    c, _ = _client(tmp_path)
    post = lambda **kw: c.post("/api/report", json={"page": "/board", "text": "x", **kw}).status_code
    assert post(text="   ") == 422
    assert post(text="x" * 4001) == 422
    assert post(text="a\x00b") == 422
    assert post(page="/nope") == 422
    assert post(context={"k" * 41: "v"}) == 422
    assert post(context={f"k{i}": "v" for i in range(13)}) == 422
    assert post(context={"k": "a\nb"}) == 422
    assert post() == 200


def test_every_page_carries_the_report_control_and_the_shared_chrome(tmp_path):
    c, _ = _client(tmp_path)
    for path in ("/", "/board", "/scale", "/settings"):
        page = c.get(path).text
        assert "/static/chrome.css" in page, path
        assert "/static/envelope.js" in page, path
    js = c.get("/static/envelope.js").text
    assert "function installReport" in js
    for own in ("board.js", "scale.js", "app.js", "settings.js"):
        assert "installReport(" in c.get(f"/static/{own}").text, own
