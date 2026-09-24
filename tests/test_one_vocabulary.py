"""Every vocabulary in this codebase has exactly one definition.

This project has paid for the alternative twice. `cli.py` and
`preamble.py` once kept separate ideas of "unruled" and disagreed; the
constant written to end that had a THIRD copy inside `validate()`, the
one place that decides whether a write is refused. These tests assert
agreement rather than trusting a comment that claims it.

Each test names the wrong answer the drift would produce, because a
constant-equality assertion with no reason attached is the first thing
someone deletes when it goes red.
"""
import re
from pathlib import Path

import pytest

from spoke.ledger import (
    CLOSED_STATES, RULING_REQUIRED_STATES, STATES, Node, is_unruled,
)
from spoke.ledger.board import FLAG_KINDS as BOARD_FLAG_KINDS, SEVERITY
from spoke.ledger.flags import FLAG_KINDS, STALE_MULTIPLIER, STALE_NEVER
from spoke.ledger.schema import validate

SRC = Path(__file__).resolve().parent.parent / "spoke"


def _node(**kw) -> Node:
    base = dict(
        name="thing", type="item", state="open", title="A thing", body="body",
        relations=(), ruling=None, blocked_by=(), provenance=(),
        opened=None, updated=None, by=None, claimed_by=None, claimed_at=None,
    )
    base.update(kw)
    return Node(**base)


# ── states ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("state", STATES)
def test_the_gate_and_the_display_agree_about_unruled(state):
    """Drift here means a state every surface marks [UNRULED] is one the
    gate accepts with no ruling -- a defect visible everywhere and
    refused nowhere."""
    node = _node(state=state, ruling=None, blocked_by=("x",) if state == "blocked" else ())
    refused = any("requires a non-empty ruling" in r for r in validate(node))
    assert refused == is_unruled(node), state


def test_every_state_has_a_staleness_answer():
    """STALE_MULTIPLIER and STALE_NEVER cover STATES between them, and
    they are held together by a `.get(state, 1.0)` default. Add a state
    and it silently becomes ordinary open work -- no error, no failure,
    just a threshold nobody chose."""
    covered = set(STALE_MULTIPLIER) | set(STALE_NEVER)
    assert set(STATES) <= covered, set(STATES) - covered


def test_no_state_table_names_a_state_the_validator_refuses():
    """This used to pin `superseded` as the ONE deliberate extra: named
    by CLOSED_STATES and STALE_NEVER, refused by STATES, so that adding
    it had to be a decision. It was decided 2026-09-13: a replaced
    decision is a different fact from a dropped one, and the scan was
    collapsing the two. Now no table may outrun the validator."""
    extra = (set(STALE_MULTIPLIER) | set(STALE_NEVER) | set(CLOSED_STATES)) - set(STATES)
    assert extra == set(), extra


def test_the_states_requiring_a_ruling_are_named_once():
    """Nothing outside ledger/__init__.py may spell this tuple out."""
    literal = re.compile(r'"deferred"\s*,\s*"abandoned"\s*,\s*"deviated"(\s*,\s*"superseded")?')
    offenders = [
        str(f.relative_to(SRC)) for f in SRC.rglob("*.py")
        if f.name != "__init__.py" and literal.search(f.read_text())
    ]
    assert offenders == [], offenders
    assert RULING_REQUIRED_STATES == ("deferred", "abandoned", "deviated", "superseded")


# ── flag kinds ────────────────────────────────────────────────────────

def test_every_flag_kind_has_a_severity():
    """A kind with no severity is coloured by a dict `.get` default, so a
    new one would render amber and mean nothing."""
    assert set(SEVERITY) == set(FLAG_KINDS)


def test_the_board_does_not_keep_its_own_flag_list():
    assert BOARD_FLAG_KINDS is FLAG_KINDS


def test_flags_py_produces_exactly_the_kinds_it_declares():
    """The kinds are emitted as string literals scattered through
    compute_flags and _absent_flags. If one is emitted that FLAG_KINDS
    does not name, every filter in the app refuses a flag the engine
    raises."""
    source = (SRC / "ledger" / "flags.py").read_text()
    emitted = set(re.findall(r'Flag\(\s*"([a-z]+)"', source))
    emitted |= set(re.findall(r'"([a-z]+)", (?:cap|n\.name)', source))
    assert emitted <= set(FLAG_KINDS), emitted - set(FLAG_KINDS)


def test_no_module_hardcodes_the_flag_kinds():
    """cli.py kept the six as a literal set to validate `--flag`, so a
    seventh kind was accepted by the board and refused by the terminal."""
    literal = re.compile(r'"blocked"\s*,\s*"held"|"held"\s*,\s*"deviates"')
    offenders = [
        str(f.relative_to(SRC)) for f in SRC.rglob("*.py")
        if f.name != "flags.py" and literal.search(f.read_text())
    ]
    assert offenders == [], offenders


# ── shared regexes and predicates ─────────────────────────────────────

def test_the_link_pattern_is_defined_once():
    """Two definitions of what a wikilink IS: one decided which links
    were broken, the other which were clustering signals."""
    # anchored: INDEX_LINK is a different pattern and contains "LINK"
    pattern = re.compile(r"^LINK\s*=\s*re\.compile", re.M)
    hits = [str(f.relative_to(SRC)) for f in SRC.rglob("*.py") if pattern.search(f.read_text())]
    assert hits == ["checks/__init__.py"], hits


def test_not_a_memory_is_defined_once():
    pattern = re.compile(r"NOT_A_MEMORY\s*=\s*\{")
    hits = [str(f.relative_to(SRC)) for f in SRC.rglob("*.py") if pattern.search(f.read_text())]
    assert hits == ["checks/__init__.py"], hits


def test_code_stripping_is_symmetric_across_the_checks():
    """staleness.py stripped inline spans only, so a citation inside a
    fenced block had its claim keywords matched from surrounding code."""
    from spoke.checks import strip_code

    fenced = "before\n```\nNOT MERGED yet\n```\nafter"
    assert "NOT MERGED" not in strip_code(fenced)
    assert "NOT MERGED" not in strip_code(fenced, " ")
    # and the space form does not weld words together
    assert "beforeafter" not in strip_code("before`x`after", " ")


def test_read_and_write_agree_about_a_safe_node_name(tmp_path):
    """`read("Foo")` used to succeed while `write` on the identical name
    was refused: the path check allowed uppercase and the schema regex
    did not. Two independent checks are a safeguard only while they
    agree about what they are checking."""
    import subprocess
    from spoke.ledger.store import LedgerStore

    root = tmp_path / "store"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "MEMORY.md").write_text("- [a](a.md) - hook\n")
    store = LedgerStore(root)

    for name in ("Foo", ".hidden", "with space"):
        assert store._safe_node_path(name) is None, name
        assert any("not a safe slug" in r for r in validate(_node(name=name))), name
