import pytest
from spoke.ledger import LedgerError, Node, Relation
from spoke.ledger.schema import parse_node, serialise_node, validate

MINIMAL = """---
name: wire-the-gate
type: item
state: open
title: Wire the entitlement gate into the folio product
---

What it is, why, and what would prove it done.
"""


def _node(**kw) -> Node:
    base = dict(name="a", type="item", state="open", title="A thing", body="why\n",
                relations=(), ruling=None, blocked_by=(), provenance=(),
                opened=None, updated=None, by=None, claimed_by=None, claimed_at=None)
    base.update(kw)
    return Node(**base)


def test_parses_the_minimal_node():
    n = parse_node(MINIMAL)
    assert n.name == "wire-the-gate"
    assert n.type == "item"
    assert n.state == "open"
    assert n.relations == ()
    assert "prove it done" in n.body


def test_round_trip_is_byte_identical():
    assert serialise_node(parse_node(MINIMAL)) == MINIMAL


def test_relations_are_a_flat_list_with_no_parent():
    text = MINIMAL.replace(
        "state: open\n",
        "state: open\nrelations:\n  - {rel: touches, to: alpha}\n  - {rel: touches, to: beta}\n",
    )
    n = parse_node(text)
    assert n.relations == (Relation("touches", "alpha"), Relation("touches", "beta"))
    assert not hasattr(n, "parent")


def test_deferred_without_a_ruling_is_refused():
    n = parse_node(MINIMAL.replace("state: open", "state: deferred"))
    reasons = validate(n)
    assert any("ruling" in r for r in reasons), reasons


def test_deferred_with_a_ruling_is_accepted():
    text = MINIMAL.replace("state: open", 'state: deferred\nruling: "not now; the store is being rewritten"')
    assert validate(parse_node(text)) == []


@pytest.mark.parametrize("state", ["abandoned", "deviated"])
def test_abandoned_and_deviated_also_require_a_ruling(state):
    n = parse_node(MINIMAL.replace("state: open", f"state: {state}"))
    assert any("ruling" in r for r in validate(n))


def test_blocked_without_blocked_by_is_refused():
    n = parse_node(MINIMAL.replace("state: open", "state: blocked"))
    assert any("blocked_by" in r for r in validate(n))


def test_blocked_with_blocked_by_is_accepted():
    text = MINIMAL.replace("state: open", "state: blocked\nblocked_by: [accounts-live-cutover]")
    assert validate(parse_node(text)) == []


def test_a_hold_may_not_carry_a_verified_date():
    # A hold is not resolved by time; only a named person lifts one.
    text = MINIMAL.replace("state: open", "state: hold\nverified: 2026-09-09")
    assert any("verified" in r for r in validate(parse_node(text)))


def test_an_unknown_state_is_refused():
    assert any("state" in r for r in validate(parse_node(MINIMAL.replace("state: open", "state: pondering"))))


def test_an_unknown_relation_type_is_refused():
    text = MINIMAL.replace("state: open\n", "state: open\nrelations:\n  - {rel: parent_of, to: alpha}\n")
    assert any("parent_of" in r for r in validate(parse_node(text)))


@pytest.mark.parametrize("bad", [
    "../MEMORY", "../../etc/passwd", "a/b", "a\\b", ".hidden", "", "..",
    "UPPER", "has space", "trailing/",
])
def test_an_unsafe_name_is_refused(bad):
    reasons = validate(_node(name=bad))
    assert any("name" in r for r in reasons), (bad, reasons)


@pytest.mark.parametrize("blank", ["", "   ", "\n", "\t"])
def test_a_blank_ruling_is_refused(blank):
    n = _node(state="deferred", ruling=blank)
    assert any("ruling" in r for r in validate(n))


@pytest.mark.parametrize("blanks", [[""], ["   "], ["", "  "]])
def test_blocked_by_of_blanks_is_refused(blanks):
    n = _node(state="blocked", blocked_by=tuple(blanks))
    assert any("blocked_by" in r for r in validate(n))


