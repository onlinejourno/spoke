from __future__ import annotations
import re
from datetime import date
from ..clock import BASELINE, Clock
from . import Finding, strip_code
from ..probe import probe_pr, probe_url, PR_REF

URL = re.compile(r"https?://[^\s)>\]\"'`]+")

# The web form of a pull-request reference. `owner/repo#28` and
# `https://github.com/owner/repo/pull/28` name the same thing, so they go
# down the same probe -- the PR API, which reports merge state and says
# "could not verify" when it cannot see the repo. Probed as a plain URL
# instead, every citation of a PRIVATE repo read as dead: GitHub answers
# 404 rather than 403 there, so as not to disclose that the repo exists.
PR_URL = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)\b")

# A record on hold is never resolved by time -- the same rule
# `flags.STALE_NEVER` states for a node, kept here because the vocabulary
# is this module's (record states), while the arithmetic is the clock's.
# A record decays at the BASELINE rate: unlike a node or a score it has
# no table of its own, and "one stale_days" IS its rule.
RECORD_STALE_NEVER = frozenset({"hold"})

# Best-effort extraction of what a record claims about a cited PR's merge
# state, from the text immediately surrounding the citation. This is a
# heuristic over free-text notes, not a schema -- see _claimed_pr_state.
_CLAIM_WINDOW = 60  # chars of context on each side of the citation
_NOT_MERGED = re.compile(r"\b(not\s+(?:yet\s+)?merged|unmerged)\b", re.IGNORECASE)
_MERGED = re.compile(r"\bmerged\b", re.IGNORECASE)
_CLOSED = re.compile(r"\bclosed\b", re.IGNORECASE)
_OPEN = re.compile(r"\bopen\b", re.IGNORECASE)
# Inline code spans (e.g. `--open`, a CSS class, not a claim about a PR)
# are stripped from the claim window before keyword matching -- verified
# against the real store, where a CSS class literally named `--open`
# sitting next to a PR citation was a real false positive until this was
# added.


# Trailing punctuation that belongs to the PROSE, not the URL. `*` and
# `_` are here because markdown emphasis closing right after a link was
# being probed as part of the address (`...fly.dev**`, `/brand/*`).
_TRAILING = ".,;*_"


def extract_citations(text: str) -> tuple[list[str], list[str]]:
    # Code first. A URL or PR ref inside a code span or a fence is a VALUE the
    # record is SHOWING -- `LLM_BASE_URL=https://api.example.com/v1`, an
    # Auth0 claim namespace that is not an address at all, a `curl '...'` step,
    # a host documented as having no root route -- not a source it CITES.
    # In a real store most URLs turn out to be of this kind, and probing them
    # produces findings that can never be closed.
    #
    # Every other check in this package already reads a stripped body;
    # checks/__init__.py records that this asymmetry had already caused one
    # bug, and this was the last holdout. A record that means to cite something
    # writes it in prose.
    text = strip_code(text, " ")
    urls = [u.rstrip(_TRAILING) for u in URL.findall(text)]
    prs = [f"{repo}#{num}" for repo, num in PR_REF.findall(text)]
    for repo, num in PR_URL.findall(text):
        ref = f"{repo}#{num}"
        if ref not in prs:
            prs.append(ref)
    # A PR URL is now covered by the PR probe, so it must not ALSO be
    # probed as a plain URL -- that is the duplicate reading that produced
    # the false `citation-dead` in the first place.
    urls = [u for u in urls if not PR_URL.match(u)]
    return urls, prs


def _verified(meta: dict, fallback: date) -> date:
    """The record's declared `verified` date, or `fallback` (the file's own
    mtime) when the field is absent. Spec §5: a record with no `verified`
    field is treated as `state: fact, verified: <file mtime>` and must be
    reportable by the age check -- not skipped, since it is exactly the
    least-examined population this tool exists to surface."""
    raw = (meta.get("metadata") or {}).get("verified") or meta.get("verified")
    if raw is None:
        return fallback
    return raw if isinstance(raw, date) else date.fromisoformat(str(raw))


def _state(meta: dict) -> str:
    return str((meta.get("metadata") or {}).get("state") or meta.get("state") or "fact")


def _claimed_pr_state(body: str, ref: str) -> str | None:
    """What the record's own text claims about `ref`'s merge state, or None
    when no claim is detectable in the text around the citation -- in that
    case there is nothing to compare the probe's answer against, so no
    disagreement can be raised for it."""
    idx = body.find(ref)
    if idx == -1:
        return None
    window = body[max(0, idx - _CLAIM_WINDOW): idx + len(ref) + _CLAIM_WINDOW]
    # Fences too, not just inline spans: a citation inside a fenced
    # block used to have its claim keywords matched out of the
    # surrounding code.
    window = strip_code(window, " ")
    if _NOT_MERGED.search(window):
        return "not merged"
    if _MERGED.search(window):
        return "merged"
    if _CLOSED.search(window):
        return "closed"
    if _OPEN.search(window):
        return "open"
    return None


def _states_agree(claimed: str, observed: str) -> bool:
    observed = observed.lower()
    if claimed == "merged":
        return observed == "merged"
    if claimed == "not merged":
        return observed != "merged"
    if claimed == "closed":
        return observed == "closed"
    if claimed == "open":
        return observed == "open"
    return True


def check_staleness(store, client, clock: Clock) -> list[Finding]:
    findings: list[Finding] = []
    for mf in store.list_records():
        meta = mf.record.meta
        # `RECORD_STALE_NEVER`, not a bare literal: this is the same
        # judgement `flags.STALE_NEVER` makes about a node on hold, and
        # while it was a string here the two could drift apart with
        # nothing failing.
        if _state(meta) in RECORD_STALE_NEVER:
            continue
        urls, prs = extract_citations(mf.record.body)
        for u in urls:
            r = probe_url(u, client)
            if r.ok:
                continue
            # Three states, not two. `citation-dead` is reserved for a
            # citation the probe actually judged and found gone; one it
            # could not judge -- auth-gated, method-refused, not a public
            # address, a template -- is reported as such. In practice most
            # citation-dead findings turn out to be of the second kind, and
            # none of those can ever be closed.
            kind = "citation-dead" if r.verifiable else "citation-unverifiable"
            findings.append(Finding(kind, mf.name, f"{u}: {r.detail}"))
        for p in prs:
            r = probe_pr(p, client)
            if not r.ok:
                # A probe that fails -- deleted repo, 404, rate-limited 403
                # -- must never be silent: an unreachable citation is at
                # least as suspect as a healthy one that disagrees.
                findings.append(Finding("citation-state", mf.name,
                                         f"{p}: could not verify — {r.detail}"))
                continue
            claimed = _claimed_pr_state(mf.record.body, p)
            if claimed is not None and not _states_agree(claimed, r.detail):
                findings.append(Finding("citation-state", mf.name,
                                         f"{p}: record claims {claimed}, GitHub reports {r.detail}"))
        # The age check runs whatever the citation probe found. It used to
        # be skipped for any record with a dead citation, which meant a
        # finding that was mostly false silently switched off a real check:
        # in practice the overlap between the two finding sets was exactly
        # zero -- every affected record went unchecked. The two questions are
        # independent --
        # "is this link reachable" is not "has anyone looked at this lately".
        mtime = date.fromtimestamp(mf.path.stat().st_mtime)
        v = _verified(meta, mtime)
        if clock.expired(v, BASELINE):
            findings.append(Finding("unverified-too-long", mf.name, f"verified {v}"))
    return findings
