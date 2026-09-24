import os
import shutil
import tempfile
import subprocess
import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_from_the_real_project_registry(tmp_path, monkeypatch):
    """Default SPOKE_REGISTRY to a path that does not exist, for every
    test, regardless of what is actually registered on the machine running
    the suite (e.g. after `spoke projects add`). Without this, a
    test that never mentions projects at all silently reads whatever real
    registry happens to sit at ~/.claude/spoke/projects.toml --
    picking up a real project's real store instead of the test's fixture.
    A test that wants a real registry sets SPOKE_REGISTRY itself later in
    its own body, which simply overrides this default.

    Also default SPOKE_CLAUDE_PROJECTS to a temporary directory, so a test
    that forgets to set it finds an empty projects tree rather than the real
    ~/.claude/projects directory.

    Also default the three schedule.py locations (LaunchAgents dir, doctor
    state dir, log dir) to temp paths, for the same reason: a
    tests/test_schedule.py test that forgets to override one of these
    landed real files at ~/.claude/spoke/schedule/estate.json and
    created ~/Library/Logs/spoke/ on the machine actually running
    this suite (caught and cleaned up once already -- this fixture is what
    makes that not a standing risk on every future test run).
    """
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "no-such-registry.toml"))
    monkeypatch.setenv("SPOKE_CLAUDE_PROJECTS", str(tmp_path / "fake-projects"))
    monkeypatch.setenv("SPOKE_LAUNCH_AGENTS_DIR", str(tmp_path / "fake-launch-agents"))
    monkeypatch.setenv("SPOKE_SCHEDULE_STATE_DIR", str(tmp_path / "fake-schedule-state"))
    monkeypatch.setenv("SPOKE_SCHEDULE_LOG_DIR", str(tmp_path / "fake-schedule-logs"))


def test_the_suite_cannot_reach_the_real_memory_store(tmp_path):
    """Prove that the isolation fixture guards both SPOKE_REGISTRY and
    SPOKE_CLAUDE_PROJECTS. This test will FAIL if the SPOKE_CLAUDE_PROJECTS
    line is removed from the fixture, proving the guard is real and not decorative.

    Because this is a normal test, the autouse fixture has already set both
    SPOKE_REGISTRY and SPOKE_CLAUDE_PROJECTS to safe temp paths. We verify
    that SPOKE_CLAUDE_PROJECTS is not the real ~/.claude/projects.
    """
    from spoke.cli import _claude_projects

    # The fixture has set SPOKE_CLAUDE_PROJECTS to a temp path.
    # Verify that _claude_projects() resolves to a safe location,
    # not the real ~/.claude/projects.
    resolved = _claude_projects()

    # The suite must NOT resolve to the real ~/.claude/projects.
    real = Path("~/.claude/projects").expanduser()
    assert resolved != real, (
        f"SPOKE_CLAUDE_PROJECTS resolves to the real projects tree: {resolved} == {real}"
    )

    # With the fixture in place, it should resolve to a temp directory path.
    assert str(tmp_path) in str(resolved), (
        f"SPOKE_CLAUDE_PROJECTS is not isolated to the fixture temp directory: {resolved} (expected path to contain {tmp_path})"
    )


