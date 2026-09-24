import subprocess
from pathlib import Path
from spoke.store import Store


def _git(d, *args):
    return subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True).stdout.strip()


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "MEMORY.md").write_text("- [Alpha](alpha.md) — old hook\n")
    (tmp_path / "alpha.md").write_text("---\nname: alpha\ndescription: d\n---\n\noriginal body\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "seed"], check=True)
    return Store(tmp_path)


def test_write_updates_file_and_commits(tmp_path):
    s = _repo(tmp_path)
    res = s.write("alpha.md", "new body\n")
    assert res.ok
    assert "new body" in (tmp_path / "alpha.md").read_text()
    assert _git(tmp_path, "status", "--porcelain") == ""


def test_gate_failure_leaves_disk_untouched(tmp_path):
    s = _repo(tmp_path)
    before = (tmp_path / "alpha.md").read_text()
    res = s.write("alpha.md", "body with [[does-not-exist]]\n")
    assert not res.ok
    assert res.findings and res.findings[0].kind == "broken-wikilink"
    assert (tmp_path / "alpha.md").read_text() == before
    assert _git(tmp_path, "status", "--porcelain") == ""


def test_commit_does_not_sweep_a_foreign_file(tmp_path):
    s = _repo(tmp_path)
    # A well-formed record another session is mid-way through writing.
    (tmp_path / "someone-elses-wip.md").write_text(
        "---\nname: someone-elses-wip\ndescription: d\n---\n\nhalf finished\n")
    res = s.write("alpha.md", "new body\n")
    assert res.ok, f"write refused, so the pathspec was never exercised: {res.findings}"
    committed = _git(tmp_path, "show", "--name-only", "--format=", "HEAD").split()
    assert "someone-elses-wip.md" not in committed
    assert "someone-elses-wip.md" in _git(tmp_path, "status", "--porcelain")


def test_commit_does_not_sweep_a_STAGED_foreign_file(tmp_path):
    s = _repo(tmp_path)
    (tmp_path / "someone-elses-wip.md").write_text(
        "---\nname: someone-elses-wip\ndescription: d\n---\n\nstaged by another process\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "someone-elses-wip.md"], check=True)
    res = s.write("alpha.md", "new body\n")
    assert res.ok, res.findings
    committed = _git(tmp_path, "show", "--name-only", "--format=", "HEAD").split()
    assert "someone-elses-wip.md" not in committed


def test_index_line_is_updated_and_committed(tmp_path):
    s = _repo(tmp_path)
    s.write("alpha.md", "new body\n", index_line="- [Alpha](alpha.md) — new hook")
    assert "new hook" in (tmp_path / "MEMORY.md").read_text()
    assert "MEMORY.md" in _git(tmp_path, "show", "--name-only", "--format=", "HEAD")


def test_write_succeeds_in_a_repo_with_no_upstream(tmp_path):
    # The default local case: no remote configured. Must not be treated as a failure.
    s = _repo(tmp_path)
    res = s.write("alpha.md", "new body\n")
    assert res.ok, f"write refused in a repo with no upstream: {res.findings}"


def test_write_refuses_and_leaves_disk_untouched_when_pull_fails(tmp_path, monkeypatch):
    # The pull step only runs at all when an upstream is configured (the
    # structural check in _has_upstream), so a repo with no remote (the
    # default _repo fixture) never reaches it. To exercise "an upstream
    # exists AND the pull genuinely fails", fake both: the upstream
    # check reports success, and the pull itself reports a rebase
    # conflict. This is deliberately kept as a monkeypatched test rather
    # than a real second repo + remote — it isolates exactly the one
    # branch under test (a real remote would also need network-free
    # clone plumbing that adds set-up without adding coverage).
    s = _repo(tmp_path)
    before = (tmp_path / "alpha.md").read_text()
    import spoke.store as store_mod
    real = store_mod.subprocess.run

    def fake_run(cmd, *a, **k):
        if "@{u}" in cmd:
            class R:
                returncode = 0
                stdout = "origin/main"
                stderr = ""
            return R()
        if "pull" in cmd:
            class R:
                returncode = 1
                stdout = ""
                stderr = "error: could not apply ... CONFLICT"
            return R()
        return real(cmd, *a, **k)
    monkeypatch.setattr(store_mod.subprocess, "run", fake_run)

    res = s.write("alpha.md", "new body\n")
    assert res.ok is False
    assert any(f.kind == "git-pull-failed" for f in res.findings)
    assert (tmp_path / "alpha.md").read_text() == before
    assert _git(tmp_path, "status", "--porcelain") == ""


def test_no_upstream_detection_does_not_depend_on_locale(tmp_path, monkeypatch):
    # A non-English git locale must not turn a normal local-only repo into a refusal.
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    s = _repo(tmp_path)
    res = s.write("alpha.md", "new body\n")
    assert res.ok, f"write refused under a non-English locale: {res.findings}"


def test_write_refuses_when_commit_fails_and_does_not_report_a_bogus_sha(tmp_path):
    # A candidate identical to the current on-disk content stages nothing,
    # so `git commit` fails with "nothing to commit" — this is a real,
    # reachable failure mode (e.g. a race where another session already
    # wrote the same content), not a hypothetical.
    s = _repo(tmp_path)
    # Match the seeded body byte-for-byte, including the blank line the
    # frontmatter parser keeps as part of the body — anything else is a
    # real content change and would legitimately commit.
    res = s.write("alpha.md", "\noriginal body\n")
    assert res.ok is False
    assert any(f.kind == "git-commit-failed" for f in res.findings)
    assert res.commit is None


def test_stale_read_is_refused_and_disk_untouched(tmp_path):
    s = _repo(tmp_path)
    # Someone else changed the record after we loaded it.
    (tmp_path / "alpha.md").write_text("---\nname: alpha\ndescription: d\n---\n\nsomeone else's edit\n")
    before = (tmp_path / "alpha.md").read_text()
    res = s.write("alpha.md", "my edit\n", expected_body="\noriginal\n")
    assert res.ok is False
    assert any(f.kind == "stale-read" for f in res.findings)
    assert (tmp_path / "alpha.md").read_text() == before


def test_matching_expected_body_is_accepted(tmp_path):
    s = _repo(tmp_path)
    current = s.read("alpha.md").record.body
    res = s.write("alpha.md", "my edit\n", expected_body=current)
    assert res.ok, res.findings


def test_preexisting_defect_elsewhere_does_not_block_the_write(tmp_path):
    s = _repo(tmp_path)
    (tmp_path / "broken-elsewhere.md").write_text("no frontmatter at all\n")
    res = s.write("alpha.md", "new body\n")
    assert res.ok, f"an unrelated broken record blocked this write: {res.findings}"
    assert any(f.file == "broken-elsewhere.md" for f in res.advisories)


def test_defect_in_the_edited_file_still_blocks(tmp_path):
    s = _repo(tmp_path)
    before = (tmp_path / "alpha.md").read_text()
    res = s.write("alpha.md", "body with [[does-not-exist]]\n")
    assert res.ok is False
    assert any(f.kind == "broken-wikilink" for f in res.findings)
    assert (tmp_path / "alpha.md").read_text() == before


def _repo_with_real_upstream(tmp_path, reject_push=False):
    """A working repo with a REAL upstream -- a local bare repo, not a
    monkeypatch -- so `_has_upstream` is genuinely true and the pull/push
    machinery in `write()` runs unmodified. When `reject_push` is set, a
    `pre-receive` hook installed on the bare repo AFTER the initial setup
    push rejects every subsequent push while leaving fetch/pull (which
    pre-receive never touches) unaffected -- the cleanest way to force a
    real, non-monkeypatched push failure without needing network access."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)

    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "MEMORY.md").write_text("- [Alpha](alpha.md) — old hook\n")
    (work / "alpha.md").write_text("---\nname: alpha\ndescription: d\n---\n\noriginal body\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "seed"], check=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(bare)], check=True)
    # Set up tracking with the hook not yet installed -- this push must
    # succeed regardless of `reject_push`, or there is no upstream to test.
    setup_push = subprocess.run(
        ["git", "-C", str(work), "push", "-u", "origin", "main"], capture_output=True, text=True)
    assert setup_push.returncode == 0, setup_push.stderr

    if reject_push:
        hook = bare / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)

    return Store(work), bare


def test_push_succeeds_against_a_real_upstream(tmp_path):
    s, bare = _repo_with_real_upstream(tmp_path)
    res = s.write("alpha.md", "new body\n")
    assert res.ok, res.findings
    assert res.pushed is True
    assert not any(f.kind == "git-push-failed" for f in res.advisories)
    # The push must have actually reached the remote, not just be claimed.
    remote_head = _git(bare, "rev-parse", "main")
    assert remote_head == res.commit


def test_push_failure_is_an_advisory_but_the_commit_still_succeeds(tmp_path):
    s, bare = _repo_with_real_upstream(tmp_path, reject_push=True)
    res = s.write("alpha.md", "new body\n")
    # I2 / spec §7 step 5: the commit itself succeeded, so `ok` stays True
    # -- but the failed push must be visible, never silently swallowed.
    assert res.ok is True
    assert res.pushed is False
    assert any(f.kind == "git-push-failed" for f in res.advisories)
    # The local commit is real even though the remote never received it.
    assert "new body" in (tmp_path / "work" / "alpha.md").read_text()


def test_no_upstream_leaves_pushed_false_with_no_advisory(tmp_path):
    # The normal local-only case (no remote configured at all): `pushed`
    # is False, but this is not a failure and must carry no advisory.
    s = _repo(tmp_path)
    res = s.write("alpha.md", "new body\n")
    assert res.ok, res.findings
    assert res.pushed is False
    assert not any(f.kind == "git-push-failed" for f in res.advisories)


def test_rev_parse_failure_after_commit_is_not_reported_as_success(tmp_path, monkeypatch):
    # M4: `rev-parse HEAD` must use the checked git call. If it fails, the
    # commit's own identity is unknown and this must not read as `ok=True`
    # carrying a silently-empty commit SHA.
    s = _repo(tmp_path)
    import spoke.store as store_mod
    real = store_mod.subprocess.run

    def fake_run(cmd, *a, **k):
        if "rev-parse" in cmd and "HEAD" in cmd:
            class R:
                returncode = 1
                stdout = ""
                stderr = "fatal: ambiguous argument 'HEAD'"
            return R()
        return real(cmd, *a, **k)
    monkeypatch.setattr(store_mod.subprocess, "run", fake_run)

    res = s.write("alpha.md", "new body\n")
    assert res.ok is False
    assert any(f.kind == "git-rev-parse-failed" for f in res.findings)
    assert res.commit is None


# -- network calls are bounded ----------------------------------------------
#
# Observed 2026-09-21: with GitHub resetting connections, one ledger write
# took over 180s and a hand push over 300s. The write lock is held across
# pull-through-commit, so every other session queues behind a hang. The
# write never lied -- it blocked -- but a bound with no reason given is
# its own silence.

def test_a_network_call_is_bounded_and_a_local_one_is_not(tmp_path, monkeypatch):
    """The helper decides, not the call site: a caller that has to pass a
    timeout is a caller that can forget to."""
    import spoke.store as store_mod

    seen: list[tuple[str, object]] = []
    real = store_mod.subprocess.run

    def spy(cmd, **kw):
        seen.append((cmd[3] if len(cmd) > 3 else cmd[-1], kw.get("timeout")))
        return real(cmd, **kw)
    monkeypatch.setattr(store_mod.subprocess, "run", spy)

    s = _repo(tmp_path)
    s.write("alpha.md", "new body\n")

    by_op = dict(seen)
    assert by_op.get("add") is None, "a local call needs no timeout"
    assert by_op.get("commit") is None, "a local call needs no timeout"
    # This repo has no upstream, so drive the network ops directly.
    store_mod._git_checked(tmp_path, "fetch")
    assert dict(seen)["fetch"] == store_mod.NETWORK_TIMEOUT_S


def test_a_timed_out_push_reads_as_a_failure_that_says_so(tmp_path, monkeypatch):
    import subprocess as sp
    import spoke.store as store_mod

    def hang(cmd, **kw):
        if "push" in cmd:
            raise sp.TimeoutExpired(cmd, kw.get("timeout", 0))
        return sp.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(store_mod.subprocess, "run", hang)

    r = store_mod._git_checked(tmp_path, "push")
    assert r.ok is False
    assert "timed out" in r.stderr and "push" in r.stderr
    assert str(store_mod.NETWORK_TIMEOUT_S) in r.stderr, "say how long it waited"


def test_a_timed_out_pull_does_not_leave_a_rebase_in_progress(tmp_path):
    """Killing `git pull --rebase` mid-flight can leave the store mid-rebase,
    where every later write fails with 'rebase in progress'. Trading a hang
    for a wedged store is not a fix."""
    import spoke.store as store_mod

    s = _repo(tmp_path)
    # Stage a real interrupted rebase the way git leaves one.
    (tmp_path / ".git" / "rebase-merge").mkdir(parents=True)

    store_mod._recover_from_interrupted_rebase(tmp_path)

    assert not (tmp_path / ".git" / "rebase-merge").exists(), \
        "an interrupted rebase must be cleaned up, not left for the next write"
    assert s.write("alpha.md", "after\n").ok, "and the store still takes a write"
