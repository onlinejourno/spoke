"""`spoke scan` -- and the rule that makes the map derived, not accumulated."""
from pathlib import Path
import subprocess

from tests.conftest import build_repo, build_store

from spoke.cli import main
from spoke.ledger.store import LedgerStore


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True)


def _code_repo(root: Path, files: dict[str, str]) -> Path:
    return build_repo(root, files)



def _store(root: Path) -> Path:
    return build_store(root)


def _setup(tmp_path, monkeypatch, projects: dict[str, Path]) -> tuple[Path, Path]:
    """Both SPOKE_REGISTRY and SPOKE_STORE_PATH are pointed at scratch
    paths. The registry ALONE is not isolation: store resolution still
    falls back to config.toml, which is how a stray node once reached a
    live store."""
    reg = tmp_path / "projects.toml"
    lines = []
    for name, repo in projects.items():
        store = _store(tmp_path / f"store-{name}")
        lines += [f'[projects."{name}"]',
                  f'repos = ["{repo}"]',
                  f'memory_store = "{store}"', ""]
    reg.write_text("\n".join(lines))
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    monkeypatch.delenv("SPOKE_STORE_PATH", raising=False)
    monkeypatch.delenv("SPOKE_PROJECT", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[checks]\nstale_days = 30\n")
    return reg, cfg


def _one(tmp_path, monkeypatch):
    repo = _code_repo(tmp_path / "mono", {
        "apps/alpha/package.json": '{"name": "alpha"}',
        "docs/adr/0001-do-it.md": "# Do it\n\nStatus: Accepted\n",
        "README.md": "# mono\n",
    })
    reg, cfg = _setup(tmp_path, monkeypatch, {"one": repo})
    return cfg, tmp_path / "store-one"


def test_scan_without_write_writes_nothing(tmp_path, capsys, monkeypatch):
    cfg, store = _one(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["scan", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "would create" in out
    assert not (store / "ledger").exists()


def test_scan_write_creates_the_nodes(tmp_path, capsys, monkeypatch):
    cfg, store = _one(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["scan", "--write", "--config", str(cfg)]) == 0
    names = sorted(p.name for p in (store / "ledger").glob("*.md"))
    assert "alpha.md" in names
    assert "mono-0001-do-it.md" in names
    assert "mono-docs.md" in names


def test_a_second_scan_write_is_a_no_op(tmp_path, capsys, monkeypatch):
    """The map is derived. Re-deriving it must not churn the store."""
    cfg, store = _one(tmp_path, monkeypatch)
    main(["scan", "--write", "--config", str(cfg)])
    head = subprocess.run(["git", "-C", str(store), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout
    capsys.readouterr()
    assert main(["scan", "--write", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "0 written" in out
    after = subprocess.run(["git", "-C", str(store), "rev-parse", "HEAD"],
                           capture_output=True, text=True, check=True).stdout
    assert head == after


def test_a_human_edited_node_is_reported_not_overwritten(tmp_path, capsys, monkeypatch):
    """Editing the markdown IS the point of a markdown store. That edit
    must survive the next scan."""
    cfg, store = _one(tmp_path, monkeypatch)
    main(["scan", "--write", "--config", str(cfg)])
    node_file = store / "ledger" / "alpha.md"
    edited = node_file.read_text() + "\nA human added this line.\n"
    node_file.write_text(edited)
    capsys.readouterr()

    assert main(["scan", "--write", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "DIVERGED" in out and "alpha" in out
    assert "A human added this line." in node_file.read_text()


def test_scan_refuses_when_two_projects_are_registered_and_none_is_named(
    tmp_path, capsys, monkeypatch
):
    a = _code_repo(tmp_path / "a", {"package.json": '{"name": "a"}'})
    b = _code_repo(tmp_path / "b", {"package.json": '{"name": "b"}'})
    _reg, cfg = _setup(tmp_path, monkeypatch, {"aye": a, "bee": b})
    capsys.readouterr()
    rc = main(["scan", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "aye" in out and "bee" in out


def test_the_active_project_name_appears_in_the_output(tmp_path, capsys, monkeypatch):
    cfg, _store = _one(tmp_path, monkeypatch)
    capsys.readouterr()
    main(["scan", "--config", str(cfg)])
    assert "project: one" in capsys.readouterr().out


def test_the_proposed_vocabulary_names_the_command_that_accepts_it(
    tmp_path, capsys, monkeypatch
):
    cfg, _store = _one(tmp_path, monkeypatch)
    capsys.readouterr()
    main(["scan", "--config", str(cfg)])
    out = capsys.readouterr().out
    # quoted, because a proposed word can contain a space
    assert "projects vocabulary --set 'product=app'" in out


def test_an_accepted_vocabulary_is_rendered_and_no_longer_proposed(
    tmp_path, capsys, monkeypatch
):
    cfg, _store = _one(tmp_path, monkeypatch)
    assert main(["projects", "vocabulary", "--set", "product=app", "--project", "one"]) == 0
    capsys.readouterr()
    main(["scan", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "[app]" in out
    assert "proposes calling product" not in out


def test_vocabulary_refuses_a_type_that_does_not_exist(tmp_path, capsys, monkeypatch):
    _cfg, _store = _one(tmp_path, monkeypatch)
    capsys.readouterr()
    rc = main(["projects", "vocabulary", "--set", "widget=thing", "--project", "one"])
    assert rc != 0
    assert "unknown node type" in capsys.readouterr().out


def test_deleting_the_registry_and_re_scanning_reproduces_the_same_map(
    tmp_path, capsys, monkeypatch
):
    """Criterion 6: derived, not accumulated."""
    cfg, store = _one(tmp_path, monkeypatch)
    main(["scan", "--write", "--config", str(cfg)])
    first = {p.name: p.read_text() for p in (store / "ledger").glob("*.md")}

    reg = tmp_path / "projects.toml"
    saved = reg.read_text()
    reg.unlink()
    reg.write_text(saved)          # same registry, rebuilt from nothing

    capsys.readouterr()
    main(["scan", "--write", "--config", str(cfg)])
    second = {p.name: p.read_text() for p in (store / "ledger").glob("*.md")}
    assert first == second


def test_the_written_nodes_are_answerable_through_a_lens(tmp_path, capsys, monkeypatch):
    """The point of the scan: the board has something to show."""
    cfg, _store = _one(tmp_path, monkeypatch)
    main(["scan", "--write", "--config", str(cfg)])
    capsys.readouterr()
    assert main(["ledger", "lens", "product", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "hub alpha: product open" in out
    assert "spokes:" in out


def test_scan_never_writes_over_a_node_it_cannot_read(tmp_path, capsys, monkeypatch):
    """A node that EXISTS but will not parse is not an absent node.

    `except (FileNotFoundError, LedgerError): existing = None` folded the
    two together, so an unreadable file was treated as missing and
    `scan --write` wrote straight over it -- destroying, for instance, a
    half-finished hand edit that broke the YAML. A broken edit needs the
    divergence rule more than a clean one, not less.
    """
    cfg, store = _one(tmp_path, monkeypatch)
    assert main(["scan", "--write", "--config", str(cfg)]) == 0

    node_file = store / "ledger" / "alpha.md"
    broken = "---\nname: mono-alpha\ntype: [unclosed\n---\n\nhalf an edit\n"
    node_file.write_text(broken)
    capsys.readouterr()

    assert main(["scan", "--write", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "UNREADABLE" in out and "alpha" in out
    assert node_file.read_text() == broken
