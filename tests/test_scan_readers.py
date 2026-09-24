import pytest
import hashlib
import subprocess

from tests.conftest import build_repo
from pathlib import Path

from spoke.scan.readers import (
    duplicated_files, read_decisions, read_docs, read_git, read_units,
    tracked_files,
)


def _repo(root: Path, files: dict[str, str], gitignore: str = "") -> Path:
    if gitignore:
        files = {".gitignore": gitignore, **files}
    return build_repo(root, files)



def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if ".git/" in str(p) or p.name == ".git":
            continue
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def test_layout_markers_name_the_units(tmp_path):
    r = _repo(tmp_path / "mono", {
        "apps/alpha/package.json": '{"name": "alpha"}',
        "apps/beta/package.json": '{"name": "beta"}',
        "package.json": '{"name": "the-monorepo"}',
    })
    files, _ = tracked_files(r)
    units = read_units(r, files)
    assert [u.label for u in units] == ["product", "app", "app"]
    # bare, not repo-prefixed: a distinctive directory name is the name
    # in use, and prefixing it produced identifiers nothing else matched
    assert {u.name for u in units} == {"mono", "alpha", "beta"}
    # the root manifest must NOT collapse the monorepo into one unit: the
    # whole is added ALONGSIDE its parts, never instead of them
    assert [u.path for u in units if u.path != "."] == ["apps/alpha", "apps/beta"]


def test_a_root_manifest_is_one_unit_when_there_is_no_layout_marker(tmp_path):
    r = _repo(tmp_path / "solo", {"pyproject.toml": '[project]\nname = "solo-tool"\n'})
    files, _ = tracked_files(r)
    units = read_units(r, files)
    assert [(u.name, u.label, u.path) for u in units] == [("solo-tool", "package", ".")]


def test_the_manifests_own_word_is_the_label(tmp_path):
    r = _repo(tmp_path / "rusty", {"Cargo.toml": '[package]\nname = "rusty"\n'})
    files, _ = tracked_files(r)
    assert read_units(r, files)[0].label == "crate"


def test_an_adr_directory_yields_decisions_with_their_status(tmp_path):
    r = _repo(tmp_path / "p", {
        "docs/adr/0001-pick-a-db.md": "# Pick a database\n\n## Status\n\nAccepted\n",
        "docs/adr/0002-drop-it.md": "# Drop it\n\nStatus: Rejected\n",
    })
    files, _ = tracked_files(r)
    d = {x.name: x for x in read_decisions(r, files)}
    assert d["p-0001-pick-a-db"].status == "accepted"
    assert d["p-0001-pick-a-db"].title == "Pick a database"
    assert d["p-0002-drop-it"].status == "rejected"


def test_a_numbered_md_with_a_status_is_an_adr_without_an_adr_directory(tmp_path):
    """Requiring the conventional directory would make the scan silently
    empty for a project that keeps ADRs elsewhere."""
    r = _repo(tmp_path / "q", {"notes/0007-a-choice.md": "# A choice\n\nStatus: Proposed\n"})
    files, _ = tracked_files(r)
    got = read_decisions(r, files)
    assert [(x.title, x.status) for x in got] == [("A choice", "proposed")]


def test_a_numbered_md_without_a_status_is_not_an_adr(tmp_path):
    r = _repo(tmp_path / "q2", {"notes/0007-just-notes.md": "# Just notes\n\nnothing\n"})
    files, _ = tracked_files(r)
    assert read_decisions(r, files) == []


def test_the_scan_writes_nothing_into_the_repo(tmp_path):
    r = _repo(tmp_path / "p", {
        "README.md": "# hi\n",
        "docs/adr/0001-x.md": "# X\n\nStatus: Accepted\n",
        "package.json": '{"name": "p"}',
    })
    before = _tree_hash(r)
    files, _ = tracked_files(r)
    read_units(r, files); read_decisions(r, files); read_docs(r, files); read_git(r)
    duplicated_files({r: files})
    assert _tree_hash(r) == before


