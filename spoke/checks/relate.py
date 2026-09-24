from __future__ import annotations
import math
import re

from . import LINK
from collections import defaultdict
from itertools import combinations

ENTITY = re.compile(r"\b(?:[A-Z][a-z]+)(?:\s+[A-Z][a-z]+)*\b")
STOPWORDS = {"The", "This", "That", "It", "A", "An", "In", "On", "Why", "How"}

# Signals appearing in more than this fraction of records are discarded as
# uninformative before pairing — sharing a coarse, near-ubiquitous signal
# (e.g. `type:project`) is not evidence two records are related.
_RARITY_CUTOFF = 0.2

# A signal shared by this few records is never discarded outright, even if
# that count exceeds 20% of a tiny store — with only a handful of records
# a genuinely rare, specific signal (e.g. 2 records naming the same rare
# entity) can still be a large fraction of the store without being
# "ubiquitous" in any meaningful sense. This floor only bites when
# max(2, 20% of n) > 20% of n, i.e. stores smaller than ~10 records; the
# real, hundreds-of-records store is governed by the percentage alone.
_RARITY_FLOOR = 2



def _signals(mf) -> set[str]:
    text = mf.record.body
    sig = {f"link:{t}" for t in LINK.findall(text)}
    meta = mf.record.meta
    rec_type = (meta.get("metadata") or {}).get("type") or meta.get("type")
    if rec_type:
        sig.add(f"type:{rec_type}")
    for ent in ENTITY.findall(text):
        words = ent.split()
        while words and words[0] in STOPWORDS:
            words = words[1:]
        stripped = " ".join(words)
        if len(stripped) >= 4:
            sig.add(f"ent:{stripped}")
    return sig


def _signal_multiplier(sg: str) -> int:
    """A link: is an explicit, authored cross-reference — strong evidence,
    counted double. A multi-word entity phrase (e.g. "Zebracorn Protocol",
    surviving after any leading stopword is stripped) is a specific proper
    noun, not "an incidentally shared capitalised word" — also counted
    double. A single bare capitalised word is weak evidence on its own."""
    if sg.startswith("link:"):
        return 2
    if sg.startswith("ent:") and len(sg[len("ent:"):].split()) >= 2:
        return 2
    return 1


def cluster(store, max_size: int = 12) -> list[list[str]]:
    records = store.list_records()
    # Fewer than 2 records means there is nothing to pair, and the rarity/
    # similarity math below divides by `n` (or by `n / rarity_threshold`
    # for an empty store, which is 0 and raises ValueError out of
    # math.log). An empty or single-record store has zero possible
    # clusters by definition, so return that directly rather than let a
    # caller hit a crash for what is legitimately "no work to do".
    if len(records) < 2:
        return []

    sigs = {m.name: _signals(m) for m in records}
    names = list(sigs)

    doc_freq: dict[str, int] = defaultdict(int)
    for s in sigs.values():
        for sg in s:
            doc_freq[sg] += 1

    n = len(names)
    rarity_threshold = max(_RARITY_FLOOR, int(_RARITY_CUTOFF * n))
    surviving = {
        name: {sg for sg in s if doc_freq[sg] <= rarity_threshold}
        for name, s in sigs.items()
    }

    # A signal that just barely survives the discard filter above (df ==
    # rarity_threshold, the most common df a surviving signal can have)
    # sits at the *bottom* of the weight range any surviving signal can
    # have — w(s) = log(n / df(s)) is minimised, not maximised, at the
    # largest surviving df. That minimum, log(n / rarity_threshold), is
    # the weight of the weakest possible piece of "common" evidence this
    # store can produce. Requiring *twice* that before a pair qualifies —
    # two such weak signals, or one doubled by _signal_multiplier — keeps
    # the old ">=2 count" rule's spirit (>=2 units of weak evidence)
    # while still letting a single rare signal (df well below
    # rarity_threshold, so w well above this minimum) qualify a pair on
    # its own, which a single common signal, at exactly one unit of the
    # minimum, cannot. Deriving it from rarity_threshold (rather than a
    # single global constant) scales it to this store's own size, so a
    # 3-record fixture and a full-sized real store are held to the
    # same qualitative bar instead of the fixture's already-tiny weights
    # being measured against a bar sized for the real store.
    similarity_threshold = 2 * math.log(n / rarity_threshold)

    by_signal: dict[str, list[str]] = defaultdict(list)
    for name, s in surviving.items():
        for sg in s:
            by_signal[sg].append(name)

    # score[a][b] = weighted similarity between a and b: sum of w(s) over
    # shared surviving signals s, w(s) = log(N / df(s)) — rare agreement
    # (low df) counts heavily, near-ubiquitous agreement (df close to N)
    # counts near nothing — with the link:/multi-word-ent: multiplier
    # applied on top. df(s) is always >= 1 here since s is only ever
    # scored for a record it appears in. A zero-weight signal (df == N,
    # present in 100% of the store) contributes no evidence and is dropped.
    score: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for sg, members in by_signal.items():
        if len(members) < 2:
            continue
        weight = _signal_multiplier(sg) * math.log(n / doc_freq[sg])
        # Drop zero-weight signals entirely — they provide no evidence.
        if weight == 0:
            continue
        for a, b in combinations(members, 2):
            score[a][b] += weight
            score[b][a] += weight

    out: list[list[str]] = []
    seen: set[frozenset[str]] = set()
    for name in names:
        # Require strictly positive score: a zero-weight pair has no evidence.
        neighbours = [
            (sc, other) for other, sc in score[name].items()
            if sc > 0 and sc >= similarity_threshold
        ]
        neighbours.sort(key=lambda t: (-t[0], t[1]))
        top = [other for _, other in neighbours[: max_size - 1]]
        if not top:
            continue
        group = sorted([name, *top])
        key = frozenset(group)
        if key in seen:
            continue
        seen.add(key)
        out.append(group)

    return [g for g in out if len(g) > 1]
