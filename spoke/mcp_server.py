"""The MCP server: the same ledger/memory gate a human meets on the CLI,
exposed over stdio to any MCP-capable agent.

The founder ruled for an adapter seam rather than owning one host -- MCP
is that seam. Every tool here calls the SAME functions `spoke.cli`
calls: `LedgerStore`, `validate` (via `LedgerStore.write`), `check_all`,
`render_preamble`. There is deliberately no second validation path -- a
second implementation of the gate is a second thing that can drift from
the first, and the whole point of this server is that an agent meets the
IDENTICAL gate a person does.

Dispatch has exactly one path, not two: `build_server()` builds one dict
of `{tool name: handler}` and hands it BOTH to the real `on_call_tool`
callback wired into the `Server` (what a live stdio session actually
calls) AND to `call_tool()` below (what tests, and any other in-process
caller, use). A mock of the dispatcher here would let the tests stay
green while the real stdio path silently diverges -- a mocked transport
that keeps a suite green over a dead component is exactly the failure
this file must not reproduce.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Callable

import mcp.types as types
from mcp.server.lowlevel import Server

from .checks.structural import check_all
from .config import Config
from .ledger import LedgerError, NODE_TYPES, Node, Relation, STATES
from .ledger.preamble import render_preamble
from .ledger import describe_skipped
from . import workspace
from .ledger.store import LedgerStore

HandlerFn = Callable[[dict], str]

# The same fixed fallback `spoke ledger preamble` defaults to (see
# cli.py's _DEFAULT_PREAMBLE_BUDGET) -- not yet the model-scaled budget
# spec S8.1 defines, just a usable default until that lands.
_PREAMBLE_BUDGET = 4000


# Imported, not forked. This used to be a byte-identical copy of
# cli._store_error, because importing cli pulls in a module that writes
# to stdout and stdout is the MCP wire. It lives in spoke.workspace now,
# which imports nothing that prints.
_store_error = workspace.store_error


def _provenance_by(args: dict) -> str:
    """The identity a write through this server is attributed to. Always
    `mcp:`- or `llm:`-prefixed, regardless of what the `by` argument
    names -- an MCP write must never read as a human's decision.
    Provenance is how a reader tells a machine's proposal from a
    person's; a node written by an agent but stamped `human:` would
    defeat that entirely.

    No `by` supplied at all becomes "mcp:unknown" -- never blank, and
    never a guess at a human name. A caller-supplied prefix other than
    mcp:/llm: (most pointedly `human:`) is stripped, not trusted."""
    raw = str(args.get("by") or "").strip()
    if not raw:
        return "mcp:unknown"
    if raw.startswith("mcp:") or raw.startswith("llm:"):
        return raw
    if ":" in raw:
        raw = raw.split(":", 1)[1].strip() or "unknown"
    return f"mcp:{raw}"


# --- read tools -------------------------------------------------------


def _tool_open_items(cfg: Config) -> str:
    err = _store_error(cfg.store_path)
    if err:
        return f"refused: {err}"
    store = LedgerStore(cfg.store_path)
    nodes = store.list_nodes()
    text, _stats = render_preamble(nodes, [], _PREAMBLE_BUDGET)
    if store.skipped:
        # A skip must never be silent -- same discipline cli._report_skipped
        # enforces for the CLI's own callers of list_nodes().
        text += "\n\n" + "\n".join(describe_skipped(store.skipped))
    return text


def _tool_ledger_show(cfg: Config, args: dict) -> str:
    err = _store_error(cfg.store_path)
    if err:
        return f"refused: {err}"
    name = str(args.get("name", ""))
    store = LedgerStore(cfg.store_path)
    try:
        node = store.read(name)
    except LedgerError as e:
        # A name that resolves outside ledger/ is a refusal, not a crash
        # -- the same containment check LedgerStore.read always runs.
        return f"refused: {e}"
    except FileNotFoundError:
        return f"refused: no such node {name!r}"

    lines = [f"{node.name}: {node.type} {node.state}", f"title: {node.title}"]
    if node.ruling:
        lines.append(f"ruling: {node.ruling}")
    if node.blocked_by:
        lines.append(f"blocked_by: {', '.join(node.blocked_by)}")
    for r in node.relations:
        lines.append(f"relation: {r.rel} {r.to}")
    lines.append("")
    lines.append(node.body)
    return "\n".join(lines)


def _tool_check(ws) -> str:
    cfg = ws.config
    err = _store_error(cfg.store_path)
    if err:
        return f"refused: {err}"
    findings = check_all(cfg.store_path, cfg.accepted_absences, ws.axes)
    if not findings:
        return "check: no structural defects"
    lines = [f"{f.kind}: {f.file}: {f.detail}" for f in findings]
    lines.append(f"\ncheck: {len(findings)} defect(s)")
    return "\n".join(lines)


def _tool_project(cfg: Config, project_name: str | None) -> str:
    lines = [f"project: {project_name or '(none -- no registry, single default store)'}"]
    lines.append(f"store: {cfg.store_path}")
    err = _store_error(cfg.store_path)
    if err:
        lines.append(f"warning: {err}")
    return "\n".join(lines)


# --- write tools --------------------------------------------------------
#
# The point of the whole server: an agent that defers, abandons, or
# deviates must supply a ruling; a blocked node must supply blocked_by.
# Both refusals come from validate() -- via LedgerStore.write() -- the
# SAME gate a human meets with `spoke ledger new`/`spoke ledger set`. A
# refused write runs validate() before anything reaches disk, so the
# store is byte-identical afterward; there is no separate, softer check
# here for an MCP caller to slip past.



def _after_write(res, line: str) -> str:
    """The success line plus every advisory -- the agent reading this
    must see a failed push as plainly as the CLI user does."""
    if not res.advisories:
        return line
    return line + "\n" + "\n".join(f"push failed - {a}" for a in res.advisories) + \
        "\nthe commit is local only; nobody else sees it until `git push` succeeds in the store"


def _tool_record(ws, args: dict) -> str:
    cfg = ws.config
    err = _store_error(cfg.store_path)
    if err:
        return f"refused: {err}"

    name = str(args.get("name", ""))
    node_type = str(args.get("type", ""))
    state = str(args.get("state", ""))
    title = str(args.get("title", ""))
    body = str(args.get("body", ""))
    ruling = args.get("ruling")
    blocked_by = tuple(args.get("blocked_by") or ())
    raw_relations = args.get("relations") or ()
    relations = tuple(
        Relation(rel=str(r.get("rel", "")), to=str(r.get("to", "")))
        for r in raw_relations
    )

    # Provenance names only the fields actually supplied on this call --
    # same "supplied" convention cli._cmd_ledger_new uses.
    supplied = ["type", "state", "title", "body"]
    if ruling is not None:
        supplied.append("ruling")
    if blocked_by:
        supplied.append("blocked_by")
    if relations:
        supplied.append("relations")

    by = _provenance_by(args)
    today = date.today().isoformat()
    provenance = tuple({"field": f, "by": by, "at": today} for f in supplied)

    node = Node(
        name=name,
        type=node_type,
        state=state,
        title=title,
        body=body,
        relations=relations,
        ruling=ruling,
        blocked_by=blocked_by,
        provenance=provenance,
        opened=today,
        updated=today,
        by=by,
        claimed_by=None,
        claimed_at=None,
    )

    res = ws.write(node)
    if not res.ok:
        # Every reason, not just the first -- same discipline as the CLI's
        # refusal path. The gate is validate(), the SAME function
        # LedgerStore.write() runs for a CLI-originated write; nothing
        # here is a second, softer check.
        return "refused:\n" + "\n".join(f"- {r}" for r in res.reasons)
    return _after_write(res, f"wrote {node.name} ({node.type}, {node.state})")


def _tool_update(ws, args: dict) -> str:
    cfg = ws.config
    err = _store_error(cfg.store_path)
    if err:
        return f"refused: {err}"

    store = LedgerStore(cfg.store_path)
    name = str(args.get("name", ""))
    try:
        node = store.read(name)
    except LedgerError as e:
        return f"refused: {e}"
    except FileNotFoundError:
        return f"refused: no such node {name!r}"

    if args.get("body") is not None and args.get("append_body") is not None:
        return "refused: body and append_body are mutually exclusive"

    changes: dict = {}
    supplied: list[str] = []
    if args.get("state") is not None:
        changes["state"] = str(args["state"])
        supplied.append("state")
    if args.get("ruling") is not None:
        changes["ruling"] = str(args["ruling"])
        supplied.append("ruling")
    if args.get("blocked_by") is not None:
        changes["blocked_by"] = tuple(args["blocked_by"])
        supplied.append("blocked_by")
    if args.get("body") is not None:
        changes["body"] = str(args["body"])
        supplied.append("body")
    if args.get("append_body") is not None:
        # Appends, never destroys: a ledger is a running record -- the
        # same distinction cli.py's --append-body/--body draws.
        changes["body"] = node.body + "\n\n" + str(args["append_body"])
        supplied.append("body")

    if not supplied:
        return (
            "refused: at least one of state, ruling, blocked_by, body, "
            "append_body is required"
        )

    by = _provenance_by(args)
    today = date.today().isoformat()
    changes["provenance"] = node.provenance + tuple(
        {"field": f, "by": by, "at": today} for f in supplied
    )
    changes["updated"] = today

    updated_node = replace(node, **changes)
    res = ws.write(updated_node, store)
    if not res.ok:
        return "refused:\n" + "\n".join(f"- {r}" for r in res.reasons)
    return _after_write(res, f"set {updated_node.name} ({updated_node.type}, {updated_node.state})")


# --- tool catalogue -------------------------------------------------------

def _tool_lens(ws, args: dict) -> str:
    """`ledger lens`, for an agent -- the SAME query and the SAME
    rendering the terminal gets.

    It used to be a second derivation with its own renderer, so the two
    could disagree, and did: the terminal had a `--flag` filter this did
    not, and they said different things about an empty result. An agent
    checking what governs the thing it is about to change should not get
    a different answer from the person who asked the same question.
    """
    from .ledger.board import BoardError, build_lens, lens_lines

    err = _store_error(ws.config.store_path)
    if err:
        return f"refused: {err}"
    try:
        hops = int(args.get("hops", 1))
    except (TypeError, ValueError):
        return f"refused: hops must be a whole number, got {args.get('hops')!r}"
    flag = args.get("flag") or ()
    if isinstance(flag, str):
        flag = (flag,)
    try:
        body = build_lens(
            LedgerStore(ws.config.store_path), str(args.get("name") or "product"),
            ws.clock, hops=hops, flag_filter=tuple(flag),
        )
    except BoardError as e:
        return f"refused: {e}"
    return "\n".join(lens_lines(body) + body["skipped_lines"])


def _tool_scale(ws, args: dict) -> str:
    """`spoke scale`, for an agent -- including the refusal."""
    from .ledger.board import BoardError, build_scale

    err = _store_error(ws.config.store_path)
    if err:
        return f"refused: {err}"
    if not ws.axes:
        return ("refused: this project has declared no capability axes. "
                "There is deliberately no built-in list: the axes that "
                "matter are the project's, not this tool's.")
    try:
        body = build_scale(
            LedgerStore(ws.config.store_path), ws.axes, ws.clock,
            node_type=str(args.get("type") or "product"),
            vocabulary=ws.vocabulary, max_value=ws.scale_max,
        )
    except BoardError as e:
        return f"refused: {e}"

    lines = []
    for row in body["rows"]:
        cells = ", ".join(
            f"{c['axis']}={c['value']}({c['basis']})" if c["value"] is not None
            else f"{c['axis']}=not scored"
            for c in row["cells"]
        )
        lines.append(f"{row['name']}: {cells}")
        if row["composite"] is None:
            lines.append(f"  NO COMPOSITE -- {row['withheld_reason']}")
        else:
            lines.append(f"  composite {row['composite']} / {body['max_value']}")
    if not lines:
        lines.append(f"no {body['kind']} nodes to score")
    lines += body["skipped_lines"]
    return "\n".join(lines)


def _tool_matrix(ws, args: dict) -> str:
    """`ledger matrix`, for an agent."""
    from .ledger.board import BoardError, build_matrix

    err = _store_error(ws.config.store_path)
    if err:
        return f"refused: {err}"
    try:
        body = build_matrix(
            LedgerStore(ws.config.store_path),
            str(args.get("rows") or "product"),
            str(args.get("cols") or "capability"),
            ws.clock, int(args.get("hops", 1)),
        )
    except BoardError as e:
        return f"refused: {e}"
    except (TypeError, ValueError):
        return f"refused: hops must be a whole number, got {args.get('hops')!r}"

    if not body["rows"] or not body["cols"]:
        # Even here: a cross with an empty side may still have had files
        # it could not read, and that is the more useful fact of the two.
        return "\n".join(
            ["matrix: one side of this cross has no nodes"] + body["skipped_lines"]
        )
    cells = {(c["row"], c["col"]): c for c in body["cells"]}
    lines = []
    for col in body["cols"]:
        covered = [r for r in body["rows"] if cells[(r, col)]["covered"]]
        lines.append(
            f"{col}: {len(covered)} of {len(body['rows'])}"
            + (f" - {', '.join(covered)}" if covered else "")
        )
    # The matrix tool rendered no skips at all -- so an agent asking for
    # coverage was told a number and never told what could not be read.
    lines += body["skipped_lines"]
    return "\n".join(lines)


_TOOL_SPECS: list[dict] = [
    {
        "name": "spoke_lens",
        "description": (
            "Ask the ledger a question through one lens: each hub node "
            "with its state, the flags raised on it WITH their origins, "
            "and its spokes. Use the 'decision' lens before changing "
            "anything governed by a ruling -- it reports open work that "
            "contradicts a decision on hold, which is exactly the thing "
            "a session cannot see from its own transcript."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "product, capability, decision, client or time"},
                "hops": {"type": "integer",
                         "description": "how far from each hub to pull spokes in (default 1)"},
                "flag": {"type": "array", "items": {"type": "string"},
                         "description": "show only hubs carrying these flag "
                                        "kinds, e.g. [\"blocked\"]"},
            },
        },
    },
    {
        "name": "spoke_scale",
        "description": (
            "The capability grid: every node's score per axis, with the "
            "BASIS of each (measured, asserted or unverified), and a "
            "composite -- or the stated reason there is none. A composite "
            "is withheld when too much of the weight is unmeasured, so a "
            "refusal here is an answer, not a failure."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string",
                         "description": "which node type to score (default product)"},
            },
        },
    },
    {
        "name": "spoke_matrix",
        "description": (
            "Cross two lenses and report coverage as 'N of M' per column "
            "-- recomputed, never remembered. Use it to answer 'how many "
            "of our products actually have X' without counting by hand."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "rows": {"type": "string", "description": "the lens on the rows"},
                "cols": {"type": "string", "description": "the lens on the columns"},
                "hops": {"type": "integer", "description": "radius (default 1)"},
            },
        },
    },
    {
        "name": "spoke_open_items",
        "description": (
            "The ranked open items on this project's ledger -- the same "
            "list a session sees automatically at SessionStart, available "
            "here on demand. Use this to catch up on open work: what is "
            "blocked, on hold, or an unruled deferral needing a decision, "
            "before starting anything new."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "spoke_ledger_show",
        "description": (
            "Show one ledger node in full: its state, ruling, blocked_by, "
            "relations, and body. Use this before acting on a specific "
            "item named by spoke_open_items or spoke_check, to read the "
            "full record instead of just its one-line summary."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "the node's name"}},
            "required": ["name"],
        },
    },
    {
        "name": "spoke_check",
        "description": (
            "Run the structural checks against this project's memory "
            "store: broken wikilinks, a stale or missing index, schema "
            "defects. Use this to find defects in the store itself, "
            "separate from the ledger's open items."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "spoke_project",
        "description": (
            "Which project is active and which memory store it resolved "
            "to. Call this FIRST, before writing anything -- an agent "
            "must be able to confirm what it is looking at before it "
            "changes it."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "spoke_record",
        "description": (
            "Create a new ledger node -- a decision, a deferral, an "
            "abandonment, a deviation, or a plain open item. Goes through "
            "the SAME gate a human meets on the command line: a "
            "deferred/abandoned/deviated state requires a non-empty "
            "ruling, a blocked state requires blocked_by, and a refused "
            "write leaves the store untouched. Always recorded under "
            "mcp: provenance, never as a human's."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "type": {"type": "string", "description": f"one of {list(NODE_TYPES)}"},
                "state": {"type": "string", "description": f"one of {list(STATES)}"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "ruling": {
                    "type": "string",
                    "description": "required when state is deferred, abandoned, or deviated",
                },
                "blocked_by": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "required (non-empty) when state is blocked",
                },
                "relations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"rel": {"type": "string"}, "to": {"type": "string"}},
                        "required": ["rel", "to"],
                    },
                },
                "by": {
                    "type": "string",
                    "description": (
                        "who/what is calling -- e.g. an agent's name. Always "
                        "recorded as mcp:<this>, never as a human's identity."
                    ),
                },
            },
            "required": ["name", "type", "state", "title", "body"],
        },
    },
    {
        "name": "spoke_update",
        "description": (
            "Change an existing ledger node's state, ruling, blocked_by, "
            "or body, through the SAME gate spoke_record uses -- the same "
            "gate a human meets with `spoke ledger set`. Use append_body "
            "for a running record (the common case); body replaces it "
            "outright, for correcting something wrong. A refused change "
            "leaves the store untouched."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "state": {"type": "string"},
                "ruling": {"type": "string"},
                "blocked_by": {"type": "array", "items": {"type": "string"}},
                "body": {"type": "string", "description": "replaces the body outright"},
                "append_body": {"type": "string", "description": "appended after the existing body"},
                "by": {
                    "type": "string",
                    "description": (
                        "who/what is calling. Always recorded as mcp:<this>, "
                        "never as a human's identity."
                    ),
                },
            },
            "required": ["name"],
        },
    },
]


def build_server(ws) -> Server:
    """Build an MCP server wired to `ws`'s ledger/memory store.

    Takes a Workspace rather than a `(Config, project name)` pair. It
    used to take the pair, and that was why the agent-facing write path
    could not honour `LedgerStore.write`'s axes invariant at all: a name
    is not a project, and `Config` carries no axes, so there was no route
    to them from here. A tool an agent drives must meet the same gate a
    human does, and now it structurally does.

    Every tool is a thin, synchronous, pure-text function stored in ONE
    dict (`handlers`). The real `on_call_tool` callback wired into the
    `Server` below dispatches through this exact dict, and so does
    `call_tool()` further down -- there is deliberately no second
    dispatch path (see module docstring).
    """
    cfg = ws.config
    project_name = ws.name
    handlers: dict[str, HandlerFn] = {
        "spoke_open_items": lambda args: _tool_open_items(cfg),
        "spoke_ledger_show": lambda args: _tool_ledger_show(cfg, args),
        "spoke_check": lambda args: _tool_check(ws),
        "spoke_project": lambda args: _tool_project(cfg, project_name),
        "spoke_lens": lambda args: _tool_lens(ws, args),
        "spoke_scale": lambda args: _tool_scale(ws, args),
        "spoke_matrix": lambda args: _tool_matrix(ws, args),
        "spoke_record": lambda args: _tool_record(ws, args),
        "spoke_update": lambda args: _tool_update(ws, args),
    }
    tools = [
        types.Tool(name=s["name"], description=s["description"], inputSchema=s["inputSchema"])
        for s in _TOOL_SPECS
    ]

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx, params):
        handler = handlers.get(params.name)
        if handler is None:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text", text=f"refused: unknown tool {params.name!r}"
                    )
                ],
                isError=True,
            )
        text = handler(params.arguments or {})
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    server = Server(
        "spoke",
        version="0.1.0",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    # Exposed so call_tool() (below) dispatches through the SAME handler
    # dict the real on_call_tool callback above closes over -- not a
    # reimplementation of it. This is the seam that keeps the test path
    # honest against the real MCP dispatch path (see module docstring).
    server._spoke_handlers = handlers  # type: ignore[attr-defined]
    return server


def call_tool(server: Server, name: str, arguments: dict) -> str:
    """Synchronous helper: dispatches through the exact handler dict
    `on_call_tool` was built from in build_server() above, so calling
    this exercises the real tool logic, not a parallel mock of it. Used
    by tests, and usable by any other in-process caller."""
    handler = server._spoke_handlers.get(name)  # type: ignore[attr-defined]
    if handler is None:
        return f"refused: unknown tool {name!r}"
    return handler(arguments or {})


def run_stdio(server: Server) -> None:
    """Run `server` over stdio until the client disconnects. stdio only --
    no HTTP, no SSE, no auth: this is a local tool over local files for a
    local agent, and none of that surface is needed here."""
    import anyio
    from mcp.server.stdio import stdio_server

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(_run)
