"""Parse, serialise and validate ledger nodes.

Built on `spoke.frontmatter`'s `parse`/`serialise` -- the byte-identical
round-trip already proven for 216 real memory records has to hold for
ledger nodes too, so this module does not open a second YAML path.
"""
from __future__ import annotations

import re

from spoke.frontmatter import Record, parse, serialise

from . import (
    CLOSED_STATES,
    LedgerError, NODE_TYPES, RELATIONS, RULING_REQUIRED_STATES, STATES,
    Node, Relation, is_unruled,
)
from .scale import DEFAULT_MAX_VALUE, Observation, Score, validate_scores

_KNOWN_META_KEYS = {
    "name", "type", "state", "title",
    "relations", "ruling", "blocked_by", "provenance",
    "opened", "updated", "by", "claimed_by", "claimed_at",
    "scores", "aliases", "expects", "checklist",
}

_CLIENT_TITLE_RE = re.compile(r"^client-[a-z0-9]+$")

# A node's name becomes a filename (`<name>.md`) under `ledger/` -- it must
# be a plain slug with no path separators and no leading dot, so it can
# never point outside that directory. See LedgerStore._node_path, which
# also re-checks this independently of validate() (belt and braces: this
# store is shared, and validation alone must not be the only thing
# standing between a node name and the memory store's real files).
SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def parse_node(text: str) -> Node:
    """Parse `text` into a `Node`.

    FINDING 2: `frontmatter.parse` already flags unparseable YAML via
    `Record.unparsed` -- but this function used to ignore that flag and
    build a Node from the resulting empty `meta` anyway, so a corrupt
    node silently became an all-blank Node (rendering as a bare `: `
    line, with nothing reported). Separately, a malformed `relations`
    entry (e.g. a bare string where a `{rel, to}` mapping belongs) raised
    an uncaught TypeError/KeyError that killed the entire listing this
    node happened to be part of. Both are now raised here, by name, as a
    single typed `LedgerError` a caller can catch per-node (see
    LedgerStore.list_nodes) instead of either going invisible or taking
    down every other node with it.
    """
    record = parse(text)
    if record.unparsed:
        raise LedgerError(f"node has unparseable frontmatter YAML: {text[:80]!r}")
    meta = record.meta or {}

    raw_relations = meta.get("relations") or ()
    for r in raw_relations:
        if not isinstance(r, dict) or "rel" not in r or "to" not in r:
            raise LedgerError(
                f"node {meta.get('name', '')!r} has a malformed relations entry "
                f"{r!r} -- each relation must be a mapping with 'rel' and 'to' keys"
            )
    relations = tuple(Relation(rel=r["rel"], to=r["to"]) for r in raw_relations)
    blocked_by = tuple(meta.get("blocked_by") or ())
    provenance = tuple(meta.get("provenance") or ())
    raw_scores = meta.get("scores") or []
    if not isinstance(raw_scores, list):
        raise LedgerError(
            "'scores' must be a list of {axis, value, basis} entries, got "
            f"{type(raw_scores).__name__}"
        )
    aliases = tuple(str(a) for a in (meta.get("aliases") or ()))
    raw_expects = meta.get("expects") or []
    if not isinstance(raw_expects, list) or not all(isinstance(e, dict) for e in raw_expects):
        raise LedgerError(
            f"node {meta.get('name', '')!r}: 'expects' must be a list of "
            "{url, status?, contains?, fresh_within?} entries"
        )
    expects = tuple({str(k): v for k, v in e.items()} for e in raw_expects)
    raw_check = meta.get("checklist") or []
    if not isinstance(raw_check, list):
        raise LedgerError(f"node {meta.get('name', '')!r}: 'checklist' must be a list")
    checklist = []
    for item in raw_check:
        if isinstance(item, dict) and "text" in item:
            # `note` is the ANSWER to the item, and the point of asking:
            # the four questions want what was observed and what would
            # fail silently. Dropping it here and in serialise_node left
            # `tick --note` printing success and storing nothing -- a
            # ticked box with no evidence behind it, which is the shape
            # of check this ledger exists to replace.
            entry = {"text": str(item["text"]), "done": bool(item.get("done", False))}
            note = str(item.get("note") or "").strip()
            if note:
                # Absent, not empty: a note nobody wrote is not a note
                # that says nothing.
                entry["note"] = note
            checklist.append(entry)
        elif isinstance(item, str):
            # "[ ] text" / "[x] text", the way a person types one
            done = item.strip().lower().startswith("[x]")
            text = item.strip()[3:].strip() if item.strip()[:1] == "[" else item.strip()
            checklist.append({"text": text, "done": done})
        else:
            raise LedgerError(f"node {meta.get('name', '')!r}: malformed checklist item {item!r}")
    checklist = tuple(checklist)
    scores = tuple(
        Score(
            axis=str(s.get("axis", "")),
            # Left as-is, NOT coerced: a non-integer must reach
            # validate() and be refused there with a message naming the
            # axis, rather than being quietly turned into a number.
            value=s.get("value"),
            basis=str(s.get("basis", "")),
            on=str(s["on"]) if s.get("on") is not None else None,
            note=s.get("note"),
            by=s.get("by"),
            previously=_observations(s.get("previously")),
        )
        for s in raw_scores
        if isinstance(s, dict)
    )
    extra = {k: v for k, v in meta.items() if k not in _KNOWN_META_KEYS}

    return Node(
        name=str(meta.get("name", "")),
        type=str(meta.get("type", "")),
        state=str(meta.get("state", "")),
        title=str(meta.get("title", "")),
        body=record.body,
        relations=relations,
        ruling=meta.get("ruling"),
        blocked_by=blocked_by,
        provenance=provenance,
        opened=meta.get("opened"),
        updated=meta.get("updated"),
        by=meta.get("by"),
        claimed_by=meta.get("claimed_by"),
        claimed_at=meta.get("claimed_at"),
        scores=scores,
        aliases=aliases,
        expects=expects,
        checklist=tuple(checklist),
        _extra=extra,
        _source=record,
    )


