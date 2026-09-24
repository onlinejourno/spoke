import textwrap
from spoke.frontmatter import parse, serialise

SAMPLE = textwrap.dedent("""\
    ---
    name: example-record
    description: "A description with: a colon"
    metadata:
      node_type: memory
      type: project
    ---

    Body text with [[a-wikilink]] and a URL https://example.invalid/x.

    - a list item
    """)


def test_parses_meta_and_body():
    r = parse(SAMPLE)
    assert r.meta["name"] == "example-record"
    assert r.meta["metadata"]["type"] == "project"
    assert "a list item" in r.body


def test_round_trip_is_byte_identical():
    assert serialise(parse(SAMPLE)) == SAMPLE


def test_body_only_file_round_trips():
    text = "no frontmatter here\n"
    r = parse(text)
    assert r.meta == {}
    assert serialise(r) == text


def test_editing_body_preserves_frontmatter_formatting():
    r = parse(SAMPLE)
    r.body = r.body.replace("Body text", "Edited text")
    out = serialise(r)
    assert 'description: "A description with: a colon"' in out
    assert "Edited text" in out


# Synthetic test for unparseable frontmatter (does not depend on real store)
MALFORMED = (
    "---\n"
    "name: broken\n"
    "description: A scalar with a second: colon inside it\n"
    "---\n"
    "\nbody text\n"
)


def test_unparseable_frontmatter_round_trips_byte_identical():
    assert serialise(parse(MALFORMED)) == MALFORMED


def test_unparseable_frontmatter_is_distinguishable_from_absent():
    broken = parse(MALFORMED)
    absent = parse("just a body, no frontmatter\n")
    assert broken.unparsed is True
    assert absent.unparsed is False
    # Both have empty meta, so meta alone cannot tell them apart.
    assert broken.meta == {} and absent.meta == {}


import os
import pytest
from pathlib import Path

# Opt-in only. This test reads a REAL memory store -- one another session
# may be writing concurrently -- so it must never run by accident:
#   - No dependency on config.toml or the current working directory: the
#     store path used to come from `load_config(Path("config.toml"))`,
#     which is CWD-dependent and, worse, was evaluated in the @skipif
#     decorator at COLLECTION time, before conftest.py's autouse fixture
#     (or anything else) could run -- so it silently read all 215 live
#     records on every suite run, whatever directory pytest happened to be
#     invoked from.
#   - A dedicated env var, SPOKE_REAL_STORE, which conftest.py does NOT
#     default or neutralise (unlike SPOKE_REGISTRY / SPOKE_CLAUDE_PROJECTS),
#     so this test stays opted OUT unless a human deliberately opts it in.
#   - When unset, the skip reason says plainly what was NOT verified --
#     this project forbids a silent skip.
SPOKE_REAL_STORE = os.environ.get("SPOKE_REAL_STORE")


@pytest.mark.skipif(
    not SPOKE_REAL_STORE,
    reason=(
        "real-store round-trip NOT RUN: set SPOKE_REAL_STORE=<path> to "
        "verify against a real store"
    ),
)
def test_every_real_record_round_trips_byte_identical():
    store = Path(SPOKE_REAL_STORE).expanduser()
    failures = []
    for f in sorted(store.glob("*.md")):
        text = f.read_text()
        if serialise(parse(text)) != text:
            failures.append(f.name)
    assert failures == [], f"round-trip changed these files: {failures}"
