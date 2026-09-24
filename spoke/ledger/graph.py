"""The ledger loaded as a graph, and lens resolution over it.

A lens is a question, not a hierarchy: there is no parent, no root, no
privileged direction. Every relation is navigable both ways, so the same
nodes are reachable through every lens -- only the hub (which nodes
anchor the view) and the flags computed over it (see flags.py) differ.
Do not special-case any lens as canonical; `resolve_lens` is the only
entry point, and it treats every `Lens` identically.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import Node, Relation, Skipped, RELATIONS, NODE_TYPES


@dataclass(frozen=True)
class Lens:
    name: str
    hub_types: tuple[str, ...]
    hub_rels: tuple[str, ...]
    # States that never HUB on this lens. A node in one of them can still
    # be a spoke of another hub -- it is context there -- but it does not
    # get a slot of its own. Empty means every state hubs.
    never_hubs: tuple[str, ...] = ()
    # Whether this lens asks "which products LACK each hub?" -- the
    # `absent` flag. It is a question about a hub's coverage across every
    # product in the graph, not about anything near the hub, so only a
    # lens whose hubs are things a product can have or lack asks it.
    # flags.py used to decide this with `lens.hub_types == ("capability",)`
    # -- a fact about the lens living in its consumer, which meant the
    # time lens (whose hubs include capabilities) silently never asked.
    reports_absence: bool = False
    # How the lens is drawn. "ring" -- hubs on a circle, members around
    # each -- is the hub-and-spoke picture and is right for a lens that
    # anchors on a few things. "timeline" lays every node out by DATE,
    # left to right, one row per layer: a chronological lens on a ring
    # is eighty-seven hubs in a circle that says nothing about time.
    layout: str = "ring"


# Defined exactly per spec Sec.4.3. `time` hubs on every node type (it is
# a chronological view, not a type-scoped one) and its relation set is
# every relation there is, since a time lens has no single governing
# relation to prefer.
LENSES: dict[str, Lens] = {
    # `serves` sits alongside `touches` here deliberately, beyond the
    # spec's Sec.4.3 table. A product lens that shows what a product
    # touches but not who it serves is showing half the product, and the
    # omission is not visible from the view -- flags.py ALREADY pairs the
    # two (`held` propagates along {"touches", "serves"}) for exactly this
    # reason, so leaving the lens with only `touches` had the two modules
    # disagreeing about what a product reaches. Recorded here so it is a
    # decision, not a drift.
    # An abandoned product is not a product anyone ships, so it is not a
    # hub the product lens anchors on. It stays in the ledger and stays
    # a spoke wherever something live still points at it. The decision
    # lens does NOT exclude abandoned: a rejected ADR is a decision --
    # decided against -- and belongs on a map of decisions.
    "product": Lens("product", ("product",), ("touches", "serves"),
                    never_hubs=("abandoned",)),
    "capability": Lens("capability", ("capability",), ("touches",),
                       reports_absence=True),
    "decision": Lens("decision", ("decision",), ("governed_by",)),
    "client": Lens("client", ("client",), ("serves",)),
    # The time lens does not report absence, and now says so. `absent`
    # is a structural question (coverage across every product) and the
    # time lens is a chronological one; a capability with no date-bearing
    # relation to some product is not a chronology fact.
    "time": Lens("time", NODE_TYPES, RELATIONS, layout="timeline"),
}


@dataclass
class Graph:
    """The whole ledger, with forward and reverse adjacency pre-built.

    `nodes` is the sole source of truth for what a node IS -- `out`/
    `inbound` only ever answer "what points where", never carry
    derived state (see flags.py's rule that nothing computed is stored
    back onto a node).
    """
    nodes: dict[str, Node]
    _out: dict[str, tuple[Relation, ...]]
    _in: dict[str, tuple[tuple[str, Relation], ...]]

    def out(self, name: str) -> tuple[Relation, ...]:
        return self._out.get(name, ())

    def inbound(self, name: str) -> tuple[tuple[str, Relation], ...]:
        return self._in.get(name, ())


def load_graph(store) -> tuple[Graph, list[str]]:
    """Build a `Graph` from every node `store.list_nodes()` returns.

    A relation whose target does not exist among those nodes is not a
    parse error -- the node itself is well-formed -- so it is not folded
    into `store.skipped`. It is instead reported here, in the returned
    skipped list, naming the dangling edge: absence must never render as
    assurance.
    """
    all_nodes = store.list_nodes()
    nodes = {n.name: n for n in all_nodes}
    skipped: list[Skipped] = []

    # Former names, resolved to the node that carries them. A shipped
    # name does not get erased: every reference written before a rename
    # still points at the old one, and reporting those as broken links
    # is reporting the residue of a rename as a defect. See Node.aliases.
    by_alias: dict[str, str] = {}
    for node in all_nodes:
        for alias in node.aliases:
            if alias in nodes:
                # A live node's name is not available as someone else's
                # former name. Refusing is the only safe answer: silently
                # preferring one would make a relation point somewhere
                # its author never wrote.
                skipped.append(Skipped(
                    "unreadable", f"{node.name}: alias {alias}",
                    f"{node.name} claims the alias {alias!r}, which is a live "
                    "node's own name -- the alias is ignored",
                ))
                continue
            owner = by_alias.get(alias)
            if owner is not None and owner != node.name:
                skipped.append(Skipped(
                    "unreadable", f"{node.name}: alias {alias}",
                    f"{alias!r} is claimed as a former name by both {owner} "
                    f"and {node.name} -- neither is used",
                ))
                by_alias[alias] = ""      # poisoned: claimed twice
                continue
            by_alias.setdefault(alias, node.name)

    out: dict[str, list[Relation]] = {}
    inbound: dict[str, list[tuple[str, Relation]]] = {}

    renamed: list[Skipped] = []
    for node in all_nodes:
        for rel in node.relations:
            target = rel.to
            if target not in nodes:
                renamed_to = by_alias.get(target)
                if renamed_to:
                    # Resolved, and SAID SO. A reader who wrote the old
                    # name learns the new one, which is exactly what a
                    # rename destroys and the thing they most need.
                    renamed.append(Skipped(
                        "renamed", f"{node.name} --{rel.rel}--> {target}",
                        f"{node.name} --{rel.rel}--> {target}: {target!r} is now "
                        f"{renamed_to!r}",
                    ))
                    rel = Relation(rel.rel, renamed_to)
                    target = renamed_to
            out.setdefault(node.name, []).append(rel)
            if target not in nodes:
                skipped.append(Skipped(
                    "dangling",
                    f"{node.name} --{rel.rel}--> {rel.to}",
                    f"{node.name} --{rel.rel}--> {rel.to}: target node does not exist",
                ))
                continue
            inbound.setdefault(rel.to, []).append((node.name, rel))

    graph = Graph(
        nodes=nodes,
        _out={k: tuple(v) for k, v in out.items()},
        _in={k: tuple(v) for k, v in inbound.items()},
    )
    return graph, skipped + renamed


def resolve_lens(graph: Graph, lens: Lens, hops: int = 1) -> dict[str, set[str]]:
    """Map each hub node's name to the set of node names within `hops`
    steps of it, in BOTH directions.

    A lens is a question, not a direction: `A touches B` must bring A
    into scope from B's side just as much as B comes into scope from A's.
    A visited set bounds the walk per hub node so a relation cycle
    (legal and common -- see the module docstring in flags.py) terminates
    rather than looping forever.

    EVERY hop is restricted to `lens.hub_rels` -- that relation type is
    what defines "hangs off this hub" for the lens (a product's hub
    relation is `touches`, a decision's is `governed_by`, and so on), and
    that must stay true at hop 2, hop 3, or hop 5 just as much as at hop
    1. A lens that only filters its first hop stops being a lens after
    one step: `widget(product) <-touches- i1 <-blocked_by- i2` would let
    `blocked_by` -- a relation the product lens never named -- pull `i2`
    into the product view merely because it happened two steps out. A
    `blocked_by`/`held` chain still needs to reach beyond the lens's own
    relations, but that is `flags._propagate`'s job (spec Sec.5's
    "in every lens" universal flags), which walks the WHOLE graph
    independent of `resolve_lens` -- not this function's.
    """
    result: dict[str, set[str]] = {}
    for name, node in graph.nodes.items():
        if node.type not in lens.hub_types or node.state in lens.never_hubs:
            continue
        seen = {name}
        frontier = {name}
        for _hop_index in range(hops):
            next_frontier: set[str] = set()
            for cur in frontier:
                for rel in graph.out(cur):
                    if rel.rel in lens.hub_rels and rel.to not in seen:
                        next_frontier.add(rel.to)
                for src, rel in graph.inbound(cur):
                    if rel.rel in lens.hub_rels and src not in seen:
                        next_frontier.add(src)
            next_frontier -= seen
            if not next_frontier:
                break
            seen |= next_frontier
            frontier = next_frontier
        result[name] = seen
    return result
