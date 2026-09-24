import json
import httpx
import pytest
from spoke.config import Config
from spoke.store import Store
from spoke.llm import LLMUnavailable
from spoke.checks.relate import cluster
from spoke.checks.contradict import find_contradictions


def _seed_two_independent_clusters(tmp_path):
    # Two disjoint pairs, each sharing its own rare multi-word entity and
    # nothing else -- p1/p2 share "Quazzlefrond Summit", q1/q2 share
    # "Blibberwocky Accord". Neither pair shares anything with the other,
    # so `plan_clusters` produces two separate 2-record clusters, both
    # sent (verified directly against the real cluster() before writing
    # this fixture). This is what lets a "partial failure" scenario exist
    # at all: one cluster's response can fail while the other's succeeds.
    (tmp_path / "MEMORY.md").write_text("")

    def w(name, body):
        (tmp_path / name).write_text(
            f"---\nname: {name[:-3]}\ndescription: d\nmetadata:\n  type: user\n---\n\n{body}\n")

    w("p1.md", "Meeting notes about the Quazzlefrond Summit next week.")
    w("p2.md", "The Quazzlefrond Summit wrapped up successfully.")
    w("q1.md", "Discussion of the Blibberwocky Accord starting soon.")
    w("q2.md", "The Blibberwocky Accord was finalized yesterday.")
    return Config(tmp_path, (), 30, "groq", None)


def _seed(tmp_path):
    (tmp_path / "MEMORY.md").write_text("")
    (tmp_path / "user_profile.md").write_text(
        "---\nname: user_profile\ndescription: d\nmetadata:\n  type: user\n---\n\n"
        "Currently on the Northwind account.\n")
    (tmp_path / "person-bio.md").write_text(
        "---\nname: person-bio\ndescription: d\nmetadata:\n  type: user\n---\n\n"
        "Northwind account closed, confirmed 2026-03-14. Now independent.\n")
    # A third, unrelated record. Without it, "type:user" and "Northwind" are
    # shared by 100% of the store (df == n == 2) and cluster() correctly
    # treats a ubiquitous signal as zero evidence (see relate.py and
    # test_relate.py::test_two_record_store_does_not_cluster_on_a_ubiquitous_signal)
    # -- the two records would never cluster and find_contradictions would
    # never see them. This third record is what test_relate.py itself adds
    # in the equivalent scenario to keep the shared signals rare instead of
    # universal.
    (tmp_path / "other.md").write_text(
        "---\nname: other\ndescription: d\nmetadata:\n  type: project\n---\n\n"
        "Notes about the Atlas deployment pipeline.\n")
    return Config(tmp_path, (), 30, "groq", None)


def test_reports_the_known_contradiction(tmp_path):
    cfg = _seed(tmp_path)

    def fake_complete(prompt, _cfg, _client):
        assert "Northwind" in prompt
        return json.dumps([{
            "file_a": "user_profile.md", "file_b": "person-bio.md",
            "quote_a": "Currently on the Northwind account.",
            "quote_b": "Northwind account closed, confirmed 2026-03-14.",
            "reason": "one says the engagement is current, the other that it ended",
        }])
    run = find_contradictions(Store(cfg.store_path), cfg, None, complete_fn=fake_complete)
    assert len(run.findings) == 1
    assert run.findings[0].files == ("user_profile.md", "person-bio.md")
    assert "ended" in run.findings[0].reason
    assert run.unparseable == 0
    assert run.errored == 0


def test_malformed_model_output_yields_nothing_not_a_crash(tmp_path):
    cfg = _seed(tmp_path)
    run = find_contradictions(Store(cfg.store_path), cfg, None,
                              complete_fn=lambda *a, **k: "not json at all")
    # No crash, and no silent loss either: this cluster's response could
    # not be used, and that must be counted, not just quietly dropped --
    # this is the exact swallow this project exists to prevent.
    assert run.findings == []
    assert run.unparseable == 1
    assert run.errored == 0


def test_missing_key_propagates_rather_than_skipping(tmp_path):
    cfg = _seed(tmp_path)

    def boom(*a, **k):
        raise LLMUnavailable("no key")
    with pytest.raises(LLMUnavailable):
        find_contradictions(Store(cfg.store_path), cfg, None, complete_fn=boom)


