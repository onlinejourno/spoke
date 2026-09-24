import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Names and paths belonging to the estate this tool was first written for.
# A clone must carry none of them -- in a file's CONTENTS or its NAME. This
# is the one list both this test and the CI "Identity gate" step use; CI
# runs this test as its real gate and keeps an identical-pattern grep only
# as a second, visible belt-and-braces step, so the two can never disagree
# about what is forbidden (final-review finding 5).
# The estate's own names, and the PEOPLE and CLIENTS its records name.
# The second half was added after the first half was passing: a gate that
# knows every product name and no person's name is a gate that waves
# through a client engagement and the date it ended, which is what the
# contradiction feature's worked example was -- copied verbatim into two
# test fixtures and two specs, green the whole time. Fixtures use the
# placeholder vocabulary in docs/PLACEHOLDERS.md instead.
FORBIDDEN = re.compile(
    r"onlinejourno|galley|newsroom|pulse|daybook|frontmatter-name|opt-homebrew|/Users/"
    r"|subhash|the hindu|hindustan times|\bacj\b",
    re.I,
)

# Directories excluded from the scan:
#  - .superpowers/: planning scratch, never tracked.
#  - .github/: the CI workflow's "Identity gate" step embeds this exact
#    pattern as literal grep text (see ci.yml) -- same self-reference.
#
# docs/ USED TO BE EXCLUDED AND IS NOT ANY MORE. It was excluded on the
# reasoning that plans and specs legitimately discuss the estate they
# were written in -- which was true, and which meant the repository's
# specs carried real product names, a real store path, live pull-request
# references and internal assessments of the author's own systems, all
# of it invisible to the gate that was supposed to be watching. The
# package was clean and the repository was not, and nothing said so.
#
# Documents now use fictional placeholders (see docs/PLACEHOLDERS.md).
# They read the same and they name nobody.
#
# tests/ USED TO BE EXCLUDED AND IS NOT ANY MORE, for the same reason
# docs/ stopped being: the exemption was granted to ONE file's
# self-reference and quietly covered forty. Behind it the suite named
# four of the estate's products as fixtures, quoted a live pull-request
# reference, and pointed an acceptance test at this machine's real
# memory store by absolute path. Only the file that must quote the
# pattern is excluded now, by name.
EXCLUDED_DIRS = {".superpowers", ".github"}

# This file quotes FORBIDDEN in order to test it, so it cannot scan
# itself. Nothing else gets that exemption.
EXCLUDED_FILES = {"tests/test_no_estate_identity.py"}


def _tracked_files() -> list[Path]:
    """The files that actually ship, per git -- not a directory walk. A
    directory walk scoped to spoke/ is exactly what let a tracked file
    OUTSIDE spoke/ (config.toml, holding this machine's store path) pass
    both gates unexamined (final-review finding 1)."""
    # Tracked, PLUS untracked files git would not ignore: the ones one
    # `git add` away from shipping. Scanning only tracked files gave a
    # new file a blind spot exactly that wide -- chrome.css carried a
    # hostname in a comment through a green suite and was caught only
    # after it was committed and pushed. Ignored files (config.toml, the
    # venv) stay out, because they never ship.
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    return [ROOT / p for p in out if (ROOT / p).is_file()]


# The ONLY permitted mentions, and they are permitted as links, not as
# identity. Spoke is MIT alongside two sibling MIT tools, and a README
# that cannot name them is a README that cannot credit them.
#
# Deliberately narrow: exact full URLs, in README.md alone. Not a
# substring, not a domain, not "anywhere in docs". A gate with a loose
# exception is a gate with a hole, and the value of this one is that it
# has never had to be argued with -- so the exception is written to be
# unarguable too.
ALLOWED_LINKS = (
    "https://github.com/onlinejourno/tare",
    "https://github.com/onlinejourno/forage",
)

# The author's own copyright line. A licence that cannot name its
# copyright holder is not a licence -- this is the one place a person's
# name belongs, and the only place it is permitted.
ALLOWED_COPYRIGHT = ("Subhash Rai",)

# The two sibling MIT repos may be NAMED (as links) in exactly three
# files: the README, where they are credited; spoke/demo.py, where they
# are the demo's subject -- chosen because they are public, real, and
# have the shapes the scan reads; and the setup page, which offers that
# demo as the third way in and must say WHOSE repositories it is about
# to clone onto someone's machine. Nowhere else.
#
# The gate refused this file itself when the links were added, which is
# the behaviour wanted: an allowance is widened on purpose, in one
# place, or not at all.
_LINK_FILES = ("README.md", "spoke/demo.py", "spoke/static/setup.html")

# file -> the exact literals permitted in it. Everything else on the
# line still has to pass, so an allowance can never carry a second name
# in beside the one it was granted for.
_ALLOWANCES = {f: ALLOWED_LINKS for f in _LINK_FILES}
_ALLOWANCES["LICENSE.md"] = ALLOWED_COPYRIGHT


def _is_allowed(rel: Path, line: str) -> bool:
    permitted = _ALLOWANCES.get(str(rel))
    if not permitted:
        return False
    remaining = line
    for literal in permitted:
        remaining = remaining.replace(literal, "")
    return not FORBIDDEN.search(remaining)


def _is_excluded(rel: Path) -> bool:
    if str(rel) in EXCLUDED_FILES:
        return True
    return bool(rel.parts) and rel.parts[0] in EXCLUDED_DIRS


