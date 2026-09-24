"""The capability score, and the composite that refuses itself.

Read scale.py's module docstring before changing anything here. The
refusal is the feature, not a safety rail bolted onto one.
"""
from datetime import date
from pathlib import Path
import subprocess

from tests.conftest import build_store

import pytest

from spoke.ledger import Node
from spoke.ledger.scale import (
    SCORE_STALE_MULTIPLIER, Axis, Observation, Score, BASES, ScaleError,
    age_days, composite, freshness, grid, validate_scores,
)
from spoke.ledger.schema import validate
from spoke.ledger.store import LedgerStore
from spoke.clock import Clock

AXES = (
    Axis("orchestration", "Orchestration", 20),
    Axis("editability", "Editability", 20),
    Axis("reliability", "Reliability", 30),
    Axis("actionability", "Actionability", 30),
)


def _node(**kw) -> Node:
    base = dict(
        name="thing", type="product", state="open", title="A thing", body="body",
        relations=(), ruling=None, blocked_by=(), provenance=(),
        opened=None, updated=None, by=None, claimed_by=None, claimed_at=None,
        scores=(),
    )
    base.update(kw)
    return Node(**base)


def _repo(tmp_path: Path) -> LedgerStore:
    root = tmp_path / "store"
    root.mkdir()
    build_store(root)
    return LedgerStore(root)


# --- the score on a node ---------------------------------------------

def test_a_score_round_trips_through_the_store(tmp_path):
    s = _repo(tmp_path)
    scores = (
        Score("reliability", 1, "measured", on="2026-09-08",
              note="/healthz 200 and nothing else. Liveness only."),
        Score("actionability", 0, "unverified",
              note="no member session can be created outside the identity provider"),
    )
    res = s.write(_node(scores=scores))
    assert res.ok, res.blocking
    back = s.read("thing")
    assert back.scores == scores


def test_a_node_with_no_scores_serialises_without_the_key(tmp_path):
    s = _repo(tmp_path)
    s.write(_node())
    assert "scores" not in (tmp_path / "store" / "ledger" / "thing.md").read_text()


def test_an_unknown_basis_is_refused():
    reasons = validate(_node(scores=(Score("reliability", 3, "vibes"),)), AXES)
    assert any("basis" in r and "vibes" in r for r in reasons), reasons


def test_a_value_outside_the_scale_is_refused():
    assert any("outside the scale" in r
               for r in validate(_node(scores=(Score("reliability", 9, "measured"),)), AXES))
    assert any("outside the scale" in r
               for r in validate(_node(scores=(Score("reliability", -1, "measured"),)), AXES))


def test_a_non_integer_value_is_refused_not_coerced():
    reasons = validate(_node(scores=(Score("reliability", "3", "measured"),)), AXES)
    assert any("whole number" in r for r in reasons), reasons


def test_a_score_on_an_axis_the_project_never_declared_is_refused():
    reasons = validate(_node(scores=(Score("vibes", 3, "measured"),)), AXES)
    assert any("has not declared" in r for r in reasons), reasons


def test_the_same_axis_scored_twice_is_refused():
    reasons = validate(_node(scores=(
        Score("reliability", 1, "measured"), Score("reliability", 4, "asserted"),
    )), AXES)
    assert any("more than once" in r for r in reasons), reasons


def test_every_reason_is_reported_not_just_the_first():
    reasons = validate_scores(
        (Score("vibes", 99, "hunch"),), AXES,
    )
    assert len(reasons) >= 3, reasons


def test_a_caller_with_no_axis_list_checks_shape_but_not_membership():
    """A caller that HAS the axes must pass them; one that does not must
    still catch a malformed score rather than waving it through."""
    assert validate_scores((Score("anything", 3, "measured"),), ()) == []
    assert validate_scores((Score("anything", 3, "hunch"),), ())


# --- the composite ----------------------------------------------------

def test_an_all_measured_composite_is_the_weighted_mean():
    scores = (
        Score("orchestration", 3, "measured"), Score("editability", 3, "measured"),
        Score("reliability", 1, "measured"), Score("actionability", 5, "measured"),
    )
    c = composite(scores, AXES)
    # (20*3 + 20*3 + 30*1 + 30*5) / 100
    assert c.value == pytest.approx(3.0)
    assert c.withheld_reason is None
    assert c.measured_weight == pytest.approx(1.0)


