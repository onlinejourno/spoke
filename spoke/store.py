from __future__ import annotations
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from .checks import Finding
from .checks import NOT_A_MEMORY
from .checks.structural import check_all
from .frontmatter import Record, parse, serialise

# Every git subprocess we launch gets a fixed, English, C locale. This is
# defence in depth only: nothing in this module makes a decision by
# pattern-matching git's stderr text, so locale cannot change behaviour —
# but pinning it keeps whatever stderr does reach a human or a log stable
# and readable regardless of the host's configured locale.
_GIT_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C"}



@dataclass
class MemoryFile:
    name: str
    path: Path
    record: Record
    index_line: str | None


@dataclass
class WriteResult:
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    commit: str | None = None
    # Non-blocking findings from elsewhere in the store, surfaced on a
    # successful write (and on any failed one too, best-effort) so a
    # caller can show them without having been refused because of them.
    advisories: list[Finding] = field(default_factory=list)
    # True only when a push to an existing upstream actually succeeded.
    # False both for "no upstream configured" (the normal local-only
    # case -- no advisory) and for "upstream exists but the push failed"
    # (an advisory is added for that case; see write()).
    pushed: bool = False



@dataclass
class _GitResult:
    ok: bool
    stdout: str
    stderr: str


# How long a git call that talks to a remote may take before it is a
# failure. Generous for a one-file commit and short enough that a person
# is still watching: with GitHub resetting connections, one ledger write
# took over 180s and a hand push over 300s before giving up, and the
# write lock is held from pull through commit -- so every other session
# queues behind the hang. Blocking is honest, but a wait with no bound
# and no reason is its own kind of silence.
NETWORK_TIMEOUT_S = 60

# The subcommands that reach a remote. Only these get a clock; add,
# commit and rev-parse are local and must never be cut off half-done.
_NETWORK_OPS = frozenset({"push", "pull", "fetch", "clone", "ls-remote"})


def _recover_from_interrupted_rebase(path: Path) -> bool:
    """Undo a rebase left in progress, returning whether there was one.

    Killing `git pull --rebase` part-way can leave the repository mid-
    rebase, and then every later write fails with "rebase in progress" --
    trading a hang for a wedged store is not a fix. Called only after a
    pull was cut off, so there is nothing here to lose: the pull never
    completed, and the next write pulls again.
    """
    git_dir = path / ".git"
    if not any((git_dir / d).exists() for d in ("rebase-merge", "rebase-apply")):
        return False
    subprocess.run(["git", "-C", str(path), "rebase", "--abort"],
                   capture_output=True, text=True, env=_GIT_ENV, timeout=NETWORK_TIMEOUT_S)
    # `rebase --abort` fails on a rebase that never reached a commit; the
    # directories are still the thing that blocks the next write, so make
    # sure they are gone either way.
    for d in ("rebase-merge", "rebase-apply"):
        shutil.rmtree(git_dir / d, ignore_errors=True)
    return True


def _git_checked(path: Path, *args: str) -> _GitResult:
    """State-changing call (pull, add, commit): the caller must handle
    failure explicitly rather than have it silently swallowed.

    A call that reaches a remote is bounded here rather than by its
    caller -- five call sites across two stores, and a caller that has to
    remember a timeout is a caller that can forget one.
    """
    op = next((a for a in args if a in _NETWORK_OPS), None)
    timeout = NETWORK_TIMEOUT_S if op else None
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                           text=True, env=_GIT_ENV, timeout=timeout)
    except subprocess.TimeoutExpired:
        # subprocess.run has already killed the child. A half-done pull
        # can leave a rebase behind it, so say so in the same breath --
        # the reason a caller prints should describe the whole event.
        note = ""
        if op == "pull" and _recover_from_interrupted_rebase(path):
            note = "; an interrupted rebase was aborted"
        return _GitResult(ok=False, stdout="",
                          stderr=f"git {op} timed out after {NETWORK_TIMEOUT_S}s{note}")
    return _GitResult(ok=r.returncode == 0, stdout=r.stdout.strip(), stderr=r.stderr.strip())


