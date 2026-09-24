import subprocess
from pathlib import Path
import pytest
from spoke.ledger import LedgerError, Node, Relation
from spoke.ledger.store import LedgerStore


def _repo(tmp_path: Path) -> LedgerStore:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / "MEMORY.md").write_text("")
    (tmp_path / "ledger").mkdir()
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    return LedgerStore(tmp_path)


def _node(**kw) -> Node:
    base = dict(name="a", type="item", state="open", title="A thing", body="why\n",
                relations=(), ruling=None, blocked_by=(), provenance=(),
                opened=None, updated=None, by=None, claimed_by=None, claimed_at=None)
    base.update(kw)
    return Node(**base)


def test_write_then_read_round_trips(tmp_path):
    s = _repo(tmp_path)
    assert s.write(_node()).ok
    assert s.read("a").title == "A thing"


def test_nodes_live_under_ledger_not_beside_the_memories(tmp_path):
    s = _repo(tmp_path)
    s.write(_node())
    assert (tmp_path / "ledger" / "a.md").exists()
    assert not (tmp_path / "a.md").exists()


def test_an_unruled_deferral_is_refused_and_disk_is_untouched(tmp_path):
    s = _repo(tmp_path)
    s.write(_node())
    before = (tmp_path / "ledger" / "a.md").read_text()
    res = s.write(_node(state="deferred"))
    assert res.ok is False
    assert any("ruling" in r for r in res.reasons)
    assert (tmp_path / "ledger" / "a.md").read_text() == before


