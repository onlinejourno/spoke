"""The board's view model: everything the browser must not decide.

Zoom layers, flag roll-up, severity, shape, badge text and coordinates
are all computed here and shipped as data. The browser positions SVG and
handles clicks; it holds no copy of the flag rules and runs no layout of
its own. Two reasons, both learned the hard way in this project:

- a second implementation of a rule is a second thing to drift, and the
  drift is silent -- the page keeps rendering, just wrongly;
- a browser-side force layout cannot be tested, so its output is
  whatever it happens to be on the day.

**A hidden layer must never hide a problem.** That is this project's
founding failure restated as a UI rule, and it is why `hub` nodes are
always drawn and always carry the flags of every member in their scope,
origins included. Zooming out removes detail, never evidence: at the
far-out zoom a product still shows red because something several layers
down is blocked, and the badge still says what and the detail still says
which node.
"""
from __future__ import annotations

from datetime import date

from . import Node
from ..clock import Clock
from .flags import FLAG_KINDS as _FLAG_KINDS, Flag, compute_flags, sort_flags
from .graph import LENSES, Graph, Lens, load_graph, resolve_lens
from .layout import layout
from .scale import BASES as BASES_ORDER
from . import NODE_TYPES, SKIP_HEADINGS, describe_skipped

# Which zoom level first draws a node of each type. The layers ARE the
# zoom levels (spec Sec.6): zooming reveals and hides layers, it does not
# merely scale.
LAYERS: dict[str, int] = {
    "project": 0,
    "product": 0,
    "capability": 1,
    "decision": 1,
    "item": 2,
    "client": 3,
}
ZOOM_LEVELS = ("far", "middle", "close", "closest")

# Colour encodes severity ONLY. Type is carried by shape and the reason
# by the badge text, so a reader who cannot tell red from amber still
# gets both -- see the board.css comment and spec Sec.6.
SEVERITY: dict[str, str] = {
    "blocked": "bad",
    "deviates": "bad",
    "unruled": "warn",
    "unchecked": "warn",
    # An unmet expectation is the thing this tool exists to surface: a
    # claim about the world that the world contradicted, or that nobody
    # has checked. It ranks with blocked.
    "unmet": "bad",
    "stale": "warn",
    "absent": "warn",
    "held": "held",
}
_SEV_RANK = {"none": 0, "held": 1, "warn": 2, "bad": 3}

# Re-exported from flags.py, which produces them. SEVERITY must give a
# colour to every one; the assertion is a test, not a comment.
FLAG_KINDS = _FLAG_KINDS

# The zoom at which clients stop being one aggregate and become
# individual nodes. Below it they are collapsed to a single "N clients"
# node: a client's identity is not what a map of the estate is for, and
# a hundred of them would drown everything else.
_CLIENTS_EXPANDED_AT = 3
_CLIENT_AGGREGATE_SUFFIX = "::clients"


def _require_lens(name: str):
    """The lens, or a refusal naming the ones that exist.

    One copy. The sentence lived in four places -- twice here, once in
    the CLI, once in the MCP server -- and four copies of a refusal is
    four chances for one of them to stop naming the alternatives, which
    is the difference between a dead end and a next command.
    """
    lens = LENSES.get(name)
    if lens is None:
        raise BoardError(
            f"no such lens {name!r} -- known lenses: {', '.join(sorted(LENSES))}"
        )
    return lens


def _require_hops(hops: int) -> int:
    """The radius, or a refusal.

    `build_matrix` never checked, and the server passed the query
    parameter straight through. `/api/matrix?hops=0` therefore answered
    200 with every cell uncovered and an `absent` flag on each -- a
    healthy estate rendered as "nothing is connected to anything", by the
    view whose whole purpose is that a coverage count stops being a
    number somebody had to trust. Measured on a real store: 480 cells,
    0 covered, 480 absent. Meanwhile /api/lens?hops=0 was a 422 on the
    same server, in the same second.
    """
    if hops < 1:
        raise BoardError(f"hops must be at least 1, got {hops}")
    return hops


