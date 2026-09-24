"""Coordinates for the board, computed here and shipped to the browser.

The browser only draws. A browser-side force layout has no bounded render
time and nothing to assert about it -- the same graph settles somewhere
different on every load, so "did the map draw correctly" stops being a
question a test can answer. Placing nodes in Python makes both true
again: the cost is measured (see `tests/test_ledger_layout.py`, which
fails the build if this ever goes quadratic) and the output is an exact
value a test can compare.

So determinism is the whole point, and it is fragile in the obvious
places: this module iterates NOTHING in set or dict order. Every set is
sorted by name before it is walked, and no position derives from `hash()`
(PYTHONHASHSEED varies between runs) or from randomness. Two runs, two
processes, two machines: the same coordinates.

**A node gets exactly one position.** A lens is not a tree -- the same
node is legitimately inside several hubs' scope (see graph.py) -- so a
node reached by more than one hub is placed once, in the cluster of the
first hub by name. Drawing it once per hub would show one item as
several, which is the misreading this whole project exists to remove.

Geometry: hubs around a circle, each hub's own members on a ring
around it, with the hub circle sized so no two member clusters overlap.
It depends only on names and on what is in scope -- never on a node's
body, state or flags -- so editing an unrelated node cannot move the map
under the reader.
"""
from __future__ import annotations

import math

from .graph import Graph, Lens

# Distances in the same arbitrary unit the board's viewport works in;
# Cytoscape scales them to fit, so only their ratios matter.
# Sized for a DOT with a label on demand. The board labels a spoke only
# when its hub is selected, and a hub only when it is flagged, selected
# or one of at most forty (board.js LABEL_BUDGET); everything else is a
# dot that labels on hover. So neighbours need room for a glyph and a
# halo, and the board staggers the labels that do show onto two rows so
# adjacent ones clear each other. Sized for labels everywhere, fifty
# hubs drew a ring 3,200 units across with nothing in the middle.
MEMBER_RADIUS_MIN = 90.0
MEMBER_SPACING = 100.0
SPINE_RADIUS_MIN = 160.0
CLUSTER_GAP = 90.0
# Half the width a hub's glyph and halo need when nothing surrounds it.
HUB_FOOTPRINT = 36.0
# Between two neighbouring empty hubs.
HUB_GAP = 40.0

# An irrational offset (in radians) applied to every ring, so that no node
# lands exactly on a ring's vertical axis for any node count: cos(theta)
# is then never 0, and a member never shares its hub's x coordinate.
_ANGLE_OFFSET = 0.4


def _ring_radius(count: int, spacing: float, minimum: float) -> float:
    """Radius that keeps `count` points at least `spacing` apart on a ring."""
    if count <= 0:
        return 0.0
    return max(minimum, spacing * count / (2 * math.pi))


def _on_ring(
    cx: float, cy: float, radius: float, index: int, count: int
) -> tuple[float, float]:
    theta = _ANGLE_OFFSET + (2 * math.pi * index / count)
    return (cx + radius * math.cos(theta), cy + radius * math.sin(theta))


def layout(resolved: dict[str, set[str]]) -> dict[str, tuple[float, float]]:
    """Position every node `resolved` puts on screen.

    `resolved` is what `resolve_lens` returns: hub node name -> the names
    in that hub's scope. Every one of them gets a coordinate, including a
    member naming a node that does not exist (`resolve_lens` follows a
    dangling relation, and the board reports such nodes rather than
    dropping them) -- a rendered node with no position would be a node the
    reader never sees, which is the one outcome this must not produce.

    Takes ONLY the resolved scope. It used to take `graph` and `lens` as
    well and document that they deliberately did not affect the geometry
    -- what a node IS must not decide where it sits, or the map would
    rearrange itself every time a body was edited. That was a promise in
    a docstring; not receiving them makes it a fact, and drops two
    arguments every caller had to be told did not matter.
    """
    hubs = sorted(resolved)
    if not hubs:
        return {}

    # First hub by name claims a shared node, so each node is placed once.
    claimed = set(hubs)
    owned: dict[str, list[str]] = {}
    for hub in hubs:
        mine = [m for m in sorted(resolved[hub]) if m not in claimed]
        claimed.update(mine)
        owned[hub] = mine

    # A hub with no members of its own is a glyph and a label, not a
    # cluster: its footprint is HUB_FOOTPRINT, not a ring's minimum
    # radius. With the minimum applied to empty hubs, fourteen hubs at
    # "hubs only" spread over a 2000-unit span for nothing to sit in.
    radii = {
        hub: (_ring_radius(len(members), MEMBER_SPACING, MEMBER_RADIUS_MIN)
              if members else HUB_FOOTPRINT)
        for hub, members in owned.items()
    }
    # Hubs sit far enough apart that the widest two clusters cannot
    # overlap. When no hub has a cluster -- the "hubs only" level -- the
    # gap is a label's worth, not a cluster's.
    any_cluster = any(owned.values())
    cluster_pitch = 2 * max(radii.values()) + (CLUSTER_GAP if any_cluster else HUB_GAP)
    hub_radius = (
        0.0
        if len(hubs) == 1
        else _ring_radius(len(hubs), cluster_pitch, SPINE_RADIUS_MIN)
    )

    positions: dict[str, tuple[float, float]] = {}
    for index, hub in enumerate(hubs):
        positions[hub] = _on_ring(0.0, 0.0, hub_radius, index, len(hubs))
    for hub in hubs:
        cx, cy = positions[hub]
        members = owned[hub]
        for index, member in enumerate(members):
            positions[member] = _on_ring(cx, cy, radii[hub], index, len(members))
    return positions


