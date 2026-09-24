"""The contradiction pass: cluster related records, then ask the model
whether any pair within a cluster asserts things that cannot both be true.

Cost shape: ONE model call per cluster that gets sent, never one call per
pair. Clustering exists precisely to bound the bill; prompting per pair
instead of per cluster would discard that bound entirely (a 213-record
store produces a few hundred clusters but thousands of distinct pairs -- a large multiplier
this project's free-until-revenue posture cannot absorb).

Clusters overlap by design (a record can sit in several neighbourhoods),
so the same unordered pair recurs across clusters. `plan_clusters` tracks
which pairs have already been covered by a cluster already selected --
processed LARGEST-FIRST, so the biggest clusters absorb the most ground
before small ones are even considered -- and skips any cluster whose
every internal pair is already covered: sending it would teach the model
nothing it has not already been asked about.

This is a HEURISTIC bound on cost, not exhaustive pairwise coverage. A
pair that lives only inside a cluster that gets skipped is never sent to
the model at all in that run. The trade is deliberate -- bounded spend
over completeness -- and callers must not read "N pairs covered" as
"every possible pair in the store was compared."

Every finding here is a PROPOSAL, never an edit. `Contradiction` always
carries both quotations verbatim so a human can judge it without opening
either file, and nothing in this module writes to the store.
"""
from __future__ import annotations
import json
import re
import httpx
from dataclasses import dataclass
from itertools import combinations
from .relate import cluster
from ..llm import LLMUnavailable, complete as _default_complete

# Models commonly wrap a JSON reply in a ```json fence even when told to
# return ONLY the array. This is the one cheap, bounded recovery attempt
# `_parse_response` makes before giving up -- not an open-ended repair loop.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

PROMPT = """You are auditing a set of notes for statements that cannot both be true.

Return ONLY a JSON array. Each element must be an object with keys:
file_a, file_b, quote_a, quote_b, reason.
Quote verbatim from the notes. Only report a pair whose claims genuinely
cannot both hold -- not differences of emphasis, scope, or date-stamping.
There may be more than two notes below; report every incompatible pair
you find among them, or an empty array if nothing contradicts.

NOTES:
{notes}
"""


@dataclass(frozen=True)
class Contradiction:
    """A proposed contradiction between two records. Both quotes are always
    populated -- this is a proposal for a human to weigh, not an applied
    edit, and it must stand on its own without either file open."""
    files: tuple[str, str]
    quote_a: str
    quote_b: str
    reason: str


@dataclass(frozen=True)
class ContradictionRun:
    """The outcome of one contradiction pass, including the calls that did
    NOT come back usable. `sent` is how many clusters were actually
    prompted (billed); `unparseable` counts responses whose JSON could not
    be recovered even after the one cheap fenced-code-block retry;
    `errored` counts calls where the model call itself raised (HTTP
    error, malformed transport response) before there was any text to
    parse; `malformed` counts individual items that parsed as JSON but
    whose object did not carry the required keys (file_a/file_b/quote_a/
    quote_b/reason) -- valid JSON, unusable content, one level deeper than
    `unparseable`. All three must be surfaced by a caller -- a run where
    every sent cluster ends up in `unparseable`/`errored`/`malformed` was
    billed in full and found nothing not because the store is clean, but
    because the integration is broken. That is indistinguishable from a
    clean run unless a caller checks these counts, which is exactly the
    failure shape this project exists to prevent."""
    findings: list[Contradiction]
    sent: int
    unparseable: int
    errored: int
    malformed: int = 0


def _parse_response(raw: object) -> list | None:
    """Parse a model response into a JSON list, or return None if it
    cannot be. Tries the raw text first; if that fails and the text
    contains a fenced code block (```json ... ``` or ``` ... ```, the
    common way models wrap JSON despite being told not to), tries the
    fenced contents ONCE. Never raises -- callers count a None as one
    unparseable response rather than crash the whole pass over it."""
    if not isinstance(raw, str):
        return None
    try:
        items = json.loads(raw)
        return items if isinstance(items, list) else None
    except json.JSONDecodeError:
        pass
    m = _FENCE.search(raw)
    if not m:
        return None
    try:
        items = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return items if isinstance(items, list) else None


@dataclass(frozen=True)
class Plan:
    """The cost shape of a contradiction pass, computed before any model
    call is made. `all_groups` is every cluster the store produced;
    `groups_to_send` is the subset that will actually be prompted after
    skipping fully-covered ones; `covered_pairs` is the distinct unordered
    pairs those sent clusters span."""
    all_groups: list[list[str]]
    groups_to_send: list[list[str]]
    covered_pairs: list[tuple[str, str]]


