"""The ledger store -- gated writes into `<memory_store>/ledger/`.

Shares its git discipline with `spoke.store.Store` rather than
reimplementing it: `_git_checked` (an explicit-pathspec, checked-by-exit-
code subprocess wrapper) and `_has_upstream` (checked by exit code, never
by matching stderr text) are imported from there, not copied. The one
piece of that store's discipline this module does NOT need is running its
gate against a scratch copy of the file tree: `Store`'s gate scans
serialised file content for whole-store properties (link integrity, index
consistency), so it needs a scratch tree to scan. A ledger node's gate --
`validate()` -- is a pure function of the in-memory `Node`; it needs no
file content at all, so "disk untouched on refusal" falls out for free by
running validate() before any write, with no scratch copy required.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from spoke.store import _git_checked, _has_upstream
import contextlib
import fcntl
import hashlib
import tempfile


@contextlib.contextmanager
def _write_lock(memory_store: Path):
    """An exclusive, blocking, cross-process lock for writes to one
    store. Keyed by the store's resolved path so two stores never share
    one, and kept in the temp dir so the lock file is never something a
    scan could list, commit or push."""
    key = hashlib.sha256(str(memory_store.resolve()).encode()).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"spoke-write-{key}.lock"
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

from . import LedgerError, Node, Skipped
from .scale import DEFAULT_MAX_VALUE
from .schema import SAFE_NAME_RE, parse_node, serialise_node, validate


@dataclass
class WriteResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    commit: str | None = None
    # True only when a push to an existing upstream actually succeeded.
    # False for "no upstream" (local-only by design, no advisory) and for
    # "upstream exists, push failed" -- which adds an advisory below. The
    # record store has drawn this line since spec section 7 step 5; the
    # ledger store committed and stopped, and twelve nodes from one
    # doctor run sat local-only behind twelve 'wrote' lines.
    pushed: bool = False
    # Non-blocking: the write itself stood, but something about it must
    # be shown, never swallowed. Every caller prints every one.
    advisories: list[str] = field(default_factory=list)


class LedgerStore:
    def __init__(self, memory_store: Path):
        # Resolved once, here, and reused everywhere below -- comparing a
        # resolved candidate path against an UNresolved store path is
        # exactly the bug this store shipped with (write() raised
        # ValueError on a symlinked store instead of writing). This
        # estate's memory stores are routinely reached through a symlink
        # (many project directories point at one canonical store), so
        # that is not a hypothetical.
        self.memory_store = Path(memory_store).resolve()
        self.path = self.memory_store / "ledger"
        # Populated by list_nodes(): filenames it declined to include
        # because they resolve outside ledger/. A skip must never be
        # silent, so callers (see cli.py's `ledger list`) surface this.
        self.skipped: list[Skipped] = []

    def _resolved_ledger_root(self) -> Path | None:
        """Resolve `self.path` (`<memory_store>/ledger`) and confirm the
        result really lands inside `memory_store`, or None if it does not.

        FINDING 1 fix: the old containment check compared a resolved
        candidate against `self.path.resolve()` alone. When `ledger/`
        itself is a symlink pointing outside the store, that comparison
        is worthless -- the candidate and the "root" it is checked
        against are both resolved through the very same escaping
        symlink, so they always agree with each other even though
        neither is genuinely inside `memory_store`. `write()` observed
        this directly: it wrote a file outside the store, then raised an
        uncaught ValueError from `p.relative_to(self.memory_store)` --
        after disk had already changed.

        Grounding containment in `self.memory_store` (resolved once, at
        construction, and never re-derived from anything that could
        itself be a symlink) closes that hole. Called before every read,
        write, and list -- not cached from construction -- so a `ledger/`
        that becomes an escaping symlink after the store object was built
        is still refused.
        """
        try:
            resolved = self.path.resolve()
        except (ValueError, OSError):
            return None
        if resolved != self.memory_store and self.memory_store not in resolved.parents:
            return None
        return resolved

    def _safe_node_path(self, name: str) -> Path | None:
        """Resolve `name` to a path inside `ledger/`, or None if unsafe.

        This is deliberately independent of `schema.validate`'s name check:
        that check can be bypassed (a forced attribute on a frozen
        dataclass, a subclass, a monkeypatch), and this store is shared
        with other sessions' real memory files. Two checks, both required,
        neither trusting the other:

        - the name itself must be a plain flat filename -- no path
          separators, no leading dot, no ".." anywhere, and no C0 control
          character (NUL included). Without this, a name like "a/b" would
          resolve to a path that is technically *inside* ledger/ (nested a
          subdirectory deeper) and so would slip past a pure containment
          check below, while still not being the single flat file a node
          name is supposed to name; a NUL byte, left unchecked, reaches
          Path.resolve() and raises an uncaught ValueError instead of a
          clean refusal.
        - the resolved path must land inside `ledger/`. This is what
          actually stops a ".." escape even if the character check above
          were somehow bypassed -- resolving the real path the operation
          would touch and refusing unless it is genuinely inside
          `ledger/` cannot be fooled by string tricks the first check
          might miss.

        Path resolution itself is wrapped in a try/except: some exotic
        input we have not thought of could still make `Path.resolve()`
        raise (ValueError, OSError), and that must become the same clean
        refusal, never a traceback.
        """
        # The SAME predicate `validate()` refuses a write on. These two
        # checks are belt and braces on purpose -- but they DISAGREED:
        # this one allowed uppercase and the regex did not, so
        # `read("Foo")` succeeded while `write` on the identical name was
        # refused. Two independent checks are a safeguard only while they
        # agree about what they are checking.
        if not SAFE_NAME_RE.match(name or ""):
            return None
        if (
            not name
            or "/" in name
            or "\\" in name
            or name.startswith(".")
            or ".." in name
            or any(ord(ch) < 0x20 for ch in name)
        ):
            return None
        ledger_root = self._resolved_ledger_root()
        if ledger_root is None:
            return None
        try:
            candidate = (self.path / f"{name}.md").resolve()
        except (ValueError, OSError):
            return None
        if candidate != ledger_root and ledger_root not in candidate.parents:
            return None
        return candidate

    def read(self, name: str) -> Node:
        p = self._safe_node_path(name)
        if p is None:
            raise LedgerError(f"name {name!r} is not a safe ledger node name -- refused")
        if not p.exists():
            raise FileNotFoundError(name)
        return parse_node(p.read_text())

    def list_nodes(self) -> list[Node]:
        """Every node genuinely inside `ledger/`.

        Globbing `ledger/*.md` is not enough: a symlink planted in
        `ledger/` (e.g. `ledger/evil.md -> ../../secret.md`) glob-matches
        the pattern and `read_text()` follows it, leaking content from
        outside the store. Each candidate is therefore resolved and
        checked for containment the same way `_safe_node_path` checks a
        name -- an entry that resolves outside `ledger/` is excluded, and
        its filename is recorded on `self.skipped` rather than dropped
        silently. Absence must never render as assurance.

        FINDING 1: containment is checked against `_resolved_ledger_root()`
        (grounded in `memory_store`), not against `self.path.resolve()`
        alone -- see that method's docstring. If `ledger/` itself resolves
        outside `memory_store`, the whole listing refuses rather than
        silently walking a directory outside the store.

        FINDING 2: a node whose frontmatter is unparseable, or whose
        `relations` entries are malformed, must not become an all-blank
        `Node` (invisible in a rendered listing) and must not raise
        uncaught out of this loop and kill every other node's listing
        (`parse_node` raises `LedgerError` for both cases -- see
        schema.py). Each such node is excluded and recorded on
        `self.skipped`, WITH the reason (unlike a symlink escape, which
        records only the filename -- the caller-facing format for that
        case is depended on by existing tests/callers), so a malformed
        node is reported, never blank and never fatal to the rest.
        """
        self.skipped: list[Skipped] = []
        if not self.path.exists():
            return []
        ledger_root = self._resolved_ledger_root()
        if ledger_root is None:
            self.skipped.append(Skipped(
                "escaped", "ledger/",
                "ledger/ itself resolves outside the memory store -- refusing to list",
            ))
            return []
        nodes = []
        for p in sorted(self.path.glob("*.md")):
            try:
                resolved = p.resolve()
            except (ValueError, OSError):
                self.skipped.append(Skipped("escaped", p.name))
                continue
            if resolved != ledger_root and ledger_root not in resolved.parents:
                self.skipped.append(Skipped("escaped", p.name))
                continue
            try:
                node = parse_node(p.read_text())
            except LedgerError as e:
                self.skipped.append(Skipped("unreadable", p.name, f"{p.name}: {e}"))
                continue
            nodes.append(node)
        return nodes

    def write(self, node: Node, axes: tuple = (), max_value: int = DEFAULT_MAX_VALUE) -> WriteResult:
        """Write `node` through the gate.

        `axes` is the project's declared capability axes. A caller that
        has them MUST pass them, or a score naming an axis nobody
        declared is stored and then renders nowhere -- indistinguishable,
        from the author's side, from it having worked.
        """
        # The gate runs first, on the in-memory node -- disk is untouched
        # on refusal because nothing has been written yet, not because a
        # write was reverted.
        reasons = validate(node, axes, max_value)
        if reasons:
            return WriteResult(ok=False, reasons=reasons)

        p = self._safe_node_path(node.name)
        if p is None:
            return WriteResult(
                ok=False,
                reasons=[f"name {node.name!r} resolves outside ledger/ -- refused"],
            )

        # FINDING 1: the relative path for the eventual `git add`/`commit`
        # is computed here -- before anything on disk changes, not after
        # `p.write_text()` -- so a path problem can only ever surface as
        # a clean refusal, never as an uncaught ValueError raised once
        # disk has already been modified. `_safe_node_path` already
        # proves `p` is inside a ledger root that is itself inside
        # `memory_store` (see `_resolved_ledger_root`), so this should
        # never actually raise; the try/except is defence in depth, not
        # the primary guard.
        try:
            rel_path = str(p.relative_to(self.memory_store))
        except ValueError:
            return WriteResult(
                ok=False,
                reasons=[f"{p} is not inside {self.memory_store} -- refused before any write"],
            )

        # One writer at a time, across processes. A doctor run and a scan
        # in another terminal wrote the same store within the same
        # second and the loser's `git pull --rebase` died with 'Cannot
        # rebase onto multiple branches' -- refused cleanly, but a probe
        # result silently missing for a day. The lock covers pull
        # through commit; readers never take it.
        with _write_lock(self.memory_store):
            return self._write_locked(node, p, rel_path)

    def _write_locked(self, node: Node, p: Path, rel_path: str) -> WriteResult:
        if _has_upstream(self.memory_store):
            pull = _git_checked(self.memory_store, "pull", "--rebase", "-q")
            if not pull.ok:
                return WriteResult(ok=False, reasons=[f"git-pull-failed: {pull.stderr[:500]}"])

        try:
            self.path.mkdir(parents=True, exist_ok=True)
        except FileExistsError:
            return WriteResult(
                ok=False,
                reasons=[f"{self.path} exists and is not a directory -- cannot write ledger nodes"],
            )
        p.write_text(serialise_node(node))

        add = _git_checked(self.memory_store, "add", "--", rel_path)
        if not add.ok:
            return WriteResult(ok=False, reasons=[f"git-add-failed: {add.stderr[:500]}"])

        commit = _git_checked(
            self.memory_store, "commit", "-q", "-m", f"ledger: write {node.name}", "--", rel_path
        )
        if not commit.ok:
            return WriteResult(ok=False, reasons=[f"git-commit-failed: {commit.stderr[:500]}"])

        rev = _git_checked(self.memory_store, "rev-parse", "HEAD")
        if not rev.ok:
            return WriteResult(ok=False, reasons=[f"git-rev-parse-failed: {rev.stderr[:500]}"])

        pushed = False
        advisories: list[str] = []
        if _has_upstream(self.memory_store):
            push = _git_checked(self.memory_store, "push", "-q")
            if push.ok:
                pushed = True
            else:
                # The commit stands -- ok stays True -- but a commit
                # nobody else can see must not read as shared.
                advisories.append(f"git-push-failed: {push.stderr[:500]}")
        return WriteResult(ok=True, commit=rev.stdout, pushed=pushed, advisories=advisories)
