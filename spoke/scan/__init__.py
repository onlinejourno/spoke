"""Turn what the readers found into ledger nodes -- proposed, never asserted.

The scan is the population source that cannot rot: it re-derives, so a
map built from it is never older than its last run. A hand-authored map
is wrong the moment someone stops maintaining it, which is the failure
this whole effort exists to remove.

Two rules the rest of the module exists to keep:

**Every node carries `probe:` provenance and a date. None reads as
human-authored.** A vocabulary or a map the human has not looked at is a
guess wearing a schema, and in one real run several near-duplicate records
were identified from filenames alone and three of the five were wrong.
Structural inference over repos is far better evidence than filenames --
the lesson still holds: mark the guess.

**The engine's `type` is not the project's word.** `NODE_TYPES` is a
closed six, and every lens, flag and propagation rule is type-agnostic
only because it stays closed. So the scan proposes a LABEL from the
repo's own vocabulary (`apps/` -> "app", `crates/` -> "crate") and the
surfaces render that. A project that calls its units chapters never sees
the word "product"; `resolve_lens` never has to know it calls them
chapters.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from ..ledger import RULING_REQUIRED_STATES, Node, Relation
from .readers import (
    shared_package,
    Decision, Unit, duplicated_files, read_decisions, read_docs, read_git,
    read_units, slug, tracked_files,
)

SCANNER = "spoke-scan"

# An ADR's status word -> the ledger state that means the same thing.
# Deliberately only the four conventional words: a status this table does
# not know is REPORTED as unmapped, never guessed at. A wrong `abandoned`
# would be a silent lie about a live decision, which is worse than an
# admitted gap.
ADR_STATES: dict[str, str] = {
    "proposed": "open",
    "draft": "open",
    "accepted": "done",
    "rejected": "abandoned",
    "deprecated": "abandoned",
    "superseded": "superseded",
}


@dataclass(frozen=True)
class Proposal:
    node: Node
    label: str
    evidence: tuple[str, ...]
    # False when the label is the scan's own guess rather than a word the
    # human has accepted into the project's vocabulary. The surface must
    # render the two differently -- see the module docstring.
    accepted_label: bool = False


@dataclass(frozen=True)
class ScanResult:
    proposals: tuple[Proposal, ...]
    notes: tuple[str, ...]
    # engine type -> the word the scan proposes for it, for the labels
    # that are NOT yet accepted.
    proposed_vocabulary: dict[str, str]


def body_digest(body: str) -> str:
    """A fingerprint of exactly what the scan wrote."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _prov(today: date, evidence: str, body: str) -> tuple[dict, ...]:
    """Provenance carrying a digest of the body it accompanies.

    The digest is what makes the re-run rule self-verifying. Without it,
    "has a human edited this node?" could only be answered from a
    `human:` provenance entry -- and a human editing the markdown
    directly (the whole point of a markdown store) would not add one, so
    the next scan would silently overwrite their work. With it, a body
    that no longer hashes to what the scan recorded IS the evidence of a
    human edit, whether or not anyone remembered to say so.
    """
    return (
        {
            "by": f"probe:{SCANNER}",
            "on": today.isoformat(),
            "from": evidence,
            "digest": body_digest(body),
        },
    )


def ownership(existing) -> str:
    """'owned' | 'diverged' -- may this scan overwrite `existing`?

    'owned': every provenance entry is this scanner's AND the body still
    hashes to what that scanner recorded. Safe to re-derive.
    'diverged': anything else -- a human wrote it, a human edited it, or
    it predates the digest. Reported, never overwritten.
    """
    prov = existing.provenance or ()
    if not prov:
        return "diverged"
    if not all(str(e.get("by", "")) == f"probe:{SCANNER}" for e in prov):
        return "diverged"
    recorded = next((e.get("digest") for e in reversed(prov) if e.get("digest")), None)
    if not recorded or recorded != body_digest(existing.body):
        return "diverged"
    return "owned"


@dataclass
class Applied:
    """What applying a scan did, or would do. `lines` are one per node
    in the same shape the CLI prints; `refused` names the writes the
    gate rejected, with the reasons."""
    written: int = 0
    unchanged: int = 0
    diverged: int = 0
    refused: int = 0
    withdrawn: int = 0
    lines: list = field(default_factory=list)

    @property
    def counts(self) -> dict:
        return {"written": self.written, "unchanged": self.unchanged,
                "diverged": self.diverged, "refused": self.refused,
                "withdrawn": self.withdrawn}


_STAMP = re.compile(r"^(Derived by \S+ on )\d{4}-\d{2}-\d{2}", re.M)


