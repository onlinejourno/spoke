import httpx
from spoke.cli import main


def _clean_store(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text(
        "---\nname: a\ndescription: d\nmetadata:\n  verified: 2026-09-01\n---\n\nno citations\n")
    return tmp_path


def _store_with_a_stale_record(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text(
        "---\nname: a\ndescription: d\nmetadata:\n  verified: 2020-01-01\n---\n\nno citations\n")
    return tmp_path


def _install_mock_client(monkeypatch):
    """Force _cmd_stale's httpx.Client onto a MockTransport so no real network call happens."""
    real_client_cls = httpx.Client

    def handler(request):
        return httpx.Response(200)

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr("spoke.cli.httpx.Client", fake_client)


def test_stale_with_no_findings_exits_zero(tmp_path, monkeypatch, capsys):
    _install_mock_client(monkeypatch)
    monkeypatch.setenv("SPOKE_STORE_PATH", str(_clean_store(tmp_path)))
    assert main(["stale"]) == 0
    assert "0 finding" in capsys.readouterr().out


def test_stale_with_a_finding_exits_one(tmp_path, monkeypatch, capsys):
    _install_mock_client(monkeypatch)
    monkeypatch.setenv("SPOKE_STORE_PATH", str(_store_with_a_stale_record(tmp_path)))
    assert main(["stale"]) == 1
    out = capsys.readouterr().out
    assert "unverified-too-long" in out