def plan_clusters(store, max_size: int = 12) -> Plan:
    """Cluster the store, then decide which clusters actually need to be
    sent to the model -- the plan a caller can report BEFORE spending
    anything. See the module docstring for the largest-first / skip-if-
    fully-covered heuristic and why it is a bound on cost, not exhaustive
    pairwise coverage.
    """
    # An empty store has nothing to cluster or send. `relate.cluster()`
    # itself now returns [] cleanly for fewer than 2 records, but this
    # short-circuit also avoids a needless cluster() call for the common
    # "brand-new store" case.
    if not store.list_records():
        return Plan(all_groups=[], groups_to_send=[], covered_pairs=[])

    groups = cluster(store, max_size=max_size)
    # Largest first: a big cluster covers the most pairs per call, so
    # processing it before its smaller sub-neighbourhoods gives those
    # smaller ones the best chance of being fully covered (and skipped).
    ordered = sorted(groups, key=lambda g: (-len(g), g))

    covered: set[frozenset[str]] = set()
    to_send: list[list[str]] = []
    for group in ordered:
        pairs = [frozenset((a, b)) for a, b in combinations(sorted(group), 2)]
        if pairs and all(p in covered for p in pairs):
            # Every pair in this cluster was already sent as part of a
            # bigger cluster already selected -- it can teach the model
            # nothing new, so paying for it again would be pure waste.
            continue
        to_send.append(group)
        covered.update(pairs)

    covered_pairs = sorted(tuple(sorted(p)) for p in covered)
    return Plan(all_groups=groups, groups_to_send=to_send, covered_pairs=covered_pairs)


def find_contradictions(store, cfg, client, complete_fn=_default_complete) -> ContradictionRun:
    plan = plan_clusters(store)
    out: list[Contradiction] = []
    unparseable = 0
    errored = 0
    malformed = 0
    for group in plan.groups_to_send:
        # One call per CLUSTER, carrying every record's full text -- not
        # one call per pair. The model returns the pairs within this set
        # it judges incompatible; the schema is unchanged because it was
        # always pairwise (file_a/file_b) regardless of how many notes
        # were in the prompt that produced it.
        notes = "\n\n".join(
            f"### {name}\n{store.read(name).record.body}" for name in group)
        try:
            raw = complete_fn(PROMPT.format(notes=notes), cfg, client)
        except LLMUnavailable:
            # A missing/invalid credential is a configuration defect the
            # caller must see immediately, not a per-cluster outcome to
            # tally alongside ordinary parse failures -- propagate as
            # before (see test_missing_key_propagates_rather_than_skipping).
            raise
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            # Transport/HTTP failure (5xx, connection error) or a malformed
            # success response (empty/missing choices, no content) from
            # THIS call. Previously this propagated as an uncaught
            # traceback -- inconsistent with the clean "NOT RUN" message a
            # missing credential gets, and useless to a cron's log. Count
            # it and keep going: one bad cluster must not lose every other
            # cluster's result, but it must never disappear silently either
            # -- see `errored` on ContradictionRun.
            errored += 1
            continue

        items = _parse_response(raw)
        if items is None:
            unparseable += 1
            continue
        for it in items:
            try:
                out.append(Contradiction(
                    files=(it["file_a"], it["file_b"]),
                    quote_a=it["quote_a"], quote_b=it["quote_b"], reason=it["reason"]))
            except (KeyError, TypeError):
                # Valid JSON, but this element does not carry the schema
                # this pass depends on (wrong/missing keys). Fixed at the
                # response level in 9ab60f5 (see `unparseable`); this is
                # the same shape one level down, inside an otherwise-usable
                # response -- must be counted, not dropped uncounted.
                malformed += 1
                continue

    # Two sent clusters can both contain the same pair when neither's
    # pairs are a strict subset of the other's (partial overlap, not full
    # coverage) -- `plan_clusters` only skips a cluster when EVERY one of
    # its pairs is already covered, so a partially-new cluster still gets
    # sent and can re-ask about a pair another sent cluster already
    # covered. That is an accepted cost trade (see module docstring), but
    # showing the same pair as a duplicate proposal teaches a reviewer
    # nothing new, so the first proposal per unordered pair wins here.
    seen: set[frozenset[str]] = set()
    deduped: list[Contradiction] = []
    for c in out:
        key = frozenset(c.files)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    return ContradictionRun(
        findings=deduped, sent=len(plan.groups_to_send),
        unparseable=unparseable, errored=errored, malformed=malformed)