# ── the timeline ───────────────────────────────────────────────────
# Same unit as the ring. The x scale FITS the data: a span of years and a
# span of a fortnight both draw about TIMELINE_WIDTH wide, because the
# question a timeline answers is "what happened when, relative to the
# rest", not "how many pixels is a day". Within a row, nodes keep date
# ORDER and are never closer than COLUMN: where the dates are denser
# than that, the row stretches locally, so a busy day is a run of nodes
# in order rather than a stack or a smear -- and no two nodes can share
# a position, by construction.
TIMELINE_WIDTH = 1600.0
COLUMN = 100.0          # a dot and a halo; labels stagger onto two rows (board.js)
ROW_HEIGHT = 150.0
UNDATED_GAP = 240.0
UNDATED_PER_LINE = 10
UNDATED_LINE = 130.0    # two lanes of glyph-and-label
LANE = 60.0             # the second lane of a zigzag row


def timeline(
    names: set[str], date_of, row_of
) -> dict[str, tuple[float, float]]:
    """Position `names` by date, left to right, one row per layer.

    `date_of(name)` returns an ordinal day or None; `row_of(name)` an
    integer row (the board's LAYERS). Deterministic: every tie is broken
    by name, never by iteration order.

    Undated nodes form a run to the left of the earliest date --
    present, and visibly outside time.
    """
    dated = sorted((n for n in names if date_of(n) is not None), key=lambda n: (date_of(n), n))
    undated = sorted(n for n in names if date_of(n) is None)
    if not dated and not undated:
        return {}
    first = date_of(dated[0]) if dated else 0
    last = date_of(dated[-1]) if dated else 0
    span = max(1, last - first)
    day = min(60.0, max(1.0, TIMELINE_WIDTH / span))

    # Band tops: a row is ROW_HEIGHT tall, plus a line for each extra
    # line its undated block wraps into, so bands never overlap.
    per_row_undated: dict[int, int] = {}
    for n in undated:
        per_row_undated[row_of(n)] = per_row_undated.get(row_of(n), 0) + 1
    rows = sorted({row_of(n) for n in names})
    band_top: dict[int, float] = {}
    y = 0.0
    for r in rows:
        band_top[r] = y
        lines = max(1, -(-per_row_undated.get(r, 0) // UNDATED_PER_LINE))
        y += ROW_HEIGHT + LANE + (lines - 1) * UNDATED_LINE

    positions: dict[str, tuple[float, float]] = {}
    last_x: dict[int, float] = {}
    count: dict[int, int] = {}
    for n in dated:
        row = row_of(n)
        x = (date_of(n) - first) * day
        if row in last_x:
            x = max(x, last_x[row] + COLUMN)
        last_x[row] = x
        # Consecutive nodes in a row alternate between two lanes, so a
        # label only has to clear the node two columns over, not the
        # next one: twice the room at the same column width.
        lane = count.get(row, 0) % 2
        count[row] = count.get(row, 0) + 1
        positions[n] = (x, band_top[row] + lane * LANE)

    # The undated block ends UNDATED_GAP left of day zero. It has no
    # order to keep, so it wraps into lines of UNDATED_PER_LINE inside
    # the row's band rather than running off to the left.
    per_row: dict[int, list[str]] = {}
    for n in undated:
        per_row.setdefault(row_of(n), []).append(n)
    for row, ns in per_row.items():
        width = min(len(ns), UNDATED_PER_LINE)
        for i, n in enumerate(ns):
            line, col = divmod(i, UNDATED_PER_LINE)
            # the same zigzag the dated rows use, so a label only has
            # to clear the node two columns over
            positions[n] = (
                -UNDATED_GAP - (width - 1 - col) * COLUMN,
                band_top[row] + line * UNDATED_LINE + (col % 2) * LANE,
            )
    return positions


def timeline_ticks(names: set[str], date_of) -> list[dict]:
    """Month ticks for the axis, in the SAME x scale `timeline` places
    nodes with -- derived from the same first date and the same
    day width, so the axis cannot say a node sits in a month it does
    not. One entry per month boundary inside the dated span, plus the
    span's first and last day, plus the undated block's label."""
    import datetime as _dt

    dated = sorted((n for n in names if date_of(n) is not None), key=lambda n: (date_of(n), n))
    ticks: list[dict] = []
    if any(date_of(n) is None for n in names):
        ticks.append({"x": -UNDATED_GAP - (min(UNDATED_PER_LINE, sum(1 for n in names if date_of(n) is None)) - 1) * COLUMN / 2,
                      "label": "undated", "kind": "undated"})
    if not dated:
        return ticks
    first = date_of(dated[0]); last = date_of(dated[-1])
    span = max(1, last - first)
    day = min(60.0, max(1.0, TIMELINE_WIDTH / span))
    f = _dt.date.fromordinal(first); l = _dt.date.fromordinal(last)
    ticks.append({"x": 0.0, "label": f.isoformat(), "kind": "edge"})
    y, m = f.year, f.month
    while True:
        m += 1
        if m > 12:
            m = 1; y += 1
        d = _dt.date(y, m, 1)
        if d > l:
            break
        ticks.append({"x": (d.toordinal() - first) * day, "label": d.strftime("%b %Y"), "kind": "month"})
    if last != first:
        ticks.append({"x": (last - first) * day, "label": l.isoformat(), "kind": "edge"})
    return ticks
