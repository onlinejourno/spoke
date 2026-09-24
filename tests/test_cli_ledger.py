from datetime import date
from pathlib import Path
import subprocess

from tests.conftest import build_store
import pytest
from spoke.cli import main
from spoke.ledger.store import LedgerStore


def _store(tmp_path: Path) -> Path:
    build_store(tmp_path)
    return tmp_path


def _cfg(tmp_path: Path, store: Path) -> Path:
    f = tmp_path / "config.toml"
    f.write_text(f'[store]\npath = "{store}"\n')
    return f


def test_new_then_list(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    assert main(["ledger", "new", "alpha", "--type", "item", "--state", "open",
                 "--title", "A thing", "--body", "why", "--config", str(cfg)]) == 0
    assert main(["ledger", "list", "--config", str(cfg)]) == 0
    assert "alpha" in capsys.readouterr().out


def test_new_deferred_without_a_ruling_is_refused(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    rc = main(["ledger", "new", "beta", "--type", "item", "--state", "deferred",
               "--title", "A deferral", "--body", "why", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "ruling" in out
    assert not (store / "ledger" / "beta.md").exists()


def test_new_deferred_with_a_ruling_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    assert main(["ledger", "new", "beta", "--type", "item", "--state", "deferred",
                 "--title", "A deferral", "--ruling", "not now; store is being rewritten",
                 "--body", "why", "--config", str(cfg)]) == 0


def test_show_prints_the_body_and_relations(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    main(["ledger", "new", "gamma", "--type", "item", "--state", "open", "--title", "T",
          "--rel", "touches:some-product", "--body", "why", "--config", str(cfg)])
    main(["ledger", "show", "gamma", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "touches" in out and "some-product" in out


# --- Finding 1: a NUL byte / control character reaching `ledger show`
# must be a clean refusal, never a raw traceback. ---

@pytest.mark.parametrize("bad", ["a\x00b", "a\nb"])
def test_show_refuses_a_control_character_cleanly(tmp_path, capsys, monkeypatch, bad):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    rc = main(["ledger", "show", bad, "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "refused" in out
    assert "Traceback" not in out


# --- Finding 2: `ledger list` must exclude a symlink planted inside
# ledger/ and report the exclusion rather than staying silent about it. ---

def test_list_reports_a_skipped_symlink_and_preamble_does_not_leak_it(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    main(["ledger", "new", "alpha", "--type", "item", "--state", "open",
          "--title", "A thing", "--body", "why", "--config", str(cfg)])

    secret = tmp_path / "secret.md"
    secret.write_text(
        "---\nname: secret\ntype: item\nstate: open\ntitle: t\n---\n\nTOP-SECRET-OUTSIDE-CONTENT\n"
    )
    (store / "ledger" / "evil.md").symlink_to(secret)

    rc = main(["ledger", "list", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out
    # the kind is NAMED now, not fused into "outside ledger/ or could not be read"
    assert "1 entry resolved outside ledger/" in out
    assert "evil.md" in out

    main(["ledger", "preamble", "--config", str(cfg)])
    preamble_out = capsys.readouterr().out
    assert "TOP-SECRET-OUTSIDE-CONTENT" not in preamble_out
    # Finding 3: `ledger preamble` must report the skip exactly as
    # `ledger list` does -- this is the surface that runs unasked at
    # session start, so silence here matters more than anywhere else.
    assert "1 entry resolved outside ledger/" in preamble_out
    assert "evil.md" in preamble_out


# --- Finding 4: an unruled `abandoned`/`deviated` node must carry the
# same [UNRULED] marker as an unruled `deferred` one in `ledger list`, and
# must outrank a closed `done` item in `ledger preamble`; a ruled `done`
# item must not appear in the preamble at all. ---

def test_list_marks_unruled_abandoned_and_deviated_not_just_deferred(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    # validate() refuses an unruled abandoned/deviated node on a fresh
    # write, so these are written directly to disk (as an existing,
    # already-on-disk record would be) to reproduce the "existing node"
    # case ledger list/preamble must still catch.
    (store / "ledger").mkdir()
    (store / "ledger" / "gone.md").write_text(
        "---\nname: gone\ntype: item\nstate: abandoned\ntitle: t\n---\n\nbody\n"
    )
    (store / "ledger" / "changed.md").write_text(
        "---\nname: changed\ntype: item\nstate: deviated\ntitle: t\n---\n\nbody\n"
    )

    rc = main(["ledger", "list", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "gone: item abandoned [UNRULED]" in out
    assert "changed: item deviated [UNRULED]" in out


def test_preamble_surfaces_unruled_abandoned_above_a_done_item_and_hides_a_ruled_done(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    (store / "ledger").mkdir()
    (store / "ledger" / "gone.md").write_text(
        "---\nname: gone\ntype: item\nstate: abandoned\ntitle: t\n---\n\nbody\n"
    )
    (store / "ledger" / "finished.md").write_text(
        "---\nname: finished\ntype: item\nstate: done\ntitle: t\n---\n\nbody\n"
    )

    rc = main(["ledger", "preamble", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "gone" in out
    assert "finished" not in out


# --- Finding 4: `--budget` must document its default and that it is a
# fixed fallback, not yet scaled to the model's context window. ---

def test_preamble_help_documents_the_budget_default_and_its_status(capsys):
    with pytest.raises(SystemExit):
        main(["ledger", "preamble", "--help"])
    out = capsys.readouterr().out
    assert "4000" in out
    assert "not yet scaled" in out


# --- Design mismatch fix: render_preamble() was designed on the
# assumption that the tool controls what reaches a session, so memory
# index lines and ledger items compete inside one budget. Claude Code
# injects MEMORY.md natively at SessionStart and the hook cannot prevent
# it, so wiring the shared-budget behaviour as-is duplicates ~3,900
# tokens of content the session already has. --ledger-only renders
# ledger items only, passing no memory index lines; off by default so
# the existing shared-budget behaviour (and its tests) are untouched. ---

def test_ledger_only_omits_memory_index_lines(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")  # writes MEMORY.md with a "- [a](a.md)" line
    cfg = _cfg(tmp_path, store)
    main(["ledger", "new", "alpha", "--type", "item", "--state", "open",
          "--title", "A thing", "--body", "why", "--config", str(cfg)])

    rc = main(["ledger", "preamble", "--ledger-only", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out
    assert not any(line.startswith("- [") for line in out.splitlines())


def test_without_the_flag_memory_index_lines_still_appear(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    main(["ledger", "new", "alpha", "--type", "item", "--state", "open",
          "--title", "A thing", "--body", "why", "--config", str(cfg)])

    rc = main(["ledger", "preamble", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert any(line.startswith("- [") for line in out.splitlines())


def test_ledger_only_with_no_open_items_says_so_honestly_not_empty(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")  # has a memory index but no ledger/ nodes
    cfg = _cfg(tmp_path, store)

    rc = main(["ledger", "preamble", "--ledger-only", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no open ledger items" in out.lower()
    assert not any(line.startswith("- [") for line in out.splitlines())


def test_ledger_only_help_explains_why_it_exists(capsys):
    with pytest.raises(SystemExit):
        main(["ledger", "preamble", "--help"])
    out = capsys.readouterr().out
    assert "--ledger-only" in out
    assert "Claude Code" in out
    assert "SessionStart" in out
    assert "duplicat" in out.lower()


# --- Minor 6: `ledger set` -- the only gated way to change an existing
# node's state. Before this there was no way to mark a node `done` other
# than hand-editing the .md file, which bypasses validate() entirely. ---

def test_set_deferred_without_a_ruling_is_refused_and_the_file_is_unchanged(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    main(["ledger", "new", "delta", "--type", "item", "--state", "open",
          "--title", "A thing", "--body", "why", "--config", str(cfg)])
    before = (store / "ledger" / "delta.md").read_text()

    rc = main(["ledger", "set", "delta", "--state", "deferred", "--config", str(cfg)])
    out = capsys.readouterr().out

    assert rc != 0
    assert "ruling" in out
    assert (store / "ledger" / "delta.md").read_text() == before


def test_set_deferred_with_a_ruling_succeeds_and_the_file_reflects_it(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    main(["ledger", "new", "delta", "--type", "item", "--state", "open",
          "--title", "A thing", "--body", "why", "--config", str(cfg)])

    rc = main(["ledger", "set", "delta", "--state", "deferred",
               "--ruling", "not now; store is being rewritten", "--config", str(cfg)])
    out = capsys.readouterr().out

    assert rc == 0, out
    text = (store / "ledger" / "delta.md").read_text()
    assert "state: deferred" in text
    assert "not now; store is being rewritten" in text
    # A human: provenance entry was appended for the changed fields.
    assert "human:" in text


def test_set_a_nonexistent_node_reports_cleanly(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    rc = main(["ledger", "set", "nope", "--state", "done", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "no such node" in out


def test_set_with_no_flags_at_all_is_refused(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    main(["ledger", "new", "delta", "--type", "item", "--state", "open",
          "--title", "A thing", "--body", "why", "--config", str(cfg)])
    rc = main(["ledger", "set", "delta", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "at least one" in out


# --- gap fix: `ledger set` could not touch the body -- a node could record
# that a state changed but not what actually happened. --append-body and
# --body close that; both go through the same gate as every other field. ---

def test_append_body_keeps_what_was_there(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    main(["ledger", "new", "n", "--type", "item", "--state", "open",
          "--title", "A thing", "--body", "original\n", "--config", str(cfg)])

    rc = main(["ledger", "set", "n", "--append-body", "later note", "--config", str(cfg)])
    assert rc == 0

    body = LedgerStore(store).read("n").body
    assert "original" in body and "later note" in body
    assert body.index("original") < body.index("later note")


def test_body_replaces(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    main(["ledger", "new", "n", "--type", "item", "--state", "open",
          "--title", "A thing", "--body", "original\n", "--config", str(cfg)])

    rc = main(["ledger", "set", "n", "--body", "replaced", "--config", str(cfg)])
    assert rc == 0

    body = LedgerStore(store).read("n").body
    assert "original" not in body
    assert "replaced" in body


def test_body_and_append_body_together_are_refused(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    main(["ledger", "new", "n", "--type", "item", "--state", "open",
          "--title", "A thing", "--body", "original\n", "--config", str(cfg)])
    before = (store / "ledger" / "n.md").read_text()

    rc = main(["ledger", "set", "n", "--body", "a", "--append-body", "b", "--config", str(cfg)])
    out = capsys.readouterr().out

    assert rc != 0
    assert "mutually exclusive" in out
    assert (store / "ledger" / "n.md").read_text() == before


def test_appending_records_provenance_and_updates_the_date(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    main(["ledger", "new", "n", "--type", "item", "--state", "open",
          "--title", "A thing", "--body", "original\n", "--config", str(cfg)])

    rc = main(["ledger", "set", "n", "--append-body", "later note", "--config", str(cfg)])
    assert rc == 0

    node = LedgerStore(store).read("n")
    # `ledger new` already recorded one body provenance entry (body is a
    # required field there); `set` must append a second one for this edit,
    # not replace or skip it.
    body_provenance = [p for p in node.provenance if p.get("field") == "body"]
    assert len(body_provenance) == 2
    assert body_provenance[-1]["by"].startswith("human:")
    assert node.updated == date.today().isoformat()


def test_the_gate_still_runs_on_a_body_edit(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    main(["ledger", "new", "n", "--type", "item", "--state", "open",
          "--title", "A thing", "--body", "original\n", "--config", str(cfg)])
    main(["ledger", "set", "n", "--state", "deferred", "--ruling", "not now",
          "--config", str(cfg)])

    # A node already deferred with a ruling stays valid when only the body
    # changes -- the ruling is untouched, so the gate has nothing to refuse.
    rc = main(["ledger", "set", "n", "--append-body", "still not now",
               "--config", str(cfg)])
    assert rc == 0
    node = LedgerStore(store).read("n")
    assert "still not now" in node.body
    assert node.ruling == "not now"

    # But blanking the ruling in the SAME call that edits the body is
    # refused -- the gate runs on the whole node, not just the body -- and
    # the file on disk is unchanged.
    before = (store / "ledger" / "n.md").read_text()
    rc = main(["ledger", "set", "n", "--ruling", "", "--append-body", "gone",
               "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "ruling" in out
    assert (store / "ledger" / "n.md").read_text() == before


def test_a_former_name_can_be_recorded_and_keeps_old_references_working(
    tmp_path, capsys, monkeypatch
):
    """The residue of a rename, carried instead of reported as a defect.

    `no-renames-of-shipped-names` is a hold precisely because "a name
    simply does not get erased" -- every reference written before a
    rename still points at the old one. An alias is how the map keeps
    those working WITHOUT renaming anything, which supports that hold
    rather than challenging it.
    """
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "absent.toml"))
    store = _store(tmp_path / "store")
    cfg = _cfg(tmp_path, store)
    assert main(["ledger", "new", "spoke", "--type", "product", "--state", "open",
                 "--title", "Spoke", "--body", "why", "--alias", "memory-doctor",
                 "--config", str(cfg)]) == 0
    assert main(["ledger", "new", "a-canary", "--type", "decision", "--state", "open",
                 "--title", "A canary", "--body", "why",
                 "--rel", "touches:memory-doctor", "--config", str(cfg)]) == 0
    capsys.readouterr()

    assert main(["ledger", "lens", "product", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    # resolved: the old reference reaches the renamed node
    assert "spokes: a-canary" in out
    # and SAID SO: whoever wrote the old name learns the new one
    assert "through a former name" in out and "is now 'spoke'" in out
    assert "pointed at a node that does not exist" not in out

    capsys.readouterr()
    main(["ledger", "show", "spoke", "--config", str(cfg)])
    assert "also known as: memory-doctor" in capsys.readouterr().out


def test_every_ledger_write_command_says_so_and_exits_3_when_the_push_failed(tmp_path, monkeypatch, capsys):
    """'wrote' must never stand in for 'shared'. A store whose remote
    rejects the push gets the commit, a line saying the push failed and
    why, and a distinct exit code -- on every command that writes."""
    from tests.test_ledger_store import _repo_with_upstream
    _, work, _ = _repo_with_upstream(tmp_path, reject_push=True)
    monkeypatch.setenv("SPOKE_REGISTRY", str(tmp_path / "reg.toml"))
    cfg = tmp_path / "config.toml"; cfg.write_text(f'[store]\npath = "{work}"\n')
    args = ["--config", str(cfg)]

    rc = main(["ledger", "new", "n1", "--type", "item", "--state", "open", "--title", "N",
               "--body", "b", "--questions", *args])
    out = capsys.readouterr().out
    assert rc == 3, out
    assert "ledger: wrote n1" in out and "ledger: push failed - git-push-failed:" in out
    assert "local only" in out
    assert (work / "ledger" / "n1.md").exists()

    for argv in (["ledger", "set", "n1", "--append-body", "more"],
                 ["ledger", "tick", "n1", "1", "--note", "seen"],
                 ["ledger", "expect", "n1", "--url", "https://x.example/", "--status", "200"]):
        rc = main([*argv, *args])
        out = capsys.readouterr().out
        assert rc == 3, (argv, out)
        assert "ledger: push failed - git-push-failed:" in out, (argv, out)