def _require_node_type(node_type: str) -> str:
    """The node type, or a refusal naming the ones that exist."""
    if node_type not in NODE_TYPES:
        raise BoardError(
            f"no such node type {node_type!r} -- known types: "
            f"{', '.join(NODE_TYPES)}"
        )
    return node_type


def _envelope(store, dangling, **payload) -> dict:
    """Every board payload, with the parts none of them may omit.

    The three build functions each hand-assembled this, so a missing
    argument in one of them was invisible: `build_matrix` passed
    `store=None` to `_skipped_payload` and was therefore structurally
    incapable of reporting an unreadable or escaped file -- the two kinds
    only the store knows about. The matrix could report a dangling
    relation and could not report a file it could not read at all.

    Absence must never render as assurance; an envelope nobody can
    forget to fill is how that stops depending on memory.
    """
    entries = list(dangling) + list(getattr(store, "skipped", ()) or ())
    return {
        **payload,
        # The SAME renderer every text surface uses, run once here rather
        # than re-derived per surface. The MCP scale tool was formatting
        # these itself and printing "1 entry" three times where the CLI
        # said "3 entries" -- one condition, two counts.
        "skipped_lines": describe_skipped(entries),
        # Never omitted, never empty-by-accident. Each entry carries its
        # KIND, and `skip_headings` carries the one phrasing of each, so a
        # page renders a heading it was GIVEN rather than inventing one --
        # which is how two pages came to make opposite claims about this
        # very field.
        "skipped": _skipped_payload(dangling, store),
        "skip_headings": dict(SKIP_HEADINGS),
    }


def _skipped_payload(dangling, store) -> list[dict]:
    """Every unread thing, each carrying the kind of not-read it was."""
    items = list(dangling)
    if store is not None:
        items += list(getattr(store, "skipped", ()) or ())
    return [
        {"kind": s.kind, "subject": s.subject, "detail": str(s)}
        for s in items
    ]


class BoardError(ValueError):
    """The request cannot be served as asked -- an unknown lens, zoom or
    flag. Raised rather than defaulted: quietly substituting something
    that works produces a page that answers a question nobody asked."""


def _badge(flags: tuple[Flag, ...]) -> str:
    counts: dict[str, int] = {}
    for f in flags:
        counts[f.kind] = counts.get(f.kind, 0) + 1
    return ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()))


def _severity(flags: tuple[Flag, ...]) -> str:
    worst = "none"
    for f in flags:
        sev = SEVERITY.get(f.kind, "warn")
        if _SEV_RANK[sev] > _SEV_RANK[worst]:
            worst = sev
    return worst


_sorted = sort_flags       # the ordering is flags.py's, not a second copy


def _origins(graph, flags, vocabulary) -> dict:
    """What each flag origin IS, keyed by name, for the rail to speak
    from: type (in the project's words), state, title, and the ruling
    if one was written. An origin that is not a node -- a name that
    does not resolve -- still gets an entry, saying only that."""
    out: dict = {}
    for hub_flags in flags.values():
        for f in hub_flags:
            if f.origin in out:
                continue
            n = graph.nodes.get(f.origin)
            if n is None:
                out[f.origin] = {"name": f.origin, "kind": None, "state": None,
                                 "title": None, "ruling": None}
                continue
            out[f.origin] = {
                "name": n.name,
                "kind": vocabulary.get(n.type, n.type),
                "state": n.state,
                "title": n.title or "",
                "ruling": n.ruling or None,
            }
    return out