def test_a_pair_present_in_two_overlapping_clusters_is_prompted_once(tmp_path):
    # a shares one rare entity with b and a different rare entity with c, but
    # b and c share nothing with each other. cluster() (verified directly
    # below) then produces overlapping neighbourhoods -- [a,b,c], [a,b], and
    # [a,c] -- rather than a clean partition, so the (a, b) pair is a
    # candidate from two of those groups. Sending it to the model once per
    # group it appears in is money spent twice on the same question;
    # find_contradictions must dedupe it to a single call.
    (tmp_path / "MEMORY.md").write_text("")

    def w(name, typ, body):
        (tmp_path / name).write_text(
            f"---\nname: {name[:-3]}\ndescription: d\nmetadata:\n  type: {typ}\n---\n\n{body}\n")

    w("a.md", "user", "Currently discussing the Zebracorn Protocol and separately the Wombat Circus initiative.")
    w("b.md", "user", "The Zebracorn Protocol was finished last week.")
    w("c.md", "user", "The Wombat Circus initiative wrapped up too.")
    w("d.md", "project", "Notes about the Atlas deployment pipeline entirely unrelated.")
    w("e.md", "project", "Something about the Loupe render queue, nothing else.")
    w("f.md", "project", "Almanac calendar sync details only.")
    cfg = Config(tmp_path, (), 30, "groq", None)

    groups = cluster(Store(cfg.store_path))
    assert groups.count(["a.md", "b.md"]) == 1 and any(
        set(g) >= {"a.md", "b.md"} and len(g) > 2 for g in groups), groups

    calls = []

    def fake_complete(prompt, _cfg, _client):
        calls.append(prompt)
        return "[]"

    find_contradictions(Store(cfg.store_path), cfg, None, complete_fn=fake_complete)

    # The (a, b) pair is a candidate from more than one overlapping group.
    # It must only ever be sent to the model once: count how many distinct
    # prompts carry both a.md's and b.md's own text.
    both = [p for p in calls if "Zebracorn Protocol was finished last week" in p
            and "discussing the Zebracorn Protocol" in p]
    assert len(both) == 1, calls
    # And this must be ONE call for the whole [a,b,c] cluster, not three
    # separate calls for (a,b), (a,c), (b,c) -- the whole point of
    # clustering is to bound the bill to one call per cluster sent.
    assert len(calls) == 1, calls


def test_one_model_call_per_cluster_not_per_pair(tmp_path):
    # A single cluster of 5 records has 10 internal pairs (C(5,2) = 10).
    # Prompting per pair would cost 10 calls; prompting per cluster must
    # cost exactly ONE -- this is the whole saving clustering exists for.
    (tmp_path / "MEMORY.md").write_text("")
    for i in range(5):
        (tmp_path / f"m{i}.md").write_text(
            f"---\nname: m{i}\ndescription: d\nmetadata:\n  type: project\n"
            f"---\n\nsee [[hub]] for details.\n")
    # Padding so the shared link stays below relate.py's rarity cutoff
    # (doc_freq must be <= max(2, 0.2*n)); 5 shared + 20 filler = 25
    # records, threshold = max(2, 5) = 5, doc_freq(link:hub) = 5 <= 5.
    for i in range(20):
        (tmp_path / f"filler{i}.md").write_text(
            f"---\nname: filler{i}\ndescription: d\nmetadata:\n  type: project\n"
            f"---\n\nordinary notes with nothing shared.\n")
    cfg = Config(tmp_path, (), 30, "groq", None)

    groups = cluster(Store(cfg.store_path))
    assert groups == [["m0.md", "m1.md", "m2.md", "m3.md", "m4.md"]], groups

    calls = []

    def stub(prompt, _cfg, _client):
        calls.append(prompt)
        return "[]"

    find_contradictions(Store(cfg.store_path), cfg, None, complete_fn=stub)
    assert len(calls) == 1, f"expected 1 call for a 5-record cluster, got {len(calls)}"
    # The one call must carry every member's text, not just a pair of them.
    for i in range(5):
        assert f"m{i}.md" in calls[0]