def test_commit_does_not_sweep_a_foreign_file(tmp_path):
    s = _repo(tmp_path)
    (tmp_path / "someone-elses-wip.md").write_text("---\nname: x\ndescription: d\n---\n\nwip\n")
    res = s.write(_node())
    assert res.ok, res.reasons
    committed = subprocess.run(
        ["git", "-C", str(tmp_path), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True, check=True).stdout.split()
    assert "someone-elses-wip.md" not in committed


def test_list_nodes_returns_every_node(tmp_path):
    s = _repo(tmp_path)
    s.write(_node(name="a"))
    s.write(_node(name="b"))
    assert sorted(n.name for n in s.list_nodes()) == ["a", "b"]


def test_relations_survive_a_write_and_read(tmp_path):
    s = _repo(tmp_path)
    s.write(_node(relations=(Relation("blocked_by", "other"),), state="blocked", blocked_by=("other",)))
    assert s.read("a").relations == (Relation("blocked_by", "other"),)


@pytest.mark.parametrize("bad", [
    "../MEMORY", "../../etc/passwd", "a/b", "a\\b", ".hidden", "", "..",
    "UPPER", "has space", "trailing/",
])
def test_a_name_that_could_escape_the_ledger_is_refused(tmp_path, bad):
    s = _repo(tmp_path)
    canary = (tmp_path / "MEMORY.md").read_text()
    res = s.write(_node(name=bad))
    assert res.ok is False, f"{bad!r} was accepted"
    assert (tmp_path / "MEMORY.md").read_text() == canary
    assert not (tmp_path / "etc").exists()


def test_the_store_refuses_a_path_outside_the_ledger_even_if_validation_is_bypassed(tmp_path, monkeypatch):
    # The store must not depend solely on validate() to stay inside ledger/.
    # object.__setattr__ on a frozen Node does NOT bypass validate() here --
    # validate() reads the live attribute, so it would still catch a name
    # mutated that way. To prove the store's OWN path check is what refuses,
    # independently of validate(), stub validate() itself to always pass and
    # confirm LedgerStore.write still refuses the traversal.
    import spoke.ledger.store as store_mod
    monkeypatch.setattr(store_mod, "validate", lambda node, axes=(), max_value=5: [])

    s = _repo(tmp_path)
    canary = (tmp_path / "MEMORY.md").read_text()
    res = s.write(_node(name="../MEMORY"))
    assert res.ok is False
    assert (tmp_path / "MEMORY.md").read_text() == canary


@pytest.mark.parametrize("bad", ["../MEMORY", "../../etc/passwd", "a/b", "..", ".hidden"])
def test_read_refuses_a_name_that_escapes_the_ledger(tmp_path, bad):
    s = _repo(tmp_path)
    with pytest.raises(LedgerError) as e:
        s.read(bad)
    assert "ledger" in str(e.value).lower()


def test_read_still_works_for_a_normal_name(tmp_path):
    s = _repo(tmp_path)
    s.write(_node(name="alpha"))
    assert s.read("alpha").name == "alpha"


def test_ledger_as_a_file_is_refused_not_a_crash(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / "MEMORY.md").write_text("")
    (tmp_path / "ledger").write_text("not a directory")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    s = LedgerStore(tmp_path)
    res = s.write(_node())
    assert res.ok is False
    assert any("ledger" in r for r in res.reasons)


# --- Finding 1: a NUL byte (or other C0 control char) must be a clean
# refusal, never an uncaught ValueError from Path.resolve(). ---

@pytest.mark.parametrize("bad", ["a\x00b", "\x00", "a\nb"])
def test_read_refuses_a_control_character_cleanly_not_a_traceback(tmp_path, bad):
    s = _repo(tmp_path)
    with pytest.raises(LedgerError) as e:
        s.read(bad)
    assert "ledger" in str(e.value).lower()


# --- Finding 2: list_nodes() must not follow a symlink planted inside
# ledger/ that points outside the store, and the exclusion must be
# reported, not silent. ---

def test_list_nodes_excludes_a_symlink_planted_inside_ledger(tmp_path):
    s = _repo(tmp_path)
    s.write(_node(name="a"))
    secret = tmp_path.parent / "secret.md"
    secret.write_text("---\nname: secret\ntype: item\nstate: open\ntitle: t\n---\n\noutside content\n")
    (tmp_path / "ledger" / "evil.md").symlink_to(secret)

    nodes = s.list_nodes()

    assert sorted(n.name for n in nodes) == ["a"]
    assert s.skipped == ["evil.md"]


def test_preamble_does_not_leak_a_symlinked_node_outside_ledger(tmp_path):
    from spoke.ledger.preamble import render_preamble

    s = _repo(tmp_path)
    secret = tmp_path.parent / "secret.md"
    secret.write_text(
        "---\nname: secret\ntype: item\nstate: open\ntitle: t\n---\n\nTOP-SECRET-OUTSIDE-CONTENT\n"
    )
    (tmp_path / "ledger" / "evil.md").symlink_to(secret)

    nodes = s.list_nodes()
    text, _ = render_preamble(nodes, [], 4000)

    assert "TOP-SECRET-OUTSIDE-CONTENT" not in text
    assert s.skipped == ["evil.md"]


# --- Finding 3: write() must compare resolved-to-resolved, so a store
# reached through a symlink (this estate's normal topology) writes
# instead of raising ValueError. ---

def test_write_succeeds_against_a_store_reached_through_a_symlink(tmp_path):
    real = tmp_path / "real-store"
    real.mkdir()
    subprocess.run(["git", "init", "-q", str(real)], check=True)
    subprocess.run(["git", "-C", str(real), "config", "user.email", "t@example.test"], check=True)
    subprocess.run(["git", "-C", str(real), "config", "user.name", "T"], check=True)
    (real / "MEMORY.md").write_text("")
    (real / "ledger").mkdir()
    subprocess.run(["git", "-C", str(real), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(real), "commit", "-qm", "init"], check=True)

    link = tmp_path / "store-via-symlink"
    link.symlink_to(real, target_is_directory=True)

    s = LedgerStore(link)
    res = s.write(_node())

    assert res.ok, res.reasons
    assert (real / "ledger" / "a.md").exists()


# --- Finding 1: `ledger/` ITSELF (not the store) is a symlink escaping
# `memory_store` -- write() must refuse before touching disk, never write
# outside the store and then crash. ---

def test_write_refuses_when_ledger_itself_is_a_symlink_escaping_the_store(tmp_path):
    real = tmp_path / "real-store"
    real.mkdir()
    subprocess.run(["git", "init", "-q", str(real)], check=True)
    subprocess.run(["git", "-C", str(real), "config", "user.email", "t@example.test"], check=True)
    subprocess.run(["git", "-C", str(real), "config", "user.name", "T"], check=True)
    (real / "MEMORY.md").write_text("")
    outside = tmp_path / "outside-ledger"
    outside.mkdir()
    (real / "ledger").symlink_to(outside, target_is_directory=True)
    subprocess.run(["git", "-C", str(real), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(real), "commit", "-qm", "init"], check=True)

    s = LedgerStore(real)
    res = s.write(_node())

    # Refused, not a crash, and NOTHING appears at the escape target --
    # the old bug wrote the file outside the store and only THEN raised.
    assert res.ok is False
    assert not any(outside.iterdir()), f"a file escaped the store: {list(outside.iterdir())}"

    # list_nodes() must not walk the escaping directory as if it were a
    # normal ledger/ either -- it refuses (empty result) and reports why.
    nodes = s.list_nodes()
    assert nodes == []
    assert s.skipped != []


def test_read_refuses_when_ledger_itself_is_a_symlink_escaping_the_store(tmp_path):
    real = tmp_path / "real-store2"
    real.mkdir()
    subprocess.run(["git", "init", "-q", str(real)], check=True)
    subprocess.run(["git", "-C", str(real), "config", "user.email", "t@example.test"], check=True)
    subprocess.run(["git", "-C", str(real), "config", "user.name", "T"], check=True)
    (real / "MEMORY.md").write_text("")
    outside = tmp_path / "outside-ledger2"
    outside.mkdir()
    (outside / "a.md").write_text(
        "---\nname: a\ntype: item\nstate: open\ntitle: t\n---\n\noutside content\n"
    )
    (real / "ledger").symlink_to(outside, target_is_directory=True)
    subprocess.run(["git", "-C", str(real), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(real), "commit", "-qm", "init"], check=True)

    s = LedgerStore(real)
    with pytest.raises(LedgerError):
        s.read("a")


# --- Finding 2: a node with unparseable YAML, and a node with a malformed
# relations entry, must each be excluded and reported -- never a blank
# Node, never fatal to the whole listing. ---

def test_list_nodes_excludes_and_reports_unparseable_yaml(tmp_path):
    s = _repo(tmp_path)
    s.write(_node(name="good"))
    # Invalid YAML: an unterminated flow sequence.
    (tmp_path / "ledger" / "bad.md").write_text(
        "---\nname: bad\ntype: item\nstate: open\nrelations: [touches\n---\n\nbroken\n"
    )

    nodes = s.list_nodes()

    assert sorted(n.name for n in nodes) == ["good"]
    assert any("bad.md" in entry for entry in s.skipped)


def test_list_nodes_excludes_and_reports_a_malformed_relation(tmp_path):
    s = _repo(tmp_path)
    s.write(_node(name="good"))
    # A string where a {rel, to} mapping is required.
    (tmp_path / "ledger" / "bad.md").write_text(
        "---\nname: bad\ntype: item\nstate: open\ntitle: t\nrelations: [touches]\n---\n\nbody\n"
    )

    nodes = s.list_nodes()

    assert sorted(n.name for n in nodes) == ["good"]
    assert any("bad.md" in entry for entry in s.skipped)


def test_a_malformed_node_does_not_take_down_the_rest_of_the_listing(tmp_path):
    s = _repo(tmp_path)
    s.write(_node(name="alpha"))
    s.write(_node(name="beta"))
    (tmp_path / "ledger" / "bad.md").write_text(
        "---\nname: bad\ntype: item\nstate: open\nrelations: [touches\n---\n\nbroken\n"
    )

    # Must not raise -- this is precisely the "kills the whole listing
    # with a traceback" failure mode Finding 2 describes.
    nodes = s.list_nodes()

    assert sorted(n.name for n in nodes) == ["alpha", "beta"]
    assert len(s.skipped) == 1


def test_a_write_holds_an_exclusive_cross_process_lock_from_pull_through_commit(tmp_path, monkeypatch):
    """Two writers in two processes -- a doctor run and a scan -- hit the
    same store within a second; the loser's pull died with 'Cannot rebase
    onto multiple branches'. The lock is a file lock, so it must be held
    by a DIFFERENT file descriptor than the test's own probe."""
    import fcntl
    import spoke.ledger.store as st
    s = _repo(tmp_path)
    key = __import__("hashlib").sha256(str(tmp_path.resolve()).encode()).hexdigest()[:16]
    lock_path = Path(__import__("tempfile").gettempdir()) / f"spoke-write-{key}.lock"
    seen = {}
    real = st._git_checked

    def probing(cwd, *args):
        if args and args[0] == "commit":
            with open(lock_path, "w") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen["held"] = False
                    fcntl.flock(fh, fcntl.LOCK_UN)
                except BlockingIOError:
                    seen["held"] = True
        return real(cwd, *args)

    monkeypatch.setattr(st, "_git_checked", probing)
    assert s.write(_node()).ok
    assert seen.get("held") is True, "commit ran without the write lock held"
    # and it is released afterwards
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh, fcntl.LOCK_UN)


