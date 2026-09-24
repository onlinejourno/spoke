"""Capability scores, and the composite that refuses to exist.

A capability scale in this estate's own records produced, as its most
useful output, a refusal:

    Coverage (25% buyer weight), Actionability (20%), Evidence (15%) =
    NOT VERIFIED. No composite was issued, deliberately: 60% of the buyer
    weighting was unobservable, so a buyer number would be fiction.

A layer that rendered scores alone would have shown a confident average
over the 40% that WAS observable, and the reader would have had no way to
tell. That is this project's founding failure -- absence rendering as
assurance -- with a number on it.

So `basis` is not metadata attached to a score. It is half the score, and
only one of its three values counts toward a composite. An axis with no
score at all is a FOURTH state, and it is not zero: zero is a finding,
missing is an absence, and a scale that cannot tell them apart is worse
than no scale.

The axes themselves belong to the project, never to this package. A
hardcoded axis list would be the same defect as a hardcoded outlet name
or a personal store path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..clock import BASELINE, RE_VERIFY, Clock
from . import parse_iso_date

# Only `measured` counts. The other two are the honest ways of saying "we
# do not actually know", and they are kept distinct because they call for
# different work: an `asserted` score needs somebody to go and look, an
# `unverified` one needs the obstacle removed first.
BASES = ("measured", "asserted", "unverified")

# The default ceiling. A capability scale is a judgement, and a range
# wider than a person can hold in their head produces false precision.
DEFAULT_MAX_VALUE = 5

# The share of total axis weight that may be unmeasured before a
# composite is withheld. One third, from precedent: the record that
# motivated this module withheld at 60% unmeasured and called 32%
# "defensible". A default nobody chose is exactly the unstated assumption
# this project exists to remove -- so every composite, issued or
# withheld, prints this number and the actual figure alongside it.
DEFAULT_MAX_UNMEASURED_WEIGHT = 1 / 3


# Per-basis staleness, the same register as `flags.STALE_MULTIPLIER` and
# read the same way: a multiplier of the project's single `stale_days`
# value, with the bases that never decay held in a separate frozenset
# rather than given an enormous threshold.
#
# Only `measured` decays, and the asymmetry is the whole point. This
# layer exists to stop a number passing for one somebody checked; only a
# `measured` score is at that risk, because only `measured` counts. An
# `asserted` score already says "nobody looked" in its own channel and
# an `unverified` one already says "could not be looked at" -- neither
# was ever an observation, so there is nothing about them that a date
# can make untrue, and stamping them stale as well would put a second
# warning on top of the one that already says what is wrong. For
# `unverified` there is a further reason not to: its `on` is the day the
# OBSTACLE was found, not the day a system was seen, and ageing it would
# imply the obstacle expired -- a claim nobody made. Both stay fully
# visible; they simply do not decay.
SCORE_STALE_NEVER = frozenset({"asserted", "unverified"})

# 2.0, not the 1.0 baseline: the node table prices "re-verify something
# that was checked" at 2.0 (`done`) and "a task rotting" at 1.0 (`open`),
# and a measured score is the first shape, not the second. At 1.0 every
# measurement would go amber a month after it was taken, and a warning
# that fires on everything is one people learn to scroll past.
SCORE_STALE_MULTIPLIER: dict[str, float] = {
    "measured": RE_VERIFY,
}

# What `freshness()` can say about a score. `None` is a FIFTH answer and
# means "freshness does not apply here" -- an unscored cell, or a basis
# in SCORE_STALE_NEVER. It is not "fresh".
FRESHNESS_STATES = ("fresh", "stale", "undated")


class ScaleError(ValueError):
    """A score or axis that cannot be used. Raised, never defaulted."""


@dataclass(frozen=True)
class Axis:
    key: str
    label: str
    weight: float = 1.0


@dataclass(frozen=True)
class Observation:
    """One superseded reading of an axis: what was seen, on what basis,
    when. Identical to a `Score` minus the axis, which the `Score` it
    hangs off already names.

    Kept because direction needs two readings and a score that REPLACED
    its predecessor had exactly one, forever -- so nothing could say
    whether a capability was climbing or falling, only how old the latest
    number was. See the ledger node `scores-keep-their-series`.
    """
    value: int
    basis: str
    on: str | None = None
    note: str | None = None
    by: str | None = None


@dataclass(frozen=True)
class Score:
    axis: str
    value: int
    basis: str
    on: str | None = None
    note: str | None = None
    by: str | None = None
    # Superseded readings, OLDEST FIRST so the file reads chronologically
    # and a slope reads left to right. History hangs off the current
    # score and is never a second score: exactly one `Score` per axis
    # survives, so the duplicate-axis guard, `grid`, `composite` and
    # every consumer that keys by axis are untouched by this existing.
    # Deliberately unbounded -- a cap would drop the far end of a series
    # silently, and the far end is where a direction is visible.
    previously: tuple["Observation", ...] = ()

    @property
    def counts(self) -> bool:
        return self.basis == "measured"

    @property
    def series(self) -> tuple["Observation", ...]:
        """Every reading of this axis, oldest first, the current one
        last. What a direction would be computed from -- and there is
        deliberately no direction computed here yet: two readings a day
        apart and two a year apart are not the same evidence, and which
        of them counts as a trend is a decision nobody has taken."""
        return self.previously + (
            Observation(self.value, self.basis, self.on, self.note, self.by),
        )


def age_days(score: Score, clock: Clock) -> int | None:
    """Days between the score's observation date and the clock's today,
    or None when it carries no readable date at all."""
    return clock.age_days(score.on)


def score_stale_threshold(basis: str, clock: Clock) -> int | None:
    """How many days a score on this basis stays current, or None when
    the basis never decays. Whole days, and the SAME number the surfaces
    print: a threshold shown to a reader that is not the one actually
    compared against is a lie with a decimal point in it."""
    if basis in SCORE_STALE_NEVER:
        return None
    return clock.threshold(SCORE_STALE_MULTIPLIER.get(basis, BASELINE))


def freshness(score: Score, clock: Clock) -> str | None:
    """One of FRESHNESS_STATES, or None when freshness does not apply to
    this score's basis (see SCORE_STALE_NEVER).

    `undated` is deliberately NOT the "no evidence, so no flag" outcome
    that `flags._stale_cutoff` gives a node with no `updated`. A node is
    not itself a claim of fact; a measured score is one, and its entire
    warrant is "somebody observed this on <date>". With no date the
    warrant is absent while the number still counts -- which is this
    project's founding failure exactly, so it is stated rather than
    passed over.
    """
    if score.basis in SCORE_STALE_NEVER:
        return None
    on = parse_iso_date(score.on)
    if on is None:
        return "undated"
    return "stale" if clock.expired(score.on, SCORE_STALE_MULTIPLIER.get(
        score.basis, BASELINE)) else "fresh"


@dataclass(frozen=True)
class Composite:
    value: float | None
    withheld_reason: str | None
    measured_weight: float           # 0..1, the share of weight actually measured
    threshold: float                 # the share that had to be measured
    missing: tuple[str, ...]         # axis LABELS that did not count, in axis order


def validate_scores(
    scores: tuple[Score, ...],
    axes: tuple[Axis, ...],
    max_value: int = DEFAULT_MAX_VALUE,
) -> list[str]:
    """Every reason `scores` cannot be accepted -- all of them, not the
    first, so a caller fixing them sees the whole list at once."""
    reasons: list[str] = []
    keys = {a.key for a in axes}
    seen: set[str] = set()
    for s in scores:
        if not s.axis:
            reasons.append("a score must name an axis")
            continue
        if axes and s.axis not in keys:
            # Refused rather than stored: a score on an axis the project
            # never declared would render nowhere, and the author would
            # have no way to tell that from it working.
            reasons.append(
                f"score on axis {s.axis!r}, which this project has not declared -- "
                f"declared axes: {', '.join(sorted(keys)) or '(none)'}"
            )
        if s.basis not in BASES:
            reasons.append(
                f"axis {s.axis!r}: unknown basis {s.basis!r} -- must be one of "
                f"{', '.join(BASES)}. A score whose basis is not stated cannot be "
                "told apart from one that was checked."
            )
        if not isinstance(s.value, int) or isinstance(s.value, bool):
            reasons.append(f"axis {s.axis!r}: value must be a whole number, got {s.value!r}")
        elif not 0 <= s.value <= max_value:
            reasons.append(
                f"axis {s.axis!r}: value {s.value} is outside the scale 0-{max_value}"
            )
        if s.axis in seen:
            reasons.append(f"axis {s.axis!r} is scored more than once on the same node")
        seen.add(s.axis)
        # A superseded reading meets the same gate the current one does.
        # It is evidence of a direction, and evidence nobody checked is
        # the thing this whole layer refuses.
        current_on = parse_iso_date(s.on)
        for prior in s.previously:
            if prior.basis not in BASES:
                reasons.append(
                    f"axis {s.axis!r}: a prior observation has unknown basis "
                    f"{prior.basis!r} -- must be one of {', '.join(BASES)}"
                )
            if not isinstance(prior.value, int) or isinstance(prior.value, bool):
                reasons.append(
                    f"axis {s.axis!r}: a prior observation's value must be a whole "
                    f"number, got {prior.value!r}"
                )
            elif not 0 <= prior.value <= max_value:
                reasons.append(
                    f"axis {s.axis!r}: a prior observation's value {prior.value} is "
                    f"outside the scale 0-{max_value}"
                )
            prior_on = parse_iso_date(prior.on)
            if current_on is not None and prior_on is not None and prior_on > current_on:
                reasons.append(
                    f"axis {s.axis!r}: a prior observation ({prior.on}) is newer than "
                    f"the current score ({s.on}) -- the newest reading would sit in "
                    "the history while an older one rendered as today's"
                )
    return reasons


def composite(
    scores: tuple[Score, ...],
    axes: tuple[Axis, ...],
    max_unmeasured_weight: float = DEFAULT_MAX_UNMEASURED_WEIGHT,
    max_value: int = DEFAULT_MAX_VALUE,
    *,
    clock: Clock | None = None,
) -> Composite:
    """The weighted mean of the MEASURED-AND-CURRENT scores -- or a refusal.

    Weights are normalised, so a project whose axis weights sum to 87 or
    to 350 gets the same answer as one that made them sum to 100. Being
    strict about that would refuse a perfectly clear declaration over
    arithmetic the tool can do itself.

    Given a clock (`today` WITH `stale_days`), a measured score that has
    gone stale, or that carries no observation date at all, stops
    counting: it joins the unmeasured weight and the withholding rule
    above does its existing job on it. The alternative -- mark it and
    keep counting it -- was considered and rejected, because a mark on a
    number that still reads as current is precisely the failure this
    layer exists to remove; this module already treats "not measured" as
    "cannot count", and by its own logic a stale measurement is no longer
    a measurement.

    The cost of that choice is real and is paid in the open: dropping a
    stale LOW score can make a composite RISE, so ageing would look like
    improving. That is why every issued composite still prints its
    measured share and names what did not count WITH ITS AGE
    ("Reliability (measured 252d ago -- stale)") -- the same honesty
    channel that already carries "not scored" and "asserted".

    With no clock, no decay is applied: a caller holding a bag of scores
    and no date cannot be told what is current, and inventing a "today"
    for it would be a measurement of nothing. Half a clock is a bug at
    the call site and is refused rather than defaulted.
    """
    if not axes:
        return Composite(None, "no axes declared for this project", 0.0,
                         1 - max_unmeasured_weight, ())

    total_weight = sum(a.weight for a in axes)
    if total_weight <= 0:
        return Composite(None, "every declared axis has zero weight", 0.0,
                         1 - max_unmeasured_weight, tuple(a.label for a in axes))

    by_axis = {s.axis: s for s in scores}
    measured_weight = 0.0
    weighted_sum = 0.0
    missing: list[str] = []
    for axis in axes:
        s = by_axis.get(axis.key)
        share = axis.weight / total_weight
        # A missing axis, an asserted one and a measurement that has gone
        # out of date are all "does not count", but the label carries WHY
        # -- with the age, where there is one -- so the reader can act on
        # it and can see a composite that rose for what it is.
        why: str | None = None
        if s is None:
            why = "not scored"
        elif not s.counts:
            why = s.basis
        elif clock is not None:
            state = freshness(s, clock)
            if state == "stale":
                why = f"measured {age_days(s, clock)}d ago -- stale"
            elif state == "undated":
                why = "measured, but undated"
        if why is None:
            measured_weight += share
            weighted_sum += share * s.value
        else:
            missing.append(f"{axis.label} ({why})")

    required = 1 - max_unmeasured_weight
    if measured_weight < required - 1e-9:
        return Composite(
            value=None,
            withheld_reason=(
                f"only {measured_weight * 100:.0f}% of the axis weight is measured "
                f"(this project requires {required * 100:.0f}%). "
                f"Unmeasured: {', '.join(missing)}. A number over the measured part "
                "alone would read as a score for the whole thing."
            ),
            measured_weight=measured_weight,
            threshold=required,
            missing=tuple(missing),
        )
    return Composite(
        value=weighted_sum / measured_weight,
        withheld_reason=None,
        measured_weight=measured_weight,
        threshold=required,
        missing=tuple(missing),
    )


def grid(nodes, axes: tuple[Axis, ...]) -> dict[tuple[str, str], Score | None]:
    """(node name, axis key) -> the Score, or None for NOT SCORED.

    Every pair is present, including the unscored ones. A grid that
    simply omitted them would render as a gap the reader has to notice,
    and the whole point is that an absence is a finding the view states.
    """
    out: dict[tuple[str, str], Score | None] = {}
    for node in nodes:
        by_axis = {s.axis: s for s in getattr(node, "scores", ())}
        for axis in axes:
            out[(node.name, axis.key)] = by_axis.get(axis.key)
    return out
