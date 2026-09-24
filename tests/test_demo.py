"""`spoke demo`: a map of two real repos, built in one command, showing
everything the product can say. Tests use local stand-ins for the two
repos so nothing is cloned."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from spoke import demo
from spoke.clock import Clock
from spoke.ledger.flags import compute_flags
from spoke.ledger.graph import LENSES, load_graph
from spoke.ledger.store import LedgerStore
from spoke.projects import load_registry
from tests.conftest import build_repo


def _repos(tmp_path) -> dict[str, Path]:
    return {
        "tare": build_repo(tmp_path / "r" / "tare", {
            "package.json": '{"name": "tare"}',
            "docs/adr/0001-unified-crawler-ua-for-signal-probes.md": "# UA\n\n**Status:** accepted\n",
        }),
        "forage": build_repo(tmp_path / "r" / "forage", {
            "pyproject.toml": '[project]\nname = "forage"\n',
            "docs/index.md": "# docs\n",
        }),
    }


def test_the_demo_builds_a_map_that_raises_every_flag_the_gate_allows(tmp_path):
    r = demo.build(tmp_path / "d", _repos(tmp_path), today=date(2026, 9, 13))
    assert r.store.is_dir() and r.registry.exists() and r.config.exists()
    assert r.scanned["written"] >= 2 and r.scanned["refused"] == 0
    assert "SPOKE_REGISTRY=" in r.serve_command and "--config" in r.serve_command

    g, skipped = load_graph(LedgerStore(r.store))
    kinds = set()
    for lens in LENSES.values():
        for flags in compute_flags(g, lens, Clock(date(2026, 9, 13), 30)).values():
            kinds.update(f.kind for f in flags)
    # `unruled` cannot be produced through the gate -- a deferral without
    # a ruling is refused -- so it is the one kind a demo cannot show.
    assert kinds == {"held", "blocked", "deviates", "stale", "absent"}
    assert [s.kind for s in skipped] == ["renamed"], "the former name resolves, and is reported"
    assert {n.state for n in g.nodes.values()} >= {"hold", "blocked", "deferred", "superseded", "open"}


def test_the_demo_scores_every_basis_and_issues_exactly_one_composite(tmp_path):
    from spoke.ledger.board import build_scale

    r = demo.build(tmp_path / "d", _repos(tmp_path), today=date(2026, 9, 13))
    body = build_scale(LedgerStore(r.store), demo.AXES, Clock(date(2026, 9, 13), 30),
                       max_value=demo.SCALE_MAX)
    rows = {row["name"]: row for row in body["rows"]}
    bases = {c["basis"] for row in rows.values() for c in row["cells"] if c.get("basis")}
    assert bases == {"measured", "asserted", "unverified"}
    assert rows["forage"]["composite"] is not None, "one composite issued"
    assert rows["tare"]["composite"] is None and "stale" in rows["tare"]["withheld_reason"], "one withheld, saying why"
    assert body["max_value"] == demo.SCALE_MAX


def test_the_demo_is_idempotent_and_touches_nothing_outside_its_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "users-own-registry.toml"))
    repos = _repos(tmp_path)
    once = demo.build(tmp_path / "d", repos, today=date(2026, 9, 13))
    twice = demo.build(tmp_path / "d", repos, today=date(2026, 9, 13))
    assert once.nodes == twice.nodes
    assert not (tmp_path / "users-own-registry.toml").exists(), "the user's registry is untouched"
    assert load_registry(once.registry)[demo.PROJECT].memory_store == once.store
