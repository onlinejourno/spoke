"""A map of two real repos, built in one command, showing everything the
product can say.

`spoke demo <dir>` clones Tare and Forage -- two small MIT tools by the
same author, chosen because they are public, real, and have the shapes
the scan reads: a manifest, a docs/ directory, ADRs with a Status line
-- into <dir>/repos, builds a scratch memory store and project registry
there, scans, and then lays on top of what the scan derived one example
of each thing a person records: a hold and what it reaches, a block and
its chain, a deferral with its ruling, a decision that was superseded
and says by what, a node that went stale, a capability one product has
and the other lacks, a former name that still resolves, a problem
reported from a page, and scores on three axes with every basis. Every
flag kind the board can raise is raised at least once.

Nothing here touches the user's own store, registry or config: the demo
lives entirely under <dir>, and the command it prints at the end points
`serve` at it explicitly.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

from .config import Config, save_config
from .ledger import Node, Relation
from .ledger.scale import Axis, Score
from .ledger.store import LedgerStore
from .projects import add_project, load_registry, save_registry
from .scan import apply, scan_repos

#: The two demo repos. Public, MIT, by the author of this tool. The one
#: place a repository other than this one is named in the package, and
#: tests/test_no_estate_identity.py allows exactly these two, here.
DEMO_REPOS: dict[str, str] = {
    "tare": "https://github.com/onlinejourno/tare",
    "forage": "https://github.com/onlinejourno/forage",
}

PROJECT = "demo"

AXES = (
    Axis("coverage", "Coverage", 40),
    Axis("reliability", "Reliability", 35),
    Axis("editability", "Editability", 25),
)
SCALE_MAX = 4


@dataclass(frozen=True)
class DemoResult:
    root: Path
    store: Path
    registry: Path
    config: Path
    repos: dict[str, Path]
    scanned: dict
    nodes: int
    serve_command: str


class DemoError(RuntimeError):
    pass


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def clone(dest: Path, name: str, url: str) -> Path:
    """A shallow clone into dest/repos/<name>; reused when already there."""
    target = dest / "repos" / name
    if (target / ".git").exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["git", "clone", "--depth", "1", "--quiet", url, str(target)],
                       check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise DemoError(f"could not clone {url}: {e.stderr.strip() or e}") from e
    return target


def _store(dest: Path) -> Path:
    """A scratch memory store: a git repo with an index and two records,
    the shape Claude Code leaves behind. Reused when already there."""
    store = dest / "store"
    if (store / ".git").exists():
        return store
    store.mkdir(parents=True, exist_ok=True)
    (store / "MEMORY.md").write_text(
        "- [Crawl budget is the product](crawl-budget-is-the-product.md) — "
        "Forage measures what a crawler will actually fetch, not what a sitemap claims\n"
        "- [Signals over scores](signals-over-scores.md) — "
        "Tare reports the signals it observed; a score is a reading of them\n"
    )
    (store / "crawl-budget-is-the-product.md").write_text(
        "---\nname: crawl-budget-is-the-product\n"
        "description: Forage measures what a crawler will actually fetch, not what a sitemap claims\n"
        "metadata:\n  type: project\n---\n\n"
        "A sitemap is a claim. Forage fetches as a crawler would and reports the gap.\n"
    )
    (store / "signals-over-scores.md").write_text(
        "---\nname: signals-over-scores\n"
        "description: Tare reports the signals it observed; a score is a reading of them\n"
        "metadata:\n  type: project\n---\n\n"
        "The rubric lives beside the signals so a reader can disagree with the reading.\n"
    )
    _git(store, "init", "-q")
    _git(store, "-c", "user.name=spoke-demo", "-c", "user.email=demo@spoke.invalid",
         "add", "-A")
    _git(store, "-c", "user.name=spoke-demo", "-c", "user.email=demo@spoke.invalid",
         "commit", "-qm", "the demo store")
    return store


def _node(name: str, type_: str, state: str, title: str, body: str, *,
          rels: tuple = (), ruling: str | None = None, blocked_by: tuple = (),
          updated: str | None = None, by: str = "human:demo", aliases: tuple = (),
          scores: tuple = (), today: date) -> Node:
    prov = tuple({"field": f, "by": by, "at": today.isoformat()} for f in ("title", "body", "state"))
    return Node(
        name=name, type=type_, state=state, title=title, body=body,
        relations=tuple(Relation(r, t) for r, t in rels), ruling=ruling,
        blocked_by=blocked_by, provenance=prov,
        opened=today.isoformat(), updated=updated or today.isoformat(),
        by=by, claimed_by=None, claimed_at=None, aliases=aliases, scores=scores,
    )


def _write(store: LedgerStore, node: Node) -> None:
    # Re-running the demo must be a no-op, not a refusal: a write that
    # changes nothing has nothing to commit, and the store says so.
    try:
        if store.read(node.name) == node:
            return
    except FileNotFoundError:
        pass
    res = store.write(node, AXES, SCALE_MAX)
    if not res.ok:
        raise DemoError(f"the demo's own node {node.name!r} was refused by the gate: "
                        + "; ".join(res.reasons))


def build(dest: Path, repos: dict[str, Path] | None = None,
          today: date | None = None) -> DemoResult:
    """Build the demo under `dest`. `repos` maps name -> local path to use
    instead of cloning (tests, and offline use)."""
    today = today or date.today()
    dest = dest.expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)

    paths = {}
    for name, url in DEMO_REPOS.items():
        paths[name] = (repos or {}).get(name) or clone(dest, name, url)
        if not paths[name].is_dir():
            raise DemoError(f"{name}: {paths[name]} is not a directory")

    store_path = _store(dest)
    registry = dest / "projects.toml"
    if not registry.exists() or PROJECT not in load_registry(registry):
        add_project(registry, PROJECT, [paths["tare"], paths["forage"]], memory_store=store_path)
    reg = load_registry(registry)
    reg[PROJECT] = replace(reg[PROJECT], axes=AXES, scale_max=SCALE_MAX,
                           vocabulary={"product": "tool", "item": "note"})
    save_registry(registry, reg)

    config = dest / "config.toml"
    save_config(config, Config(store_path=store_path, accepted_absences=(), stale_days=30,
                               llm_provider="groq", llm_base_url=None,
                               brand_name="Spoke demo"))

    # 1. what the scan derives on its own
    result = scan_repos([paths["tare"], paths["forage"]], today, reg[PROJECT].vocabulary)
    ledger = LedgerStore(store_path)
    applied = apply(result, ledger, AXES, SCALE_MAX, today, write=True)

    # 2. what a person records on top. Names below are the scan's own for
    #    the two products -- the repo directory names -- so relations
    #    resolve.
    N = lambda *a, **k: _node(*a, today=today, **k)  # noqa: E731

    # a hold, and what it reaches -- one hop, because a hold is a scope
    _write(ledger, N("shipped-names-are-not-renamed", "decision", "hold",
        "Shipped names are not renamed",
        "The cost of a rename is not the rename; it is every reference that keeps using "
        "the old name. Both tools have shipped under their names. Hold.",
        rels=(("touches", "tare"), ("touches", "forage"))))

    # a block and its chain -- a block travels the whole chain
    _write(ledger, N("release-checklist-is-unwritten", "item", "open",
        "The release checklist has not been written",
        "Nobody has written down the steps a release needs.",
        rels=(("touches", "tare"),)))
    _write(ledger, N("tare-cannot-cut-a-release", "item", "blocked",
        "Tare cannot cut a release until the checklist exists",
        "A release without a checklist is a release nobody can repeat.",
        rels=(("touches", "tare"),), blocked_by=("release-checklist-is-unwritten",)))

    # a deferral with its ruling -- a deferral without one is a defect
    _write(ledger, N("forage-gets-a-capability-scale", "item", "deferred",
        "Forage gets scored on the capability scale",
        "Three axes are declared; Forage has scores on two of them so far.",
        rels=(("touches", "forage"),),
        ruling="Deferred until Forage's reliability can be observed rather than asserted. "
               "A number nobody looked at would read as one somebody did."))

    # a decision that was superseded, and says by what
    _write(ledger, N("one-user-agent-for-every-probe", "decision", "superseded",
        "Every probe identifies itself with one crawler user agent",
        "The first attempt: one UA string shared by every fetch.",
        rels=(("touches", "tare"),),
        ruling="Superseded by tare-0001-unified-crawler-ua-for-signal-probes, which is the "
               "decision as it was actually taken and recorded in the repo."))

    # something that went stale: open work nobody has touched in a year
    _write(ledger, N("crawl-budget-report-format", "item", "open",
        "Settle the crawl-budget report format",
        "Open since last year; nobody has touched it.",
        rels=(("touches", "forage"),), updated=(today - timedelta(days=380)).isoformat()))

    # a capability one tool has and the other lacks -- absent, on the capability lens
    _write(ledger, N("robots-txt-parsing", "capability", "open",
        "Parses robots.txt the way a crawler does",
        "Tare reads robots.txt to decide what a probe may fetch. Forage does not yet.",
        rels=(("touches", "tare"),)))

    # open work under a decision that is on hold -- deviates
    _write(ledger, N("rename-tare-to-tare-audit", "item", "open",
        "Rename Tare to Tare Audit",
        "Proposed rename, opened while the hold on renames stands.",
        rels=(("governed_by", "shipped-names-are-not-renamed"), ("touches", "tare"))))

    # a former name that still resolves, and a note that uses it
    try:
        tare = ledger.read("tare")
    except FileNotFoundError:
        tare = None
    if tare is not None:
        _write(ledger, replace(tare, aliases=tare.aliases + ("tare-checker",)))
    _write(ledger, N("tare-checker-needs-a-changelog", "item", "open",
        "The tool once called tare-checker needs a changelog",
        "Written against the old name on purpose: the map resolves it and says so.",
        rels=(("touches", "tare-checker"),)))

    # a problem reported from a page
    _write(ledger, N("surface-the-key-hides-behind-the-footer-demo", "item", "open",
        "Surface: the key hides behind the footer on a short window",
        "Reported from /board.\n\n---\nReported from /board on "
        f"{today.isoformat()}.\n\nWhat the page knew about itself:\n- lens: product\n- zoom: far\n",
        by="human:board:/board"))

    # 3. scores: every basis, one stale, one missing
    def score(name: str, *scores: Score) -> None:
        try:
            n = ledger.read(name)
        except FileNotFoundError:
            return
        _write(ledger, replace(n, scores=tuple(scores)))

    old = (today - timedelta(days=200)).isoformat()
    score("tare",
          Score("coverage", 3, "measured", on=today.isoformat(),
                note="Probes the eleven signals the README lists; verified against a live page",
                by="human:demo"),
          Score("reliability", 2, "measured", on=old,
                note="A run two hundred days ago; nobody has looked since", by="human:demo"),
          Score("editability", 1, "asserted", on=today.isoformat(),
                note="The README says thresholds are configurable. Nobody checked.", by="human:demo"))
    # Forage clears the threshold and gets a composite; Tare does not --
    # its reliability is stale and its editability asserted -- and the
    # page says exactly why. One of each is the demonstration.
    score("forage",
          Score("coverage", 2, "measured", on=today.isoformat(),
                note="Fetches as a crawler; does not yet read robots.txt", by="human:demo"),
          Score("reliability", 3, "measured", on=today.isoformat(),
                note="Ten runs against the same site, ten identical reports", by="human:demo"),
          Score("editability", 0, "unverified", on=today.isoformat(),
                note="No settings surface to observe; the number is a placeholder that does not count",
                by="human:demo"))

    nodes = len(ledger.list_nodes())
    cmd = (f"SPOKE_REGISTRY={registry} spoke serve --config {config} --port 8790 --open")
    return DemoResult(root=dest, store=store_path, registry=registry, config=config,
                      repos=paths, scanned={"proposals": len(result.proposals), **applied.counts},
                      nodes=nodes, serve_command=cmd)