def build_view(
    store,
    lens_name: str,
    clock: Clock,
    zoom: str = "far",
    hops: int = 1,
    flag_filter: tuple[str, ...] = (),
    vocabulary: dict[str, str] | None = None,
) -> dict:
    """The whole board payload for one lens at one zoom.

    `vocabulary` maps an engine node type to the project's own word for
    it. Every node carries a `kind` -- that word -- and the surfaces show
    `kind`, never `type`. A project that calls its units "chapters" must
    never be shown the word "product" by a tool that has been told the
    word it actually uses.
    """
    vocabulary = dict(vocabulary or {})
    lens = _require_lens(lens_name)
    if zoom not in ZOOM_LEVELS:
        raise BoardError(
            f"no such zoom {zoom!r} -- known zooms: {', '.join(ZOOM_LEVELS)}"
        )
    unknown = set(flag_filter) - set(FLAG_KINDS)
    if unknown:
        raise BoardError(
            f"unknown flag kind(s) {', '.join(sorted(unknown))} -- "
            f"known kinds: {', '.join(FLAG_KINDS)}"
        )
    _require_hops(hops)

    graph, dangling = load_graph(store)
    scope = resolve_lens(graph, lens, hops)
    flags = compute_flags(graph, lens, clock, hops)

    if flag_filter:
        wanted = set(flag_filter)
        scope = {
            s: members for s, members in scope.items()
            if any(f.kind in wanted for f in flags.get(s, ()))
        }

    z = ZOOM_LEVELS.index(zoom)

    # Per-node flags. A hub carries EVERY flag in its scope -- that is
    # the roll-up, and it is why hiding a layer cannot hide a problem. A
    # member carries only the flags it is itself the origin of, so the
    # same problem is not reported twice as if it were two.
    node_flags: dict[str, set[Flag]] = {}
    for hub, members in scope.items():
        node_flags.setdefault(hub, set()).update(flags.get(hub, ()))
        for member in members:
            if member == hub:
                continue
            node_flags.setdefault(member, set()).update(
                f for f in flags.get(hub, ()) if f.origin == member
            )

    rendered: dict[str, set[str]] = {}
    aggregates: dict[str, list[str]] = {}
    for hub, members in scope.items():
        keep: set[str] = {hub}          # a hub is always drawn: it anchors the view
        hidden_clients: list[str] = []
        for member in members:
            node = graph.nodes.get(member)
            if node is None:
                continue
            if node.type == "client" and z < _CLIENTS_EXPANDED_AT:
                hidden_clients.append(member)
                continue
            if LAYERS.get(node.type, 2) <= z:
                keep.add(member)
        rendered[hub] = keep
        # Clients collapse into one node from the `close` zoom onward;
        # further out they are not drawn at all, but their flags have
        # already rolled up onto the hub above.
        if hidden_clients and z >= LAYERS["item"]:
            aggregates[hub] = sorted(hidden_clients)
            # The aggregate is laid out WITH everything else, not patched
            # in afterwards. It used to get its coordinate from
            # `hub + (90, 90)` -- MEMBER_RADIUS_MIN copied by value out of
            # the module that owns positioning -- so the one guarantee
            # layout is tested for, that no two rendered nodes share a
            # position, did not cover the payload actually shipped.
            keep.add(f"{hub}{_CLIENT_AGGREGATE_SUFFIX}")

    if lens.layout == "timeline":
        from . import parse_iso_date
        from .layout import timeline

        def _day(name: str):
            n = graph.nodes.get(name)
            if n is None:
                return None
            # Chronology, not activity: a node sits where it was opened
            # (an ADR's own date, or first seen). `updated` is what a
            # probe or a doctor run touches, and a decision must not slide
            # along the axis because something checked it this morning.
            d = parse_iso_date(n.opened)
            return d.toordinal() if d else None

        def _row(name: str) -> int:
            n = graph.nodes.get(name)
            return LAYERS.get(n.type, 2) if n else 3

        placed = {name for members in rendered.values() for name in members} | set(rendered)
        positions = timeline(placed, _day, _row)
        from .layout import timeline_ticks
        axis = timeline_ticks(placed, _day)
    else:
        positions = {k: tuple(v) for k, v in layout(rendered).items()}
        axis = []

    nodes: list[dict] = []
    seen: set[str] = set()
    for hub, keep in rendered.items():
        for name in sorted(keep):
            if name in seen or name.endswith(_CLIENT_AGGREGATE_SUFFIX):
                continue
            seen.add(name)
            node: Node | None = graph.nodes.get(name)
            nf = _sorted(node_flags.get(name, ()))
            nodes.append({
                "name": name,
                "type": node.type if node else "item",
                "kind": vocabulary.get(node.type if node else "item",
                                       node.type if node else "item"),
                "state": node.state if node else "missing",
                "title": node.title if node else "",
                "checklist": [dict(c) for c in (node.checklist if node else ())],
                "expects": [dict(e) for e in (node.expects if node else ())],
                "label": name,
                "layer": LAYERS.get(node.type if node else "item", 2),
                "hub": name in scope,
                "flags": [{"kind": f.kind, "origin": f.origin, "detail": f.detail} for f in nf],
                "badge": _badge(nf),
                "severity": _severity(nf),
                # The server decides the shape, the same way it decides
                # the badge and the severity. The renderer used to derive
                # it from `type` on its own and ignore this field -- so it
                # was live here and dead there, and a payload field the
                # page silently overrides is a decision made twice.
                "shape": node.type if node else "item",
            })

    for hub, clients in aggregates.items():
        agg_flags = _sorted({
            f for c in clients for f in node_flags.get(c, ())
        })
        flagged = sum(1 for c in clients if node_flags.get(c))
        name = f"{hub}{_CLIENT_AGGREGATE_SUFFIX}"
        nodes.append({
            "name": name,
            "type": "clients",
            "kind": vocabulary.get("client", "client"),
            "state": "aggregate",
            "title": "",
            "label": f"{len(clients)} {vocabulary.get('client', 'client')}"
                     f"{'' if len(clients) == 1 else 's'} · {flagged} flagged",
            "layer": LAYERS["client"],
            "hub": False,
            "flags": [{"kind": f.kind, "origin": f.origin, "detail": f.detail} for f in agg_flags],
            "badge": _badge(agg_flags),
            "severity": _severity(agg_flags),
            "shape": "clients",
        })

    drawn = {n["name"] for n in nodes}
    edges = [
        {"from": src, "to": rel.to, "rel": rel.rel}
        for src in sorted(drawn)
        for rel in graph.out(src)
        if rel.to in drawn
    ]
    for hub, clients in aggregates.items():
        edges.append({
            "from": hub, "to": f"{hub}{_CLIENT_AGGREGATE_SUFFIX}", "rel": "serves",
        })

    return _envelope(
        store, dangling,
        lens=lens.name,
        lenses=sorted(LENSES),
        # Why a lens can be empty, said by the server that knows: how many
        # nodes the ledger holds in total, and how many are of the type
        # this lens hubs on BEFORE any flag filter. The page tells these
        # three apart -- an empty ledger, a type nobody has recorded, and
        # a filter that excludes everything -- instead of drawing the
        # same blank ground for all three.
        ledger_total=len(graph.nodes),
        hub_type_count=sum(1 for n in graph.nodes.values()
                           if n.type in lens.hub_types and n.state not in lens.never_hubs),
        hub_types=[vocabulary.get(t, t) for t in lens.hub_types],
        layout=lens.layout,
        axis=axis,
        zoom=zoom,
        zooms=list(ZOOM_LEVELS),
        # What each level past the first ADDS, by layer, in the project's
        # own words -- so the control can say "+ decision · capability"
        # rather than "middle", which promised a magnification it never
        # performed.
        zoom_adds={
            ZOOM_LEVELS[i]: sorted(
                vocabulary.get(t, t) for t, layer in LAYERS.items() if layer == i
            )
            for i in range(1, len(ZOOM_LEVELS))
        },
        hops=hops,
        flag_kinds=list(FLAG_KINDS),
        # The KEY is drawn from these, by the same code that colours the
        # nodes. A legend the page authored by hand would be a second
        # statement of what the colours mean, and two statements drift.
        severities=dict(SEVERITY),
        severity_order=[k for k in _SEV_RANK if k != "none"],
        node_types=list(NODE_TYPES),
        vocabulary=vocabulary,
        nodes=nodes,
        edges=edges,
        positions={k: list(v) for k, v in positions.items() if k in drawn},
        # Every flag names its ORIGIN -- the hold, the block, the node with
        # no ruling. The rail groups by that, because eight hubs each
        # showing "1 held" are one fact: one hold reaching eight. What the
        # origin says -- its title and ruling -- is what a reader needs on
        # that line, and it is not otherwise in the payload (at far zoom
        # the origin is usually not drawn).
        origins=_origins(graph, flags, vocabulary),
    )


