from dataclasses import replace
from pathlib import Path
import pytest
from spoke.projects import (
    Project, RegistryError, load_registry, save_registry, add_project,
    remove_project,
)


def test_absent_registry_is_empty_not_an_error(tmp_path):
    assert load_registry(tmp_path / "projects.toml") == {}


def test_add_then_load_round_trips(tmp_path):
    reg = tmp_path / "projects.toml"
    add_project(reg, "alpha", [Path("/repos/one"), Path("/repos/two")])
    got = load_registry(reg)
    assert set(got) == {"alpha"}
    assert got["alpha"].repos == (Path("/repos/one"), Path("/repos/two"))
    assert got["alpha"].memory_store is None


def test_two_projects_do_not_mention_each_other(tmp_path):
    reg = tmp_path / "projects.toml"
    add_project(reg, "alpha", [Path("/repos/one")])
    add_project(reg, "beta", [Path("/elsewhere/two")])
    got = load_registry(reg)
    assert set(got) == {"alpha", "beta"}
    assert got["alpha"].repos == (Path("/repos/one"),)
    assert got["beta"].repos == (Path("/elsewhere/two"),)


def test_adding_a_duplicate_name_is_refused(tmp_path):
    reg = tmp_path / "projects.toml"
    add_project(reg, "alpha", [Path("/repos/one")])
    with pytest.raises(RegistryError) as e:
        add_project(reg, "alpha", [Path("/repos/other")])
    assert "alpha" in str(e.value)


def test_a_project_with_no_repos_is_refused(tmp_path):
    with pytest.raises(RegistryError):
        add_project(tmp_path / "projects.toml", "alpha", [])


def test_explicit_memory_store_survives_the_round_trip(tmp_path):
    reg = tmp_path / "projects.toml"
    add_project(reg, "alpha", [Path("/repos/one")], memory_store=Path("/custom/store"))
    assert load_registry(reg)["alpha"].memory_store == Path("/custom/store")


def test_accepted_absences_survive_the_round_trip(tmp_path):
    reg = tmp_path / "projects.toml"
    add_project(reg, "alpha", [Path("/repos/one")], accepted_absences=["x", "y"])
    assert load_registry(reg)["alpha"].accepted_absences == ("x", "y")


def test_a_project_with_no_accepted_absences_declared_is_none_not_empty(tmp_path):
    """NOT DECLARED (None) round-trips as None, not as an empty tuple --
    conflating the two is exactly the bug Fix 2 removes: an explicit
    "accept nothing" must be distinguishable from "no opinion"."""
    reg = tmp_path / "projects.toml"
    add_project(reg, "alpha", [Path("/repos/one")])
    assert load_registry(reg)["alpha"].accepted_absences is None
    # And the key itself must be entirely absent from the written file --
    # not `accepted_absences = []` -- so a hand-read of the registry shows
    # the same "not declared" state the Python object carries.
    assert "accepted_absences" not in reg.read_text()


def test_a_project_can_declare_accepted_absences_explicitly_empty(tmp_path):
    """DECLARED EMPTY (a project explicitly accepts nothing) must survive
    the round trip as () -- distinct from None -- and must be written to
    the file as `accepted_absences = []`, not omitted."""
    reg = tmp_path / "projects.toml"
    add_project(reg, "alpha", [Path("/repos/one")], accepted_absences=[])
    assert load_registry(reg)["alpha"].accepted_absences == ()
    assert "accepted_absences = []" in reg.read_text()


def test_an_accepted_absence_with_a_quote_or_backslash_round_trips(tmp_path):
    reg = tmp_path / "projects.toml"
    odd = 'has"quote\\and-backslash'
    add_project(reg, "alpha", [Path("/repos/one")], accepted_absences=[odd])
    assert load_registry(reg)["alpha"].accepted_absences == (odd,)


def test_registry_is_created_in_a_directory_that_does_not_exist_yet(tmp_path):
    # The default registry lives under ~/.claude/spoke/, which does
    # not exist on a fresh machine. The first `projects add` must not crash.
    reg = tmp_path / "deep" / "nested" / "projects.toml"
    add_project(reg, "alpha", [Path("/repos/one")])
    assert reg.exists()
    assert load_registry(reg)["alpha"].repos == (Path("/repos/one"),)


