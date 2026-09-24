from spoke.cli import main


def _store(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\n---\n\nbody\n")
    return tmp_path


def test_clean_store_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SPOKE_STORE_PATH", str(_store(tmp_path)))
    assert main(["check"]) == 0
    assert "no structural defects" in capsys.readouterr().out.lower()


def test_defect_exits_one_and_names_it(tmp_path, monkeypatch, capsys):
    d = _store(tmp_path)
    (d / "a.md").write_text("---\nname: a\ndescription: d\n---\n\n[[ghost]]\n")
    monkeypatch.setenv("SPOKE_STORE_PATH", str(d))
    assert main(["check"]) == 1
    assert "ghost" in capsys.readouterr().out


def test_accepted_absence_keeps_it_green(tmp_path, monkeypatch):
    d = _store(tmp_path)
    (d / "a.md").write_text("---\nname: a\ndescription: d\n---\n\n[[known-gap]]\n")
    (d / "config.toml").write_text(
        f'[store]\npath = "{d}"\naccepted_absences = ["known-gap"]\n')
    monkeypatch.delenv("SPOKE_STORE_PATH", raising=False)
    assert main(["check", "--config", str(d / "config.toml")]) == 0