def test_ignored_paths_and_dotenv_are_never_listed(tmp_path):
    r = _repo(
        tmp_path / "p",
        {"README.md": "# hi\n", "secret/keep.txt": "x\n"},
        gitignore="secret/\n",
    )
    (r / ".env").write_text("TOKEN=abc\n")
    (r / "secret").mkdir(exist_ok=True)
    (r / "secret" / "keep.txt").write_text("x\n")
    files, _ = tracked_files(r)
    assert not any(f.startswith("secret/") for f in files), files
    assert not any(".env" in f for f in files), files


def test_identical_files_across_two_repos_are_reported_as_duplication(tmp_path):
    body = "shared\n" * 60          # over MIN_DUPLICATE_BYTES; see the reader
    a = _repo(tmp_path / "a", {"vendor/nav.js": body, "own.txt": "a\n"})
    b = _repo(tmp_path / "b", {"lib/nav.js": body, "own.txt": "b\n"})
    fa, _ = tracked_files(a)
    fb, _ = tracked_files(b)
    dupes = duplicated_files({a: fa, b: fb})
    paths = sorted(rel for hits in dupes.values() for _, rel in hits)
    assert paths == ["lib/nav.js", "vendor/nav.js"]


def test_a_file_repeated_inside_one_repo_is_not_duplication(tmp_path):
    """The signal is code shared ACROSS repos. Two copies inside one repo
    are that repo's business."""
    a = _repo(tmp_path / "a", {"x/nav.js": "shared\n" * 60, "y/nav.js": "shared\n" * 60})
    fa, _ = tracked_files(a)
    assert duplicated_files({a: fa}) == {}


def test_listing_order_does_not_change_the_result(tmp_path):
    r = _repo(tmp_path / "mono", {
        "apps/zeta/package.json": '{"name": "zeta"}',
        "apps/alpha/package.json": '{"name": "alpha"}',
    })
    files, _ = tracked_files(r)
    assert read_units(r, files) == read_units(r, list(reversed(files)))


def test_a_directory_that_is_not_a_git_repo_is_a_note_not_silence(tmp_path):
    plain = tmp_path / "plain"
    (plain / "docs").mkdir(parents=True)
    (plain / "README.md").write_text("# hi\n")
    files, notes = tracked_files(plain)
    assert "README.md" in files
    assert notes and "not a git repository" in notes[0]