def test_a_fully_covered_cluster_is_skipped(tmp_path):
    # a/b/c form one 3-member cluster [a,b,c] plus two smaller overlapping
    # sub-clusters [a,b] and [a,c] (see the overlapping-clusters test
    # above for the same construction). Once [a,b,c] is sent, both (a,b)
    # and (a,c) are already covered, so neither smaller cluster teaches
    # the model anything new -- both must be skipped, not sent.
    (tmp_path / "MEMORY.md").write_text("")

    def w(name, typ, body):
        (tmp_path / name).write_text(
            f"---\nname: {name[:-3]}\ndescription: d\nmetadata:\n  type: {typ}\n---\n\n{body}\n")

    w("a.md", "user", "Currently discussing the Zebracorn Protocol and separately the Wombat Circus initiative.")
    w("b.md", "user", "The Zebracorn Protocol was finished last week.")
    w("c.md", "user", "The Wombat Circus initiative wrapped up too.")
    w("d.md", "project", "Notes about the Atlas deployment pipeline entirely unrelated.")
    w("e.md", "project", "Something about the Loupe render queue, nothing else.")
    w("f.md", "project", "Almanac calendar sync details only.")
    cfg = Config(tmp_path, (), 30, "groq", None)

    groups = cluster(Store(cfg.store_path))
    assert len(groups) == 3, groups  # [a,b,c], [a,b], [a,c] -- confirmed overlapping

    calls = []

    def stub(prompt, _cfg, _client):
        calls.append(prompt)
        return "[]"

    find_contradictions(Store(cfg.store_path), cfg, None, complete_fn=stub)
    # Only the big [a,b,c] cluster should be sent; the two smaller,
    # fully-covered sub-clusters must be skipped.
    assert len(calls) == 1, f"expected 1 call (the [a,b,c] cluster only), got {len(calls)}"


def test_partial_unparseable_is_reported_but_not_fatal(tmp_path):
    # One of two independent clusters parses; the other returns junk.
    # This must report the count of unparseable responses AND still
    # return the one genuine finding -- a bad response from one cluster
    # must not swallow, or be swallowed by, a good response from another.
    cfg = _seed_two_independent_clusters(tmp_path)

    def stub(prompt, _cfg, _client):
        if "Quazzlefrond" in prompt:
            return json.dumps([{
                "file_a": "p1.md", "file_b": "p2.md",
                "quote_a": "Meeting notes about the Quazzlefrond Summit next week.",
                "quote_b": "The Quazzlefrond Summit wrapped up successfully.",
                "reason": "one says the summit is upcoming, the other that it is over",
            }])
        return "I'm sorry, I can't help with that."

    run = find_contradictions(Store(cfg.store_path), cfg, None, complete_fn=stub)
    assert run.sent == 2
    assert run.unparseable == 1
    assert run.errored == 0
    assert len(run.findings) == 1
    assert run.findings[0].files == ("p1.md", "p2.md")


def test_fenced_json_is_recovered(tmp_path):
    # Models commonly wrap JSON in a ```json fence despite being told to
    # return ONLY the array. This must count as a successful parse, not
    # an unparseable response -- the fence is noise around real content,
    # not a broken response.
    cfg = _seed_two_independent_clusters(tmp_path)
    stub = lambda prompt, _cfg, _client: '```json\n[]\n```'

    run = find_contradictions(Store(cfg.store_path), cfg, None, complete_fn=stub)
    assert run.sent == 2
    assert run.unparseable == 0
    assert run.errored == 0
    assert run.findings == []


def test_malformed_item_inside_a_parsed_response_is_counted_not_dropped(tmp_path):
    # I3: the response IS valid JSON (so `unparseable` stays 0), but one
    # element uses the wrong key names. Previously this item vanished
    # uncounted -- "0 candidate(s)", exit 0 -- the same shape as the
    # response-level swallow fixed in 9ab60f5, one level deeper.
    cfg = _seed(tmp_path)

    def stub(prompt, _cfg, _client):
        return json.dumps([
            {"fileA": "user_profile.md", "fileB": "person-bio.md",
             "quoteA": "x", "quoteB": "y", "why": "wrong keys entirely"},
        ])

    run = find_contradictions(Store(cfg.store_path), cfg, None, complete_fn=stub)
    assert run.findings == []
    assert run.unparseable == 0
    assert run.errored == 0
    assert run.malformed == 1


def test_transport_error_is_counted_as_errored_not_a_crash(tmp_path):
    # An HTTP 500 (or any transport failure) from the model call itself
    # must not propagate as an uncaught traceback -- it must be caught,
    # counted, and reported in the same clean form as an unparseable
    # response, never silently absorbed and never a crash.
    cfg = _seed_two_independent_clusters(tmp_path)

    def stub(prompt, _cfg, _client):
        raise httpx.HTTPStatusError("500 Internal Server Error", request=None, response=None)

    run = find_contradictions(Store(cfg.store_path), cfg, None, complete_fn=stub)
    assert run.sent == 2
    assert run.errored == 2
    assert run.unparseable == 0
    assert run.findings == []
