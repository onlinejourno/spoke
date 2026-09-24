"""Flags computed over the ledger graph, at render time, never stored.

A stored roll-up is a cache, and a stale cache that looks authoritative
is the exact failure this whole project exists to remove -- so nothing
here ever writes back onto a `Node`. `compute_flags` is a pure function
of `(graph, lens, today, stale_days)`; call it again and it recomputes
from scratch every time.

Two propagation regimes, matching spec Sec.5:

- **Universal flags** (`blocked`, `held`, `deviates`) are raised by a
  node's own state (or, for `deviates`, its `governed_by` relation) and
  then spread along ONE specific relation type each -- `blocked_by`,
  `touches`+`serves`, and `governed_by` respectively -- via a fixpoint
  walk over the WHOLE graph, independent of which lens is active. This
  is what "in every lens" means: the flag is not confined to the current
  lens's own hub relation, so a hold on a decision still reaches every
  product and client it touches even when you're looking through the
  decision lens.
- **Lens-scoped flags** (`unruled`, `stale`) are properties of a single
  node, attributed to a hub only when that node is within the ACTIVE
  lens's own resolved neighbourhood (`graph.resolve_lens`) -- "along the
  active hub". They do not propagate along any dedicated relation of
  their own.

Every propagation is a fixpoint walk with a per-origin visited set, never
a recursive descent -- `A blocked_by B` while `B derived_from A` is a
real, legal shape and must terminate, not hang.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..clock import BASELINE, PROBE_WINDOW_DAYS, RE_VERIFY, URGENT, Clock
from . import CLOSED_STATES, is_unruled
from .graph import Graph, Lens, resolve_lens
from .schema import expect_subject, parse_duration_hours


# Every flag kind there is. Produced here, so this is where the list
# lives -- board.py used to re-list all six in SEVERITY and cli.py kept a
# seventh copy as a literal set to validate `--flag`. A new kind was
# three different wrong answers: accepted by one filter, refused by
# another, and coloured "warn" by a dict `.get` default.
FLAG_KINDS = ("absent", "blocked", "deviates", "held", "stale", "unchecked", "unmet", "unruled")


@dataclass(frozen=True)
class Flag:
    kind: str
    origin: str
    detail: str


def sort_flags(flags) -> tuple[Flag, ...]:
    """The one ordering. Written out three times before this -- here, in
    board.py and in matrix.py -- so two surfaces could have listed the
    same flags in different orders and nothing would have said so."""
    return tuple(sorted(flags, key=lambda f: (f.kind, f.origin, f.detail)))


def _touches_neighbors(graph: Graph, name: str) -> set[str]:
    """Nodes connected to `name` by a `touches` relation, either
    direction -- `touches` is navigable both ways like every relation
    (see graph.py); a product need not be the one that declared the
    edge to be considered touched by it.
    """
    out = {rel.to for rel in graph.out(name) if rel.rel == "touches"}
    inbound = {src for src, rel in graph.inbound(name) if rel.rel == "touches"}
    return out | inbound


def _direct_reach(
    graph: Graph, seeds: dict[str, Flag], rel_types: set[str]
) -> dict[str, set[Flag]]:
    """Each seed's flag on the seed itself and on its immediate
    neighbours along `rel_types` -- one hop, never onward.

    For a flag that names a SCOPE rather than a chain. See the comment at
    the `held` call site for why that distinction matters and what
    happened when it was missing.
    """
    out: dict[str, set[Flag]] = {}
    for origin, flag in seeds.items():
        out.setdefault(origin, set()).add(flag)
        for rel in graph.out(origin):
            if rel.rel in rel_types:
                out.setdefault(rel.to, set()).add(flag)
        for src, rel in graph.inbound(origin):
            if rel.rel in rel_types:
                out.setdefault(src, set()).add(flag)
    return out


def _propagate(graph: Graph, seeds: dict[str, Flag], rel_types: set[str]) -> dict[str, set[Flag]]:
    """Spread every seed's flag along `rel_types` edges, in both
    directions, until no new (node, origin) pair is reached.

    ONE multi-source BFS, not one BFS per seed: every seed starts in the
    shared frontier together, and each node accumulates the set of origin
    names that have reached it (`visited`), never revisiting an origin it
    already recorded. A large ledger has a few hub nodes (a `product` or
    `capability` touched by hundreds of items) that a huge share of seeds
    all reach -- the old per-seed loop walked the whole graph again for
    every one of those seeds (measured at 3.8s on a 5,000-node graph with
    many `hold` seeds); this walks the graph's edges once per BFS round
    regardless of how many seeds converge on a hub, which is what turns
    O(seeds x N) into O(N + E). Behaviour is unchanged: the same nodes end
    up with the same `Flag`s (Flag.origin included), just computed without
    the redundant re-walking.

    Termination: `visited[node]` only ever grows (a subset of `seeds`),
    so the fixpoint is reached in at most `len(seeds)` rounds even across
    a cycle -- `A blocked_by B` while `B derived_from A` is legal and must
    not hang, and it doesn't: a node stops re-emitting once every origin
    that can reach it already has.
    """
    if not seeds:
        return {}
    visited: dict[str, set[str]] = {}
    frontier: dict[str, set[str]] = {}
    for origin_name in seeds:
        visited.setdefault(origin_name, set()).add(origin_name)
        frontier.setdefault(origin_name, set()).add(origin_name)

    while frontier:
        next_frontier: dict[str, set[str]] = {}
        for cur, new_origins in frontier.items():
            neighbors = {rel.to for rel in graph.out(cur) if rel.rel in rel_types}
            neighbors |= {src for src, rel in graph.inbound(cur) if rel.rel in rel_types}
            for nbr in neighbors:
                have = visited.setdefault(nbr, set())
                fresh = new_origins - have
                if fresh:
                    have |= fresh
                    next_frontier.setdefault(nbr, set()).update(fresh)
        frontier = next_frontier

    return {node: {seeds[o] for o in origins} for node, origins in visited.items()}


# Per-state staleness, spec Sec.5's threshold table. A `hold` is never
# resolved by time -- only a named person lifts one -- and `abandoned`/
# `superseded` are closed on purpose, so time passing tells you nothing:
# both are excluded from staleness entirely, not just given a long
# threshold. ("superseded" is not currently a member of ledger.STATES --
# a node's state can never actually be it today -- but it is kept here so
# this table stays a faithful copy of the spec's and does not silently
# stop matching it if STATES grows to include it.)
STALE_NEVER = frozenset({"hold", "abandoned", "superseded"})

# Every other state's threshold as a multiplier of the single `stale_days`
# config value (spec: "the numbers are provisional... set from the
# distinctions above" -- there is no separate per-state number in Config
# yet, so this multiplier table is the explicit, documented mapping the
# brief allows in place of one). `deviated` and `derived` are not named in
# the spec's table; until it says otherwise they get the same (1.0,
# ordinary open work) treatment as `open`.
# The NAMES come from `clock`, not the numbers: `done` and a `measured`
# score are documented as the same judgement -- "re-verify something that
# was checked" -- and while both said `2.0` that equivalence was a
# sentence in a comment that nothing could break.
STALE_MULTIPLIER: dict[str, float] = {
    "blocked": URGENT,      # a block nobody has touched is the point of the flag
    "open": BASELINE,       # the baseline stale_days value
    "deviated": BASELINE,   # not in the spec table; treated as ordinary open work
    "derived": BASELINE,    # not in the spec table; treated as ordinary open work
    "deferred": RE_VERIFY,  # the ruling ages and gets re-reviewed
    "done": RE_VERIFY,      # re-verification, not decay
}


def _stale_cutoff(clock: Clock, state: str, updated: str | None) -> bool:
    """True when `state` has a staleness threshold at all, and `updated`
    parses as an ISO date older than that threshold (state's multiplier
    times `stale_days`) before `today`.

    A missing or unparseable `updated` is NOT flagged stale -- guessing
    staleness from absent data would be a false positive this function
    has no evidence for. (A SCORE's `on` is treated the opposite way --
    see `scale.freshness`, which says why the two differ.)

    Reading the date, hand-edited YAML `date` objects included, is
    `ledger.parse_iso_date`; the same trap is waiting in `scale.py` and
    one copy of the fix is enough.
    """
    if state in STALE_NEVER:
        return False
    return clock.expired(updated, STALE_MULTIPLIER.get(state, BASELINE))


def _absent_flags(graph: Graph, capability_names: list[str]) -> dict[str, set[Flag]]:
    """For each capability hub node, the products that lack it while
    other products in the graph have it -- what is MISSING, not what is
    broken. `absent` needs the whole graph, not the lens subgraph: a
    product with zero connection to a capability is exactly the case
    this flag exists to surface, so the baseline is every product node
    that exists, not only the ones already near some capability.
    """
    all_products = {n.name for n in graph.nodes.values() if n.type == "product"}
    result: dict[str, set[Flag]] = {}
    for cap in capability_names:
        has_it: set[str] = set()
        for product in all_products:
            hop1 = _touches_neighbors(graph, product)
            if cap in hop1:
                has_it.add(product)
                continue
            if any(cap in _touches_neighbors(graph, n) for n in hop1):
                has_it.add(product)
        missing = all_products - has_it
        if missing:
            detail = f"no touches from: {', '.join(sorted(missing))}"
            result[cap] = {Flag("absent", cap, detail)}
    return result


def compute_flags(
    graph: Graph, lens: Lens, clock: Clock, hops: int = 1
) -> dict[str, tuple[Flag, ...]]:
    """The flags visible through `lens`, keyed by hub node name.

    Nothing here is written back onto any `Node` -- call this again and
    it recomputes from the graph, every time.

    `hops` is the SAME widening a caller passes to `resolve_lens` for the
    membership it displays. It is a parameter rather than a fixed 1
    because a caller that widens the view and does not widen the flags
    shows a member with no explanation for the flag it raises -- the two
    must be resolved at the same radius or the result contradicts
    itself. Universal flags (`blocked`/`held`/`deviates`) are unaffected:
    they already walk the whole graph.
    """
    # Universal flags: self-raised, then spread along their own
    # dedicated relation, over the whole graph, independent of `lens`.
    blocked_seeds = {
        n.name: Flag("blocked", n.name, f"{n.name} is blocked")
        for n in graph.nodes.values()
        if n.state == "blocked"
    }
    blocked_reach = _propagate(graph, blocked_seeds, {"blocked_by"})

    held_seeds = {
        n.name: Flag("held", n.name, f"{n.name} is on hold")
        for n in graph.nodes.values()
        if n.state == "hold"
    }
    # NOT transitive, unlike `blocked`. A hold is a SCOPE: it holds the
    # things the decision names, and not whatever those things happen to
    # touch next. `blocked` is a chain -- if the thing blocking you is
    # itself blocked, you are too -- and that difference is why one
    # propagates onward and the other does not.
    #
    # Found by running it on a real estate. One hold declaring six
    # `touches` coloured all twelve products, because a repo's
    # documentation node touches every unit in that repo and so bridged
    # the hold to units the decision had never named. At six nodes it was
    # invisible; at sixty-two everything was held, and a flag that marks
    # everything marks nothing.
    held_reach = _direct_reach(graph, held_seeds, {"touches", "serves"})

    deviates_seeds: dict[str, Flag] = {}
    for n in graph.nodes.values():
        if n.state in CLOSED_STATES:
            continue
        for rel in n.relations:
            if rel.rel != "governed_by":
                continue
            decision = graph.nodes.get(rel.to)
            if decision is not None and decision.state == "hold":
                deviates_seeds[n.name] = Flag(
                    "deviates", n.name, f"{n.name} is open under a hold ({rel.to})"
                )
                break
    deviates_reach = _propagate(graph, deviates_seeds, {"governed_by"})

    absent_reach = (
        _absent_flags(graph, [n.name for n in graph.nodes.values() if n.type in lens.hub_types])
        if lens.reports_absence
        else {}
    )

    scope = resolve_lens(graph, lens, hops)
    result: dict[str, tuple[Flag, ...]] = {}
    for hub, members in scope.items():
        flags: set[Flag] = set()
        for member in members:
            flags |= blocked_reach.get(member, set())
            flags |= held_reach.get(member, set())
            flags |= deviates_reach.get(member, set())
            node = graph.nodes.get(member)
            if node is None:
                continue
            if is_unruled(node):
                flags.add(Flag("unruled", node.name, f"{node.name} is {node.state} with no ruling"))
            # An expectation is unmet when the last probe said so, when
            # there has been no probe at all, and when the last probe is
            # too old to still be an answer. Nobody having looked is not
            # the same as it being true -- and neither is nobody having
            # looked LATELY, which is the same sentence one level up.
            #
            # That last case is why this exists: the flag asked whether
            # the probe passed and never when it ran, so a doctor that
            # stopped -- billing, a broken plist, a machine shut for a
            # month -- left every expectation reading "met" for ever on
            # one old success, and nothing went red. The expectations
            # layer was built to remove exactly that silence.
            for e in node.expects:
                last = e.get("last") or {}
                subject = expect_subject(e)
                if not last:
                    flags.add(Flag("unmet", node.name, f"{node.name} expects {subject} -- never checked"))
                elif not last.get("ok"):
                    # A probe that ran and said no keeps saying no: its
                    # reason outranks its age, or a reader chases the
                    # schedule when the page is down.
                    flags.add(Flag("unmet", node.name, f"{node.name} expects {subject}: {last.get('detail')}"))
                else:
                    age = clock.age_days(last.get("at"))
                    if age is None:
                        # Unreadable is not recent. Otherwise one
                        # malformed timestamp buys permanent silence.
                        flags.add(Flag("unmet", node.name,
                                       f"{node.name} expects {subject} -- last passed, but when "
                                       f"is unreadable ({last.get('at')!r})"))
                    else:
                        # The expectation's own freshness clause when it
                        # has one: a page that must be fresh within 36h
                        # cannot be vouched for by a probe from last week.
                        hours = parse_duration_hours(e.get("fresh_within"))
                        window = max(1, -(-hours // 24)) if hours else PROBE_WINDOW_DAYS
                        if age > window:
                            flags.add(Flag("unmet", node.name,
                                           f"{node.name} expects {subject} -- last passed {age}d ago, "
                                           f"and has not been checked since (within {window}d)"))
            left = [c for c in node.checklist if not c.get("done")]
            if left:
                flags.add(Flag("unchecked", node.name,
                               f"{node.name} has {len(left)} of {len(node.checklist)} checklist item(s) unchecked"))
            if _stale_cutoff(clock, node.state, node.updated):
                # THIS state's threshold, not the bare `stale_days`. The two
                # differ by the state's multiplier, so a `done` node flagged
                # at 60 days used to announce itself as "over 30d" -- the
                # number shown was not the number compared against.
                days = clock.threshold(STALE_MULTIPLIER.get(node.state, BASELINE))
                flags.add(Flag("stale", node.name, f"{node.name} not updated in over {days}d"))
        flags |= absent_reach.get(hub, set())
        result[hub] = sort_flags(flags)
    return result