def test_the_composite_counts_only_measured_axes():
    measured_only = (
        Score("orchestration", 3, "measured"), Score("editability", 3, "measured"),
        Score("reliability", 1, "measured"),
    )
    plus_an_assertion = measured_only + (Score("actionability", 5, "asserted"),)
    a = composite(measured_only, AXES)
    b = composite(plus_an_assertion, AXES)
    assert a.value == b.value            # the assertion changed nothing
    assert "Actionability (asserted)" in b.missing


def test_the_composite_is_withheld_when_too_much_weight_is_unmeasured():
    """The motivating case: 60% of the buyer weight unobservable."""
    axes = (
        Axis("coverage", "Coverage", 25), Axis("actionability", "Actionability", 20),
        Axis("evidence", "Evidence", 15), Axis("editability", "Editability", 20),
        Axis("reliability", "Reliability", 20),
    )
    scores = (Score("editability", 3, "measured"), Score("reliability", 1, "measured"))
    c = composite(scores, axes)
    assert c.value is None
    assert c.withheld_reason
    assert "40%" in c.withheld_reason              # the measured share, stated
    assert "67%" in c.withheld_reason              # the threshold, stated
    for label in ("Coverage", "Actionability", "Evidence"):
        assert label in c.withheld_reason


def test_a_missing_axis_is_not_a_zero():
    """A zero is a finding; a missing score is an absence. If the two
    were the same, an unscored product would rank below a genuinely bad
    one that somebody actually looked at."""
    all_zero = tuple(Score(a.key, 0, "measured") for a in AXES)
    assert composite(all_zero, AXES).value == 0.0
    assert composite((), AXES).value is None


def test_weights_that_do_not_sum_to_100_are_normalised_not_refused():
    odd = (Axis("a", "A", 7), Axis("b", "B", 3))
    scores = (Score("a", 5, "measured"), Score("b", 0, "measured"))
    assert composite(scores, odd).value == pytest.approx(3.5)


def test_no_axes_declared_is_a_stated_reason_not_a_crash():
    c = composite((), ())
    assert c.value is None and "no axes declared" in c.withheld_reason


def test_the_threshold_is_always_reported_even_when_the_composite_is_issued():
    """A default nobody chose is an unstated assumption. It is never
    invisible: the figure travels with every result."""
    scores = tuple(Score(a.key, 3, "measured") for a in AXES)
    c = composite(scores, AXES)
    assert c.threshold > 0 and c.measured_weight == pytest.approx(1.0)


# --- the grid ---------------------------------------------------------

def test_the_grid_holds_every_pair_including_the_unscored_ones():
    n = _node(scores=(Score("reliability", 1, "measured"),))
    g = grid([n], AXES)
    assert set(g) == {(n.name, a.key) for a in AXES}
    assert g[(n.name, "reliability")].value == 1
    assert g[(n.name, "editability")] is None


def test_the_three_bases_are_exactly_these():
    assert BASES == ("measured", "asserted", "unverified")


# --- freshness: the score's own observation date ----------------------
#
# `stale` watches a NODE's `updated`. Nothing watched a SCORE's `on`, so a
# measurement taken months ago rendered identically to one taken today --
# absence rendering as assurance, with a number on it. See the ledger node
# `score-freshness-stops-counting` for the decision these tests encode.

def test_a_measured_score_inside_its_threshold_is_fresh():
    s = Score("reliability", 3, "measured", on="2026-09-01")
    assert freshness(s, Clock(date(2026, 9, 10), 30)) == "fresh"


def test_a_measured_score_past_its_threshold_is_stale():
    s = Score("reliability", 3, "measured", on="2026-01-01")
    assert freshness(s, Clock(date(2026, 9, 10), 30)) == "stale"


def test_the_measured_threshold_is_twice_the_node_stale_days():
    """Deliberate, and asserted here so changing it is a decision rather
    than a drift: a measurement is a verification that needs re-taking
    (the node table's `done` = 2.0), not a task rotting (`open` = 1.0)."""
    assert SCORE_STALE_MULTIPLIER["measured"] == 2.0
    on_the_line = Score("reliability", 3, "measured", on="2026-07-12")   # 60d
    just_past = Score("reliability", 3, "measured", on="2026-07-11")     # 61d
    assert freshness(on_the_line, Clock(date(2026, 9, 10), 30)) == "fresh"
    assert freshness(just_past, Clock(date(2026, 9, 10), 30)) == "stale"


def test_an_asserted_score_never_goes_stale():
    """Nobody looked, so there is no observation to go out of date. The
    defect is permanent and already stated in the basis channel; a
    staleness mark on top would compete with the one signal that matters."""
    s = Score("actionability", 5, "asserted", on="2019-01-01")
    assert freshness(s, Clock(date(2026, 9, 10), 30)) is None


