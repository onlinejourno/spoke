"""Two lenses crossed into a grid.

Spec Sec.4.4's point: *"2 of 18" stops being a sentence someone had to
count and becomes a view that cannot go out of date.* Counting coverage
by hand is how "only 2 of 18 products call the entitlement gate" became a
claim nobody could re-check; a crossed matrix recomputes it from the
graph every time it is asked.

**Coverage is an intersection, not a direction.** A cell is covered when
the row hub's neighbourhood and the column hub's neighbourhood share
at least one node. That definition is lens-agnostic -- it makes no
assumption about which relation type either lens hubs on, and it stays
symmetric, so crossing product x capability and capability x product give
transposed answers rather than different ones. `Cell.shared` carries the
nodes that made it true, so a covered cell can always be asked *why*.

**An empty cell is the finding.** The absence of a connection is what
this view exists to surface, so a cell with no intersection carries an
explicit `absent` flag rather than being blank. Blank reads as "nothing
to say"; the whole failure this project addresses is absence reading as
assurance.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..clock import Clock
from .flags import Flag, compute_flags, sort_flags
from .graph import Graph, Lens, resolve_lens


@dataclass(frozen=True)
class Cell:
    """One row x column intersection.

    Read `.covered` to ask whether there is a connection and `.flags` to
    ask what it says. There used to be `__bool__` and `__iter__` as well,
    so `if m.cell(r, c)` and `for f in m.cell(r, c)` would each "mean the
    obvious thing" -- but the docstring beside them already told the
    reader to prefer the named fields, which is the tell that they were
    interface with no behaviour behind them. Two idioms for one question,
    one of them silently reading a different field from the other, is how
    a reader ends up believing an uncovered cell is empty.
    """
    covered: bool
    shared: tuple[str, ...]
    flags: tuple[Flag, ...]


@dataclass(frozen=True)
class Matrix:
    rows: tuple[str, ...]
    cols: tuple[str, ...]
    _cells: dict[tuple[str, str], Cell]

    def cell(self, row: str, col: str) -> Cell:
        return self._cells[(row, col)]


def cross(
    graph: Graph,
    row_lens: Lens,
    col_lens: Lens,
    clock: Clock,
    hops: int = 1,
) -> Matrix:
    """Cross `row_lens` with `col_lens` over `graph`.

    Flags in a cell are restricted to those whose ORIGIN lies in the
    shared neighbourhood. A row hub's full flag set belongs to the
    row, not to any particular column of it; attributing all of them to
    every covered cell would spread one problem across a whole line of
    the grid and make the view useless for locating anything.
    """
    row_scope = resolve_lens(graph, row_lens, hops)
    col_scope = resolve_lens(graph, col_lens, hops)
    row_flags = compute_flags(graph, row_lens, clock, hops)
    col_flags = compute_flags(graph, col_lens, clock, hops)

    rows = tuple(sorted(row_scope))
    cols = tuple(sorted(col_scope))

    cells: dict[tuple[str, str], Cell] = {}
    for r in rows:
        for c in cols:
            shared = row_scope[r] & col_scope[c]
            if shared:
                origins = shared
                flags = {f for f in row_flags.get(r, ()) if f.origin in origins}
                flags |= {f for f in col_flags.get(c, ()) if f.origin in origins}
                cells[(r, c)] = Cell(
                    covered=True,
                    shared=tuple(sorted(shared)),
                    flags=sort_flags(flags),
                )
            else:
                cells[(r, c)] = Cell(
                    covered=False,
                    shared=(),
                    flags=(Flag("absent", r, f"{r} has no path to {c}"),),
                )
    return Matrix(rows=rows, cols=cols, _cells=cells)