def test_remove_forgets_the_registration_only(tmp_path):
    reg = tmp_path / "projects.toml"
    add_project(reg, "alpha", [Path("/repos/one")])
    add_project(reg, "beta", [Path("/repos/two")])
    remove_project(reg, "alpha")
    assert set(load_registry(reg)) == {"beta"}


def test_removing_an_unknown_project_is_refused_and_lists_the_known(tmp_path):
    reg = tmp_path / "projects.toml"
    add_project(reg, "alpha", [Path("/repos/one")])
    with pytest.raises(RegistryError) as e:
        remove_project(reg, "nope")
    assert "nope" in str(e.value) and "alpha" in str(e.value)


from spoke.projects import slug_for, derive_stores


def test_slug_replaces_every_non_alphanumeric():
    assert slug_for(Path("/home/u/projects/folio-dev")) == "-home-u-projects-folio-dev"
    assert slug_for(Path("/opt/local/bin")) == "-opt-local-bin"
    assert slug_for(Path("/a/b.c/d_e")) == "-a-b-c-d-e"


def _store(root: Path, slug: str, record: str = "a.md") -> Path:
    d = root / slug / "memory"
    d.mkdir(parents=True)
    (d / record).write_text("---\nname: a\ndescription: d\n---\n\nbody\n")
    return d


def test_derives_one_store_per_repo(tmp_path):
    claude = tmp_path / "claude"
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    s1 = _store(claude, slug_for(r1))
    s2 = _store(claude, slug_for(r2))
    p = Project("alpha", (r1, r2), None)
    assert sorted(derive_stores(p, claude)) == sorted([s1, s2])


def test_symlinked_stores_are_deduplicated(tmp_path):
    # This estate points many project dirs at one canonical store. Without
    # resolve(), the same records are counted once per repo path.
    claude = tmp_path / "claude"
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    canonical = _store(claude, slug_for(r1))
    second = claude / slug_for(r2)
    second.mkdir(parents=True)
    (second / "memory").symlink_to(canonical)
    p = Project("alpha", (r1, r2), None)
    assert derive_stores(p, claude) == [canonical.resolve()]


def test_a_repo_with_no_store_contributes_nothing(tmp_path):
    claude = tmp_path / "claude"
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    s1 = _store(claude, slug_for(r1))
    p = Project("alpha", (r1, r2), None)
    assert derive_stores(p, claude) == [s1]


def test_an_empty_store_is_not_returned(tmp_path):
    claude = tmp_path / "claude"
    r1 = tmp_path / "r1"
    (claude / slug_for(r1) / "memory").mkdir(parents=True)
    assert derive_stores(Project("alpha", (r1,), None), claude) == []


def test_an_explicit_memory_store_overrides_derivation(tmp_path):
    claude = tmp_path / "claude"
    r1 = tmp_path / "r1"
    _store(claude, slug_for(r1))
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "z.md").write_text("---\nname: z\ndescription: d\n---\n\nbody\n")
    p = Project("alpha", (r1,), custom)
    assert derive_stores(p, claude) == [custom.resolve()]


from spoke.config import Config
from spoke.projects import select_project, config_for


def _base() -> Config:
    return Config(Path("/unused"), ("keep-me",), 30, "groq", None)


def test_one_project_is_selected_without_being_named(tmp_path):
    reg = {"alpha": Project("alpha", (tmp_path / "r",), None)}
    assert select_project(reg, None).name == "alpha"


def test_two_projects_and_no_name_is_refused_and_lists_them(tmp_path):
    reg = {
        "alpha": Project("alpha", (tmp_path / "a",), None),
        "beta": Project("beta", (tmp_path / "b",), None),
    }
    with pytest.raises(RegistryError) as e:
        select_project(reg, None)
    msg = str(e.value)
    assert "alpha" in msg and "beta" in msg


def test_an_unknown_project_name_is_refused_and_lists_the_known(tmp_path):
    reg = {"alpha": Project("alpha", (tmp_path / "a",), None)}
    with pytest.raises(RegistryError) as e:
        select_project(reg, "nope")
    assert "nope" in str(e.value) and "alpha" in str(e.value)


def test_an_empty_registry_is_refused_with_guidance(tmp_path):
    with pytest.raises(RegistryError) as e:
        select_project({}, None)
    assert "projects add" in str(e.value)


