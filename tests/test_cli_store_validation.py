"""I1: a mis-pointed store must never read as a clean store.

`Path.glob` over a directory that doesn't exist (a typo'd path in
config.toml, an env var pointing at the wrong place) silently returns
nothing, so every check downstream of it -- `check`, `stale`, `doctor`,
`contradictions` -- would otherwise report "0 defects" / "0 findings" /
"0 new" forever, indistinguishable from real health. Every subcommand
must instead refuse up front, name the path, and exit non-zero.
"""
from spoke.cli import main


def test_nonexistent_store_path_is_refused_on_every_subcommand(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("SPOKE_STORE_PATH", str(missing))
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")

    for args in (["check"], ["stale"], ["doctor"], ["contradictions"]):
        rc = main(args)
        out = capsys.readouterr().out
        assert rc != 0, f"{args}: exited zero against a nonexistent store path"
        assert str(missing) in out, f"{args}: did not name the bad path\n{out}"
        assert "NOT RUN" in out, f"{args}: did not say the run was refused\n{out}"


def test_empty_store_directory_is_refused_on_every_subcommand(tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty-store"
    empty.mkdir()
    monkeypatch.setenv("SPOKE_STORE_PATH", str(empty))
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")

    for args in (["check"], ["stale"], ["doctor"], ["contradictions"]):
        rc = main(args)
        out = capsys.readouterr().out
        assert rc != 0, f"{args}: exited zero against an empty store directory"
        assert str(empty) in out, f"{args}: did not name the empty path\n{out}"
        assert "NOT RUN" in out, f"{args}: did not say the run was refused\n{out}"


def test_a_file_where_the_store_path_should_be_is_refused(tmp_path, monkeypatch, capsys):
    not_a_dir = tmp_path / "a-file-not-a-directory"
    not_a_dir.write_text("oops")
    monkeypatch.setenv("SPOKE_STORE_PATH", str(not_a_dir))

    rc = main(["check"])
    out = capsys.readouterr().out
    assert rc != 0
    assert "not a directory" in out
