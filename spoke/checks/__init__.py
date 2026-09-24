"""What every check shares: the finding it reports, and the two rules
about record text they all have to agree on.

The regexes below were each written out twice. `LINK` lived in
structural.py (deciding which wikilinks are broken) and in relate.py
(deciding which links are clustering signals), so the two could have
disagreed about what a link even is. And the code-stripping rule was
worse than duplicated -- it was asymmetric: structural.py stripped fenced
blocks AND inline spans, staleness.py stripped only inline spans, so a
pull-request citation inside a fenced block had its claim keywords
matched out of surrounding code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

LINK = re.compile(r"\[\[([^\]]+)\]\]")
CODE_SPAN = re.compile(r"`[^`\n]*`")
CODE_FENCE = re.compile(r"^```[^\n]*\n.*?^```[ \t]*$", re.DOTALL | re.MULTILINE)


def strip_code(text: str, replacement: str = "") -> str:
    """`text` with fenced blocks and inline spans removed.

    Fences first: they span newlines, so leaving them in would hide a
    wikilink from the single-line CODE_SPAN pattern, or let CODE_SPAN
    pair a fence's opening backticks with a later inline span.

    `replacement` is a parameter because the two callers genuinely differ.
    Structural checks delete the code outright; the staleness check
    substitutes a space, because it matches claim keywords in a window of
    prose and deleting a span there could weld two words into one.
    """
    return CODE_SPAN.sub(replacement, CODE_FENCE.sub(replacement, text))


# Which files in the store are NOT memory records. Defined here rather
# than in store.py because store.py imports this package, so the reverse
# import would be a cycle -- and because "what counts as a record" is a
# fact both the store and every check must agree on. They each kept a
# copy: adding a name to one would have made the gate report `unindexed`
# for a file the store declines to list.
NOT_A_MEMORY = {"MEMORY.md", "README.md"}


@dataclass(frozen=True)
class Finding:
    kind: str
    file: str
    detail: str
