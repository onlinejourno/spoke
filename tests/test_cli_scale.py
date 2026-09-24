"""`projects axes`, `ledger score`, `scale` -- the CLI over the layer."""
from pathlib import Path
import subprocess

from tests.conftest import build_store

from spoke.cli import main


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _store(root: Path) -> Path:
    return build_store(root)


def _setup(tmp_path, monkeypatch, axes_line: str = "") -> Path:
    """Registry AND store both scratch. The registry alone is not
    isolation -- store resolution still falls back to config.toml."""
    store = _store(tmp_path / "store")
    reg = tmp_path / "projects.toml"
    reg.write_text(
        '[projects."p"]\n'
        f'repos = ["{tmp_path}"]\n'
        f'memory_store = "{store}"\n'
        + axes_line
    )
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    monkeypatch.delenv("SPOKE_STORE_PATH", raising=False)
    monkeypatch.delenv("SPOKE_PROJECT", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[checks]\nstale_days = 30\n")
    return cfg


AXES_LINE = (
    'axes = ['
    '{ key = "orchestration", label = "Orchestration", weight = 20 }, '
    '{ key = "editability", label = "Editability", weight = 20 }, '
    '{ key = "reliability", label = "Reliability", weight = 30 }, '
    '{ key = "actionability", label = "Actionability", weight = 30 }]\n'
)


def _product(cfg: Path, name: str) -> None:
    assert main(["ledger", "new", name, "--type", "product", "--state", "open",
                 "--title", name, "--body", "why", "--config", str(cfg)]) == 0


# --- axes -------------------------------------------------------------

def test_axes_round_trip_through_the_registry(tmp_path, capsys, monkeypatch):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["projects", "axes", "--set", "reliability=Reliability:30",
                 "--set", "editability=Editability:20", "--project", "p"]) == 0
    capsys.readouterr()
    assert main(["projects", "axes", "--project", "p"]) == 0
    out = capsys.readouterr().out
    assert "reliability: Reliability (weight 30, 60%)" in out
    assert "editability: Editability (weight 20, 40%)" in out


def test_axes_keep_their_declared_order(tmp_path, capsys, monkeypatch):
    """The order is the reading order of the scale, and the project chose
    it -- re-declaring an axis must not shuffle it to the end."""
    _setup(tmp_path, monkeypatch)
    main(["projects", "axes", "--set", "a=A:1", "--set", "b=B:1",
          "--set", "c=C:1", "--project", "p"])
    main(["projects", "axes", "--set", "a=A:9", "--project", "p"])
    capsys.readouterr()
    main(["projects", "axes", "--project", "p"])
    lines = [l for l in capsys.readouterr().out.splitlines() if ": " in l and "(" in l]
    assert [l.split(":")[0] for l in lines] == ["a", "b", "c"]


def test_an_axis_with_no_declaration_says_how_to_declare_one(tmp_path, capsys, monkeypatch):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["projects", "axes", "--project", "p"]) == 0
    assert "projects axes --set" in capsys.readouterr().out


def test_a_negative_weight_is_refused(tmp_path, capsys, monkeypatch):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["projects", "axes", "--set", "a=A:-3", "--project", "p"]) != 0
    assert "cannot be negative" in capsys.readouterr().out


# --- score ------------------------------------------------------------