def build_lens(
    store,
    lens_name: str,
    clock: Clock,
    hops: int = 1,
    flag_filter: tuple[str, ...] = (),
) -> dict:
    """A lens as a QUERY RESULT: each hub, its flags with their origins,
    and its spokes.

    Distinct from `build_view`, which answers the same question for a
    drawing -- zoom layers, aggregates, coordinates. This is the answer a
    reader gets in a terminal or an agent gets over MCP, and it used to
    be derived independently in both of those places: two copies of
    `load_graph` -> `resolve_lens` -> `compute_flags`, and two renderers
    that had already drifted. The CLI grew a `--flag` filter that the
    agent-facing surface never got, and the two disagreed about what to
    say when the result was empty.
    """
    lens = _require_lens(lens_name)
    _require_hops(hops)
    unknown = set(flag_filter) - set(FLAG_KINDS)
    if unknown:
        raise BoardError(
            f"unknown flag kind(s) {', '.join(sorted(unknown))} -- "
            f"known kinds: {', '.join(FLAG_KINDS)}"
        )

    graph, dangling = load_graph(store)
    scope = resolve_lens(graph, lens, hops)
    # The SAME radius for flags as for membership: widening one without
    # the other shows a spoke whose flag has no visible cause.
    flags = compute_flags(graph, lens, clock, hops)

    wanted = set(flag_filter)
    hubs = []
    for name in sorted(scope):
        hub_flags = flags.get(name, ())
        if wanted and not any(f.kind in wanted for f in hub_flags):
            continue
        node = graph.nodes[name]
        hubs.append({
            "name": name,
            "type": node.type,
            "state": node.state,
            "flags": [{"kind": f.kind, "origin": f.origin, "detail": f.detail}
                      for f in hub_flags],
            "spokes": sorted(scope[name] - {name}),
        })

    return _envelope(
        store, dangling,
        lens=lens.name,
        hops=hops,
        filtered=bool(wanted),
        hubs=hubs,
    )