def test_the_library_default_is_isolated_too_not_just_the_cli_helper(tmp_path):
    """FINDING 5: the CLI helper (_claude_projects) was the ONLY thing that
    honoured SPOKE_CLAUDE_PROJECTS -- derive_stores/project_for_path/
    config_for defaulted their own `claude_projects` parameter straight to
    the module constant CLAUDE_PROJECTS, bound at IMPORT time, so a caller
    (several tests in test_projects.py included) that omitted the argument
    fell through to the REAL ~/.claude/projects. The test above proved only
    the CLI helper's isolation; this proves the library functions
    themselves are isolated when called with no `claude_projects` argument
    at all -- the exact call shape those tests use.

    This does not just assert isolation from the real tree (a function
    that silently did nothing would also pass that); it proves the
    function ACTUALLY USES SPOKE_CLAUDE_PROJECTS by planting a store only
    under the fixture's fake tree and confirming derive_stores() finds it
    with no explicit `claude_projects` argument. Revert the Finding 5 fix
    (put `claude_projects: Path = CLAUDE_PROJECTS` back on derive_stores)
    and this fails: the function looks under the real ~/.claude/projects,
    the fake store is never found, and derive_stores() returns [].
    """
    from spoke.projects import Project, derive_stores, slug_for

    repo = tmp_path / "some-repo"
    repo.mkdir()
    # The autouse fixture above has already set SPOKE_CLAUDE_PROJECTS to
    # tmp_path / "fake-projects" -- plant the store ONLY there, never under
    # the real ~/.claude/projects, so finding it proves the env var was
    # actually read, not merely that nothing crashed.
    fake_store = tmp_path / "fake-projects" / slug_for(repo) / "memory"
    fake_store.mkdir(parents=True)
    (fake_store / "x.md").write_text("---\nname: x\ndescription: d\n---\n\nbody\n")

    project = Project(name="p", repos=(repo,))

    # No claude_projects argument -- exactly the call shape
    # tests/test_projects.py's project_for_path calls use, and the shape
    # that silently reached the real tree before this fix.
    stores = derive_stores(project)

    assert stores == [fake_store.resolve()], (
        f"derive_stores() with no explicit claude_projects did not honour "
        f"SPOKE_CLAUDE_PROJECTS: got {stores}, expected [{fake_store.resolve()}]"
    )


def test_the_suite_cannot_reach_real_schedule_locations(tmp_path):
    """Same proof as above, for the three real per-machine locations
    schedule.py defaults to when a test does not override them: the real
    LaunchAgents directory, and the real state/log directories under
    ~/.claude and ~/Library/Logs. A prior version of this suite wrote a
    real file to ~/.claude/spoke/schedule/estate.json because
    only two of these three were isolated -- this test fails if any of
    the three isolating env vars is removed from the fixture above."""
    from spoke.schedule import launch_agents_dir, default_state_dir, default_log_dir

    real_launch_agents = Path("~/Library/LaunchAgents").expanduser()
    real_state_dir = Path("~/.claude/spoke/schedule").expanduser()
    real_log_dir = Path("~/Library/Logs/spoke").expanduser()

    assert launch_agents_dir() != real_launch_agents
    assert default_state_dir() != real_state_dir
    assert default_log_dir() != real_log_dir
    for resolved in (launch_agents_dir(), default_state_dir(), default_log_dir()):
        assert str(tmp_path) in str(resolved), (
            f"{resolved} is not isolated to the fixture temp directory {tmp_path}"
        )


# --- fixtures for the MCP server tests -------------------------------------
#
# A git-backed memory store, isolated under tmp_path (never the real
# store -- the autouse fixture above already keeps SPOKE_REGISTRY and
# SPOKE_CLAUDE_PROJECTS off the real tree, and these fixtures build a
# Config directly, bypassing the registry entirely, so there is no path
# from a test using them to any real project's store).


def _init_scratch_store(store: Path) -> None:
    store.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(store)], check=True)
    subprocess.run(["git", "-C", str(store), "config", "user.email", "t@example.test"], check=True)
    subprocess.run(["git", "-C", str(store), "config", "user.name", "T"], check=True)
    # And do not let git start background work on a fixture at all: the
    # copy skips locks, this stops them being written in the first place.
    subprocess.run(["git", "-C", str(store), "config", "maintenance.auto", "false"], check=True)
    (store / "MEMORY.md").write_text("- [a](a.md) - hook\n")
    (store / "a.md").write_text("---\nname: a\ndescription: d\n---\n\nbody\n")
    subprocess.run(["git", "-C", str(store), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(store), "commit", "-qm", "init"], check=True)