def _unstamped(body: str) -> str:
    """The body with its 'Derived ... on <date>' stamp blanked, so two
    scans of an unchanged repo on different days compare equal. Before
    this, a scan on a new day rewrote every node it owned -- 143 of 218
    on the estate -- and the rewrite dropped whatever the scan does not
    derive: the expectations and checklists a person had put there."""
    return _STAMP.sub(r"\1<date>", body)


def _first_seen(existing) -> str | None:
    """When the scan first wrote this node: the earliest provenance date."""
    dates = sorted(str(e.get("on")) for e in (existing.provenance or ()) if e.get("on"))
    return dates[0] if dates else None


def _carry(node: Node, existing, today: date) -> Node:
    """What a re-scan keeps from the node it is about to overwrite.

    The scan derives type, state, title, body and relations from the
    repo. Everything else on the node was put there by a person or a
    probe -- expectations, checklist, aliases, a claim -- and a re-derive
    is not a reason to lose it. `opened` is the decision's own date when
    the ADR states one; otherwise it sticks to the day the node was first
    seen, so a timeline does not slide with every scan."""
    opened = node.opened or (existing.opened if existing else None) \
        or (_first_seen(existing) if existing else None) or today.isoformat()
    if existing is None:
        return replace(node, opened=opened)
    return replace(
        node, opened=opened,
        # Scores are the plainest case: a person wrote each one, with a
        # basis and a note, and the first re-scan after this rule was
        # missing dropped sixteen of them in one morning.
        scores=existing.scores,
        expects=existing.expects, checklist=existing.checklist, aliases=existing.aliases,
        claimed_by=existing.claimed_by, claimed_at=existing.claimed_at,
        updated=node.updated or existing.updated,
    )


def _touching(hits: list[tuple[Path, str]], units_by_repo: dict) -> tuple[Relation, ...]:
    """The units a shared package touches: the PART whose path holds each
    copy, or every part of the repo when the copy sits outside all of
    them. Never the whole -- it is one `serves` hop from each part.

    Before this, a copy inside one Watch was recorded as touching every
    Watch in the repo, so a package regwatch did not hold was drawn as if
    it did, and the absence the capability lens exists to report was
    reported nowhere."""
    out: list[Relation] = []
    seen: set[str] = set()
    for repo, rel in hits:
        units = units_by_repo.get(repo, [])
        wholes = {w.part_of for w in units if w.part_of}
        parts = [u for u in units if u.name not in wholes]
        inside = [u for u in parts if u.path != "." and rel.startswith(u.path.rstrip("/") + "/")]
        for u in inside or parts:
            if u.name not in seen:
                seen.add(u.name)
                out.append(Relation("touches", u.name))
    return tuple(out)


def apply(result: ScanResult, store, axes: tuple, scale_max: int, today: date,
          write: bool) -> Applied:
    """Apply a scan's proposals to `store` through the gate, then withdraw
    what the scan owns and no longer proposes.

    ONE loop. It was three -- the CLI, the server's /api/scan and the demo
    each had a copy -- and the divergence rule ("a node a human edited is
    never overwritten") is the one rule here a copy must not drift on.
    `write=False` reports what would happen and touches nothing.
    """
    out = Applied()
    for prop in sorted(result.proposals, key=lambda p: p.node.name):
        node = prop.node
        try:
            existing = store.read(node.name)
        except FileNotFoundError:
            existing = None
        except Exception as e:
            out.diverged += 1
            out.lines.append(f"~ {node.name}: UNREADABLE -- {e}; left alone")
            continue
        if existing is not None and ownership(existing) == "diverged":
            out.diverged += 1
            out.lines.append(f"~ {node.name}: DIVERGED -- edited since the scan wrote it, "
                             "or not written by it. Left alone.")
            continue
        node = _carry(node, existing, today)
        if existing is not None and _unstamped(existing.body) == _unstamped(node.body) \
                and existing.state == node.state and existing.relations == node.relations \
                and existing.opened == node.opened:
            out.unchanged += 1
            continue
        if not write:
            out.written += 1
            verb = "would update" if existing is not None else "would create"
            out.lines.append(f"+ {node.name}: {verb} ({node.type}, {node.state})")
            continue
        res = store.write(node, axes, scale_max)
        if not res.ok:
            out.refused += 1
            out.lines.append(f"! {node.name}: refused - {'; '.join(res.reasons)}")
            continue
        out.written += 1
        out.lines.append(f"+ {node.name}: written ({node.type}, {node.state})")

    proposed = {p.node.name for p in result.proposals}
    for existing in sorted(store.list_nodes(), key=lambda n: n.name):
        if existing.name in proposed or existing.state == "abandoned":
            continue
        if ownership(existing) != "owned":
            continue
        if not write:
            out.withdrawn += 1
            out.lines.append(f"- {existing.name}: would withdraw ({existing.type}, {existing.state})")
            continue
        res = store.write(withdrawn(existing, today), axes, scale_max)
        if res.ok:
            out.withdrawn += 1
            out.lines.append(f"- {existing.name}: withdrawn ({existing.type})")
        else:
            out.lines.append(f"! {existing.name}: could not withdraw - {'; '.join(res.reasons)}")
    return out