def lens_lines(body: dict) -> list[str]:
    """One lens, as text. The single renderer both text surfaces use.

    An empty result is a result: printing nothing at all reads as "the
    command did not run", which is the ambiguity that lets a silent
    failure pass for a clean bill. The two surfaces used to say different
    things here.
    """
    lines = [f"lens: {body['lens']}"]
    for hub in body["hubs"]:
        lines.append(f"hub {hub['name']}: {hub['type']} {hub['state']}")
        for f in hub["flags"]:
            lines.append(f"  flag: {f['kind']} <- {f['origin']}: {f['detail']}")
        if hub["spokes"]:
            lines.append(f"  spokes: {', '.join(hub['spokes'])}")
    if not body["hubs"]:
        scoped = "matching" if body["filtered"] else "in the ledger"
        lines.append(f"lens: no {body['lens']} nodes {scoped}")
    return lines


def build_matrix(store, rows: str, cols: str, clock: Clock, hops: int = 1) -> dict:
    from .matrix import cross

    row_lens = _require_lens(rows)
    col_lens = _require_lens(cols)
    _require_hops(hops)
    graph, dangling = load_graph(store)
    m = cross(graph, row_lens, col_lens, clock, hops)
    return _envelope(
        store, dangling,
        rows=list(m.rows),
        cols=list(m.cols),
        cells=[
            {
                "row": r, "col": c,
                "covered": m.cell(r, c).covered,
                "shared": list(m.cell(r, c).shared),
                "flags": [
                    {"kind": f.kind, "origin": f.origin, "detail": f.detail}
                    for f in m.cell(r, c).flags
                ],
            }
            for r in m.rows for c in m.cols
        ],
    )


