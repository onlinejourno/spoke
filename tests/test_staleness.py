import os
from datetime import date, datetime
import httpx
from spoke.store import Store
from spoke.checks.staleness import extract_citations, check_staleness
from spoke.clock import Clock


def test_extracts_urls_and_pr_refs():
    urls, prs = extract_citations(
        "see https://example.com/a and example-org/digest#96 plus <https://x.test/b>")
    assert "https://example.com/a" in urls
    assert "https://x.test/b" in urls
    assert prs == ["example-org/digest#96"]


def test_flags_a_dead_url(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text(
        "---\nname: a\ndescription: d\nmetadata:\n  verified: 2026-09-01\n---\n\n"
        "live at https://example.com/gone\n")

    def handler(request):
        return httpx.Response(404)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    findings = check_staleness(Store(tmp_path), client, Clock(date(2026, 9, 8), 30))
    assert [f.kind for f in findings] == ["citation-dead"]
    assert "404" in findings[0].detail


def test_flags_old_verified_date(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text(
        "---\nname: a\ndescription: d\nmetadata:\n  verified: 2026-01-01\n---\n\nno citations\n")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    findings = check_staleness(Store(tmp_path), client, Clock(date(2026, 9, 8), 30))
    assert [f.kind for f in findings] == ["unverified-too-long"]


def test_a_url_inside_a_code_span_is_a_value_not_a_citation():
    """REVERSED 2026-09-23. This used to assert the opposite -- that a URL in a
    code span IS extracted, minus the backtick.

    Measured against the real store: 43 of its 73 URLs (58%) exist ONLY inside
    code spans, and they are not sources cited for a claim. They are values
    being shown: `LLM_BASE_URL=https://api.groq.com/openai/v1`, the Auth0 claim
    namespace `https://example-org.com/roles` (not an address at all), a
    `curl '...'` step, a host documented as having no root route. Probing them
    produced findings that could never be closed.

    Every other check in this package already reads a code-stripped body --
    strip_code exists for exactly this, and checks/__init__.py records that the
    asymmetry had already caused one bug. This was the last holdout.

    A record that means to CITE something writes it in prose.
    """
    urls, _ = extract_citations("see `https://example.com/thing` for detail")
    assert urls == []


def test_a_url_in_prose_is_still_a_citation():
    urls, _ = extract_citations("published at https://example.com/thing")
    assert urls == ["https://example.com/thing"]


def test_a_url_inside_a_fenced_block_is_not_a_citation():
    body = "run it:\n\n```bash\ncurl https://example.com/in/2026-08-01.html\n```\n"
    assert extract_citations(body)[0] == []


def test_a_pr_ref_in_a_code_span_is_not_a_citation_either():
    """Same rule for PR refs: `owner/repo#12` shown as a literal is a value."""
    _, prs = extract_citations("the branch name is `example-org/spoke#12` here")
    assert prs == []
    _, prs = extract_citations("landed in example-org/spoke#12")
    assert prs == ["example-org/spoke#12"]


def test_hold_state_is_never_stale(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text(
        "---\nname: a\ndescription: d\nmetadata:\n  state: hold\n  verified: 2020-01-01\n---\n\nx\n")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert check_staleness(Store(tmp_path), client, Clock(date(2026, 9, 8), 30)) == []


def test_missing_verified_field_falls_back_to_file_mtime(tmp_path):
    # C1: a record with NO `verified` field is exactly the population this
    # tool was built for (0 of 211 real records carried the field). It
    # must fall back to the file's own mtime and still be reportable when
    # that mtime is old -- never silently skipped.
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    a = tmp_path / "a.md"
    a.write_text("---\nname: a\ndescription: d\n---\n\nno verified field at all\n")
    old_ts = datetime(2020, 1, 1).timestamp()
    os.utime(a, (old_ts, old_ts))

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    findings = check_staleness(Store(tmp_path), client, Clock(date(2026, 9, 8), 30))
    assert [f.kind for f in findings] == ["unverified-too-long"]
    assert "2020-01-01" in findings[0].detail


def test_missing_verified_field_with_recent_mtime_is_not_stale(tmp_path):
    # The flip side of the above: a fresh file with no `verified` field
    # falls back to "just written", not to a permanently-flagged state.
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\n---\n\nno verified field\n")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert check_staleness(Store(tmp_path), client, Clock(date(2026, 9, 8), 30)) == []


def _handler_for(pr_json):
    def handler(request):
        return httpx.Response(200, json=pr_json)
    return handler


def test_pr_probe_failure_produces_a_finding(tmp_path):
    # C2: a probe that FAILS (deleted repo, 404, rate-limited 403) must
    # never be silent -- previously this was the one case that produced
    # NO finding at all.
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text(
        "---\nname: a\ndescription: d\nmetadata:\n  verified: 2026-09-01\n---\n\n"
        "shipped in example-org/theme#28 (merged)\n")

    def handler(request):
        return httpx.Response(404)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    findings = check_staleness(Store(tmp_path), client, Clock(date(2026, 9, 8), 30))
    assert [f.kind for f in findings] == ["citation-state"]
    assert "example-org/theme#28" in findings[0].detail


def test_pr_probe_success_agreeing_with_the_record_produces_no_finding(tmp_path):
    # C2: the record claims "merged", GitHub confirms "merged" -- a healthy,
    # agreeing citation must NOT be reported as a defect (previously every
    # successful probe was reported, inverted from what the spec wants).
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text(
        "---\nname: a\ndescription: d\nmetadata:\n  verified: 2026-09-01\n---\n\n"
        "shipped in example-org/theme#28 (merged)\n")

    client = httpx.Client(transport=httpx.MockTransport(_handler_for({"merged": True})))
    findings = check_staleness(Store(tmp_path), client, Clock(date(2026, 9, 8), 30))
    assert findings == []


def test_a_code_span_near_the_citation_is_not_mistaken_for_a_claim(tmp_path):
    # Regression, found live against the real store: `--open` is a CSS
    # class name in a code span, not a claim about the PR's state. Without
    # stripping code spans from the claim window, this produced a false
    # "record claims open" disagreement against a healthy, agreeing PR.
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text(
        "---\nname: a\ndescription: d\nmetadata:\n  verified: 2026-09-01\n---\n\n"
        "the toggle JS adds `--open` alone opens AND closes. PR example-org/digest#63.\n")

    client = httpx.Client(transport=httpx.MockTransport(_handler_for({"merged": True})))
    findings = check_staleness(Store(tmp_path), client, Clock(date(2026, 9, 8), 30))
    assert findings == []


def test_pr_probe_success_disagreeing_with_the_record_produces_a_finding(tmp_path):
    # C2: the record claims the PR is NOT merged, but GitHub reports it
    # merged -- report the disagreement, naming both what was claimed and
    # what was observed.
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text(
        "---\nname: a\ndescription: d\nmetadata:\n  verified: 2026-09-01\n---\n\n"
        "example-org/theme#28, NOT MERGED, nothing deployed\n")

    client = httpx.Client(transport=httpx.MockTransport(_handler_for({"merged": True})))
    findings = check_staleness(Store(tmp_path), client, Clock(date(2026, 9, 8), 30))
    assert [f.kind for f in findings] == ["citation-state"]
    assert "claims not merged" in findings[0].detail
    assert "merged" in findings[0].detail.split("GitHub reports")[1]


# --- A citation probe must never switch off the age check ------------------

def test_a_dead_citation_no_longer_suppresses_the_age_check(tmp_path):
    # Measured on a real store: many records carried a
    # citation-dead finding and NOT ONE of them was ever age-checked --
    # the overlap between the two finding sets was exactly 0. A mostly
    # false alarm was silently disabling a real check on 30 records.
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text(
        "---\nname: a\ndescription: d\nmetadata:\n  verified: 2026-01-01\n---\n\n"
        "live at https://example.com/gone\n")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    findings = check_staleness(Store(tmp_path), client, Clock(date(2026, 9, 8), 30))
    assert sorted(f.kind for f in findings) == ["citation-dead", "unverified-too-long"]


def test_auth_gated_citation_is_reported_as_unverifiable(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text(
        "---\nname: a\ndescription: d\nmetadata:\n  verified: 2026-09-01\n---\n\n"
        "sheet at https://claude.ai/artifact/Ao9Htm14kPkxiqRAh9bMdW\n")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    findings = check_staleness(Store(tmp_path), client, Clock(date(2026, 9, 8), 30))
    assert [f.kind for f in findings] == ["citation-unverifiable"]
    assert "403" in findings[0].detail


def test_markdown_emphasis_is_not_part_of_the_url():
    # Live findings probed `https://example-org-atlas.fly.dev**` and
    # `https://example-org.com/brand/*` -- the regex ate the bold markers.
    urls, _ = extract_citations("see **https://example.com/a** and https://example.com/b*")
    assert urls == ["https://example.com/a", "https://example.com/b"]


def test_a_github_pull_url_is_treated_as_a_pr_reference(tmp_path):
    # `https://github.com/owner/repo/pull/28` and `owner/repo#28` name the
    # same thing. Probed as a plain URL it 404s for every PRIVATE repo --
    # GitHub answers 404 rather than 403 so as not to disclose existence --
    # and was reported `citation-dead`. Routed to the PR probe it gets the
    # honest "could not verify" the ref form already got.
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text(
        "---\nname: a\ndescription: d\nmetadata:\n  verified: 2026-09-01\n---\n\n"
        "fixed in https://github.com/example-org/theme/pull/28\n")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    findings = check_staleness(Store(tmp_path), client, Clock(date(2026, 9, 8), 30))
    assert [f.kind for f in findings] == ["citation-state"]
    assert "example-org/theme#28" in findings[0].detail


def test_a_github_pull_url_reports_its_merge_state():
    urls, prs = extract_citations("shipped in https://github.com/example-org/theme/pull/28")
    assert prs == ["example-org/theme#28"]
    assert urls == [], "the same citation must not also be probed as a plain URL"
