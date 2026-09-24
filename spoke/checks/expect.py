"""Expectations: question 4 of the task ledger, run on a schedule.

A node can say what it EXPECTS to be observably true -- a URL answers,
carries some text, was modified within some window -- and `doctor`
probes each expectation and writes what it found back onto the node
under `last`. The flag `unmet` is computed at render from `last`, like
every other flag: nothing here decides the flag, it records the
observation the flag is read from.

The reason this exists: a scheduled machine on one of the estate's apps
fired every morning for six weeks, did nothing useful, exited 0, and
nothing noticed, because nothing had been told a brief was SUPPOSED to
appear. An expectation is that telling, written where a run can read it.

Every fetch goes through the same SSRF guard the citation probe uses: an
expectation is authored by a person into a file, and a file is not a
trusted input just because it is local.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

from ..ledger.schema import expect_subject, parse_duration_hours
from ..probe import is_safe_url, _MAX_REDIRECTS, _REDIRECT_CODES
from . import Finding

PROBE = "probe:spoke-doctor"


def _fetch(url: str, client):
    """GET with bounded redirects through the SSRF guard. Returns the
    response or a string saying why there is none."""
    from urllib.parse import urljoin

    if not is_safe_url(url):
        return "blocked by SSRF guard"
    current = url
    for _ in range(_MAX_REDIRECTS):
        try:
            r = client.get(current, follow_redirects=False)
        except Exception as exc:
            return f"error: {exc}"
        if r.status_code in _REDIRECT_CODES:
            location = r.headers.get("location")
            if not location:
                return f"HTTP {r.status_code} with no Location"
            target = urljoin(current, location)
            if not is_safe_url(target):
                return f"redirect to a blocked address: {target}"
            current = target
            continue
        return r
    return "too many redirects"


def _linked_worktrees(path: str) -> int | str:
    """How many worktrees a repo has besides its main one, or a string
    saying why that could not be read. Every way of not knowing is a
    string -- a count that could not be taken must never read as zero.

    Why this exists: sessions isolate into a worktree per task and walk
    away; nothing reaps them, so the count only grows and nothing turns
    red. One repo on the estate had forty before anyone counted.
    """
    import os
    import subprocess

    if not os.path.isdir(path):
        return f"no such directory: {path}"
    try:
        r = subprocess.run(["git", "-C", path, "worktree", "list", "--porcelain"],
                           capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return "git is not installed"
    except subprocess.TimeoutExpired:
        return "git worktree list timed out"
    if r.returncode != 0:
        return (r.stderr.strip().splitlines() or [f"git exited {r.returncode}"])[0]
    entries = [ln for ln in r.stdout.splitlines() if ln.startswith("worktree ")]
    # The first entry is always the main worktree; the rest are linked.
    return max(len(entries) - 1, 0)


def check_one(expect: dict, client, now: datetime) -> tuple[bool, str]:
    """(met, detail). Every clause an expectation states must hold; the
    detail names the first that did not, or says what held."""
    if "path" in expect:
        n = _linked_worktrees(str(expect["path"]))
        if isinstance(n, str):
            return False, n
        ceiling = int(expect["worktrees_max"])
        if n > ceiling:
            return False, f"{n} linked worktrees, expected at most {ceiling}"
        return True, f"{n} linked worktrees (at most {ceiling})"
    r = _fetch(str(expect.get("url", "")), client)
    if isinstance(r, str):
        return False, r
    held: list[str] = []
    want = int(expect.get("status", 200))
    if r.status_code != want:
        return False, f"HTTP {r.status_code}, expected {want}"
    held.append(f"HTTP {r.status_code}")
    if "contains" in expect:
        needle = str(expect["contains"])
        body = r.text if hasattr(r, "text") else ""
        if needle not in body:
            return False, f"body does not contain {needle!r}"
        held.append(f"contains {needle!r}")
    if "fresh_within" in expect:
        hours = parse_duration_hours(expect["fresh_within"])
        modified = None
        if "dated_by" in expect:
            # The page states its own date in the body after a marker --
            # "Beat: tech · 2026-09-14" -- which is the brief's date, not
            # the server's. Read that. It is the honest signal for a page
            # that renders on request and carries no Last-Modified.
            import re
            m = re.search(re.escape(str(expect["dated_by"])) + r"\s*(\d{4}-\d{2}-\d{2})",
                          r.text if hasattr(r, "text") else "")
            if not m:
                return False, f"no date follows {expect['dated_by']!r} in the body"
            modified = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
        else:
            stamp = r.headers.get("last-modified")
            if not stamp:
                # No date is not "fresh". An expectation about freshness
                # that cannot be read is unmet, and the detail says why --
                # the alternative is a freshness check that passes on
                # everything that forgets to date itself.
                return False, (f"no Last-Modified header to judge freshness by "
                               f"(expected within {expect['fresh_within']}); if the page states "
                               "its own date, say what it follows with dated_by")
            try:
                modified = parsedate_to_datetime(stamp)
            except (TypeError, ValueError):
                return False, f"unreadable Last-Modified header {stamp!r}"
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=timezone.utc)
        age_h = (now - modified).total_seconds() / 3600
        if age_h > hours:
            return False, f"last modified {age_h / 24:.1f}d ago, expected within {expect['fresh_within']}"
        # A body date has day resolution; "3h ago" would claim more than it knows.
        held.append(f"dated {modified.date().isoformat()}" if "dated_by" in expect else f"modified {age_h:.0f}h ago")
    return True, "; ".join(held)


def check_expectations(ledger, client, today: date, now: datetime | None = None,
                       write: bool = True, axes: tuple = (), scale_max: int = 5) -> list[Finding]:
    """Probe every expectation on every node; write `last` back; report
    the unmet ones as findings. `write=False` probes and reports without
    touching the store (the CLI's dry run). `axes`/`scale_max` are the
    project's, so writing the probe result back does not trip the
    score gate on a node that carries scores."""
    now = now or datetime.now(timezone.utc)
    findings: list[Finding] = []
    for node in ledger.list_nodes():
        if not node.expects:
            continue
        probed = []
        for e in node.expects:
            met, detail = check_one(e, client, now)
            probed.append({**e, "last": {"at": now.isoformat(timespec="seconds"), "ok": met,
                                         "detail": detail, "by": PROBE}})
            if not met:
                findings.append(Finding("unmet", node.name, f"{expect_subject(e)}: {detail}"))
        if write:
            # Written on every run, met or not: `last.at` is the record
            # that the check happened, which is its own fact -- an
            # expectation nobody has checked is not a met one.
            res = ledger.write(replace(node, expects=tuple(probed)), axes, scale_max)
            if not res.ok:
                findings.append(Finding("unmet", node.name,
                                        "could not record the probe: " + "; ".join(res.reasons)))
            for a in res.advisories:
                # Recorded, but only here: the store's upstream never got
                # it, so every other checkout still shows the old verdict.
                findings.append(Finding("unmet", node.name, "probe recorded locally only: " + a))
    return findings
