"""The ledger's node vocabulary.

A node is a single unit of work-history: what was decided, deferred,
abandoned, or built differently than agreed -- the things a session
considers and a transcript alone does not surface again.

There is deliberately no parent field and no canonical root here. A node
carries a flat list of typed `relations`; whatever position it has is
whatever a lens (a later view over the graph) makes of those relations.
Adding a parent or an implied ordering would re-impose the linearity this
schema exists to remove -- do not add one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from spoke.frontmatter import Record

# `superseded` is admitted because two other tables already spoke of it:
# CLOSED_STATES and flags.STALE_NEVER both named a state the validator
# refused, and the scan quietly mapped an ADR's "superseded" onto
# "abandoned" -- which says the decision was dropped, when it was
# REPLACED. Those are different facts about the same file.
STATES = ("open", "done", "deferred", "deviated", "derived", "abandoned",
          "superseded", "blocked", "hold")

RELATIONS = (
    "touches",
    "blocked_by",
    "derived_from",
    "deviates_from",
    "depends_on",
    "governed_by",
    "serves",
    "supersedes",
)

NODE_TYPES = ("project", "product", "capability", "client", "decision", "item")

# States that require a ruling before they are accepted (see schema.validate)
# -- and that therefore count as "unruled" (a defect, not a plain closed
# item) when a node in one of them carries no ruling. This is the ONE
# definition: `cli.py`'s `ledger list` and `preamble.py`'s ranking/filtering
# both import it rather than keeping their own copy. Before this fix they
# disagreed -- preamble.py treated only "deferred" as unruled, so an
# unruled "abandoned" or "deviated" node carried no marker there and could
# even rank BELOW a closed "done" item, while cli.py's `ledger list`
# (matching the spec) already covered all three.
# `superseded` requires a ruling for the same reason a rename carries an
# alias: a decision that was replaced must say what replaced it, or every
# reader who finds it is left where the map found `memory-doctor` --
# knowing only that this is not the current name.
RULING_REQUIRED_STATES = ("deferred", "abandoned", "deviated", "superseded")

# States that are "closed" -- resolved, and not something a session needs
# surfaced on every start -- UNLESS the closing itself is unruled, in which
# case it is a defect and must still surface (see preamble.py).
CLOSED_STATES = ("done", "abandoned", "superseded")


def is_unruled(node: object) -> bool:
    """True when `node` is in a state that requires a ruling (see
    RULING_REQUIRED_STATES) and carries none. A considered decision must be
    distinguishable from a silent drop; this is the single predicate both
    `cli.py` and `preamble.py` use to decide that, so they cannot drift
    apart again the way they did before this fix."""
    return node.state in RULING_REQUIRED_STATES and not (node.ruling or "").strip()


# ── What could not be read, and WHY ──────────────────────────────────
#
# These used to be four different conditions collapsed into one
# `list[str]`, and the moment the kind left the data every surface had to
# guess it back. They guessed differently: the board page told the reader
# "N relation(s) point at a node that does not exist" and the scale page
# told the reader "N thing(s) could not be read" -- about the SAME list,
# so each was wrong half the time. The CLI's own sentence fused three of
# them with an "or", which is the tell.
#
# The kind belongs in the data.
SKIP_KINDS = ("escaped", "unreadable", "dangling", "renamed")

# The one phrasing of each kind. Shipped to every surface (see
# board.build_view) so a page renders a heading it was GIVEN and cannot
# invent a wrong one.
# Past tense throughout, deliberately: it is the one form that agrees
# with both "1 entry" and "2 entries", so the renderer needs no
# singular/plural verb table to go wrong.
SKIP_HEADINGS: dict[str, str] = {
    "escaped": "resolved outside ledger/, not read",
    "unreadable": "could not be read",
    "dangling": "pointed at a node that does not exist",
    # Not a defect. A reference written before a rename, resolved through
    # the former name the node still answers to -- reported so the reader
    # who wrote the old name learns the new one.
    # Number-neutral: these follow "1 entry" as often as "3 entries".
    "renamed": "resolved through a former name",
}


class Skipped(str):
    """One thing that was not read, and which kind of not-read it was.

    A `str` subclass, deliberately. The value IS the message this record
    replaced, character for character, so every existing caller keeps
    working untouched -- `", ".join(store.skipped)`, `"ghost" in entry`,
    `store.skipped == ["evil.md"]`, a JSON dump. The kind is purely
    additive: new information, no migration, no compatibility shim, and
    no window in which half the producers had been converted.

    That is the whole reason this could be introduced across five
    producers and four surfaces in one change. Adding a field to a
    payload is easy; adding one to a value that a dozen call sites
    already compare, join and search is not, and a subclass is the only
    shape that does it without touching any of them.

    Read `.kind` when you are deciding what to SAY about a skip; read the
    value itself when you are just showing it.
    """

    __slots__ = ("kind", "subject", "detail")

    def __new__(cls, kind: str, subject: str, detail: str | None = None):
        self = super().__new__(cls, detail if detail is not None else subject)
        self.kind = kind
        self.subject = subject
        self.detail = detail
        return self

    def __repr__(self) -> str:
        return f"Skipped({self.kind!r}, {self.subject!r}, {self.detail!r})"


def describe_skipped(items) -> list[str]:
    """One line per kind, counted -- the single renderer every text
    surface uses, so the CLI and the MCP server cannot phrase the same
    condition two ways again."""
    by_kind: dict[str, list[str]] = {}
    for item in items:
        kind = getattr(item, "kind", "unreadable")
        by_kind.setdefault(kind, []).append(str(item))
    lines = []
    for kind in SKIP_KINDS:
        hits = by_kind.get(kind)
        if not hits:
            continue
        noun = "entry" if len(hits) == 1 else "entries"
        lines.append(
            f"{len(hits)} {noun} {SKIP_HEADINGS[kind]}: {', '.join(hits)}"
        )
    return lines

def parse_iso_date(value: object) -> date | None:
    """`value` as a date, or None when it is absent or is not one.

    The value may already BE a `date`. The store always writes date
    fields quoted, so anything Spoke wrote round-trips as a string -- but
    the store is a directory of markdown files whose whole point is that
    a human can edit them, and an unquoted `updated: 2026-01-01` is what
    a human writes. YAML types that as a date, and a bare
    `date.fromisoformat(value)` raises TypeError (not ValueError) on it,
    which took the entire flag computation down once already.

    Shared by `flags._stale_cutoff` (a node's `updated`) and
    `scale.freshness` (a score's `on`) so the two cannot drift: one of
    them learned this the hard way and the other must not have to.
    """
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        pass
    # A TIMESTAMP is a date too. The probe writes its `last.at` as
    # '2026-09-13T12:00:00+00:00', which date.fromisoformat refuses --
    # so a parser used to judge how old a probe result is returned None
    # for the one format this system writes, and an age check built on
    # it would have silently never fired.
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


class LedgerError(RuntimeError):
    """Raised when a ledger operation cannot proceed."""


@dataclass(frozen=True)
class Relation:
    rel: str
    to: str


@dataclass(frozen=True)
class Node:
    name: str
    type: str
    state: str
    title: str
    body: str
    relations: tuple[Relation, ...]
    ruling: str | None
    blocked_by: tuple[str, ...]
    provenance: tuple[dict, ...]
    opened: str | None
    updated: str | None
    by: str | None
    claimed_by: str | None
    claimed_at: str | None
    # Capability scores, one per axis at most. Authored, never inferred:
    # the repo scan does not write these and no model writes these. See
    # ledger/scale.py for why every score carries a `basis` and why an
    # unscored axis is a different thing from a zero.
    scores: tuple = ()
    # What this node EXPECTS to be observably true, and what `doctor`
    # last found. Each entry: {"url", "status"?, "contains"?,
    # "fresh_within"?, "last"?: {"at", "ok", "detail"}}. An expectation
    # is question 4 of the task ledger -- "what watches it from now on"
    # -- written down where a scheduled run can read it and act. Nothing
    # here is inferred; a person states the claim, the probe reports
    # against it, and the flag `unmet` is computed at render from `last`.
    expects: tuple = ()
    # The task ledger's questions, or any list a person keeps on a node,
    # each {"text", "done"}. A node cannot be closed while an item is
    # unchecked: that is the point of a checklist.
    checklist: tuple = ()
    # Former names this node answers to.
    #
    # A shipped name does not get erased -- that is the whole reason
    # `no-renames-of-shipped-names` is a hold, and its own wording is
    # "the cost is not the rename, it is the residue". Every reference
    # written before a rename still points at the old name, and a map
    # that calls those references broken is reporting the residue as a
    # defect instead of carrying it.
    #
    # An alias resolves a relation to this node AND is reported as having
    # done so. Not silently: a reader who wrote `memory-doctor` should
    # learn that it is now `spoke`, which is the one thing a rename
    # destroys and the thing they most need.
    aliases: tuple[str, ...] = ()
    # Not part of the documented interface: fidelity plumbing used only by
    # schema.py. `_extra` carries any frontmatter keys this schema does not
    # model (e.g. a stray `verified` on a node) so validate() can see them
    # and serialise_node() does not silently drop them. `_source` carries
    # the originally-parsed Record so an unmodified node serialises back to
    # byte-identical text, the same guarantee spoke.frontmatter already
    # proves for plain memory records. Both default away so a Node built
    # directly (as ledger/store.py callers and its tests do) never needs
    # to supply them.
    _extra: dict = field(default_factory=dict, compare=False, repr=False)
    _source: Record | None = field(default=None, compare=False, repr=False)
