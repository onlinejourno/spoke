from spoke.checks.structural import check_wikilinks


def _write(d, name, text):
    (d / name).write_text(text)


def test_reports_missing_target(tmp_path):
    _write(tmp_path, "a.md", "see [[b]] and [[missing-one]]\n")
    _write(tmp_path, "b.md", "hello\n")
    findings = check_wikilinks(tmp_path, accepted=())
    assert [f.detail for f in findings] == ["missing-one"]
    assert findings[0].file == "a.md"
    assert findings[0].kind == "broken-wikilink"


def test_accepted_absence_is_not_reported(tmp_path):
    _write(tmp_path, "a.md", "see [[known-gap]]\n")
    assert check_wikilinks(tmp_path, accepted=("known-gap",)) == []


def test_link_with_md_extension_is_reported_as_malformed(tmp_path):
    _write(tmp_path, "a.md", "see [[b.md]]\n")
    _write(tmp_path, "b.md", "hello\n")
    findings = check_wikilinks(tmp_path, accepted=())
    assert findings[0].kind == "malformed-wikilink"


def test_path_like_link_is_ignored(tmp_path):
    _write(tmp_path, "a.md", "see [[../outside/thing.md]]\n")
    assert check_wikilinks(tmp_path, accepted=()) == []


def test_non_memory_files_are_not_scanned(tmp_path):
    # README documents the link format; MEMORY.md is the index. Neither is a memory.
    _write(tmp_path, "README.md", "Link related memories with [[slug]].\n")
    _write(tmp_path, "MEMORY.md", "- [A](a.md) - hook\n")
    _write(tmp_path, "a.md", "clean\n")
    assert check_wikilinks(tmp_path, accepted=()) == []


def test_link_inside_a_code_span_is_ignored(tmp_path):
    # gitleaks TOML uses [[allowlist]] headers; that is syntax, not a link.
    _write(tmp_path, "a.md", "gitleaks ignores the `[[allowlists]]` block entirely\n")
    assert check_wikilinks(tmp_path, accepted=()) == []


def test_link_inside_a_fenced_block_is_ignored(tmp_path):
    _write(tmp_path, "a.md", "prose\n\n```yaml\nsee [[not-a-real-memory]]\n```\n\nmore prose\n")
    assert check_wikilinks(tmp_path, accepted=()) == []


def test_link_outside_a_fence_is_still_reported(tmp_path):
    _write(tmp_path, "a.md", "```\ncode [[ignored]]\n```\n\nreal [[also-missing]] here\n")
    findings = check_wikilinks(tmp_path, accepted=())
    assert [f.detail for f in findings] == ["also-missing"]


def test_a_memory_may_link_to_a_ledger_node(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [a](a.md) - hook\n")
    _write(tmp_path, "a.md", "governed by [[a-decision]]\n")
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / "a-decision.md").write_text(
        "---\nname: a-decision\ntype: decision\nstate: hold\ntitle: T\n---\n\nbody\n")
    assert check_wikilinks(tmp_path, accepted=()) == []


def test_a_ledger_node_may_link_to_a_memory(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [a](a.md) - hook\n")
    _write(tmp_path, "a.md", "body\n")
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / "n.md").write_text(
        "---\nname: n\ntype: item\nstate: open\ntitle: T\n---\n\nsee [[a]]\n")
    assert check_wikilinks(tmp_path, accepted=()) == []


def test_a_genuinely_missing_target_is_still_reported_from_either_side(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [a](a.md) - hook\n")
    _write(tmp_path, "a.md", "see [[ghost]]\n")
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / "n.md").write_text(
        "---\nname: n\ntype: item\nstate: open\ntitle: T\n---\n\nsee [[phantom]]\n")
    details = sorted(f.detail for f in check_wikilinks(tmp_path, accepted=()))
    assert details == ["ghost", "phantom"]


def test_a_store_with_no_ledger_directory_still_works(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [a](a.md) - hook\n")
    _write(tmp_path, "a.md", "clean\n")
    assert check_wikilinks(tmp_path, accepted=()) == []
