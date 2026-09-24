import httpx
from spoke.cli import main


class _FakeResponse:
    """Stands in for an httpx.Response carrying a chat-completion body,
    so `spoke.llm.complete` gets real-shaped JSON back without any
    network call ever happening."""

    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _pin_a_model(monkeypatch):
    """A cloud provider with no model pinned REFUSES rather than
    defaulting -- the estate's fail-closed ruling. A test that exercises
    a model path has to say which model, exactly as a real run does."""
    monkeypatch.setenv("LLM_MODEL", "test-model-not-real")


def _stub_model_response(monkeypatch, content: str):
    _pin_a_model(monkeypatch)
    # Patches the one seam every real model call passes through
    # (httpx.Client.post, called from spoke.llm.complete) so the CLI's
    # real code path runs end to end with no network or API key needed --
    # only the wire response is faked. Same content for every call.
    def fake_post(self, url, headers=None, json=None, **kwargs):
        return _FakeResponse(content)
    monkeypatch.setattr(httpx.Client, "post", fake_post)


def _stub_model_response_by_prompt(monkeypatch, respond):
    _pin_a_model(monkeypatch)
    # Like _stub_model_response, but `respond(prompt) -> content` lets a
    # test answer differently per cluster -- needed to exercise a genuine
    # partial failure (one cluster's call succeeds, another's does not)
    # at the CLI level rather than only at the find_contradictions level.
    def fake_post(self, url, headers=None, json=None, **kwargs):
        prompt = json["messages"][0]["content"]
        return _FakeResponse(respond(prompt))
    monkeypatch.setattr(httpx.Client, "post", fake_post)


def _store_with_two_independent_clusters(tmp_path):
    # Two disjoint pairs, each sharing its own rare multi-word entity and
    # nothing else -- see tests/test_contradict.py::
    # _seed_two_independent_clusters for the identical construction and
    # why it produces two separate clusters, both sent.
    (tmp_path / "MEMORY.md").write_text("")

    def w(name, body):
        (tmp_path / name).write_text(
            f"---\nname: {name[:-3]}\ndescription: d\nmetadata:\n  type: user\n---\n\n{body}\n")

    w("p1.md", "Meeting notes about the Quazzlefrond Summit next week.")
    w("p2.md", "The Quazzlefrond Summit wrapped up successfully.")
    w("q1.md", "Discussion of the Blibberwocky Accord starting soon.")
    w("q2.md", "The Blibberwocky Accord was finalized yesterday.")
    return tmp_path


def _store_with_a_cluster(tmp_path):
    # a/b/c cluster on rare shared entities (see test_contradict.py for the
    # same construction and why it produces overlapping groups); this store
    # has real work for the contradiction pass to do.
    (tmp_path / "MEMORY.md").write_text("")

    def w(name, typ, body):
        (tmp_path / name).write_text(
            f"---\nname: {name[:-3]}\ndescription: d\nmetadata:\n  type: {typ}\n---\n\n{body}\n")

    w("a.md", "user", "Currently discussing the Zebracorn Protocol and separately the Wombat Circus initiative.")
    w("b.md", "user", "The Zebracorn Protocol was finished last week.")
    w("c.md", "user", "The Wombat Circus initiative wrapped up too.")
    return tmp_path


def _empty_store(tmp_path):
    (tmp_path / "MEMORY.md").write_text("")
    return tmp_path


def test_no_credential_exits_nonzero_and_names_the_missing_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("SPOKE_STORE_PATH", str(_store_with_a_cluster(tmp_path)))
    assert main(["contradictions"]) == 2
    out = capsys.readouterr().out
    assert "LLM_API_KEY" in out
    assert "NOT RUN" in out


def test_reports_cluster_and_pair_counts_before_any_model_call(tmp_path, monkeypatch, capsys):
    # Even with no credential, the cost-shape report must print before the
    # (failing) model call is attempted -- a user must see the size of the
    # bill before it is incurred, or refused.
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("SPOKE_STORE_PATH", str(_store_with_a_cluster(tmp_path)))
    main(["contradictions"])
    out = capsys.readouterr().out
    assert "cluster(s)" in out
    assert "distinct pair(s)" in out


def test_empty_store_needs_no_credential_and_exits_zero(tmp_path, monkeypatch, capsys):
    # No clusters means no pairs means no model call is ever attempted, so
    # a missing credential is irrelevant and must not block a no-op run.
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("SPOKE_STORE_PATH", str(_empty_store(tmp_path)))
    assert main(["contradictions"]) == 0
    out = capsys.readouterr().out
    assert "0 cluster(s) total, 0 to send, 0 distinct pair(s) covered" in out


def test_all_unparseable_responses_exit_non_zero(tmp_path, monkeypatch, capsys):
    # Every cluster's response is a refusal, not JSON. The bill was paid
    # (a real, present credential, a real call attempted) and nothing
    # came back usable -- that must not look like a clean store. This is
    # the exact failure shape this project exists to catch: a step that
    # ran, printed, and passed while doing nothing.
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")
    monkeypatch.setenv("SPOKE_STORE_PATH", str(_store_with_a_cluster(tmp_path)))
    _stub_model_response(monkeypatch, "I'm sorry, I can't help with that.")

    rc = main(["contradictions"])
    out = capsys.readouterr().out
    assert rc != 0, out
    assert "could not be parsed" in out
    assert "0 candidate(s)" in out


def test_malformed_items_are_reported_in_cli_output(tmp_path, monkeypatch, capsys):
    # I3: a response that parses as JSON but whose object uses the wrong
    # key names must be visible in the CLI output, not silently absorbed
    # into "0 candidate(s)".
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")
    monkeypatch.setenv("SPOKE_STORE_PATH", str(_store_with_a_cluster(tmp_path)))
    _stub_model_response(monkeypatch, '[{"fileA": "a.md", "fileB": "b.md"}]')

    rc = main(["contradictions"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "1 item(s)" in out
    assert "missing required fields" in out
    assert "0 candidate(s)" in out


def test_partial_unparseable_is_reported_but_not_fatal(tmp_path, monkeypatch, capsys):
    # One of two independent clusters parses (a real finding comes back),
    # the other returns junk. This must be reported -- the WARNING line
    # with the count -- but must NOT trip the "every sent cluster failed"
    # fail-closed exit, since some of the run genuinely worked.
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")
    monkeypatch.setenv("SPOKE_STORE_PATH", str(_store_with_two_independent_clusters(tmp_path)))

    def respond(prompt):
        if "Quazzlefrond" in prompt:
            return ('[{"file_a": "p1.md", "file_b": "p2.md", '
                     '"quote_a": "Meeting notes about the Quazzlefrond Summit next week.", '
                     '"quote_b": "The Quazzlefrond Summit wrapped up successfully.", '
                     '"reason": "one says upcoming, the other says wrapped up"}]')
        return "I'm sorry, I can't help with that."
    _stub_model_response_by_prompt(monkeypatch, respond)

    rc = main(["contradictions"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "WARNING" in out
    assert "1 of 2 cluster response(s) could not be parsed" in out
    assert "1 candidate(s)" in out
