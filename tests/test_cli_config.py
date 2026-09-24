from pathlib import Path
import pytest
from spoke.cli import main


def test_no_store_configured_exits_non_zero_with_a_clean_message(tmp_path, capsys):
    # A misconfiguration must refuse cleanly, not traceback, and must never
    # fall back to some other project's store.
    empty = tmp_path / "empty.toml"
    empty.write_text("[checks]\nstale_days = 30\n")
    rc = main(["check", "--config", str(empty)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "no store path configured" in out
    assert "SPOKE_STORE_PATH" in out


def test_env_var_alone_is_enough_to_point_at_a_project(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    (store / "MEMORY.md").write_text("- [A](a.md) - hook\n")
    (store / "a.md").write_text("---\nname: a\ndescription: d\n---\n\nbody\n")
    monkeypatch.setenv("SPOKE_STORE_PATH", str(store))
    empty = tmp_path / "empty.toml"
    empty.write_text("[checks]\nstale_days = 30\n")
    assert main(["check", "--config", str(empty)]) == 0
