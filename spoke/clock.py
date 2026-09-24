"""One question — has this evidence expired — asked of three subjects.

There were three answers and no module that owned it.

- A ledger NODE decays by its `state`: `flags.STALE_MULTIPLIER` and
  `STALE_NEVER`, behind a private boolean.
- A capability SCORE decays by its `basis`: `scale.SCORE_STALE_MULTIPLIER`
  and `SCORE_STALE_NEVER`, behind a public tri-state.
- A memory RECORD decays with no table at all, in `checks.staleness`,
  with `hold` excluded by a bare literal and a comment restating
  `STALE_NEVER`'s rationale word for word without importing it.

All three read the same `stale_days` from config. All three cross-
referenced each other in prose, correctly, and nothing enforced any of
it. `SCORE_STALE_MULTIPLIER["measured"]` is documented as deliberately
equal to `STALE_MULTIPLIER["done"]` -- "re-verify something that was
checked" -- and was pinned in a test as the literal `2.0`, so changing
the node table would have left every test green and the documented
equivalence quietly gone.

This module owns the arithmetic and the relationship. The two
vocabularies stay where their words live -- states in `ledger`, bases in
`scale` -- because a table keyed by a vocabulary belongs beside it; what
moves here is the RULE those tables are read by, and the fact that they
share a number.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .ledger import parse_iso_date

#: The baseline multiplier: ordinary open work, decaying at exactly
#: `stale_days`. Every other multiplier is a deliberate multiple of this.
BASELINE = 1.0

#: "Re-verify something that was already checked", as opposed to "a task
#: rotting". Used by the node table for `done` and by the score table for
#: `measured` -- the same judgement about the same kind of thing, and the
#: reason they must be one constant rather than two copies of `2.0`.
RE_VERIFY = 2.0

#: How long a probe result may stand before it stops counting as an
#: answer. The doctor runs daily, so one missed run is a laptop that was
#: shut; two in a row is a schedule that is not running. Days, not a
#: multiplier of `stale_days`: a month-old probe reading "met" is the
#: defect, so it cannot be tied to a 30-day default.
PROBE_WINDOW_DAYS = 2

#: "A block nobody has touched", which is the point of the flag, so it
#: decays sooner than ordinary work.
URGENT = 0.5


@dataclass(frozen=True)
class Clock:
    """The two values every staleness question needs, as one value.

    They travelled as two parameters through nine signatures, and
    `composite()` had to check at runtime that both halves arrived --
    raising because "one without the other cannot decide whether a score
    is current, and quietly picking a threshold for the caller is how an
    unstated assumption gets into a number". That is the same argument
    `Workspace` makes about the axes, answered the same way: make it
    impossible to hold half of it.
    """

    today: date
    stale_days: int

    def threshold(self, multiplier: float | None) -> int | None:
        """Days before something decaying at `multiplier` goes stale, or
        None when it never does."""
        if multiplier is None:
            return None
        return int(self.stale_days * multiplier)

    def age_days(self, when) -> int | None:
        """How old `when` is, or None when it is absent or unparseable.

        A missing date is NOT an age of zero. Guessing an age from absent
        data would be a false positive this has no evidence for -- which
        is why the callers report "undated" as its own answer rather than
        folding it into "fresh".
        """
        parsed = parse_iso_date(when)
        return None if parsed is None else (self.today - parsed).days

    def expired(self, when, multiplier: float | None) -> bool:
        """True only when `when` parses AND is older than its threshold.

        Never true for something that decays at no rate, and never true
        for a date that is absent or unreadable -- the caller decides
        what an absent date means, because the three subjects genuinely
        differ on it.
        """
        threshold = self.threshold(multiplier)
        if threshold is None:
            return False
        age = self.age_days(when)
        return age is not None and age > threshold
