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
