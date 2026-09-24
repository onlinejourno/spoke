import json
from datetime import date
import httpx
from spoke.config import Config
from spoke.doctor import run_doctor


def _store(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\n---\n\nclean\n")
    return Config(tmp_path, (), 30, "groq", None)


def _client():
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))


def test_first_run_records_state(tmp_path):
    cfg = _store(tmp_path)
    state = tmp_path / "state.json"
    res = run_doctor(cfg, _client(), state, date(2026, 9, 8))
    assert res.new == [] and res.total == 0
    assert json.loads(state.read_text())["last_run"] == "2026-09-08"


def test_only_new_defects_are_reported(tmp_path):
    cfg = _store(tmp_path)
    state = tmp_path / "state.json"
    run_doctor(cfg, _client(), state, date(2026, 9, 8))
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\n---\n\n[[ghost]]\n")
    res = run_doctor(cfg, _client(), state, date(2026, 9, 9))
    assert [f.detail for f in res.new] == ["ghost"]
    res2 = run_doctor(cfg, _client(), state, date(2026, 9, 10))
    assert res2.new == [] and res2.total == 1


def test_resolved_defects_are_reported(tmp_path):
    cfg = _store(tmp_path)
    state = tmp_path / "state.json"
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\n---\n\n[[ghost]]\n")
    run_doctor(cfg, _client(), state, date(2026, 9, 8))
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\n---\n\nclean\n")
    res = run_doctor(cfg, _client(), state, date(2026, 9, 9))
    assert [f.detail for f in res.resolved] == ["ghost"]


def test_two_consecutive_clean_runs_both_update_last_run(tmp_path):
    """A watcher that cannot be distinguished from silence is the failure it
    exists to prevent -- last_run must advance on a clean run, not just
    when something is found."""
    cfg = _store(tmp_path)
    state = tmp_path / "state.json"
    res1 = run_doctor(cfg, _client(), state, date(2026, 9, 8))
    first_last_run = json.loads(state.read_text())["last_run"]
    assert res1.new == [] and res1.total == 0
    assert first_last_run == "2026-09-08"

    res2 = run_doctor(cfg, _client(), state, date(2026, 9, 9))
    second_last_run = json.loads(state.read_text())["last_run"]
    assert res2.new == [] and res2.total == 0
    assert second_last_run == "2026-09-09"
    assert second_last_run != first_last_run
    # And the second run can see that a previous run happened, and when.
    assert res2.previous_run == "2026-09-08"


def test_pipe_in_detail_round_trips(tmp_path):
    """A wikilink target containing '|' (e.g. [[ghost|alias]]) becomes a
    Finding whose detail contains '|'. The key encoding/decoding must not
    truncate or corrupt it, or the same defect would spuriously reappear as
    'new' on every run forever."""
    cfg = _store(tmp_path)
    state = tmp_path / "state.json"
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\n---\n\n[[ghost|alias]]\n")

    res1 = run_doctor(cfg, _client(), state, date(2026, 9, 8))
    assert res1.total == 1
    assert res1.new[0].detail == "ghost|alias"

    # Nothing changed -- a correct round trip means this is NOT reported as new.
    res2 = run_doctor(cfg, _client(), state, date(2026, 9, 9))
    assert res2.new == []
    assert res2.total == 1


def test_corrupt_state_file_is_reset_not_fatal(tmp_path):
    """A corrupt/truncated/hand-edited state file must not take the watcher
    down on the next cron run -- it is treated as no previous state, the
    reset is reported visibly, and a valid state file is rewritten."""
    cfg = _store(tmp_path)
    state = tmp_path / "state.json"
    state.write_text("{not valid json, this is garbage")

    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\n---\n\n[[ghost]]\n")
    res = run_doctor(cfg, _client(), state, date(2026, 9, 8))

    # Run completes and reports the finding as new (previous state is empty).
    assert [f.detail for f in res.new] == ["ghost"]
    assert res.total == 1
    # The reset is named, not swallowed silently.
    assert res.state_reset is not None
    assert str(state) in res.state_reset

    # A valid state file is rewritten -- json.loads must succeed, and the
    # next run must see this run's finding as no-longer-new.
    written = json.loads(state.read_text())
    assert written["last_run"] == "2026-09-08"
    res2 = run_doctor(cfg, _client(), state, date(2026, 9, 9))
    assert res2.new == [] and res2.total == 1
    assert res2.state_reset is None


def test_malformed_but_valid_json_state_is_also_reset(tmp_path):
    """Valid JSON that doesn't have the expected shape (e.g. a findings key
    that isn't a list of 'kind|file|detail' strings) must also be handled
    explicitly, not raise out of run_doctor."""
    cfg = _store(tmp_path)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_run": "2026-01-01", "findings": ["not-enough-pipes"]}))

    res = run_doctor(cfg, _client(), state, date(2026, 9, 8))
    assert res.state_reset is not None
    assert res.total == 0