_TEMPLATE: Path | None = None


def _TEMPLATE_for_test() -> Path:
    """The template store, built if it does not exist yet. Only the test
    that plants a lock file in it needs to name it."""
    global _TEMPLATE
    if _TEMPLATE is None or not _TEMPLATE.exists():
        _TEMPLATE = Path(tempfile.mkdtemp(prefix="spoke-store-template-")) / "store"
        _init_scratch_store(_TEMPLATE)
    return _TEMPLATE


def build_store(dest: Path, records: dict[str, str] | None = None,
                index: str | None = None) -> Path:
    """A git-backed memory store at `dest`, built once and copied after.

    "A git-backed store with a MEMORY.md and one record" is this suite's
    central noun, and it had nineteen implementations -- 25 `git init`
    call sites outside this file, four private `_git` helpers, thirteen
    private `_store` helpers. Every one of them paid the same cost:
    measured on this machine, `git init` + config + add + commit is
    149ms, and copying the finished tree is 8.4ms. Eighteen times.

    The template is built lazily and once per session. Tests get their
    own copy, so nothing shared is mutable.
    """
    global _TEMPLATE
    if _TEMPLATE is None or not _TEMPLATE.exists():
        _TEMPLATE = Path(tempfile.mkdtemp(prefix="spoke-store-template-")) / "store"
        _init_scratch_store(_TEMPLATE)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # dirs_exist_ok: several callers pass tmp_path itself, which
    # pytest has already created.
    # Transient git locks are skipped. The template is a real repository
    # and git runs background maintenance on real repositories; a
    # maintenance.lock that existed when the directory was listed and was
    # gone by the time shutil reached it took CI red on a change that had
    # nothing to do with it. A lock is never wanted in a copy anyway.
    shutil.copytree(_TEMPLATE, dest, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("*.lock"))

    # The git repository is the expensive part; adding records to a copy
    # of it is not. Callers whose seed genuinely differs -- a record with
    # a particular name or body -- say so rather than rebuilding a store
    # from scratch to get it.
    if records or index is not None:
        # `records` DECLARES the store's records; it does not add to the
        # template's. A caller asserting "the store holds exactly these"
        # would otherwise be told about the template's own seed record,
        # which is a fixture detail leaking into a test's subject.
        if records:
            for existing in dest.glob("*.md"):
                if existing.name != "MEMORY.md":
                    existing.unlink()
        for name, body in (records or {}).items():
            (dest / name).write_text(body)
        if index is not None:
            (dest / "MEMORY.md").write_text(index)
        subprocess.run(["git", "-C", str(dest), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(dest), "commit", "-qm", "seed"], check=True)
    return dest


_REPO_TEMPLATE: Path | None = None


def build_repo(dest: Path, files: dict[str, str]) -> Path:
    """A git repository at `dest` containing `files`, in one commit.

    The scan's tests build CODE repositories rather than memory stores --
    a different noun, with the same cost. `git init` plus two `config`
    calls is the fixed part and is identical every time, so it is built
    once and copied; only the files and the single commit are per-test.
    """
    global _REPO_TEMPLATE
    if _REPO_TEMPLATE is None or not _REPO_TEMPLATE.exists():
        _REPO_TEMPLATE = Path(tempfile.mkdtemp(prefix="spoke-repo-template-")) / "repo"
        _REPO_TEMPLATE.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(_REPO_TEMPLATE)], check=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(_REPO_TEMPLATE), "config", k, v], check=True)

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_REPO_TEMPLATE, dest, dirs_exist_ok=True)
    for rel, body in files.items():
        f = dest / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    subprocess.run(["git", "-C", str(dest), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(dest), "commit", "-qm", "seed"], check=True)
    return dest


