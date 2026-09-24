"""The token-budgeted preamble -- what a session sees before it starts.

Surfacing is capped by TOKENS, not item count. A count is the wrong unit --
it is meaningless across models and hides the real cost. Measured on a real
install, a memory index alone ran to thousands of tokens injected into every
session, and growing.

Ledger items and memory index lines compete inside ONE budget. That is
what makes the ranking real: nothing can be added without displacing
something, so the order this module produces is not a courtesy, it is
the actual admission list. Fill order is flag severity (blocked, hold,
then an unruled deferred -- a considered decision with no ruling is a
defect, not a plain deferral), then age, then the memory index lines --
every ledger item outranks every memory line, since only the ledger
carries a severity signal at all.

What does not fit is never silently dropped: one line names the
remainder (`+N more`), and the rendered text ends with a line reporting
its own real cost -- an invisible tax is exactly how the existing
preamble grew to thousands of tokens with nobody deciding it should.
"""
from __future__ import annotations

from datetime import date

from . import parse_iso_date, CLOSED_STATES, Node, is_unruled

# Characters per token, used only to keep the preamble roughly inside its
# budget across whatever model is actually in use -- see estimate_tokens.
_CHARS_PER_TOKEN = 4

_REMAINDER_TEMPLATE = "+{hidden} more — run `spoke ledger list`"