def test_an_unverified_score_never_goes_stale():
    """Its date records when the obstacle was found, not when a system
    was seen. Ageing it would suggest the obstacle expired -- a claim
    nobody made."""
    s = Score("actionability", 0, "unverified", on="2019-01-01")
    assert freshness(s, Clock(date(2026, 9, 10), 30)) is None


def test_a_measured_score_with_no_observation_date_is_undated():
    """NOT "fresh", and not silently passed over the way a node with no
    `updated` is. A node is not itself a claim of fact; a measured score
    IS one, and its whole warrant is "observed on <date>"."""
    s = Score("reliability", 3, "measured")
    assert freshness(s, Clock(date(2026, 9, 10), 30)) == "undated"


def test_an_unparseable_observation_date_is_undated_not_a_crash():
    """The store is a directory of markdown a human may hand-edit."""
    s = Score("reliability", 3, "measured", on="last Tuesday")
    assert freshness(s, Clock(date(2026, 9, 10), 30)) == "undated"


def test_a_yaml_typed_date_is_read_like_a_string():
    """An unquoted `on: 2026-01-01` is what a human writes, and YAML
    types it as a date. Same trap flags.py already fell into once."""
    s = Score("reliability", 3, "measured", on=date(2026, 1, 1))
    assert freshness(s, Clock(date(2026, 9, 10), 30)) == "stale"


def test_age_days_is_the_gap_between_the_observation_and_today():
    assert age_days(Score("r", 3, "measured", on="2026-09-01"), Clock(date(2026, 9, 10), 30)) == 9
    assert age_days(Score("r", 3, "measured"), Clock(date(2026, 9, 10), 30)) is None


# --- freshness and the composite --------------------------------------

def test_a_stale_score_stops_counting_toward_the_composite():
    """The decision: a stale measurement is, by this module's own logic,
    no longer a measurement, so it joins the unmeasured weight and the
    withholding rule does its existing job. The alternative -- mark it
    and keep counting -- leaves the number reading as current."""
    fresh = tuple(Score(a.key, 3, "measured", on="2026-09-01") for a in AXES)
    aged = fresh[:-1] + (Score("actionability", 3, "measured", on="2026-01-01"),)
    today = date(2026, 9, 10)
    assert composite(fresh, AXES, clock=Clock(today, 30)).value == pytest.approx(3.0)
    c = composite(aged, AXES, clock=Clock(today, 30))
    assert c.measured_weight == pytest.approx(0.7)


def test_the_withheld_reason_names_the_stale_axis_and_how_old_it_is():
    """A composite that ROSE because a bad measurement aged out must be
    legible as computed over less -- so the age is in the label, not just
    the word "stale"."""
    scores = (
        Score("orchestration", 3, "measured", on="2026-09-01"),
        Score("editability", 3, "measured", on="2026-01-01"),
        Score("reliability", 1, "measured", on="2026-01-01"),
        Score("actionability", 5, "measured", on="2026-01-01"),
    )
    c = composite(scores, AXES, clock=Clock(date(2026, 9, 10), 30))
    assert c.value is None
    assert "Reliability (measured 252d ago -- stale)" in c.missing


def test_an_undated_measured_score_does_not_count_either():
    scores = tuple(Score(a.key, 3, "measured") for a in AXES)
    c = composite(scores, AXES, clock=Clock(date(2026, 9, 10), 30))
    assert c.value is None
    assert "Reliability (measured, but undated)" in c.missing


def test_a_composite_asked_with_no_clock_does_not_decay_anything():
    """The documented default. `composite` over a bag of scores with no
    clock cannot know what today is, so it applies no decay -- and every
    surface in this package passes one (proved separately)."""
    scores = tuple(Score(a.key, 3, "measured", on="2019-01-01") for a in AXES)
    assert composite(scores, AXES).value == pytest.approx(3.0)


def test_half_a_clock_cannot_be_expressed_at_all():
    """This used to be `test_half_a_clock_is_refused_rather_than_defaulted`,
    and it asserted a runtime raise: `composite()` checked that `today` and
    `stale_days` had arrived together, because one without the other cannot
    decide whether a score is current.

    The raise is gone because the bug it caught can no longer be written.
    The two values arrive as one `Clock`, so there is no call that supplies
    half of them -- the same answer `Workspace` gives for the axes. What is
    pinned here is the property, not the error message: `composite` takes a
    clock or no clock, and `Clock` itself cannot be built from one half.
    """
    import inspect

    params = inspect.signature(composite).parameters
    assert "today" not in params and "stale_days" not in params
    assert params["clock"].default is None

    with pytest.raises(TypeError):
        Clock(date(2026, 9, 10))  # a clock needs both halves or it is not one