# --- Finding 2: parse_node() must raise on unparseable YAML and on a
# malformed relations entry, never silently produce a blank Node. ---

def test_parse_node_raises_on_unparseable_yaml():
    with pytest.raises(LedgerError):
        parse_node("---\nname: bad\ntype: item\nstate: open\nrelations: [touches\n---\n\nbroken\n")


def test_parse_node_raises_on_a_relation_that_is_not_a_mapping():
    text = MINIMAL.replace(
        "state: open\n", "state: open\nrelations: [touches]\n",
    )
    with pytest.raises(LedgerError):
        parse_node(text)


def test_parse_node_raises_on_a_relation_mapping_missing_to():
    text = MINIMAL.replace(
        "state: open\n", "state: open\nrelations:\n  - {rel: touches}\n",
    )
    with pytest.raises(LedgerError):
        parse_node(text)


# --- Finding 7: an empty or whitespace-only relation target is refused. ---

@pytest.mark.parametrize("empty_to", ["", "   "])
def test_a_relation_with_an_empty_to_is_refused(empty_to):
    n = _node(relations=(Relation(rel="touches", to=empty_to),))
    reasons = validate(n)
    assert any("to" in r for r in reasons), reasons


def test_a_relation_with_a_real_to_is_accepted():
    n = _node(relations=(Relation(rel="touches", to="some-product"),))
    assert validate(n) == []


def test_a_client_node_carrying_a_name_is_refused():
    # Client nodes carry an opaque id and a state only.
    text = MINIMAL.replace("type: item", "type: client").replace(
        "title: Wire the entitlement gate into the folio product", "title: Acme Newspapers Ltd"
    )
    reasons = validate(parse_node(text))
    assert any("client" in r.lower() for r in reasons), reasons


def test_every_state_another_table_names_is_a_state():
    """CLOSED_STATES and flags.STALE_NEVER both named `superseded` while
    STATES refused it -- so a state two tables reasoned about could never
    exist. The tables that mention a state must not outrun the one that
    admits it."""
    from spoke.ledger import CLOSED_STATES, RULING_REQUIRED_STATES, STATES
    from spoke.ledger.flags import STALE_MULTIPLIER, STALE_NEVER

    for table in (CLOSED_STATES, RULING_REQUIRED_STATES, STALE_NEVER, STALE_MULTIPLIER):
        assert set(table) <= set(STATES), set(table) - set(STATES)


def test_superseded_is_closed_never_stale_and_must_say_by_what():
    from spoke.ledger import CLOSED_STATES, RULING_REQUIRED_STATES
    from spoke.ledger.flags import STALE_NEVER

    assert "superseded" in CLOSED_STATES
    assert "superseded" in STALE_NEVER
    assert "superseded" in RULING_REQUIRED_STATES


def test_a_checklist_note_survives_the_round_trip():
    """The four questions ask what was observed and what would fail
    silently. Both the parser and the serialiser built checklist items as
    {text, done} only, so every answer was dropped on the way to disk:
    `tick --note` printed success and stored nothing, and 20 of 20 ticked
    items in the real store carried no evidence at all.

    A checklist that records only that somebody ticked a box is the
    decoration the four questions exist to replace.
    """
    from spoke.ledger.schema import parse_node, serialise_node

    node = _node(checklist=({"text": "prove it", "done": True,
                             "note": "observed live: HTTP 200, dated 2026-09-26"},
                            {"text": "not yet", "done": False}))
    text = serialise_node(node)
    assert "observed live" in text, "the note must reach the file"

    back = parse_node(text)
    assert back.checklist[0]["note"] == "observed live: HTTP 200, dated 2026-09-26"
    assert back.checklist[0]["done"] is True
    # An item with no note keeps none -- absent is not an empty string.
    assert "note" not in back.checklist[1]
