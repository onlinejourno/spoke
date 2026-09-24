"""`ledger lens` -- the CLI surface over graph.py + flags.py.

Output is a QUERY RESULT, not a picture: each hub node with its own
state, its flags with their origins, and its members. Dangling relations
are printed, never dropped -- absence must not render as assurance.
"""
from pathlib import Path
import subprocess

from tests.conftest import build_store

from spoke.cli import main


def _store(tmp_path: Path) -> Path:
    return build_store(tmp_path)


def _cfg(tmp_path: Path, store: Path, stale_days: int = 30) -> Path:
    f = tmp_path / "config.toml"
    f.write_text(f'[store]\npath = "{store}"\n\n[checks]\nstale_days = {stale_days}\n')
    return f


def _new(cfg: Path, name: str, type_: str, state: str, *extra: str) -> None:
    argv = ["ledger", "new", name, "--type", type_, "--state", state,
            "--title", name, "--body", "why", "--config", str(cfg), *extra]
    assert main(argv) == 0, argv


def _bare(tmp_path, monkeypatch, stale_days: int = 30) -> Path:
    """A scratch store plus config, with the registry pointed at a file
    that does not exist so no real project can be resolved into it."""
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    monkeypatch.delenv("SPOKE_STORE_PATH", raising=False)
    return _cfg(tmp_path, _store(tmp_path / "store"), stale_days)


def test_lens_lists_hub_nodes_and_their_flags(tmp_path, capsys, monkeypatch):
    cfg = _bare(tmp_path, monkeypatch)
    _new(cfg, "widget", "product", "open")
    _new(cfg, "pause", "decision", "hold", "--rel", "touches:widget")
    capsys.readouterr()

    assert main(["ledger", "lens", "product", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "widget" in out
    assert "held" in out
    assert "pause" in out          # the flag names its origin, not just its kind


def test_an_unknown_lens_is_refused_and_lists_the_known(tmp_path, capsys, monkeypatch):
    cfg = _bare(tmp_path, monkeypatch)
    capsys.readouterr()
    rc = main(["ledger", "lens", "nope", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "product" in out and "capability" in out and "decision" in out


def test_flag_filter_narrows_the_result(tmp_path, capsys, monkeypatch):
    cfg = _bare(tmp_path, monkeypatch)
    _new(cfg, "flagged-product", "product", "open")
    _new(cfg, "clean-product", "product", "open")
    _new(cfg, "pause", "decision", "hold", "--rel", "touches:flagged-product")
    capsys.readouterr()

    assert main(["ledger", "lens", "product", "--flag", "held", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "flagged-product" in out
    assert "clean-product" not in out


def test_dangling_relations_are_reported(tmp_path, capsys, monkeypatch):
    cfg = _bare(tmp_path, monkeypatch)
    _new(cfg, "widget", "product", "open")
    _new(cfg, "orphan", "item", "open", "--rel", "touches:ghost")
    capsys.readouterr()

    assert main(["ledger", "lens", "product", "--config", str(cfg)]) == 0
    assert "ghost" in capsys.readouterr().out


def test_an_empty_ledger_says_so_rather_than_printing_nothing(tmp_path, capsys, monkeypatch):
    cfg = _bare(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["ledger", "lens", "product", "--config", str(cfg)]) == 0
    assert "no " in capsys.readouterr().out.lower()


def test_hops_widens_the_membership(tmp_path, capsys, monkeypatch):
    cfg = _bare(tmp_path, monkeypatch)
    _new(cfg, "widget", "product", "open")
    _new(cfg, "near", "item", "open", "--rel", "touches:widget")
    _new(cfg, "far", "item", "open", "--rel", "touches:near")
    capsys.readouterr()

    main(["ledger", "lens", "product", "--config", str(cfg)])
    one_hop = capsys.readouterr().out
    main(["ledger", "lens", "product", "--hops", "2", "--config", str(cfg)])
    two_hop = capsys.readouterr().out

    assert "near" in one_hop and "far" not in one_hop
    assert "far" in two_hop


def test_the_configured_stale_days_reaches_the_computation(tmp_path, capsys, monkeypatch):
    """The Self-Review's one accepted risk, pinned.

    `compute_flags` takes `stale_days` explicitly so tests can fix a
    date; that means the CLI has to PASS the config value, and nothing
    but a test would notice if it silently used a default instead. A
    1-day threshold must flag a node an 3650-day one must not.
    """
    store = _store(tmp_path / "store")
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    monkeypatch.delenv("SPOKE_STORE_PATH", raising=False)

    tight = tmp_path / "tight.toml"
    tight.write_text(f'[store]\npath = "{store}"\n\n[checks]\nstale_days = 1\n')
    loose = tmp_path / "loose.toml"
    loose.write_text(f'[store]\npath = "{store}"\n\n[checks]\nstale_days = 200000\n')

    _new(tight, "widget", "product", "open")
    _new(tight, "old", "item", "open", "--rel", "touches:widget")
    # `updated` is stamped by the store on write, so age it by hand --
    # the only way to make "old" actually old inside a test.
    node_file = store / "ledger" / "old.md"
    text = node_file.read_text()
    assert "updated:" in text, text
    node_file.write_text(
        "\n".join(
            "updated: 2000-01-01" if line.startswith("updated:") else line
            for line in text.splitlines()
        )
        + "\n"
    )
    capsys.readouterr()

    main(["ledger", "lens", "product", "--config", str(tight)])
    assert "stale" in capsys.readouterr().out

    main(["ledger", "lens", "product", "--config", str(loose)])
    assert "stale" not in capsys.readouterr().out


def test_matrix_prints_the_grid_and_recomputes_the_count(tmp_path, capsys, monkeypatch):
    cfg = _bare(tmp_path, monkeypatch)
    _new(cfg, "gate", "capability", "open")
    _new(cfg, "alpha", "product", "open")
    _new(cfg, "beta", "product", "open")
    _new(cfg, "wire-alpha", "item", "open", "--rel", "touches:alpha", "--rel", "touches:gate")
    capsys.readouterr()

    assert main(["ledger", "matrix", "product", "capability", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "gate: 1 of 2 products" in out
    assert "alpha" in out and "beta" in out


def test_matrix_refuses_an_unknown_lens_on_either_axis(tmp_path, capsys, monkeypatch):
    cfg = _bare(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["ledger", "matrix", "product", "nope", "--config", str(cfg)]) != 0
    assert "known lenses" in capsys.readouterr().out
    assert main(["ledger", "matrix", "nope", "product", "--config", str(cfg)]) != 0
    assert "known lenses" in capsys.readouterr().out


def test_matrix_on_an_empty_ledger_names_the_empty_side(tmp_path, capsys, monkeypatch):
    cfg = _bare(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["ledger", "matrix", "product", "capability", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "no product nodes" in out and "no capability nodes" in out


def test_the_output_names_hubs_and_spokes(tmp_path, capsys, monkeypatch):
    """Hub and spoke is the tool's own metaphor, so the surface has to
    use the words: the node a lens anchors on is a hub, and what it pulls
    in around it are its spokes."""
    cfg = _bare(tmp_path, monkeypatch)
    _new(cfg, "widget", "product", "open")
    _new(cfg, "near", "item", "open", "--rel", "touches:widget")
    capsys.readouterr()

    assert main(["ledger", "lens", "product", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "hub widget: product open" in out
    assert "spokes: near" in out
