"""One project, resolved and ready to act on.

Before this module, every command reassembled the same noun by hand out
of `(Config, project name, vocabulary, axes, today, stale_days)` -- and
each surface assembled a different subset, so each surface knew a
different amount about the project it was working on.

The cost was not tidiness. `LedgerStore.write(node, axes)` says in its
own docstring that a caller holding the axes MUST pass them, or a score
naming an axis nobody declared is stored and then renders nowhere,
indistinguishable from having worked. There were five write sites and
ONE passed them. The MCP server could not comply at all: it was handed a
project *name*, and `Config` carries no axes, so the agent-facing write
path had no route to them.

An invariant that lives in a docstring is a request. `Workspace.write`
makes it structural: there is no way to reach the store through a
Workspace and forget what the write must be validated against.

Two smaller things this module ends by owning them:

- **`today` is fixed once per command.** It used to be `date.today()`
  called at thirteen sites, twice inside `ledger score` alone, so a run
  crossing midnight stamped a score's `on` and its node's `updated` with
  different dates. A workspace is made at one instant and every date in
  that command comes from it.
- **The registry is read once.** `_resolve` and `_active_project` each
  loaded it and re-selected the project, so every command did the whole
  resolution twice and could, in principle, get two answers.

Nothing here prints. Resolution notices travel on `notices` for the
caller to render, so the MCP server can surface the store-override
warning that the CLI prints and the MCP server used to drop.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import Config, load_config, load_config_file
from .projects import Project, config_for, default_claude_projects, load_registry, select_project
from .clock import Clock

DEFAULT_REGISTRY = "~/.claude/spoke/projects.toml"


def registry_path() -> Path:
    return Path(os.environ.get("SPOKE_REGISTRY") or DEFAULT_REGISTRY).expanduser()


def store_error(store_path: Path) -> str | None:
    """None when `store_path` is usable; otherwise a message naming
    exactly what is wrong with it.

    A mis-pointed store (a typo'd config path, an empty directory) must
    never read as a clean store: `Path.glob` over a nonexistent directory
    returns nothing, so `check`, `stale`, `doctor` and `contradictions`
    would report "0 defects" forever, indistinguishable from real health.

    Lives here rather than in cli.py because it had three importers and
    no home: `server.py` imported it from `cli` lazily to dodge a module
    cycle, and `mcp_server.py` kept a byte-identical fork because
    importing `cli` would pull in a module that writes to stdout, and
    stdout is the MCP wire. A shared predicate needs a seam of its own.
    """
    if store_path is None or str(store_path) in ("", "."):
        # `Path("")` is how load_config_file represents "the file names no
        # store". It resolves to the working directory, which exists, so
        # without this line an unconfigured install reads as a store with
        # no records -- and the first-run screen never appears.
        return "no memory store configured"
    if not store_path.exists():
        return f"store path does not exist: {store_path}"
    if not store_path.is_dir():
        return f"store path is not a directory: {store_path}"
    if not any(store_path.glob("*.md")):
        return f"store path contains no .md records: {store_path}"
    return None


@dataclass(frozen=True)
class Workspace:
    """Everything a command needs to act on one project."""

    config: Config
    project: Project | None
    today: date
    notices: tuple[str, ...] = ()

    # ── what the project declares ────────────────────────────────────
    @property
    def name(self) -> str | None:
        return self.project.name if self.project else None

    @property
    def axes(self) -> tuple:
        return tuple(self.project.axes) if self.project else ()

    @property
    def scale_max(self) -> int:
        from .ledger.scale import DEFAULT_MAX_VALUE
        return self.project.scale_max if self.project else DEFAULT_MAX_VALUE

    @property
    def vocabulary(self) -> dict[str, str]:
        return dict(self.project.vocabulary) if self.project else {}

    @property
    def repos(self) -> tuple[Path, ...]:
        return tuple(self.project.repos) if self.project else ()

    # ── what the config decides ──────────────────────────────────────
    @property
    def store_path(self) -> Path:
        return self.config.store_path

    @property
    def stale_days(self) -> int:
        return self.config.stale_days

    @property
    def clock(self) -> Clock:
        """The `(today, stale_days)` pair as one value.

        Every staleness question in the codebase needs both halves, and
        they travelled as two parameters through nine signatures. Handing
        out the pair already joined is what stops half of it arriving.
        """
        return Clock(today=self.today, stale_days=self.stale_days)

    def store_error(self) -> str | None:
        return store_error(self.store_path)

    # ── the stores ───────────────────────────────────────────────────
    def ledger(self):
        from .ledger.store import LedgerStore

        return LedgerStore(self.store_path)

    def memory(self):
        from .store import Store

        return Store(self.store_path)

    def write(self, node, store=None):
        """Write `node` through the gate, against THIS project's axes.

        The only reason this method exists: `LedgerStore.write` takes the
        axes as an argument a caller can forget, and four of five callers
        did. Going through a Workspace, there is nothing to forget.
        """
        return (store or self.ledger()).write(node, self.axes, self.scale_max)


def resolve(
    config_path: Path | None = None,
    project: str | None = None,
    today: date | None = None,
    registry: Path | None = None,
    claude_projects: Path | None = None,
) -> Workspace:
    """Resolve one project into a Workspace.

    The registry is read FIRST and exactly once. A multi-project config
    file legitimately has no `[store].path`, so requiring one before
    consulting the registry would refuse every valid multi-project
    invocation.

    `today` is a parameter so a test can pin it and so a caller that
    already has one does not create a second.
    """
    cfg_path = Path(config_path) if config_path else Path("config.toml")
    when = today or date.today()
    reg = load_registry(registry or registry_path())
    if not reg:
        return Workspace(config=load_config(cfg_path), project=None, today=when)

    requested = project or os.environ.get("SPOKE_PROJECT")
    proj = select_project(reg, requested)
    base = load_config(cfg_path, require_store=False)
    resolved = config_for(proj, base, claude_projects or default_claude_projects())
    return Workspace(
        config=resolved,
        project=proj,
        today=when,
        notices=_override_notices(cfg_path, proj, resolved),
    )


def _override_notices(cfg_path: Path, proj: Project, resolved: Config) -> tuple[str, ...]:
    """A note when the registry's project store overrides a store path the
    config file explicitly named.

    Without it, a config written to point at one store -- a scratch config
    meant for testing, say -- is redirected to a different, live store
    with nothing on screen saying so. That is not hypothetical: a
    throwaway node reached the founder's live memory store exactly this
    way.

    Reads the FILE only, never an environment override, because the
    question is what the file says versus what the project resolved to.
    """
    file_store = load_config_file(cfg_path).store_path
    if file_store == Path(""):
        return ()                       # names no store -- the normal case
    if file_store.expanduser().resolve() == resolved.store_path.resolve():
        return ()                       # they agree
    return (
        f"note: config names {file_store}, but project {proj.name!r} resolves to\n"
        f"      {resolved.store_path} -- using the project's store.\n"
        "      Pass --project or set SPOKE_REGISTRY to change this.",
    )