def _observations(raw: object) -> tuple[Observation, ...]:
    """A score's superseded readings, left uncoerced the same way the
    current score's `value` is: a bad one must reach validate() and be
    named there, not be quietly turned into a number."""
    if not isinstance(raw, list):
        return ()
    return tuple(
        Observation(
            value=o.get("value"),
            basis=str(o.get("basis", "")),
            on=str(o["on"]) if o.get("on") is not None else None,
            note=o.get("note"),
            by=o.get("by"),
        )
        for o in raw
        if isinstance(o, dict)
    )


def _meta_from_node(node: Node) -> dict:
    meta = dict(node._extra)
    meta["name"] = node.name
    meta["type"] = node.type
    meta["state"] = node.state
    meta["title"] = node.title
    if node.relations:
        meta["relations"] = [{"rel": r.rel, "to": r.to} for r in node.relations]
    if node.ruling is not None:
        meta["ruling"] = node.ruling
    if node.blocked_by:
        meta["blocked_by"] = list(node.blocked_by)
    if node.provenance:
        meta["provenance"] = [dict(p) for p in node.provenance]
    if node.opened is not None:
        meta["opened"] = node.opened
    if node.updated is not None:
        meta["updated"] = node.updated
    if node.by is not None:
        meta["by"] = node.by
    if node.claimed_by is not None:
        meta["claimed_by"] = node.claimed_by
    if node.claimed_at is not None:
        meta["claimed_at"] = node.claimed_at
    if node.aliases:
        meta["aliases"] = list(node.aliases)
    if node.expects:
        meta["expects"] = [dict(e) for e in node.expects]
    if node.checklist:
        meta["checklist"] = [
            {"text": c["text"], "done": bool(c["done"]),
             **({"note": c["note"]} if str(c.get("note") or "").strip() else {})}
            for c in node.checklist
        ]
    if node.scores:
        meta["scores"] = [
            {
                k: v for k, v in (
                    ("axis", s.axis), ("value", s.value), ("basis", s.basis),
                    ("on", s.on), ("note", s.note), ("by", s.by),
                    ("previously", [
                        {
                            k2: v2 for k2, v2 in (
                                ("value", o.value), ("basis", o.basis),
                                ("on", o.on), ("note", o.note), ("by", o.by),
                            ) if v2 is not None
                        }
                        for o in s.previously
                    ] or None),
                ) if v is not None
            }
            for s in node.scores
        ]
    return meta