def _scan(files: list[Path], root: Path) -> list[str]:
    """Check both the NAME and the CONTENTS of each file. Name-only
    matters because `touch spoke/onlinejourno_defaults.toml` ships
    estate identity before a single byte is written into it -- a
    contents-only gate (the previous shape) lets that straight through,
    and the plan's own ledger-vocabulary data files are the likely
    re-entry route (final-review finding 3)."""
    offenders = []
    for p in files:
        rel = p.relative_to(root)
        if _is_excluded(rel):
            continue
        if FORBIDDEN.search(str(rel)):
            offenders.append(f"{rel}: forbidden estate identity in the file name")
        if p.exists() and p.is_file():
            for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                if FORBIDDEN.search(line) and not _is_allowed(rel, line):
                    offenders.append(f"{rel}:{i}: {line.strip()[:90]}")
    return offenders


def test_no_estate_identity_in_the_shipped_tree():
    offenders = _scan(_tracked_files(), ROOT)
    assert not offenders, (
        "estate identity found in the git-tracked tree -- it must come "
        "from config, never source or a file name:\n  " + "\n  ".join(offenders)
    )


def test_a_forbidden_file_name_under_spoke_is_caught(tmp_path):
    """Proves the name-matching half of the gate is real, not decorative.
    Before Fix 3, `touch spoke/onlinejourno_defaults.toml` passed both
    gates -- an EMPTY file with a forbidden name had no content to match.
    """
    (tmp_path / "spoke").mkdir()
    bad = tmp_path / "spoke" / "onlinejourno_defaults.toml"
    bad.write_text("")  # deliberately empty: the name alone must be enough
    offenders = _scan([bad], tmp_path)
    assert offenders, "a forbidden file name under spoke/ must be caught even with empty contents"
    assert "onlinejourno_defaults.toml" in offenders[0]


def test_the_real_config_is_not_tracked(tmp_path):
    """Fix 1: config.toml on this machine holds this machine's store path
    and estate-specific accepted_absences. It must stay on disk (so the
    tool keeps working here) but leave the git index (so a clone never
    receives it). config.example.toml, carrying only placeholders, ships
    instead."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert "config.toml" not in tracked, (
        "config.toml is tracked and carries this machine's store path and "
        "estate-specific accepted absences; ship config.example.toml instead"
    )


def test_the_link_exception_is_only_those_two_urls_in_the_readme():
    """A gate with a loose exception is a gate with a hole."""
    from pathlib import Path as _P

    # the sibling links pass, in the README only
    line = "[Tare](https://github.com/onlinejourno/tare) and [Forage](https://github.com/onlinejourno/forage)"
    assert _is_allowed(_P("README.md"), line)
    assert not _is_allowed(_P("spoke/cli.py"), line)

    # anything else on the same line does not ride in with them
    assert not _is_allowed(_P("README.md"),
                           "https://github.com/onlinejourno/tare and onlinejourno/newsroom")
    assert not _is_allowed(_P("README.md"), "https://github.com/onlinejourno/audit")
    assert not _is_allowed(_P("README.md"), "path = /Users/someone/store")

    # the copyright line passes, in the licence only, and carries nothing with it
    assert _is_allowed(_P("LICENSE.md"), "Copyright (c) 2026 Subhash Rai")
    assert not _is_allowed(_P("README.md"), "Copyright (c) 2026 Subhash Rai")
    assert not _is_allowed(_P("LICENSE.md"), "Subhash Rai, of the Northwind engagement at the Hindu")


def test_a_persons_name_in_a_fixture_is_caught(tmp_path):
    """The half that was missing. A record naming a person and a client
    reads like ordinary test data, which is why it survived four months
    of a green gate."""
    bad = tmp_path / "tests" / "test_x.py"
    bad.parent.mkdir()
    bad.write_text('rec("a.md", "Currently in an engagement with The Hindu")\n')
    assert _scan([bad], tmp_path), "a person's client engagement must not pass as test data"


# --- the belt and the braces must actually agree ---------------------------
# This file's own header says CI "keeps an identical-pattern grep only as a
# second, visible belt-and-braces step, so the two can never disagree about
# what is forbidden". Nothing enforced that claim, and they did disagree: the
# grep never learned ALLOWED_LINKS, so from the day the two MIT siblings were
# credited in the README, CI was red on every push while this test passed.
# Twenty-odd red runs, 2026-09-13 to 2026-09-23. A comment asserting a
# property is not a check on it.

CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _ci_text() -> str:
    assert CI_WORKFLOW.exists(), f"no CI workflow at {CI_WORKFLOW}"
    return CI_WORKFLOW.read_text()


def test_the_ci_grep_forbids_exactly_what_this_test_forbids():
    """The pattern is duplicated into shell, so it can drift. Compare them."""
    m = re.search(r"^\s*pattern='([^']+)'", _ci_text(), re.M)
    assert m, "no pattern='...' line in the CI identity-gate step"
    assert m.group(1) == FORBIDDEN.pattern, (
        "the CI grep and FORBIDDEN disagree about what is forbidden:\n"
        f"  ci.yml:   {m.group(1)}\n"
        f"  FORBIDDEN: {FORBIDDEN.pattern}")


def test_the_ci_grep_knows_about_the_allowed_links():
    """The carve-out is the half that drifted. Every allowed link and every
    file it is allowed in must appear in the step, or the grep will fail on
    lines this gate deliberately permits -- which is what happened."""
    text = _ci_text()
    for link in ALLOWED_LINKS:
        assert link in text, f"the CI grep does not exempt {link}"
    for name in _LINK_FILES:
        assert name in text, f"the CI grep does not name {name} as an allowed file"
    for literal in ALLOWED_COPYRIGHT:
        assert literal in text, f"the CI grep does not exempt {literal!r}"
    assert "LICENSE.md)" in text, "the CI grep does not carve out LICENSE.md"
