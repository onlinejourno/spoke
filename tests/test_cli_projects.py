from pathlib import Path
import pytest
from spoke.cli import main
from spoke.projects import load_registry


def _record(d: Path, name: str) -> None:
    (d / f"{name}.md").write_text(f"---\nname: {name}\ndescription: d\n---\n\nbody\n")


def _store_for(claude: Path, repo: Path, records: list[str]) -> Path:
    from spoke.projects import slug_for
    d = claude / slug_for(repo) / "memory"
    d.mkdir(parents=True)
    (d / "MEMORY.md").write_text("".join(f"- [{n}]({n}.md) - hook\n" for n in records))
    for n in records:
        _record(d, n)
    return d


def _cfg_file(tmp_path: Path) -> Path:
    f = tmp_path / "config.toml"
    f.write_text("[checks]\nstale_days = 30\n")
    return f


def test_two_projects_and_no_selection_refuses_and_lists_them(tmp_path, capsys, monkeypatch):
    claude = tmp_path / "claude"
    ra, rb = tmp_path / "ra", tmp_path / "rb"
    _store_for(claude, ra, ["alpha-rec"])
    _store_for(claude, rb, ["beta-rec"])
    reg = tmp_path / "projects.toml"
    from spoke.projects import add_project
    add_project(reg, "alpha", [ra])
    add_project(reg, "beta", [rb])
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(claude))
    rc = main(["check", "--config", str(_cfg_file(tmp_path))])
    out = capsys.readouterr().out
    assert rc != 0
    assert "alpha" in out and "beta" in out
    assert "refusing to guess" in out


def test_naming_a_project_reads_only_that_projects_store(tmp_path, capsys, monkeypatch):
    claude = tmp_path / "claude"
    ra, rb = tmp_path / "ra", tmp_path / "rb"
    _store_for(claude, ra, ["alpha-rec"])
    sb = _store_for(claude, rb, ["beta-rec"])
    # Give beta a defect alpha does not have, so the two are distinguishable.
    (sb / "broken.md").write_text("no frontmatter at all\n")
    (sb / "MEMORY.md").write_text(
        "- [beta-rec](beta-rec.md) - hook\n- [broken](broken.md) - hook\n"
    )
    reg = tmp_path / "projects.toml"
    from spoke.projects import add_project
    add_project(reg, "alpha", [ra])
    add_project(reg, "beta", [rb])
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(claude))
    cfg = _cfg_file(tmp_path)

    assert main(["check", "--project", "alpha", "--config", str(cfg)]) == 0
    assert main(["check", "--project", "beta", "--config", str(cfg)]) != 0
    out = capsys.readouterr().out
    assert "broken.md" in out


def test_the_active_project_is_named_in_the_output(tmp_path, capsys, monkeypatch):
    claude = tmp_path / "claude"
    ra = tmp_path / "ra"
    _store_for(claude, ra, ["alpha-rec"])
    reg = tmp_path / "projects.toml"
    from spoke.projects import add_project
    add_project(reg, "alpha", [ra])
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(claude))
    main(["check", "--config", str(_cfg_file(tmp_path))])
    assert "alpha" in capsys.readouterr().out


def test_env_var_selects_the_project(tmp_path, capsys, monkeypatch):
    claude = tmp_path / "claude"
    ra, rb = tmp_path / "ra", tmp_path / "rb"
    _store_for(claude, ra, ["alpha-rec"])
    _store_for(claude, rb, ["beta-rec"])
    reg = tmp_path / "projects.toml"
    from spoke.projects import add_project
    add_project(reg, "alpha", [ra])
    add_project(reg, "beta", [rb])
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(claude))
    monkeypatch.setenv("SPOKE_PROJECT", "alpha")
    assert main(["check", "--config", str(_cfg_file(tmp_path))]) == 0


def test_projects_add_then_list(tmp_path, capsys, monkeypatch):
    reg = tmp_path / "projects.toml"
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    assert main(["projects", "add", "alpha", "--repo", str(tmp_path / "ra")]) == 0
    assert main(["projects", "list"]) == 0
    assert "alpha" in capsys.readouterr().out


def test_projects_add_accept_then_list_shows_absences(tmp_path, capsys, monkeypatch):
    reg = tmp_path / "projects.toml"
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    assert main([
        "projects", "add", "alpha", "--repo", str(tmp_path / "ra"),
        "--accept", "one", "--accept", "two",
    ]) == 0
    capsys.readouterr()
    assert main(["projects", "list"]) == 0
    out = capsys.readouterr().out
    assert "one" in out and "two" in out


def test_projects_add_without_accept_prints_no_absences_line(tmp_path, capsys, monkeypatch):
    """NOT DECLARED (the CLI default when --accept is never passed) prints
    nothing project-specific about absences -- there is nothing to show."""
    reg = tmp_path / "projects.toml"
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    assert main(["projects", "add", "alpha", "--repo", str(tmp_path / "ra")]) == 0
    capsys.readouterr()
    assert main(["projects", "list"]) == 0
    out = capsys.readouterr().out
    assert "accepted absences" not in out


