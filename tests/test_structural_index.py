from spoke.checks.structural import check_index, check_schema, check_all
from spoke.frontmatter import parse

# Same malformed shape proven in tests/test_frontmatter.py: an unquoted
# plain scalar with a second ": " inside it is not valid YAML, so this
# frontmatter block parses with unparsed=True and meta == {}.
MALFORMED_FRONTMATTER = (
    "---\n"
    "name: broken\n"
    "description: A scalar with a second: colon inside it\n"
    "---\n"
    "\nbody text\n"
)


def test_index_line_pointing_at_missing_file(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n- [B](b.md) — hook\n")
    (tmp_path / "a.md").write_text("x\n")
    findings = check_index(tmp_path)
    assert [(f.kind, f.detail) for f in findings] == [("index-target-missing", "b.md")]


def test_file_with_no_index_line(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text("x\n")
    (tmp_path / "orphan.md").write_text("x\n")
    findings = check_index(tmp_path)
    assert [(f.kind, f.detail) for f in findings] == [("unindexed", "orphan.md")]


def test_readme_and_index_are_not_orphans(tmp_path):
    (tmp_path / "MEMORY.md").write_text("")
    (tmp_path / "README.md").write_text("x\n")
    assert check_index(tmp_path) == []


def test_a_ledger_node_needs_no_memory_md_index_line(tmp_path):
    # MEMORY.md indexes memories, not ledger nodes -- a ledger node must
    # not be reported as unindexed just because it has no index line.
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text("x\n")
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / "n.md").write_text(
        "---\nname: n\ntype: item\nstate: open\ntitle: T\n---\n\nbody\n")
    assert check_index(tmp_path) == []


def test_schema_drift_type_at_top_level(tmp_path):
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\ntype: user\n---\n\nbody\n")
    findings = check_schema(tmp_path)
    assert ("schema-type-at-top-level", "a.md") in [(f.kind, f.file) for f in findings]


def test_schema_snake_case_filename(tmp_path):
    (tmp_path / "user_profile.md").write_text("---\nname: x\ndescription: d\nmetadata:\n  type: user\n---\n\nb\n")
    findings = check_schema(tmp_path)
    assert ("schema-snake-case-name", "user_profile.md") in [(f.kind, f.file) for f in findings]


def test_schema_missing_description(tmp_path):
    (tmp_path / "a.md").write_text("---\nname: a\nmetadata:\n  type: user\n---\n\nb\n")
    findings = check_schema(tmp_path)
    assert ("schema-missing-field", "a.md") in [(f.kind, f.file) for f in findings]


def test_check_all_aggregates(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\nmetadata:\n  type: user\n---\n\n[[nope]]\n")
    kinds = {f.kind for f in check_all(tmp_path, accepted=())}
    assert "broken-wikilink" in kinds


def test_unparseable_frontmatter_is_reported_distinctly_not_as_missing(tmp_path):
    # Confirm the fixture genuinely fails YAML parsing before relying on it.
    record = parse(MALFORMED_FRONTMATTER)
    assert record.unparsed is True
    assert record.meta == {}

    (tmp_path / "a.md").write_text(MALFORMED_FRONTMATTER)
    findings = check_schema(tmp_path)
    kinds = [(f.kind, f.file) for f in findings]
    assert ("schema-unparseable-frontmatter", "a.md") in kinds
    assert ("schema-missing-frontmatter", "a.md") not in kinds


def test_no_frontmatter_block_at_all_is_reported_as_missing(tmp_path):
    (tmp_path / "a.md").write_text("just a body, no frontmatter\n")
    findings = check_schema(tmp_path)
    kinds = [(f.kind, f.file) for f in findings]
    assert ("schema-missing-frontmatter", "a.md") in kinds
    assert ("schema-unparseable-frontmatter", "a.md") not in kinds


def test_empty_required_field_counts_as_missing(tmp_path):
    (tmp_path / "a.md").write_text('---\nname: ""\ndescription: d\n---\n\nbody\n')
    kinds = [(f.kind, f.detail) for f in check_schema(tmp_path)]
    assert ("schema-missing-field", "name") in kinds


def test_whitespace_only_required_field_counts_as_missing(tmp_path):
    (tmp_path / "a.md").write_text('---\nname: "   "\ndescription: d\n---\n\nbody\n')
    kinds = [(f.kind, f.detail) for f in check_schema(tmp_path)]
    assert ("schema-missing-field", "name") in kinds


def test_present_non_empty_field_is_not_reported(tmp_path):
    (tmp_path / "a.md").write_text('---\nname: real-name\ndescription: d\n---\n\nbody\n')
    assert [f for f in check_schema(tmp_path) if f.detail in ("name", "description")] == []


# Claude Code's loader stops reading MEMORY.md at 200 lines or 25,000
# bytes, whichever comes first; everything past that never reaches a
# session, and nothing turns red. These pin the check that makes it red.
def _index_of(tmp_path, lines: int, width: int = 20):
    (tmp_path / "a.md").write_text("x\n")
    body = ["- [A](a.md) — hook"] + ["x" * width] * (lines - 1)
    (tmp_path / "MEMORY.md").write_text("\n".join(body) + "\n")


def _budget(tmp_path):
    return [(f.kind, f.detail) for f in check_index(tmp_path) if "budget" in f.kind]


def test_index_comfortably_under_budget_is_quiet(tmp_path):
    _index_of(tmp_path, 150)
    assert _budget(tmp_path) == []


def test_index_past_line_limit_is_over_budget(tmp_path):
    _index_of(tmp_path, 201)
    assert ("index-over-budget", "lines: past 200, tail entries are not loaded") in _budget(tmp_path)


def test_index_near_line_limit_warns(tmp_path):
    _index_of(tmp_path, 194)
    assert _budget(tmp_path) == [("index-near-budget", "lines: within 10 of the 200 limit")]


def test_index_past_byte_limit_is_over_budget(tmp_path):
    _index_of(tmp_path, 100, width=260)  # ~26 KB in 100 lines
    assert _budget(tmp_path) == [
        ("index-over-budget", "bytes: past 25000, tail entries are not loaded")]


def test_index_near_byte_limit_warns(tmp_path):
    _index_of(tmp_path, 100, width=235)  # ~23.6 KB
    assert _budget(tmp_path) == [("index-near-budget", "bytes: within 2000 of the 25000 limit")]


def test_budget_detail_is_stable_as_the_index_grows(tmp_path):
    # doctor keys findings on kind|file|detail; a count in the detail
    # would re-report the same condition as NEW every day it changes.
    _index_of(tmp_path, 195)
    first = _budget(tmp_path)
    _index_of(tmp_path, 197)
    assert _budget(tmp_path) == first