def test_a_score_carries_no_series_until_it_is_re_scored():
    assert Score("reliability", 3, "measured").previously == ()


def test_the_series_round_trips_through_the_store(tmp_path):
    s = _repo(tmp_path)
    score = Score(
        "reliability", 3, "measured", on="2026-09-10", note="watched it run",
        previously=(
            Observation(1, "measured", on="2026-01-01", note="liveness only"),
            Observation(2, "measured", on="2026-05-01"),
        ),
    )
    assert s.write(_node(scores=(score,)), AXES).ok
    back = s.read("thing").scores[0]
    assert back.previously == score.previously


def test_the_series_does_not_change_what_counts_now():
    """History is evidence of a direction, never a second current score.
    A composite that started averaging over superseded observations would
    be reporting a system that no longer exists."""
    with_series = tuple(
        Score(a.key, 3, "measured", on="2026-09-10",
              previously=(Observation(0, "measured", on="2026-01-01"),))
        for a in AXES
    )
    without = tuple(Score(a.key, 3, "measured", on="2026-09-10") for a in AXES)
    today = date(2026, 9, 10)
    a = composite(with_series, AXES, clock=Clock(today, 30))
    b = composite(without, AXES, clock=Clock(today, 30))
    assert a.value == b.value == pytest.approx(3.0)


def test_a_prior_observation_with_an_unknown_basis_is_refused():
    """The same rule as the current score, and for the same reason: an
    observation whose basis is not stated cannot be told apart from one
    that was checked."""
    s = Score("reliability", 3, "measured",
              previously=(Observation(1, "hunch"),))
    reasons = validate_scores((s,), AXES)
    assert any("hunch" in r for r in reasons)


def test_a_prior_observation_outside_the_scale_is_refused():
    s = Score("reliability", 3, "measured",
              previously=(Observation(9, "measured"),))
    assert any("outside the scale" in r for r in validate_scores((s,), AXES))


def test_a_prior_observation_dated_after_the_current_one_is_refused():
    """It would make "current" wrong -- the newest observation would be
    sitting in the history while an older one rendered as today's."""
    s = Score("reliability", 3, "measured", on="2026-01-01",
              previously=(Observation(1, "measured", on="2026-09-10"),))
    reasons = validate_scores((s,), AXES)
    assert any("newer than the current score" in r for r in reasons)


def test_a_node_and_a_score_price_re_verification_identically():
    """`STALE_MULTIPLIER["done"]` and `SCORE_STALE_MULTIPLIER["measured"]`
    are documented as the SAME judgement -- "re-verify something that was
    checked", as opposed to "a task rotting" -- and the docs of each cite
    the other.

    While both were the literal `2.0`, that equivalence was a sentence in
    a comment. Changing one table would have left every test green and
    the documented relationship quietly gone. Now both name `RE_VERIFY`,
    and this asserts the relationship rather than the number: no literal
    appears here, so re-pricing re-verification moves both together or
    fails right there.
    """
    from spoke.clock import BASELINE, RE_VERIFY
    from spoke.ledger.flags import STALE_MULTIPLIER

    assert STALE_MULTIPLIER["done"] == SCORE_STALE_MULTIPLIER["measured"] == RE_VERIFY
    # and it is a deliberate multiple of the baseline, not a coincidence
    assert RE_VERIFY != BASELINE
    assert STALE_MULTIPLIER["open"] == BASELINE


def test_the_threshold_a_reader_is_shown_is_the_one_compared_against():
    """A `done` node decays at RE_VERIFY, so at stale_days=30 it goes stale
    after 60 days -- and the flag used to announce it as "over 30d",
    printing the config value rather than the threshold it had actually
    crossed. scale.score_stale_threshold already states the rule this
    pins: a threshold shown to a reader that is not the one compared
    against is a lie with a decimal point in it.
    """
    from datetime import timedelta

    from spoke.clock import Clock
    from spoke.ledger.flags import STALE_MULTIPLIER, _stale_cutoff

    clock = Clock(date(2026, 9, 9), 30)
    threshold = clock.threshold(STALE_MULTIPLIER["done"])
    assert threshold == 60

    just_past = (clock.today - timedelta(days=threshold + 1)).isoformat()
    just_within = (clock.today - timedelta(days=threshold)).isoformat()
    assert _stale_cutoff(clock, "done", just_past) is True
    assert _stale_cutoff(clock, "done", just_within) is False