def withdrawn(existing, today: date):
    """`existing` as the scan withdraws it: abandoned, with a ruling that
    says the scan stopped deriving it and when.

    A scan that writes what it finds and never retracts what it stops
    finding leaves the map asserting things no repo says any more. That
    is the phantom-unit problem generalised: fix a heuristic, and every
    node the old heuristic produced lingers as a decision somebody has
    to notice. Only nodes the scan OWNS (its provenance, unedited body)
    are withdrawn; anything a human touched is theirs.

    The body is left as it was and the new provenance entry digests it,
    so the node stays owned -- a later scan that derives it again can
    revive it without a human in the loop.
    """
    from dataclasses import replace

    ruling = (f"Withdrawn by {SCANNER} on {today.isoformat()}: no longer "
              "derived from any repo in this project.")
    return replace(
        existing, state="abandoned", ruling=ruling,
        provenance=tuple(existing.provenance or ()) + _prov(today, "withdrawal", existing.body),
    )


def _label(engine_type: str, proposed: str, vocabulary: dict[str, str]) -> tuple[str, bool]:
    accepted = vocabulary.get(engine_type)
    if accepted:
        return accepted, True
    return proposed, False


def scan_repos(
    repos: list[Path], today: date, vocabulary: dict[str, str] | None = None
) -> ScanResult:
    """Read every repo and propose the map it implies.

    Deterministic by construction: repos are processed in sorted order,
    every listing the readers return is already sorted, and nothing here
    iterates a set. A map that changes when nothing did would defeat the
    only property that makes a derived map worth having.
    """
    vocabulary = dict(vocabulary or {})
    repos = sorted((Path(r) for r in repos), key=str)
    notes: list[str] = []
    proposals: list[Proposal] = []
    proposed_vocab: dict[str, str] = {}

    files_by_repo: dict[Path, list[str]] = {}
    units_by_repo: dict[Path, list[Unit]] = {}
    decisions_by_repo: dict[Path, list[Decision]] = {}

    for repo in repos:
        if not repo.exists():
            notes.append(f"{repo}: does not exist -- nothing scanned from it")
            files_by_repo[repo] = []
            units_by_repo[repo] = []
            decisions_by_repo[repo] = []
            continue
        files, repo_notes = tracked_files(repo)
        notes.extend(repo_notes)
        files_by_repo[repo] = files
        units_by_repo[repo] = read_units(repo, files)
        decisions_by_repo[repo] = read_decisions(repo, files)
        if not units_by_repo[repo]:
            notes.append(
                f"{repo}: no deployable unit found -- no layout marker "
                f"(apps/, packages/, ...) and no root manifest"
            )

    for repo in repos:
        git = read_git(repo) if repo.exists() else {"last_commit": None, "remote": None}
        for unit in units_by_repo[repo]:
            label, accepted = _label("product", unit.label, vocabulary)
            if not accepted:
                proposed_vocab.setdefault("product", label)
            evidence = f"{repo}/{unit.path}" + (f" ({unit.manifest})" if unit.manifest else "")
            _unit_body = (
                f"Derived by {SCANNER} on {today.isoformat()} from {evidence}.\n\n"
                f"Remote: {git.get('remote') or 'unknown'}.\n"
            )
            if unit.manifest is None and unit.path != ".":
                # A directory under a layout marker with no manifest: a
                # README and nothing else, usually. It is listed because
                # the repo lists it, and it says here what was found, so
                # "declared" does not read as "built".
                _unit_body += "\nNo manifest was found here: a directory, not a package.\n"
            if unit.part_of:
                _unit_body += (
                    f"\nOne part of {unit.part_of}, which holds several parts "
                    "and is named by none of them.\n"
                )
            # `serves` is the product lens's own spine, so a part reaches
            # its whole on the map without a second kind of edge.
            unit_relations = (
                (Relation(rel="serves", to=unit.part_of),) if unit.part_of else ()
            )
            proposals.append(Proposal(
                node=Node(
                    name=unit.name, type="product", state="open",
                    title=unit.name,
                    body=_unit_body,
                    relations=unit_relations, ruling=None, blocked_by=(),
                    provenance=_prov(today, evidence, _unit_body),
                    opened=None,
                    # The repo's last-commit date feeds `stale` without
                    # anybody maintaining a field.
                    updated=git.get("last_commit"),
                    by=None, claimed_by=None, claimed_at=None,
                ),
                label=label, evidence=(evidence,), accepted_label=accepted,
            ))

        for d in decisions_by_repo[repo]:
            state = ADR_STATES.get(d.status or "", None)
            ruling = None
            if state is None:
                state = "open"
                if d.status:
                    notes.append(
                        f"{repo}/{d.path}: status {d.status!r} is not one of "
                        f"{', '.join(sorted(ADR_STATES))} -- recorded as 'open' "
                        "rather than guessed at"
                    )
            elif state in RULING_REQUIRED_STATES:
                # These states require a ruling or LedgerStore.write()
                # refuses -- the scan must not produce output its own
                # gate rejects. For `superseded` the ADR itself names
                # what replaced it; the ruling points the reader there.
                ruling = f"ADR status: {d.status} ({d.path})"
            label, accepted = _label("decision", "decision", vocabulary)
            evidence = f"{repo}/{d.path}"
            related = tuple(
                Relation("touches", u.name) for u in units_by_repo[repo]
            )
            _dec_body = (
                f"Derived by {SCANNER} on {today.isoformat()} from {evidence}.\n\n"
                f"ADR status: {d.status or 'not stated'}.\n"
            )
            proposals.append(Proposal(
                node=Node(
                    name=d.name, type="decision", state=state, title=d.title,
                    body=_dec_body,
                    relations=related, ruling=ruling, blocked_by=(),
                    provenance=_prov(today, evidence, _dec_body),
                    # The ADR's own date when it states one; otherwise
                    # `apply` fills in first-seen. Never the read date.
                    opened=d.date, updated=None, by=None,
                    claimed_by=None, claimed_at=None,
                ),
                label=label, evidence=(evidence,), accepted_label=accepted,
            ))

        docs = read_docs(repo, files_by_repo[repo]) if repo.exists() else []
        if docs:
            label, accepted = _label("item", "documentation", vocabulary)
            if not accepted:
                proposed_vocab.setdefault("item", label)
            evidence = f"{repo}: {len(docs)} doc file(s), e.g. {docs[0]}"
            _docs_body = (
                f"Derived by {SCANNER} on {today.isoformat()}.\n\n"
                + "\n".join(f"- {d}" for d in docs[:50])
                + ("\n- ...\n" if len(docs) > 50 else "\n")
            )
            proposals.append(Proposal(
                node=Node(
                    name=slug(repo.name, "docs"), type="item", state="open",
                    title=f"Documentation in {repo.name}",
                    body=_docs_body,
                    relations=tuple(
                        Relation("touches", u.name) for u in units_by_repo[repo]
                    ),
                    ruling=None, blocked_by=(), provenance=_prov(today, evidence, _docs_body),
                    opened=None, updated=None, by=None,
                    claimed_by=None, claimed_at=None,
                ),
                label=label, evidence=(evidence,), accepted_label=accepted,
            ))

    existing = {p.node.name for p in proposals}
    repo_files = {r: files_by_repo.get(r, []) for r in repos}
    # One capability per shared PACKAGE, not per file: the hits of every
    # duplicated file are grouped under the package they belong to.
    groups: dict[str, list[tuple[Path, str]]] = {}
    for _digest, hits in sorted(duplicated_files(repo_files).items()):
        groups.setdefault(shared_package(hits, repo_files), []).extend(hits)
    for package, hits in sorted(groups.items()):
        name = slug("shared", package.rsplit("/", 1)[-1])
        if name in existing:
            continue
        existing.add(name)
        label, accepted = _label("capability", "shared code", vocabulary)
        if not accepted:
            proposed_vocab.setdefault("capability", label)
        touching = _touching(hits, units_by_repo)
        hits = sorted(set(hits), key=lambda h: (str(h[0]), h[1]))
        files = sorted({rel.rsplit("/", 1)[-1] for _, rel in hits})
        evidence = "; ".join(f"{repo}/{rel}" for repo, rel in hits)
        _shared_body = (
            f"Derived by {SCANNER} on {today.isoformat()}: identical content in "
            f"more than one repo.\n\n"
            + "\n".join(f"- {repo}/{rel}" for repo, rel in hits) + "\n"
        )
        proposals.append(Proposal(
            node=Node(
                name=name, type="capability", state="open",
                title=f"Shared: {package}" + (f" ({len(files)} files)" if len(files) > 1 else ""),
                body=_shared_body,
                relations=touching, ruling=None, blocked_by=(),
                provenance=_prov(today, evidence, _shared_body),
                opened=None, updated=None, by=None, claimed_by=None, claimed_at=None,
            ),
            label=label, evidence=tuple(f"{repo}/{rel}" for repo, rel in hits),
            accepted_label=accepted,
        ))

    return ScanResult(
        proposals=tuple(proposals),
        notes=tuple(notes),
        proposed_vocabulary=proposed_vocab,
    )