def test_projects_list_shows_declared_empty_distinctly_from_not_declared(tmp_path, capsys, monkeypatch):
    """`list` must print something different for a project that DECLARES an
    empty accepted_absences than for one that never declared at all --
    otherwise the two states Fix 2 introduces are indistinguishable to
    whoever reads `projects list`."""
    reg = tmp_path / "projects.toml"
    from spoke.projects import add_project
    add_project(reg, "not-declared", [tmp_path / "ra"])
    add_project(reg, "declared-empty", [tmp_path / "rb"], accepted_absences=[])
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    assert main(["projects", "list"]) == 0
    out = capsys.readouterr().out
    # Sorted alphabetically: "declared-empty" block comes first, then
    # "not-declared" -- split on the second project's header to isolate
    # the first project's block for the "no absences line at all" check.
    declared_empty_block, not_declared_block = out.split("not-declared:")
    assert "accepted absences" not in not_declared_block
    assert "accepted absences" in declared_empty_block
    assert "declared empty" in declared_empty_block


def test_projects_add_then_remove_then_list(tmp_path, capsys, monkeypatch):
    reg = tmp_path / "projects.toml"
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    assert main(["projects", "add", "alpha", "--repo", str(tmp_path / "ra")]) == 0
    assert main(["projects", "add", "beta", "--repo", str(tmp_path / "rb")]) == 0
    capsys.readouterr()
    assert main(["projects", "remove", "alpha"]) == 0
    capsys.readouterr()
    assert main(["projects", "list"]) == 0
    out = capsys.readouterr().out
    assert "beta" in out
    assert "alpha" not in out


