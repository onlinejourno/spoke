import subprocess

from tests.conftest import build_store
from fastapi.testclient import TestClient
from spoke.config import Config
from spoke.server import create_app


def _repo(tmp_path):
    build_store(
        tmp_path,
        records={"alpha.md": "---\nname: alpha\ndescription: d\n---\n\noriginal\n"},
        index="- [Alpha](alpha.md) — hook\n",
    )
    return Config(tmp_path, (), 30, "groq", None)


def test_lists_records(tmp_path):
    c = TestClient(create_app(_repo(tmp_path)))
    data = c.get("/api/records").json()
    assert [r["name"] for r in data] == ["alpha.md"]
    assert data[0]["index_line"].endswith("hook")


def test_save_writes_and_commits(tmp_path):
    cfg = _repo(tmp_path)
    c = TestClient(create_app(cfg))
    r = c.post("/api/records/alpha.md", json={"body": "edited\n"})
    assert r.json()["ok"] is True
    assert "edited" in (tmp_path / "alpha.md").read_text()


def test_save_refused_on_broken_link_returns_422(tmp_path):
    cfg = _repo(tmp_path)
    c = TestClient(create_app(cfg))
    before = (tmp_path / "alpha.md").read_text()
    r = c.post("/api/records/alpha.md", json={"body": "see [[ghost]]\n"})
    assert r.status_code == 422
    assert "ghost" in r.text
    assert (tmp_path / "alpha.md").read_text() == before


def test_index_page_served(tmp_path):
    c = TestClient(create_app(_repo(tmp_path)))
    assert "<title>" in c.get("/").text


def test_save_forwards_expected_body(tmp_path):
    cfg = _repo(tmp_path)
    c = TestClient(create_app(cfg))
    loaded = c.get("/api/records/alpha.md").json()["body"]
    r = c.post("/api/records/alpha.md",
               json={"body": "edited\n", "expected_body": loaded})
    assert r.json()["ok"] is True


def test_stale_save_returns_409_and_leaves_disk_untouched(tmp_path):
    cfg = _repo(tmp_path)
    c = TestClient(create_app(cfg))
    (tmp_path / "alpha.md").write_text(
        "---\nname: alpha\ndescription: d\n---\n\nsomeone else's edit\n")
    before = (tmp_path / "alpha.md").read_text()
    r = c.post("/api/records/alpha.md",
               json={"body": "mine\n", "expected_body": "\nstale copy\n"})
    assert r.status_code == 409
    assert (tmp_path / "alpha.md").read_text() == before


def test_gate_failure_is_still_422(tmp_path):
    cfg = _repo(tmp_path)
    c = TestClient(create_app(cfg))
    r = c.post("/api/records/alpha.md", json={"body": "see [[ghost]]\n"})
    assert r.status_code == 422


def test_successful_save_surfaces_advisories(tmp_path):
    cfg = _repo(tmp_path)
    (tmp_path / "beta.md").write_text(
        "---\nname: beta\ndescription: d\n---\n\nsee [[ghost]]\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "add beta"], check=True)
    c = TestClient(create_app(cfg))
    r = c.post("/api/records/alpha.md", json={"body": "edited\n"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert any(a["file"] == "beta.md" for a in body["advisories"])


# --- the loopback guard -------------------------------------------------

def test_a_request_addressed_to_another_host_is_refused(tmp_path):
    """DNS rebinding. A page the reader is merely VISITING can point its
    own hostname at 127.0.0.1 and then talk to this server as
    same-origin, with full write access to the memory store. Binding to
    127.0.0.1 does not prevent it -- the browser making the request IS on
    127.0.0.1. The Host header is what does."""
    c = TestClient(create_app(_repo(tmp_path)))
    r = c.get("/api/records", headers={"host": "spoke.attacker.example"})
    assert r.status_code == 403 and "localhost" in r.text


def test_a_cross_origin_request_is_refused(tmp_path):
    c = TestClient(create_app(_repo(tmp_path)))
    r = c.post("/api/records/alpha.md", json={"body": "owned\n"},
               headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert "original" in (tmp_path / "alpha.md").read_text()


def test_loopback_names_and_ports_are_allowed(tmp_path):
    c = TestClient(create_app(_repo(tmp_path)))
    for host in ("127.0.0.1", "127.0.0.1:8765", "localhost:8765", "[::1]:8765"):
        assert c.get("/api/records", headers={"host": host}).status_code == 200, host
    assert c.get("/api/records",
                 headers={"origin": "http://localhost:8765"}).status_code == 200