def seed_nodes(store: Path, nodes, axes: tuple = ()) -> None:
    """Put `nodes` in the store's ledger, validated, in ONE commit.

    `LedgerStore.write` commits per node, which is right for a real write
    and wrong for a fixture: six nodes cost six commits, and that was
    ~0.8s of every test in the two HTTP test files -- the entire cost,
    dwarfing the request under test.

    The GATE still runs. `validate()` is called on each node exactly as
    `LedgerStore.write` would, so a fixture cannot build a store that
    could not legally exist; only the committing is batched.
    """
    from spoke.ledger.schema import serialise_node, validate

    ledger = Path(store) / "ledger"
    ledger.mkdir(parents=True, exist_ok=True)
    for node in nodes:
        reasons = validate(node, axes)
        assert not reasons, f"fixture node {node.name!r} would be refused: {reasons}"
        (ledger / f"{node.name}.md").write_text(serialise_node(node))
    subprocess.run(["git", "-C", str(store), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(store), "commit", "-qm", "seed nodes"], check=True)


def _write_scratch_node(store: Path, name: str, **overrides) -> None:
    from spoke.ledger import Node
    from spoke.ledger.store import LedgerStore

    fields = dict(
        name=name, type="item", state="open", title=f"Title for {name}",
        body="original body text", relations=(), ruling=None, blocked_by=(),
        provenance=(), opened="2026-09-01", updated="2026-09-01",
        by="human:test", claimed_by=None, claimed_at=None,
    )
    fields.update(overrides)
    node = Node(**fields)
    res = LedgerStore(store).write(node)
    assert res.ok, res.reasons


@pytest.fixture
def scratch_project(tmp_path):
    """(Config, project_name) for a git-backed store with two open ledger
    items, "first-item" and "second-item".

    `build_server()` takes a Workspace now, so a test that wants one
    calls `scratch_workspace`; this fixture stays for the many callers
    that only want the Config.
    """
    from spoke.config import Config

    store = tmp_path / "store"
    _init_scratch_store(store)
    _write_scratch_node(store, "first-item")
    _write_scratch_node(store, "second-item")
    cfg = Config(
        store_path=store, accepted_absences=(), stale_days=30,
        llm_provider="groq", llm_base_url=None,
    )
    return cfg, "scratch"


@pytest.fixture
def scratch_project_with_a_broken_record(tmp_path):
    """Same shape as scratch_project, plus one memory record missing its
    required `description` field -- check_schema reports it by filename
    (broken.md), which is what test_check_reports_defects looks for."""
    from spoke.config import Config

    store = tmp_path / "store"
    _init_scratch_store(store)
    _write_scratch_node(store, "first-item")
    (store / "broken.md").write_text("---\nname: broken\n---\n\nno description\n")
    subprocess.run(["git", "-C", str(store), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(store), "commit", "-qm", "broken record"], check=True)
    cfg = Config(
        store_path=store, accepted_absences=(), stale_days=30,
        llm_provider="groq", llm_base_url=None,
    )
    return cfg, "scratch"


@pytest.fixture
def scratch_workspace(scratch_project):
    """A Workspace over `scratch_project`, with `today` pinned.

    Pinned deliberately: a Workspace fixes the date once, and a test that
    let it drift would be the one place that could not observe the
    property the Workspace exists to provide."""
    from datetime import date
    from spoke.workspace import Workspace

    from spoke.projects import Project

    cfg, name = scratch_project
    # A real Project, not None: `Workspace.name` reads it, and the MCP
    # `spoke_project` tool reports it. A None project would have made the
    # fixture quietly describe a different situation than the one the
    # tests are about.
    return Workspace(config=cfg, project=Project(name, ()),
                     today=date(2026, 9, 10)), name


@pytest.fixture
def scratch_workspace_with_a_broken_record(scratch_project_with_a_broken_record):
    from datetime import date
    from spoke.workspace import Workspace

    from spoke.projects import Project

    cfg, name = scratch_project_with_a_broken_record
    return Workspace(config=cfg, project=Project(name, ()),
                     today=date(2026, 9, 10)), name