def test_config_for_points_at_the_derived_store_and_keeps_other_settings(tmp_path):
    claude = tmp_path / "claude"
    r1 = tmp_path / "r1"
    s1 = _store(claude, slug_for(r1))
    cfg = config_for(Project("alpha", (r1,), None), _base(), claude)
    assert cfg.store_path == s1.resolve()
    assert cfg.accepted_absences == ("keep-me",)
    assert cfg.stale_days == 30


def test_config_for_uses_the_projects_own_absences_when_it_declares_any(tmp_path):
    """A property of the PROJECT must not depend on the caller's cwd: a
    project that declares its own accepted_absences uses them no matter
    what base config (i.e. whatever config.toml happens to be in the
    current directory) says."""
    claude = tmp_path / "claude"
    r1 = tmp_path / "r1"
    _store(claude, slug_for(r1))
    proj = Project("alpha", (r1,), None, accepted_absences=("project-only",))
    cfg = config_for(proj, _base(), claude)
    assert cfg.accepted_absences == ("project-only",)


def test_config_for_falls_back_to_base_when_project_declares_none(tmp_path):
    claude = tmp_path / "claude"
    r1 = tmp_path / "r1"
    _store(claude, slug_for(r1))
    proj = Project("alpha", (r1,), None)  # no accepted_absences declared (None)
    base = replace(_base(), accepted_absences=("from-config-file",))
    cfg = config_for(proj, base, claude)
    assert cfg.accepted_absences == ("from-config-file",)


def test_config_for_does_not_fall_back_when_project_declares_empty(tmp_path):
    """A project that DECLARES an empty accepted_absences (an explicit "I
    accept nothing") must NOT inherit the base config's list -- () and
    "not declared" are different states (Fix 2). Before the fix, both were
    falsy and config_for indistinguishably fell back to base."""
    claude = tmp_path / "claude"
    r1 = tmp_path / "r1"
    _store(claude, slug_for(r1))
    proj = Project("alpha", (r1,), None, accepted_absences=())
    base = replace(_base(), accepted_absences=("base-absence",))
    cfg = config_for(proj, base, claude)
    assert cfg.accepted_absences == ()


def test_a_project_deriving_several_stores_is_refused_by_name(tmp_path):
    claude = tmp_path / "claude"
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    _store(claude, slug_for(r1))
    _store(claude, slug_for(r2))
    with pytest.raises(RegistryError) as e:
        config_for(Project("alpha", (r1, r2), None), _base(), claude)
    assert "memory_store" in str(e.value)


def test_a_project_deriving_no_store_is_refused(tmp_path):
    with pytest.raises(RegistryError) as e:
        config_for(Project("alpha", (tmp_path / "nope",), None), _base(), tmp_path / "claude")
    assert "alpha" in str(e.value)


# --- Fix 1: TOML-injection hardening ---------------------------------------


@pytest.mark.parametrize("bad", ["has space", 'has"quote', "has\nnewline", "-leading-dash", "", "has/slash"])
def test_invalid_project_names_are_refused(tmp_path, bad):
    with pytest.raises(RegistryError):
        add_project(tmp_path / "projects.toml", bad, [Path("/repos/one")])


def test_control_characters_in_a_path_are_refused(tmp_path):
    with pytest.raises(RegistryError) as e:
        add_project(tmp_path / "projects.toml", "alpha", [Path("/repos/line1\nline2")])
    assert "control character" in str(e.value).lower()


def test_a_quote_or_backslash_in_a_path_still_round_trips(tmp_path):
    reg = tmp_path / "projects.toml"
    odd = Path('/repos/we"ird\\path')
    add_project(reg, "alpha", [odd])
    assert load_registry(reg)["alpha"].repos == (odd,)


def test_a_corrupt_registry_raises_RegistryError_not_a_decode_error(tmp_path):
    reg = tmp_path / "projects.toml"
    reg.write_text("[projects.alpha]\nrepos = [oops\n")
    with pytest.raises(RegistryError) as e:
        load_registry(reg)
    assert str(reg) in str(e.value)


# --- Fix 2: atomic writes ----------------------------------------------------