def test_git_facts_are_none_rather_than_guessed(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert read_git(plain) == {"remote": None, "default_branch": None, "last_commit": None}


def test_a_deployable_repo_with_no_package_manifest_is_still_a_unit(tmp_path):
    """Found by running the scan for real: a live Python service with a
    Dockerfile, a fly.toml and requirements.txt but no pyproject.toml was
    reported as no unit at all, so the map said the repo did not exist."""
    r = _repo(tmp_path / "svc", {
        "requirements.txt": "flask\n",
        "Dockerfile": "FROM python\n",
        "fly.toml": 'app = "svc"\n',
        "svc/__init__.py": "",
    })
    files, _ = tracked_files(r)
    units = read_units(r, files)
    assert [(u.name, u.label) for u in units] == [("svc", "package")]


def test_a_container_manifest_alone_is_enough_to_be_a_unit(tmp_path):
    r = _repo(tmp_path / "boxed", {"Dockerfile": "FROM scratch\n", "run.sh": "echo hi\n"})
    files, _ = tracked_files(r)
    assert [(u.name, u.label) for u in read_units(r, files)] == [("boxed", "image")]


def test_conventional_and_tiny_files_are_not_reported_as_shared_code(tmp_path):
    """Two MIT licences are not a cross-cutting concern, and every empty
    __init__.py in the world hashes the same. Found by running the scan
    for real: the top result was a group of empty files."""
    body = "x = 1\n" * 60          # comfortably over MIN_DUPLICATE_BYTES
    a = _repo(tmp_path / "a", {
        "LICENSE.md": "MIT\n" * 60, "pkg/__init__.py": "", "vendor/nav.js": body,
    })
    b = _repo(tmp_path / "b", {
        "LICENSE.md": "MIT\n" * 60, "pkg/__init__.py": "", "lib/nav.js": body,
    })
    fa, _ = tracked_files(a)
    fb, _ = tracked_files(b)
    paths = sorted(rel for hits in duplicated_files({a: fa, b: fb}).values()
                   for _, rel in hits)
    assert paths == ["lib/nav.js", "vendor/nav.js"]


def test_root_level_markdown_counts_as_documentation(tmp_path):
    r = _repo(tmp_path / "p", {
        "README.md": "# p\n", "DEPLOY.md": "# deploy\n",
        "CONTRIBUTING.md": "# contributing\n", "src/notes.md": "# not a root doc\n",
    })
    files, _ = tracked_files(r)
    assert read_docs(r, files) == ["CONTRIBUTING.md", "DEPLOY.md", "README.md"]


def test_sibling_directories_with_their_own_manifests_are_units(tmp_path):
    """A monorepo with no `apps/` marker and no root manifest, whose units
    are plain top-level directories. Found in the wild: four services
    consolidated into one repo as siblings, which the scan reported as
    holding nothing at all."""
    r = _repo(tmp_path / "fleet", {
        "alpha/pyproject.toml": '[project]\nname = "alpha"\n',
        "beta/pyproject.toml": '[project]\nname = "beta"\n',
        "docs/index.md": "# docs\n",
        "README.md": "# fleet\n",
    })
    files, _ = tracked_files(r)
    units = read_units(r, files)
    assert [(u.name, u.path, u.label) for u in units] == [
        ("fleet", ".", "product"),
        ("alpha", "alpha", "package"),
        ("beta", "beta", "package"),
    ]


def test_one_directory_with_a_manifest_is_not_called_a_layout(tmp_path):
    """Two siblings agreeing on the shape is evidence; one is a guess --
    far more likely a `src/` or a vendored copy than a unit."""
    r = _repo(tmp_path / "single", {
        "src/pyproject.toml": '[project]\nname = "inner"\n',
        "README.md": "# single\n",
    })
    files, _ = tracked_files(r)
    assert read_units(r, files) == []


@pytest.mark.parametrize("line,expected", [
    ("Status: Accepted", "accepted"),
    ("**Status:** accepted — 2026-08-03", "accepted"),
    ("**Status:** accepted · **date:** 2026-07-17 · **beat:** policy", "accepted"),
    ("- Status: Rejected.", "rejected"),
    ("**Status:** design — pending review", "design"),
    ("Status: superseded (by 0007)", "superseded"),
])
def test_a_status_written_by_a_human_in_markdown_is_read(tmp_path, line, expected):
    """Real ADRs carry emphasis, a date, a beat, a parenthetical. Those
    are decoration and metadata around the answer, not part of it."""
    r = _repo(tmp_path / f"p{abs(hash(line))}", {"docs/adr/0001-x.md": f"# X\n\n{line}\n"})
    files, _ = tracked_files(r)
    assert read_decisions(r, files)[0].status == expected


def test_an_unmappable_status_survives_unchanged_rather_than_being_mangled(tmp_path):
    """The mapping table's whole value is that it refuses to guess, so
    cleaning must not quietly turn an unknown word into a known one."""
    r = _repo(tmp_path / "q", {"docs/adr/0001-x.md": "# X\n\n**Status:** marinating\n"})
    files, _ = tracked_files(r)
    assert read_decisions(r, files)[0].status == "marinating"


def test_a_distinctive_subdirectory_keeps_its_own_name(tmp_path):
    """A package's declared name is its PUBLISHING name, often namespaced
    for a registry. Using it produced units called
    `watches-example-org-regwatch` for a thing everyone -- including the
    ledger referring to it -- calls `regwatch`."""
    r = _repo(tmp_path / "watches", {
        "regwatch/pyproject.toml": '[project]\nname = "acme-regwatch"\n',
        "techwatch/pyproject.toml": '[project]\nname = "acme-techwatch"\n',
    })
    files, _ = tracked_files(r)
    assert sorted(u.name for u in read_units(r, files)) == [
        "regwatch", "techwatch", "watches",
    ]


def test_a_generic_subdirectory_is_qualified_by_its_repo(tmp_path):
    """A `worker` means nothing outside its own repo, and two repos each
    having one would collide into a single node."""
    r = _repo(tmp_path / "audit", {
        "worker/pyproject.toml": '[project]\nname = "audit-worker"\n',
        "viewer/package.json": '{"name": "audit-viewer"}',
    })
    files, _ = tracked_files(r)
    # `audit-viewer`, not `audit-audit-viewer`: the qualification says
    # which repo the generic directory belongs to, and repeating a token
    # the repo name already carries adds nothing.
    assert sorted(u.name for u in read_units(r, files)) == [
        "audit", "audit-viewer", "audit-worker",
    ]


def test_a_repo_whose_parts_do_not_carry_its_name_is_itself_a_unit(tmp_path):
    """Found in the wild: a product scanned as `-ingestor`, `-viewer` and
    `-worker`, and nothing named for the product. Every ledger node
    pointing at the product then dangled, so the map reported the
    product's own name as a broken link."""
    r = _repo(tmp_path / "masthead-audit", {
        "ingestor/pyproject.toml": '[project]\nname = "masthead-ingestor"\n',
        "viewer/package.json": '{"name": "viewer"}',
        "worker/pyproject.toml": '[project]\nname = "worker"\n',
    })
    files, _ = tracked_files(r)
    units = read_units(r, files)
    assert units[0].name == "masthead-audit"
    assert units[0].path == "."
    assert units[0].part_of is None
    assert all(u.part_of == "masthead-audit" for u in units[1:])


def test_a_repo_whose_own_name_is_already_a_unit_gains_no_second_node(tmp_path):
    """The whole is present; adding a node for it would split one thing
    into two."""
    r = _repo(tmp_path / "fleet", {
        "fleet/pyproject.toml": '[project]\nname = "fleet"\n',
        "beta/pyproject.toml": '[project]\nname = "beta"\n',
    })
    files, _ = tracked_files(r)
    units = read_units(r, files)
    assert sorted(u.name for u in units) == ["beta", "fleet"]
    assert all(u.part_of is None for u in units)


def test_a_lone_unit_is_the_repo_rather_than_a_part_of_it(tmp_path):
    """One directory carrying a manifest IS the repo under another name.
    Calling it a part of itself would invent a relationship nobody has."""
    r = _repo(tmp_path / "solo", {"src/pyproject.toml": '[project]\nname = "solo"\n'})
    files, _ = tracked_files(r)
    units = read_units(r, files)
    assert [u.part_of for u in units] == [None] * len(units)


def test_one_directory_is_one_unit_however_many_manifests_it_carries(tmp_path):
    """A directory with both a pyproject.toml and a requirements.txt was
    read as two units -- and because a declared name and a directory name
    slug differently, the two carried DIFFERENT names, so the map showed a
    phantom unit beside the real one."""
    r = _repo(tmp_path / "svc", {
        "ingestor/pyproject.toml": '[project]\nname = "svc-ingestor"\n',
        "ingestor/requirements.txt": "httpx\n",
        "viewer/package.json": '{"name": "viewer"}',
    })
    files, _ = tracked_files(r)
    paths = [u.path for u in read_units(r, files)]
    assert paths.count("ingestor") == 1


def test_an_index_of_decisions_is_not_itself_a_decision(tmp_path):
    """`docs/adr/README.md` scanned as a decision and reached the ledger as
    `almanac-readme: decision open, title: "Architecture decision records"`
    -- a node asserting that the existence of an index was an open
    architectural question."""
    r = _repo(tmp_path / "proj", {
        "docs/adr/README.md": "# Architecture decision records\n\nThe list.\n",
        "docs/adr/0001-a-real-one.md": "# A real one\n\n**Status:** accepted\n",
    })
    files, _ = tracked_files(r)
    names = [d.path for d in read_decisions(r, files)]
    assert names == ["docs/adr/0001-a-real-one.md"]


def test_a_dated_filename_is_not_a_numbered_adr(tmp_path):
    """`^\\d{3,4}[-_]` matched `2026-09-08-...` because a year is four
    digits and a dash. Every dated design spec in every repo scanned as
    a decision with an unrecognised status: 21 of 45 decisions on one
    map were plans."""
    r = _repo(tmp_path / "proj", {
        "docs/specs/2026-09-08-a-design.md": "# A design\n\n**Status:** design\n",
        "docs/adr/0007-a-real-one.md": "# Real\n\n**Status:** accepted\n",
        "notes/0042-numbered-elsewhere.md": "# Elsewhere\n\n**Status:** proposed\n",
    })
    files, _ = tracked_files(r)
    assert sorted(d.path for d in read_decisions(r, files)) == [
        "docs/adr/0007-a-real-one.md", "notes/0042-numbered-elsewhere.md",
    ]


def test_a_root_manifest_earns_the_whole_even_with_one_part(tmp_path):
    """A WordPress plugin: composer.json at the root, one packages/ child.
    The marker branch won and a lone part does not earn a whole, so the
    repo's only node was the child. A root manifest is direct evidence
    the repo is a thing in itself."""
    r = _repo(tmp_path / "plugin", {
        "composer.json": '{"name": "acme/plugin"}',
        "packages/connector-ts/package.json": '{"name": "connector-ts"}',
    })
    files, _ = tracked_files(r)
    units = read_units(r, files)
    assert [u.name for u in units] == ["plugin", "connector-ts"]
    assert units[1].part_of == "plugin"


def test_adapters_and_php_packages_are_layout_markers(tmp_path):
    r = _repo(tmp_path / "conn", {
        "composer.json": '{"name": "x/conn"}',
        "adapters/aem/package.json": '{"name": "aem"}',
        "adapters/atex/README.md": "# planned\n",
        "php-packages/core/composer.json": '{"name": "x/core"}',
    })
    files, _ = tracked_files(r)
    units = {u.name: u for u in read_units(r, files)}
    # `core` is generic, so it is qualified by the repo, as the rule says
    assert {"conn", "aem", "atex", "conn-core"} <= set(units)
    assert units["atex"].manifest is None, "a README-only adapter is listed, and says so"
    assert units["aem"].label == "adapter"


def test_tests_are_not_shared_code(tmp_path):
    """When an engine is vendored into several repos its tests come too;
    26 of 36 'shared code' nodes on one map were test files."""
    body = "x" * 300
    a = _repo(tmp_path / "a", {"engine/core.py": body, "engine/tests/test_core.py": body + "t",
                               "engine/core_test.go": body + "g", "spec/thing.spec.ts": body + "s"})
    b = _repo(tmp_path / "b", {"vendor/core.py": body, "vendor/tests/test_core.py": body + "t",
                               "vendor/core_test.go": body + "g", "spec/thing.spec.ts": body + "s"})
    dups = duplicated_files({a: tracked_files(a)[0], b: tracked_files(b)[0]})
    names = sorted(hits[0][1].rsplit("/", 1)[-1] for hits in dups.values())
    assert names == ["core.py"]


def test_a_decision_is_dated_by_its_own_filename_or_date_line_never_by_prose(tmp_path):
    """The time lens sat 171 scan-derived nodes in an 'undated' block
    while their filenames carried dates. A decision's date is its own."""
    r = _repo(tmp_path / "d", {
        "docs/adr/2026-07-20-topic-hubs.md": "# Topic hubs\n\nStatus: Accepted\n",
        "docs/adr/0002-frontmatter.md": "---\ntitle: x\ndate: 2026-08-03\n---\n# Frontmatter\n\nStatus: Accepted\n",
        "docs/adr/0003-inline.md": "# Inline\n\n**Status:** accepted\n**Date:** 2026-08-05\n",
        "docs/adr/0004-cites-a-date.md": "# Cites\n\nStatus: Accepted\n\nSupersedes the 2025-01-01 plan.\n",
    })
    files, _ = tracked_files(r)
    d = {x.name: x.date for x in read_decisions(r, files)}
    assert d["d-2026-07-20-topic-hubs"] == "2026-07-20"
    assert d["d-0002-frontmatter"] == "2026-08-03"
    assert d["d-0003-inline"] == "2026-08-05"
    assert d["d-0004-cites-a-date"] is None