def estimate_tokens(text: str) -> int:
    """Estimate `text`'s token count.

    This is an ESTIMATE, not an exact tokenizer count: it divides the
    character length by a fixed ratio of 4 characters per token (a
    common rule of thumb for English prose -- see _CHARS_PER_TOKEN),
    rounded up. It does not run any model's real tokenizer, so it will
    be off for code, dense punctuation, or non-English text -- it exists
    to keep the preamble roughly inside its budget across whatever model
    is in use, not to predict a specific model's token count exactly.
    """
    if not text:
        return 0
    return -(-len(text) // _CHARS_PER_TOKEN)  # ceil division, no float rounding


def _severity(n: Node) -> int:
    """Lower sorts first -- i.e. more severe, more urgent to surface."""
    if n.state == "blocked" or _unmet(n):
        # An unmet expectation is the world contradicting a written
        # claim, or a claim nobody has checked. It ranks with a block.
        return 0
    if n.state == "hold":
        return 1
    if is_unruled(n):
        return 2
    if _machine_derived(n):
        # A scanned inventory fact -- "<repo>: product open" -- is the
        # least urgent thing here: it says a repo exists. Anything a
        # person wrote, a surface report included, outranks it. Without
        # this, a human's "this page is broken" sorted below forty lines
        # of what the scan found, and the budget hid it.
        return 4
    return 3


def _machine_derived(n: Node) -> bool:
    """Every provenance entry is a probe's, and there is at least one.
    A node nobody has touched by hand. The scan's own ownership rule is
    the same test, kept in scan/__init__.py because it also checks the
    body digest; here only authorship matters."""
    prov = n.provenance or ()
    return bool(prov) and all(str(e.get("by", "")).startswith("probe:") for e in prov)


def _rank_key(n: Node):
    # (severity, age, name): age is opened-date ascending -- an older item
    # has been waiting longer and outranks a newer one of equal severity.
    # A node with no recorded opened date sorts as oldest, rather than
    # being silently pushed to the back.
    #
    # Through parse_iso_date, NOT the raw field: `opened` reaches here as
    # a str from every writer, but as a `date` from any YAML file where
    # it is unquoted -- a hand edit, or one writer that passed a date
    # object -- and comparing the two raised TypeError inside the one
    # function whose job is to raise things. The preamble must not be
    # crashable by the shape of a field it only sorts on.
    opened = parse_iso_date(n.opened)
    return (_severity(n), opened or date.min, n.name)


def _unmet(n: Node) -> list[str]:
    out = []
    for e in n.expects:
        last = e.get("last") or {}
        if not last:
            out.append(f"{e.get('url')}: never checked")
        elif not last.get("ok"):
            out.append(f"{e.get('url')}: {last.get('detail')}")
    return out


def _node_line(n: Node) -> str:
    bits = [f"{n.name}: {n.type} {n.state}"]
    if n.title:
        bits.append(f"- {n.title}")
    for u in _unmet(n):
        bits.append(f"-- UNMET {u}")
    left = [c for c in n.checklist if not c.get("done")]
    if left:
        bits.append(f"-- {len(left)} of {len(n.checklist)} unchecked")
    if n.state == "blocked" and n.blocked_by:
        bits.append(f"(blocked by {', '.join(b for b in n.blocked_by if b.strip())})")
    if is_unruled(n):
        bits.append("-- MISSING RULING")
    return " ".join(bits)


def _is_open(n: Node) -> bool:
    """Whether `n` belongs in the preamble at all.

    FINDING 4: nothing used to filter closed states out of the preamble,
    so a `done` node consumed the same token budget the module's own
    docstring says is for open items -- and worse, an unruled `abandoned`
    or `deviated` node (a considered decision recorded with no ruling,
    which `validate()` would refuse on a *new* write) carried no marker
    here and could rank BELOW a `done` item, because only "deferred" was
    treated as unruled at all.

    A closed state (CLOSED_STATES) is excluded UNLESS it is unruled --
    an unruled closing is a defect, not a resolved item, and must still
    surface.
    """
    if n.state in CLOSED_STATES:
        return is_unruled(n)
    return True


def _assemble(lines: list[str], hidden: int, shown: int, budget_tokens: int) -> str:
    body_lines = list(lines)
    if hidden:
        body_lines.append(_REMAINDER_TEMPLATE.format(hidden=hidden))
    body = "\n".join(body_lines)

    # The footer reports the preamble's own real cost. That cost includes
    # the footer line itself -- an invisible tax is exactly the failure
    # this line exists to prevent -- so compute it, embed it, and if
    # embedding the number changed the text's length enough to change the
    # estimate (a digit added/removed), recompute once more. The estimate
    # is coarse enough (a fixed chars-per-token ratio) that this always
    # converges in one correction.
    footer = f"preamble: {estimate_tokens(body)} of {budget_tokens} tokens ({shown} shown, {hidden} hidden)"
    text = f"{body}\n{footer}" if body else footer
    used = estimate_tokens(text)
    recomputed_footer = f"preamble: {used} of {budget_tokens} tokens ({shown} shown, {hidden} hidden)"
    if recomputed_footer != footer:
        footer = recomputed_footer
        text = f"{body}\n{footer}" if body else footer
    return text


def render_preamble(
    ledger_nodes: list[Node], memory_index_lines: list[str], budget_tokens: int
) -> tuple[str, dict]:
    """Rank ledger nodes and memory index lines into one token budget.

    Returns the rendered text and a stats dict with `shown`, `hidden`,
    `tokens_used` (the estimated cost of the text actually returned,
    footer included) and `budget` (the budget passed in).
    """
    sorted_nodes = sorted((n for n in ledger_nodes if _is_open(n)), key=_rank_key)
    candidates = [_node_line(n) for n in sorted_nodes] + list(memory_index_lines)

    shown: list[str] = []
    for candidate in candidates:
        trial = shown + [candidate]
        trial_hidden = len(candidates) - len(trial)
        trial_text = _assemble(trial, trial_hidden, len(trial), budget_tokens)
        if estimate_tokens(trial_text) > budget_tokens:
            # Sequential fill in rank order, not bin-packing: the first
            # candidate that does not fit stops the fill. A later,
            # shorter candidate is not scavenged ahead of it -- that
            # would silently reorder the ranking this module exists to
            # make real.
            break
        shown = trial

    hidden = len(candidates) - len(shown)
    text = _assemble(shown, hidden, len(shown), budget_tokens)
    stats = {
        "shown": len(shown),
        "hidden": hidden,
        "tokens_used": estimate_tokens(text),
        "budget": budget_tokens,
    }
    return text, stats
