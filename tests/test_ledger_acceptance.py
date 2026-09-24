import subprocess
from pathlib import Path
from spoke.ledger import Node
from spoke.ledger.store import LedgerStore
from spoke.ledger.preamble import render_preamble

THREE = [
    ("go-live-secret-keys", "SECRET_KEYS must be set in production before go-live"),
    ("go-live-migrations", "migrations 0002 and 0003 must be run by hand"),
    ("go-live-logout-return", "AUTH0_LOGOUT_RETURN_TO must be configured"),
]


def _repo(tmp_path: Path) -> LedgerStore:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / "MEMORY.md").write_text("")
    (tmp_path / "ledger").mkdir()
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    return LedgerStore(tmp_path)


def test_items_recorded_for_a_future_session_are_surfaced_to_it(tmp_path):
    # The real failure: three open items were recorded "for the go-live session"
    # and nothing put them in front of it. They were found by accident, days later.
    s = _repo(tmp_path)
    for name, title in THREE:
        s.write(Node(name=name, type="item", state="open", title=title, body="",
                     relations=(), ruling=None, blocked_by=(), provenance=(),
                     opened="2026-09-08", updated="2026-09-08", by="another-session",
                     claimed_by=None, claimed_at=None))

    # A different session, later, asks for nothing and is told anyway.
    fresh = LedgerStore(tmp_path)
    text, stats = render_preamble(fresh.list_nodes(), [], budget_tokens=2000)

    for name, _ in THREE:
        assert name in text, f"{name} was not surfaced:\n{text}"
    assert stats["shown"] >= 3