def _has_upstream(path: Path) -> bool:
    """Structural check, not a text match: exit 0 means the current branch
    has an upstream configured; non-zero means it doesn't. This is locale-
    independent by construction — no stderr is ever inspected."""
    r = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture_output=True, text=True, env=_GIT_ENV,
    )
    return r.returncode == 0


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)

    @property
    def index_path(self) -> Path:
        return self.path / "MEMORY.md"

    def _index_line_for(self, name: str) -> str | None:
        if not self.index_path.exists():
            return None
        for line in self.index_path.read_text().splitlines():
            if f"]({name})" in line:
                return line
        return None

    def read(self, name: str) -> MemoryFile:
        p = self.path / name
        if not p.exists():
            raise FileNotFoundError(name)
        return MemoryFile(name, p, parse(p.read_text()), self._index_line_for(name))

    def list_records(self) -> list[MemoryFile]:
        return [self.read(p.name)
                for p in sorted(self.path.glob("*.md"))
                if p.name not in NOT_A_MEMORY]

    def write(self, name: str, body: str, index_line: str | None = None,
              accepted: tuple[str, ...] = (),
              expected_body: str | None = None) -> WriteResult:
        """Write `name`'s body (and its MEMORY.md index line, if given),
        gated by the structural checks and committed with an explicit
        pathspec.

        Ordering matters for correctness under concurrency: `git pull
        --rebase` runs FIRST (when an upstream is configured), and only
        THEN is the record read and the candidate text computed. Reading
        before the pull and writing unconditionally afterwards would
        silently discard a concurrent session's change that the pull just
        brought in -- this is a lost-update bug, not a hypothetical one:
        two clones of the same remote, one committing a frontmatter edit
        while the other computes a body-only edit from a pre-pull read,
        reproduces it directly.

        `expected_body`, when given, must be a body this caller previously
        read from disk (e.g. via `Store.read(name).record.body`). If the
        on-disk body no longer matches it *after* the pull, the write is
        refused with a `stale-read` finding and disk is left untouched --
        the record changed under the caller and writing anyway would
        silently lose that change. `expected_body=None` (the default) is
        last-write-wins: callers with no prior copy of the record (e.g.
        writing a brand-new file) get the old, unguarded behaviour.
        Interactive callers that hold a previously-read body should always
        pass it.

        The structural gate (`check_all`) still scans the whole store --
        link and index integrity are whole-store properties, so that part
        can't be scoped down. But only findings whose `file` is one of the
        files this write would change (the record, and MEMORY.md when the
        index line changes) block the write; everything else comes back as
        `advisories` on a successful result instead of refusing it. Without
        this, any pre-existing defect anywhere in the store -- or another
        session's half-finished file -- would block every write to every
        record. Trade-off, by design: a regression this write introduces
        into a file it does not touch would only surface as blocking if it
        happens to name a touched file; a regression in an untouched file
        is reported as an advisory, not a refusal.

        Not atomic: the record write and the MEMORY.md write are two
        separate `Path.write_text` calls. A crash between them leaves a
        dirty working tree (one file changed, the other not, both
        uncommitted); the next `write()` call's `git pull --rebase` will
        refuse on top of that dirty tree rather than silently proceeding --
        fail-closed, not corrupting.
        """
        # Ask the structural question first: is an upstream even configured?
        # A repo with no remote (the normal case for tests and a fresh
        # local-only install) has nothing to pull, so skip straight ahead.
        # Only a repo WITH an upstream attempts the pull, and only then
        # does a non-zero exit count as a genuine failure.
        if _has_upstream(self.path):
            pull = _git_checked(self.path, "pull", "--rebase", "-q")
            if not pull.ok:
                return WriteResult(ok=False, findings=[
                    Finding("git-pull-failed", name, pull.stderr[:500])
                ])

        if expected_body is not None:
            current_body = self.read(name).record.body
            if current_body != expected_body:
                return WriteResult(ok=False, findings=[
                    Finding("stale-read", name,
                            "the record changed on disk since it was loaded; reload before saving")
                ])

        # Candidate texts are derived from a read taken AFTER the pull (and
        # after the staleness check above), never from stale pre-pull
        # content.
        mf = self.read(name)
        rec = mf.record
        rec.body = body
        texts = {name: serialise(rec)}
        if index_line is not None and mf.index_line is not None:
            texts["MEMORY.md"] = self.index_path.read_text().replace(
                mf.index_line, index_line
            )

        # Gate against the candidate result, in a scratch copy. Disk is
        # untouched unless this gate passes.
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "store"
            shutil.copytree(self.path, scratch, ignore=shutil.ignore_patterns(".git"))
            for fname, text in texts.items():
                (scratch / fname).write_text(text)
            findings = check_all(scratch, accepted)

        touched = set(texts)
        blocking = [f for f in findings if f.file in touched]
        advisories = [f for f in findings if f.file not in touched]
        if blocking:
            return WriteResult(ok=False, findings=blocking, advisories=advisories)

        for fname, text in texts.items():
            (self.path / fname).write_text(text)
        paths = sorted(texts)

        add = _git_checked(self.path, "add", "--", *paths)
        if not add.ok:
            return WriteResult(ok=False, findings=[
                Finding("git-add-failed", name, add.stderr[:500])
            ])

        commit = _git_checked(self.path, "commit", "-q", "-m", f"memory: edit {name}", "--", *paths)
        if not commit.ok:
            return WriteResult(ok=False, findings=[
                Finding("git-commit-failed", name, commit.stderr[:500])
            ])

        # Checked, not the bare `_git` helper: a failure here means the
        # commit's own SHA is unknown, which must surface as a failure --
        # not as `ok=True` carrying a silently-empty commit id.
        rev = _git_checked(self.path, "rev-parse", "HEAD")
        if not rev.ok:
            return WriteResult(ok=False, findings=[
                Finding("git-rev-parse-failed", name, rev.stderr[:500])
            ], advisories=advisories)

        pushed = False
        if _has_upstream(self.path):
            push = _git_checked(self.path, "push")
            if push.ok:
                pushed = True
            else:
                # The commit itself succeeded -- ok stays True -- but a
                # push failure must never read as success. Spec §7 step 5:
                # "On failure, say so in the UI; do not report success."
                advisories = advisories + [
                    Finding("git-push-failed", name, push.stderr[:500])
                ]

        return WriteResult(ok=True, commit=rev.stdout, pushed=pushed,
                            advisories=advisories)