def test_score_writes_through_the_gate_and_shows_up_in_scale(tmp_path, capsys, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    capsys.readouterr()
    assert main(["ledger", "score", "widget", "--axis", "reliability", "--value", "1",
                 "--basis", "measured", "--note", "liveness only",
                 "--config", str(cfg)]) == 0
    capsys.readouterr()
    main(["scale", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "widget" in out and "1*" in out


def test_score_refuses_an_axis_the_project_has_not_declared(tmp_path, capsys, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    capsys.readouterr()
    rc = main(["ledger", "score", "widget", "--axis", "vibes", "--value", "3",
               "--basis", "measured", "--config", str(cfg)])
    assert rc != 0
    assert "has not declared" in capsys.readouterr().out


def test_basis_has_no_default(tmp_path, capsys, monkeypatch):
    """A basis defaulting to 'measured' would claim somebody looked when
    nobody did -- the exact lie this layer exists to prevent."""
    import pytest
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    with pytest.raises(SystemExit):
        main(["ledger", "score", "widget", "--axis", "reliability", "--value", "1",
              "--config", str(cfg)])


def test_re_scoring_an_axis_replaces_rather_than_duplicates(tmp_path, capsys, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    main(["ledger", "score", "widget", "--axis", "reliability", "--value", "1",
          "--basis", "asserted", "--config", str(cfg)])
    main(["ledger", "score", "widget", "--axis", "reliability", "--value", "3",
          "--basis", "measured", "--config", str(cfg)])
    text = (tmp_path / "store" / "ledger" / "widget.md").read_text()
    assert text.count("axis: reliability") == 1
    assert "value: 3" in text and "basis: measured" in text


# --- scale ------------------------------------------------------------

def test_scale_prints_a_withheld_composite_with_its_reason(tmp_path, capsys, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    main(["ledger", "score", "widget", "--axis", "reliability", "--value", "1",
          "--basis", "measured", "--config", str(cfg)])
    capsys.readouterr()
    main(["scale", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "NO COMPOSITE" in out
    assert "Orchestration (not scored)" in out
    assert "30%" in out          # the measured share, stated


def test_scale_issues_a_composite_when_enough_is_measured(tmp_path, capsys, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    for axis, value in (("orchestration", 3), ("editability", 3),
                        ("reliability", 1), ("actionability", 5)):
        main(["ledger", "score", "widget", "--axis", axis, "--value", str(value),
              "--basis", "measured", "--config", str(cfg)])
    capsys.readouterr()
    main(["scale", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "widget: 3.0 / 5" in out
    assert "100% of the weight measured" in out


def test_an_unscored_axis_renders_as_missing_not_as_zero(tmp_path, capsys, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    capsys.readouterr()
    main(["scale", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "--" in out
    assert "not scored (which is not a zero)" in out


def test_the_grid_carries_the_basis_as_text_not_only_as_a_position(
    tmp_path, capsys, monkeypatch
):
    """A terminal has no colour and no texture. The mark is the channel."""
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    main(["ledger", "score", "widget", "--axis", "reliability", "--value", "1",
          "--basis", "measured", "--config", str(cfg)])
    main(["ledger", "score", "widget", "--axis", "editability", "--value", "4",
          "--basis", "asserted", "--config", str(cfg)])
    capsys.readouterr()
    main(["scale", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "1*" in out and "4~" in out
    assert "* measured" in out and "~ asserted" in out and "? unverified" in out


def test_scale_says_so_when_a_project_has_declared_no_axes(tmp_path, capsys, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["scale", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "declared no capability axes" in out
    assert "no built-in list" in out


# --- freshness in the grid ---------------------------------------------
#
# `spoke stale` watches a node's `updated`; nothing watched a score's `on`.
# A terminal has no colour and no texture, so here the mark IS the channel
# and the legend spells every mark out.

def _aged(days: int) -> str:
    from datetime import date, timedelta
    return (date.today() - timedelta(days=days)).isoformat()


def test_a_stale_measurement_is_marked_in_the_grid_and_explained(tmp_path, capsys, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    main(["ledger", "score", "widget", "--axis", "reliability", "--value", "1",
          "--basis", "measured", "--on", _aged(400), "--config", str(cfg)])
    capsys.readouterr()
    main(["scale", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "1*!" in out
    assert "! stale" in out and "60d" in out


def test_an_undated_measurement_is_marked_in_the_grid(tmp_path, capsys, monkeypatch):
    """`ledger score` always stamps a date, so this is the hand-edited
    file -- and it must not be able to pass for a current measurement."""
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    main(["ledger", "score", "widget", "--axis", "reliability", "--value", "1",
          "--basis", "measured", "--config", str(cfg)])
    node = tmp_path / "store" / "ledger" / "widget.md"
    text = "\n".join(l for l in node.read_text().splitlines() if "    on:" not in l)
    node.write_text(text + "\n")
    capsys.readouterr()
    main(["scale", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "1*#" in out
    assert "# undated" in out


def test_an_asserted_score_is_never_marked_stale_however_old(tmp_path, capsys, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    main(["ledger", "score", "widget", "--axis", "reliability", "--value", "1",
          "--basis", "asserted", "--on", _aged(4000), "--config", str(cfg)])
    capsys.readouterr()
    main(["scale", "--config", str(cfg)])
    assert "1~!" not in capsys.readouterr().out


def test_a_stale_measurement_stops_counting_in_the_cli_composite(tmp_path, capsys, monkeypatch):
    """The same rule the page applies, from the same function. If the CLI
    stopped passing its clock this row would report a confident 3.0 over
    four measurements nobody has re-taken in over a year."""
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    for axis in ("orchestration", "editability", "reliability", "actionability"):
        main(["ledger", "score", "widget", "--axis", axis, "--value", "3",
              "--basis", "measured", "--on", _aged(400), "--config", str(cfg)])
    capsys.readouterr()
    main(["scale", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "NO COMPOSITE" in out
    assert "measured 400d ago -- stale" in out


# --- the score's series -------------------------------------------------

def test_re_scoring_keeps_the_superseded_observation(tmp_path, capsys, monkeypatch):
    """`ledger score` replaced its predecessor, so a score had exactly one
    observation forever and nothing could say which way it was moving."""
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    main(["ledger", "score", "widget", "--axis", "reliability", "--value", "1",
          "--basis", "measured", "--on", "2026-01-01", "--note", "liveness only",
          "--config", str(cfg)])
    main(["ledger", "score", "widget", "--axis", "reliability", "--value", "3",
          "--basis", "measured", "--on", "2026-09-01", "--note", "a real probe",
          "--config", str(cfg)])
    text = (tmp_path / "store" / "ledger" / "widget.md").read_text()
    assert "previously:" in text
    assert "liveness only" in text          # the superseded observation survives
    assert "a real probe" in text


def test_the_current_score_is_still_exactly_one_per_axis(tmp_path, capsys, monkeypatch):
    """History hangs off the current score; it is never a second one. The
    duplicate-axis guard, and every consumer that keys by axis, stay put."""
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    for value, on in ((1, "2026-01-01"), (2, "2026-05-01"), (3, "2026-09-01")):
        main(["ledger", "score", "widget", "--axis", "reliability",
              "--value", str(value), "--basis", "measured", "--on", on,
              "--config", str(cfg)])
    text = (tmp_path / "store" / "ledger" / "widget.md").read_text()
    assert text.count("axis: reliability") == 1


def test_the_series_reads_oldest_first(tmp_path, capsys, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    for value, on in ((1, "2026-01-01"), (2, "2026-05-01"), (3, "2026-09-01")):
        main(["ledger", "score", "widget", "--axis", "reliability",
              "--value", str(value), "--basis", "measured", "--on", on,
              "--config", str(cfg)])
    text = (tmp_path / "store" / "ledger" / "widget.md").read_text()
    assert text.index("2026-01-01") < text.index("2026-05-01")


def test_re_running_the_identical_score_does_not_invent_an_observation(
    tmp_path, capsys, monkeypatch
):
    """Same value, same basis, same day is the SAME observation, not a
    second one. A series padded by idempotent re-runs would show a
    direction nobody measured."""
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    for _ in range(3):
        main(["ledger", "score", "widget", "--axis", "reliability", "--value", "1",
              "--basis", "measured", "--on", "2026-01-01", "--config", str(cfg)])
    text = (tmp_path / "store" / "ledger" / "widget.md").read_text()
    assert "previously:" not in text


def test_ledger_show_prints_the_series(tmp_path, capsys, monkeypatch):
    """Otherwise the history is stored and invisible, which is a cache."""
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    main(["ledger", "score", "widget", "--axis", "reliability", "--value", "1",
          "--basis", "measured", "--on", "2026-01-01", "--config", str(cfg)])
    main(["ledger", "score", "widget", "--axis", "reliability", "--value", "3",
          "--basis", "measured", "--on", "2026-09-01", "--config", str(cfg)])
    capsys.readouterr()
    main(["ledger", "show", "widget", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "2026-01-01" in out and "1" in out
    assert "2026-09-01" in out


def test_ledger_show_says_when_one_observation_cannot_show_a_direction(
    tmp_path, capsys, monkeypatch
):
    """The withholding register, applied to direction: one observation is
    not a trend, and the surface says so rather than staying silent."""
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    main(["ledger", "score", "widget", "--axis", "reliability", "--value", "1",
          "--basis", "measured", "--config", str(cfg)])
    capsys.readouterr()
    main(["ledger", "show", "widget", "--config", str(cfg)])
    assert "one observation" in capsys.readouterr().out


def test_ledger_set_cannot_re_bless_a_score_on_a_dropped_axis(tmp_path, capsys, monkeypatch):
    """The invariant that lived only in a docstring.

    `LedgerStore.write(node, axes)` says a caller holding the axes MUST
    pass them. Four of five call sites did not, and `ledger set` was the
    sharp one: it re-validates a node read from disk, so an existing
    score rode through with `axes=()` and the membership check was
    skipped entirely. Any state edit silently re-blessed a score on an
    axis the project had since dropped.

    Going through a Workspace there is nothing to forget, so this is now
    a refusal rather than a quiet write.
    """
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    assert main(["ledger", "score", "widget", "--axis", "reliability", "--value", "1",
                 "--basis", "measured", "--config", str(cfg)]) == 0

    # the project drops the axis the score names
    reg = tmp_path / "projects.toml"
    reg.write_text(reg.read_text().replace(
        '{ key = "reliability", label = "Reliability", weight = 30 }, ', ""))
    capsys.readouterr()

    rc = main(["ledger", "set", "widget", "--state", "done", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc != 0, out
    assert "has not declared" in out
    # and the node on disk is untouched
    assert "state: open" in (tmp_path / "store" / "ledger" / "widget.md").read_text()


def test_one_command_stamps_one_date(tmp_path, monkeypatch):
    """`date.today()` was called at thirteen sites, twice inside `ledger
    score` alone, so a run crossing midnight stamped a score's `on` and
    its node's `updated` with different dates."""
    cfg = _setup(tmp_path, monkeypatch, AXES_LINE)
    _product(cfg, "widget")
    assert main(["ledger", "score", "widget", "--axis", "reliability", "--value", "1",
                 "--basis", "measured", "--config", str(cfg)]) == 0
    text = (tmp_path / "store" / "ledger" / "widget.md").read_text()
    dates = {line.split(":", 1)[1].strip().strip("'\"")
             for line in text.splitlines()
             if line.strip().startswith(("on:", "updated:"))}
    assert len(dates) == 1, dates


def test_axes_max_sets_the_top_of_the_scale(tmp_path, capsys, monkeypatch):
    """A 0-4 project on the package's 0-5 ramp shows a full mark as 80%."""
    from spoke.projects import load_registry

    _setup(tmp_path, monkeypatch)
    reg = tmp_path / "projects.toml"
    capsys.readouterr()
    assert main(["projects", "axes", "--project", "p", "--max", "4"]) == 0
    assert "scale: 0-4" in capsys.readouterr().out
    assert main(["projects", "axes", "--project", "p", "--max", "0"]) == 2
    assert load_registry(reg)["p"].scale_max == 4