def test_save_registry_is_atomic_original_survives_a_failed_write(tmp_path, monkeypatch):
    reg = tmp_path / "projects.toml"
    add_project(reg, "alpha", [Path("/repos/one")])
    original = reg.read_text()

    real_write_text = Path.write_text

    def flaky_write_text(self, *a, **kw):
        if ".tmp-" in self.name:
            raise OSError("simulated crash mid-write")
        return real_write_text(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", flaky_write_text)

    with pytest.raises(OSError):
        add_project(reg, "beta", [Path("/repos/two")])

    assert reg.read_text() == original


# --- Fix 3: config_for distinguishes why a store is empty --------------------


def test_config_for_error_distinguishes_missing_dir_from_empty_store(tmp_path):
    claude = tmp_path / "claude"
    r1, r2 = tmp_path / "one", tmp_path / "two"
    (claude / slug_for(r2) / "memory").mkdir(parents=True)  # exists, no records
    # r1 has no directory at all under claude_projects: never had a session.
    with pytest.raises(RegistryError) as e:
        config_for(Project("alpha", (r1, r2), None), _base(), claude)
    msg = str(e.value)
    assert "no such directory - no session has run there" in msg
    assert "exists but holds no records" in msg


def test_config_for_error_names_the_explicit_memory_store_path(tmp_path):
    custom = tmp_path / "custom"
    with pytest.raises(RegistryError) as e:
        config_for(
            Project("alpha", (tmp_path / "r",), custom), _base(), tmp_path / "claude"
        )
    assert str(custom) in str(e.value)


# --- Fix 4: relative repo paths are normalised to absolute ------------------


def test_a_relative_repo_path_is_stored_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reg = tmp_path / "projects.toml"
    add_project(reg, "alpha", [Path("some/repo")])
    stored = load_registry(reg)["alpha"].repos[0]
    assert stored.is_absolute()
    assert stored == Path(tmp_path / "some/repo")


# --- final-review finding 2: a dotted name must not be a nested TOML table --


def test_a_dotted_project_name_round_trips(tmp_path):
    reg = tmp_path / "projects.toml"
    add_project(reg, "proj.v2", [Path("/repos/one")])
    got = load_registry(reg)
    assert set(got) == {"proj.v2"}
    assert got["proj.v2"].repos == (Path("/repos/one"),)


def test_a_second_dotted_sibling_does_not_destroy_the_first(tmp_path):
    reg = tmp_path / "projects.toml"
    add_project(reg, "proj.v1", [Path("/repos/one")])
    add_project(reg, "proj.v2", [Path("/repos/two")])
    got = load_registry(reg)
    assert got["proj.v1"].repos == (Path("/repos/one"),)
    assert got["proj.v2"].repos == (Path("/repos/two"),)


# --- project_for_path: what a SessionStart hook actually has -- a directory
# -- resolved into what --project actually takes -- a registered NAME. ------

from spoke.projects import project_for_path


def test_project_for_path_matches_a_repo_path(tmp_path):
    reg = {"alpha": Project("alpha", (tmp_path / "repo",), None)}
    assert project_for_path(reg, tmp_path / "repo").name == "alpha"


def test_project_for_path_matches_a_directory_inside_a_repo(tmp_path):
    reg = {"alpha": Project("alpha", (tmp_path / "repo",), None)}
    assert project_for_path(reg, tmp_path / "repo" / "sub" / "dir").name == "alpha"


def test_project_for_path_matches_the_claude_projects_slug_directory(tmp_path):
    # This is what the SessionStart hook actually has in its hand.
    from spoke.projects import slug_for
    repo = tmp_path / "repo"
    claude = tmp_path / "claude"
    reg = {"alpha": Project("alpha", (repo,), None)}
    assert project_for_path(reg, claude / slug_for(repo), claude).name == "alpha"


def test_project_for_path_returns_none_when_nothing_owns_it(tmp_path):
    reg = {"alpha": Project("alpha", (tmp_path / "repo",), None)}
    assert project_for_path(reg, tmp_path / "elsewhere") is None


def test_project_for_path_returns_none_when_two_projects_match(tmp_path):
    repo = tmp_path / "repo"
    reg = {"alpha": Project("alpha", (repo,), None), "beta": Project("beta", (repo,), None)}
    assert project_for_path(reg, repo) is None


def test_project_for_path_matches_a_derived_memory_store_directly(tmp_path):
    # Level 3: neither a repo path nor the claude-projects slug directory --
    # someone (or something) hands in the derived STORE path itself.
    from spoke.projects import slug_for
    repo = tmp_path / "repo"
    claude = tmp_path / "claude"
    store = claude / slug_for(repo) / "memory"
    store.mkdir(parents=True)
    (store / "a.md").write_text("---\nname: a\ndescription: d\n---\n\nbody\n")
    reg = {"alpha": Project("alpha", (repo,), None)}
    assert project_for_path(reg, store, claude).name == "alpha"


def test_project_for_path_slug_match_ignores_paths_outside_claude_projects(tmp_path):
    # A directory that merely happens to be NAMED like a slug, but isn't
    # actually under claude_projects, must not be treated as a slug match.
    from spoke.projects import slug_for
    repo = tmp_path / "repo"
    claude = tmp_path / "claude"
    lookalike = tmp_path / "elsewhere" / slug_for(repo)
    reg = {"alpha": Project("alpha", (repo,), None)}
    assert project_for_path(reg, lookalike, claude) is None


def test_extending_a_project_adds_repos_and_changes_nothing_else(tmp_path):
    """The only way to grow a project used to be remove-and-re-add, which
    would have dropped its memory store, accepted absences, vocabulary
    and axes. Growing touches the repo list and nothing else."""
    from dataclasses import replace

    from spoke.ledger.scale import Axis
    from spoke.projects import extend_project

    reg = tmp_path / "projects.toml"
    add_project(reg, "alpha", [Path("/repos/one")], Path("/store"), ["gone-on-purpose"])
    # give it the two fields add_project does not take, the way a hand
    # edit or `projects vocabulary --set` would
    full = load_registry(reg)
    full["alpha"] = replace(full["alpha"], vocabulary={"product": "package"},
                            axes=(Axis("reliability", "Reliability", 2.0),))
    save_registry(reg, full)
    before = load_registry(reg)["alpha"]

    p, added = extend_project(reg, "alpha", [Path("/repos/two")])
    assert added == (Path("/repos/two"),)
    assert p.repos == (Path("/repos/one"), Path("/repos/two"))
    assert p.memory_store == before.memory_store
    assert p.accepted_absences == before.accepted_absences
    assert p.vocabulary == before.vocabulary
    assert p.axes == before.axes


def test_extending_with_a_repo_already_present_is_reported_not_duplicated(tmp_path):
    from spoke.projects import extend_project

    reg = tmp_path / "projects.toml"
    add_project(reg, "alpha", [Path("/repos/one")])
    p, added = extend_project(reg, "alpha", [Path("/repos/one"), Path("/repos/two")])
    assert added == (Path("/repos/two"),)
    assert p.repos == (Path("/repos/one"), Path("/repos/two"))
    p2, added2 = extend_project(reg, "alpha", [Path("/repos/one")])
    assert added2 == ()
    assert p2.repos == p.repos


def test_extending_a_project_that_does_not_exist_is_refused(tmp_path):
    from spoke.projects import extend_project

    with pytest.raises(RegistryError) as e:
        extend_project(tmp_path / "projects.toml", "nope", [Path("/r")])
    assert "not registered" in str(e.value)


def test_a_project_declares_the_top_of_its_own_scale(tmp_path):
    """The package scores 0-5; a project scoring 0-4 rendered on that
    ramp shows a full mark as 80% -- a lie by rendering. The max is
    part of the scale, declared with the axes, and round-trips."""
    from dataclasses import replace

    reg = tmp_path / "projects.toml"
    add_project(reg, "alpha", [Path("/repos/one")])
    got = load_registry(reg)["alpha"]
    assert got.scale_max == 5, "the package default until the project says otherwise"
    full = load_registry(reg)
    full["alpha"] = replace(full["alpha"], scale_max=4)
    save_registry(reg, full)
    assert "scale_max = 4" in reg.read_text()
    assert load_registry(reg)["alpha"].scale_max == 4


def test_a_nonsense_scale_max_is_refused_on_load(tmp_path):
    reg = tmp_path / "projects.toml"
    reg.write_text('[projects."alpha"]\nrepos = ["/r"]\nscale_max = 0\n')
    with pytest.raises(RegistryError):
        load_registry(reg)