def test_no_registry_leaves_single_project_behaviour_untouched(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    (store / "MEMORY.md").write_text("- [a](a.md) - hook\n")
    _record(store, "a")
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SPOKE_STORE_PATH", str(store))
    assert main(["check", "--config", str(_cfg_file(tmp_path))]) == 0


def test_bare_command_falls_back_to_config_toml_in_cwd(tmp_path, monkeypatch):
    # Restores long-standing behaviour: running from a directory holding a
    # config.toml works without --config.
    store = tmp_path / "store"
    store.mkdir()
    (store / "MEMORY.md").write_text("- [a](a.md) - hook\n")
    (store / "a.md").write_text("---\nname: a\ndescription: d\n---\n\nbody\n")
    (tmp_path / "config.toml").write_text(f'[store]\npath = "{store}"\n')
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    monkeypatch.delenv("SPOKE_STORE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    assert main(["check"]) == 0


def test_project_flag_beats_the_env_var(tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    ra, rb = tmp_path / "ra", tmp_path / "rb"
    _store_for(claude, ra, ["alpha-rec"])
    sb = _store_for(claude, rb, ["beta-rec"])
    (sb / "broken.md").write_text("no frontmatter at all\n")
    (sb / "MEMORY.md").write_text("- [beta-rec](beta-rec.md) - hook\n- [broken](broken.md) - hook\n")
    reg = tmp_path / "projects.toml"
    from spoke.projects import add_project
    add_project(reg, "alpha", [ra])
    add_project(reg, "beta", [rb])
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(claude))
    monkeypatch.setenv("SPOKE_PROJECT", "beta")          # env says beta (which has a defect)
    assert main(["check", "--project", "alpha", "--config", str(_cfg_file(tmp_path))]) == 0


def test_a_missing_config_toml_is_still_a_clean_refusal(tmp_path, monkeypatch, capsys):
    # No config.toml, no env var, no registry: still refuses clearly rather
    # than reading somewhere unrelated.
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    monkeypatch.delenv("SPOKE_STORE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    assert main(["check"]) != 0
    assert "no store path configured" in capsys.readouterr().out


def test_a_registry_override_of_an_explicit_config_store_is_announced(tmp_path, capsys, monkeypatch):
    claude = tmp_path / "claude"
    repo = tmp_path / "repo"
    proj_store = _store_for(claude, repo, ["in-project"])
    other = tmp_path / "other"; other.mkdir()
    (other / "MEMORY.md").write_text("- [a](a.md) - hook\n")
    (other / "a.md").write_text("---\nname: a\ndescription: d\n---\n\nbody\n")
    reg = tmp_path / "projects.toml"
    from spoke.projects import add_project
    add_project(reg, "alpha", [repo])
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[store]\npath = "{other}"\n')
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(claude))
    main(["check", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert str(other) in out and str(proj_store) in out
    assert "using the project's store" in out


def test_no_note_when_the_config_names_no_store(tmp_path, capsys, monkeypatch):
    claude = tmp_path / "claude"
    repo = tmp_path / "repo"
    _store_for(claude, repo, ["in-project"])
    reg = tmp_path / "projects.toml"
    from spoke.projects import add_project
    add_project(reg, "alpha", [repo])
    cfg = tmp_path / "config.toml"
    cfg.write_text("[checks]\nstale_days = 30\n")
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(claude))
    main(["check", "--config", str(cfg)])
    assert "using the project's store" not in capsys.readouterr().out


# --- `projects which` -- the lookup the SessionStart hook calls to turn a
# directory into the project NAME that --project actually takes. ----------


def test_projects_which_prints_the_owning_project_name(tmp_path, capsys, monkeypatch):
    reg = tmp_path / "projects.toml"
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    repo = tmp_path / "ra"
    assert main(["projects", "add", "alpha", "--repo", str(repo)]) == 0
    capsys.readouterr()
    assert main(["projects", "which", str(repo)]) == 0
    assert capsys.readouterr().out.strip() == "alpha"


def test_projects_which_matches_the_claude_projects_slug_directory(tmp_path, capsys, monkeypatch):
    # What the SessionStart hook actually calls this with.
    from spoke.projects import slug_for
    claude = tmp_path / "claude"
    repo = tmp_path / "ra"
    reg = tmp_path / "projects.toml"
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(claude))
    assert main(["projects", "add", "alpha", "--repo", str(repo)]) == 0
    capsys.readouterr()
    slugdir = claude / slug_for(repo)
    assert main(["projects", "which", str(slugdir)]) == 0
    assert capsys.readouterr().out.strip() == "alpha"


def test_projects_which_refuses_and_exits_nonzero_when_nothing_owns_the_path(tmp_path, capsys, monkeypatch):
    reg = tmp_path / "projects.toml"
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    assert main(["projects", "add", "alpha", "--repo", str(tmp_path / "ra")]) == 0
    capsys.readouterr()
    rc = main(["projects", "which", str(tmp_path / "elsewhere")])
    assert rc != 0
    out = capsys.readouterr().out
    assert "no single registered project owns" in out


def test_projects_which_refuses_and_exits_nonzero_when_two_projects_match(tmp_path, capsys, monkeypatch):
    reg = tmp_path / "projects.toml"
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    shared = tmp_path / "shared"
    assert main(["projects", "add", "alpha", "--repo", str(shared)]) == 0
    assert main(["projects", "add", "beta", "--repo", str(shared)]) == 0
    capsys.readouterr()
    rc = main(["projects", "which", str(shared)])
    assert rc != 0
    out = capsys.readouterr().out
    assert "no single registered project owns" in out


def test_no_note_when_they_agree(tmp_path, capsys, monkeypatch):
    claude = tmp_path / "claude"
    repo = tmp_path / "repo"
    proj_store = _store_for(claude, repo, ["in-project"])
    reg = tmp_path / "projects.toml"
    from spoke.projects import add_project
    add_project(reg, "alpha", [repo])
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[store]\npath = "{proj_store}"\n')
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(claude))
    main(["check", "--config", str(cfg)])
    assert "using the project's store" not in capsys.readouterr().out


def test_projects_add_on_an_existing_name_grows_it(tmp_path, capsys, monkeypatch):
    """`add` used to refuse an existing name with 'remove it first'.
    Found adding a product to the estate the day it had an outage."""
    reg = tmp_path / "projects.toml"
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    ra, rb = tmp_path / "ra", tmp_path / "rb"
    assert main(["projects", "add", "alpha", "--repo", str(ra), "--accept", "x"]) == 0
    capsys.readouterr()
    assert main(["projects", "add", "alpha", "--repo", str(rb)]) == 0
    out = capsys.readouterr().out
    assert f"added {rb} to alpha" in out
    got = load_registry(reg)["alpha"]
    assert got.repos == (ra, rb)
    assert got.accepted_absences == ("x",), "growing must not touch the rest"


def test_projects_add_on_an_existing_name_reports_a_repo_already_there(tmp_path, capsys, monkeypatch):
    reg = tmp_path / "projects.toml"
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    ra = tmp_path / "ra"
    main(["projects", "add", "alpha", "--repo", str(ra)])
    capsys.readouterr()
    assert main(["projects", "add", "alpha", "--repo", str(ra)]) == 0
    assert "already in alpha; nothing changed" in capsys.readouterr().out


def test_projects_add_refuses_to_overwrite_settings_on_an_existing_name(tmp_path, capsys, monkeypatch):
    """--memory-store or --accept on an existing project would overwrite
    what it carries, under a command the caller thought was adding a
    repo. Refused, not applied."""
    reg = tmp_path / "projects.toml"
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    main(["projects", "add", "alpha", "--repo", str(tmp_path / "ra"), "--accept", "keep"])
    capsys.readouterr()
    rc = main(["projects", "add", "alpha", "--repo", str(tmp_path / "rb"), "--accept", "clobber"])
    assert rc == 2
    assert "refused" in capsys.readouterr().out
    got = load_registry(reg)["alpha"]
    assert got.accepted_absences == ("keep",)
    assert got.repos == (tmp_path / "ra",), "a refused command changes nothing"