def serialise_node(node: Node) -> str:
    meta = _meta_from_node(node)
    raw_meta = node._source._raw_meta if node._source is not None else ""
    record = Record(meta=meta, body=node.body, _raw_meta=raw_meta)
    return serialise(record)


_DURATION_RE = re.compile(r"^(\d+)([hd])$")
_EXPECT_KEYS = {"url", "status", "contains", "fresh_within", "dated_by", "last", "note",
                "path", "worktrees_max"}


def expect_subject(e: dict) -> str:
    """What an expectation is about, for a label: the URL it probes, or
    the local path it inspects. One function, so the flag, the finding,
    the show command and the dedupe key all name it the same way."""
    return str(e.get("url") or e.get("path") or "")


def parse_duration_hours(raw) -> int | None:
    """'36h' -> 36, '2d' -> 48; None when it is not one of those."""
    m = _DURATION_RE.match(str(raw).strip()) if raw is not None else None
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return n * (24 if unit == "d" else 1)


def validate_expects(expects: tuple) -> list[str]:
    """Shape, not truth: the probe decides truth. Refused here is a claim
    the probe could not even attempt."""
    reasons: list[str] = []
    for i, e in enumerate(expects, 1):
        unknown = set(e) - _EXPECT_KEYS
        if unknown:
            reasons.append(f"expects[{i}]: unknown key(s) {', '.join(sorted(unknown))}")
        if "path" in e:
            # A local expectation: something inspected on this machine,
            # not fetched. It has its own clauses and none of the URL's.
            if "url" in e:
                reasons.append(f"expects[{i}]: 'url' or 'path' -- one or the other, not both")
            path = str(e.get("path", "")).strip()
            if not path.startswith("/"):
                reasons.append(f"expects[{i}]: 'path' must be absolute, got {path!r}")
            if "worktrees_max" not in e:
                reasons.append(f"expects[{i}]: say what is expected of the path -- 'worktrees_max'")
            elif not (isinstance(e["worktrees_max"], int) and not isinstance(e["worktrees_max"], bool)
                      and e["worktrees_max"] >= 0):
                reasons.append(f"expects[{i}]: 'worktrees_max' must be a whole number")
            for k in ("status", "contains", "fresh_within", "dated_by"):
                if k in e:
                    reasons.append(f"expects[{i}]: {k!r} is a URL clause; a path is not fetched")
            continue
        if "worktrees_max" in e:
            reasons.append(f"expects[{i}]: 'worktrees_max' needs a 'path' to count in")
        url = str(e.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            reasons.append(f"expects[{i}]: 'url' must be an http(s) URL, got {url!r}")
        if "status" in e and not (isinstance(e["status"], int) and 100 <= e["status"] <= 599):
            reasons.append(f"expects[{i}]: 'status' must be an HTTP status code")
        if "contains" in e and not str(e["contains"]).strip():
            reasons.append(f"expects[{i}]: 'contains' must be non-empty text")
        if "fresh_within" in e and parse_duration_hours(e["fresh_within"]) is None:
            reasons.append(f"expects[{i}]: 'fresh_within' must be like '36h' or '2d'")
        if "dated_by" in e and "fresh_within" not in e:
            reasons.append(f"expects[{i}]: 'dated_by' says where the date is; 'fresh_within' says how fresh -- both are needed")
        if "dated_by" in e and not str(e["dated_by"]).strip():
            reasons.append(f"expects[{i}]: 'dated_by' must be the text the date follows")
        if not any(k in e for k in ("status", "contains", "fresh_within")):
            reasons.append(f"expects[{i}]: say what is expected -- a status, text it contains, "
                           "or how fresh it must be")
    return reasons


def validate(node: Node, axes: tuple = (), max_value: int = DEFAULT_MAX_VALUE) -> list[str]:
    """Every reason `node` cannot be written, all of them at once.

    `axes` is the project's declared capability axes. It defaults to
    empty, which means "this caller has no axis list to check against" --
    scores are then checked for shape but not for membership. A caller
    that HAS the list must pass it, or a score on an axis nobody declared
    would be stored and render nowhere.
    """
    reasons: list[str] = []

    if (
        not SAFE_NAME_RE.match(node.name or "")
        or "/" in node.name
        or "\\" in node.name
        or ".." in node.name
    ):
        reasons.append(
            f"name {node.name!r} is not a safe slug -- must match "
            f"{SAFE_NAME_RE.pattern!r}, no leading dot, and no '/', '\\', "
            "or '..' anywhere (a name becomes ledger/<name>.md)"
        )

    if node.state not in STATES:
        reasons.append(f"unknown state {node.state!r}: must be one of {STATES}")
    if node.type not in NODE_TYPES:
        reasons.append(f"unknown type {node.type!r}: must be one of {NODE_TYPES}")

    # `is_unruled`, not a re-spelled tuple. ledger/__init__.py calls
    # RULING_REQUIRED_STATES "the ONE definition" and lists cli.py and
    # preamble.py as its readers -- but the GATE, the one place that
    # decides whether a write is refused, kept its own literal copy. Add a
    # state to the tuple and every display would mark it unruled while
    # validate() accepted it with no ruling: the same drift the constant
    # was created to end, mirrored one layer down.
    if is_unruled(node):
        reasons.append(
            f"state {node.state!r} requires a non-empty ruling -- "
            "a considered decision must be distinguishable from a silent drop"
        )

    if node.state == "blocked" and not any(b.strip() for b in node.blocked_by):
        reasons.append("state 'blocked' requires a non-empty blocked_by")

    if node.state == "hold" and "verified" in node._extra:
        reasons.append(
            "a hold may not carry a verified date -- a hold is not resolved "
            "by time, only lifted by a named person"
        )

    for rel in node.relations:
        if rel.rel not in RELATIONS:
            reasons.append(f"unknown relation type {rel.rel!r}: must be one of {RELATIONS}")
        # FINDING 7: `validate()` checked the relation TYPE but never that
        # `to` was actually non-empty, so `--rel touches:` (CLI splits on
        # the first ':' and gets an empty string after it) parsed cleanly
        # into a Relation pointing at nothing, and was accepted.
        if not (rel.to or "").strip():
            reasons.append(
                f"relation {rel.rel!r} has an empty 'to' -- a relation must "
                "name the node it points at"
            )

    for alias in node.aliases:
        if not SAFE_NAME_RE.match(alias or ""):
            reasons.append(
                f"alias {alias!r} is not a safe slug -- an alias is a former "
                f"node NAME and must match {SAFE_NAME_RE.pattern!r}"
            )
        if alias == node.name:
            reasons.append(
                f"alias {alias!r} is this node's own name -- an alias records "
                "a name it USED to have"
            )

    reasons.extend(validate_scores(node.scores, tuple(axes), max_value))
    reasons.extend(validate_expects(node.expects))
    # A checklist is a promise to do each thing before closing. Closing
    # over an unchecked item is exactly what a checklist exists to stop.
    if node.state in CLOSED_STATES:
        left = [c["text"] for c in node.checklist if not c.get("done")]
        if left:
            reasons.append(
                f"state {node.state!r} with {len(left)} unchecked checklist item(s): "
                + "; ".join(left[:3])
            )

    if node.type == "client" and not _CLIENT_TITLE_RE.match(node.title or ""):
        reasons.append(
            "a client node's title must be an opaque id matching "
            "^client-[a-z0-9]+$ -- client nodes carry an identifier and a "
            "state, never their content"
        )

    return reasons