def build_scale(
    store,
    axes: tuple,
    clock: Clock,
    node_type: str = "product",
    max_unmeasured_weight: float | None = None,
    vocabulary: dict[str, str] | None = None,
    max_value: int | None = None,
) -> dict:
    """The capability grid as data: axes with their normalised weights,
    one row per node, every cell present with its freshness, and a
    composite or the reason there is none.

    Every cell is emitted even when nothing was scored. A payload that
    omitted the unscored pairs would render as a gap the reader has to
    notice, and noticing an absence is exactly what people do not do --
    which is why this project exists.

    The `clock` is required, not defaulted: a scale built
    without a clock cannot tell a measurement taken this morning from one
    taken last year, and every one of them would render as current. See
    scale.freshness for which bases decay and why the other two do not.
    """
    from .scale import (
        DEFAULT_MAX_UNMEASURED_WEIGHT, DEFAULT_MAX_VALUE, age_days, composite,
        freshness, score_stale_threshold,
    )

    # Refused, not answered with an empty grid. `/api/lens` 422s on an
    # unknown lens with a comment about never falling back silently, and
    # this returned HTTP 200 with `rows: []` -- which renders as "no
    # nodes to score yet", i.e. a typo in a query parameter was
    # indistinguishable from a project nobody had scored.
    _require_node_type(node_type)

    vocabulary = dict(vocabulary or {})
    threshold = (
        DEFAULT_MAX_UNMEASURED_WEIGHT
        if max_unmeasured_weight is None
        else max_unmeasured_weight
    )
    top = DEFAULT_MAX_VALUE if max_value is None else max_value
    graph, dangling = load_graph(store)
    # The same standing rule the lens for this type declares (`never_hubs`):
    # an abandoned product is not a product anyone ships, so it does not
    # get a row to score. A type with no lens excludes nothing.
    never = LENSES[node_type].never_hubs if node_type in LENSES else ()
    nodes = sorted(
        (n for n in graph.nodes.values() if n.type == node_type and n.state not in never),
        key=lambda n: n.name,
    )
    total = sum(a.weight for a in axes) or 1

    rows = []
    for node in nodes:
        by_axis = {s.axis: s for s in node.scores}
        cells = []
        for axis in axes:
            s = by_axis.get(axis.key)
            cells.append({
                "axis": axis.key,
                # None, not 0. The view must be able to tell "nobody
                # looked" from "somebody looked and it was zero".
                "value": None if s is None else s.value,
                "basis": None if s is None else s.basis,
                "note": None if s is None else s.note,
                "on": None if s is None else s.on,
                "by": None if s is None else s.by,
                # None here is a FIFTH answer, not "fresh": either
                # nothing was scored, or the basis never decays.
                "freshness": None if s is None else freshness(s, clock),
                "age_days": None if s is None else age_days(s, clock),
            })
        c = composite(node.scores, axes, threshold, top, clock=clock)
        rows.append({
            "name": node.name,
            "title": node.title,
            "kind": vocabulary.get(node.type, node.type),
            "cells": cells,
            "composite": None if c.value is None else round(c.value, 1),
            "withheld_reason": c.withheld_reason,
            "measured_weight": round(c.measured_weight, 4),
            "threshold": round(c.threshold, 4),
            "missing": list(c.missing),
        })

    return _envelope(
        store, dangling,
        max_value=top,
        # The thresholds actually applied, per basis, shipped rather than
        # re-derived on the page -- the same rule the composite threshold
        # already follows. A None means that basis never decays.
        stale_days=clock.stale_days,
        score_stale_days={b: score_stale_threshold(b, clock) for b in BASES_ORDER},
        node_type=node_type,
        # The page offered four hardcoded options and nothing checked
        # them against the engine's own list.
        node_types=list(NODE_TYPES),
        kind=vocabulary.get(node_type, node_type),
        # The project's words for the types, so the selector says
        # "package" where the grid's header says "package".
        vocabulary=dict(vocabulary),
        axes=[
            {"key": a.key, "label": a.label, "weight": a.weight,
             "share": round(a.weight / total, 4)}
            for a in axes
        ],
        bases=list(BASES_ORDER),
        rows=rows,
    )