# -- pushing ------------------------------------------------------------------
#
# The record store pushes and reports a failed push; the ledger store
# committed and stopped. Twelve nodes written in one doctor run sat
# local-only behind twelve 'wrote' lines before anyone looked at
# `git status`. A store with an upstream pushes, and says when it could not.

def _repo_with_upstream(tmp_path: Path, reject_push: bool = False):
    """A working checkout with a REAL upstream (a local bare repo). With
    `reject_push`, a pre-receive hook installed after the tracking push
    rejects every later push and leaves pull alone."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(work), "config", k, v], check=True)
    (work / "MEMORY.md").write_text("# index\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "seed"], check=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(bare)], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "-u", "origin", "main"], check=True)
    if reject_push:
        hook = bare / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
    return LedgerStore(work), work, bare


def _remote_head(bare: Path) -> str:
    return subprocess.run(["git", "-C", str(bare), "rev-parse", "main"],
                          capture_output=True, text=True).stdout.strip()


def test_a_write_to_a_store_with_an_upstream_pushes_and_the_remote_has_it(tmp_path):
    s, work, bare = _repo_with_upstream(tmp_path)
    res = s.write(_node())
    assert res.ok and res.pushed is True and res.advisories == []
    assert _remote_head(bare) == res.commit, "pushed must mean the remote has the commit"


def test_a_failed_push_is_reported_and_the_local_commit_stands(tmp_path):
    s, work, bare = _repo_with_upstream(tmp_path, reject_push=True)
    res = s.write(_node())
    assert res.ok is True, "the commit itself succeeded"
    assert res.pushed is False
    assert len(res.advisories) == 1 and res.advisories[0].startswith("git-push-failed:")
    assert (work / "ledger" / "a.md").exists()
    assert _remote_head(bare) != res.commit


def test_no_upstream_is_local_only_and_carries_no_advisory(tmp_path):
    s = _repo(tmp_path)
    res = s.write(_node())
    assert res.ok and res.pushed is False and res.advisories == []


def test_the_store_fixture_survives_a_lock_file_appearing_mid_copy(tmp_path):
    """CI went red on a race, not on a change: the fixture copies a
    template store including its live .git, and git's background
    maintenance had created .git/objects/maintenance.lock, which was gone
    again before shutil reached it --

        shutil.Error: [(.../.git/objects/maintenance.lock, ...,
                        "[Errno 2] No such file or directory")]

    A lock is transient by definition and is never wanted in a copy. This
    plants one and proves the copy ignores it rather than carrying it or
    dying on it.
    """
    from tests.conftest import build_store, _TEMPLATE_for_test

    template = _TEMPLATE_for_test()
    lock = template / ".git" / "objects" / "maintenance.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    try:
        dest = build_store(tmp_path / "copy")
        assert not (dest / ".git" / "objects" / "maintenance.lock").exists(), \
            "a transient git lock must not be copied into a fixture store"
        assert (dest / ".git").is_dir(), "the copy is still a git repository"
        assert LedgerStore(dest).write(_node()).ok, "and it still takes a write"
    finally:
        lock.unlink(missing_ok=True)
