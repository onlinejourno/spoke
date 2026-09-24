from pathlib import Path
from fastapi.testclient import TestClient
from spoke.cli import main


def _store(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\n---\n\nbody\n")
    return tmp_path


def test_serve_constructs_the_app_with_the_resolved_config_path(tmp_path, monkeypatch):
    store_dir = _store(tmp_path / "store")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'[store]\npath = "{store_dir}"\n')

    captured = {}

    def fake_run(app, host=None, port=None):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    import uvicorn
    monkeypatch.setattr(uvicorn, "run", fake_run)

    rc = main(["serve", "--config", str(cfg_path), "--port", "9999"])
    assert rc == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9999

    # The app must know the config path it was resolved from, so
    # POST /api/config can actually write to it -- proven by checking
    # GET /api/config reports that exact path.
    c = TestClient(captured["app"])
    data = c.get("/api/config").json()
    assert data["config_path"] == str(cfg_path)


def test_serve_open_actually_opens_a_browser(monkeypatch, capsys):
    """`--open` was parsed into `open_browser` and read by nothing. A flag
    that --help describes and that does nothing is worse than no flag."""
    from spoke import cli

    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    cli._open_board(8766)
    assert opened == ["http://127.0.0.1:8766/board"]
    assert "opened http://127.0.0.1:8766/board" in capsys.readouterr().out


def test_serve_open_says_so_when_there_is_no_browser(monkeypatch, capsys):
    """A headless box must not turn a working server into a failed
    command -- but it must say the window was not opened."""
    from spoke import cli

    monkeypatch.setattr("webbrowser.open", lambda url: False)
    cli._open_board(8766)
    out = capsys.readouterr().out
    assert "no browser to open" in out and "http://127.0.0.1:8766/board" in out
