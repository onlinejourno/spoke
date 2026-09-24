"""Acceptance test for the whole contradiction feature (spec criterion 6):
replay one real, verified failure against a real store.

Two records in a live store contradicted each other -- one asserted a
state the other recorded as ended, on a date -- and the contradiction
stood unnoticed. Replaying it is what proves the clustering-and-prompting
pipeline reaches the model with BOTH records in one prompt.

The store is not this repository's business, so it is not in it. Point
the test at one with four environment variables and it runs; leave them
unset and it skips, naming them. This keeps a real-store replay in the
suite without the suite carrying somebody's store path, their record
names, or what those records say about them:

    SPOKE_ACCEPTANCE_STORE   path to a git-backed memory store
    SPOKE_ACCEPTANCE_COMMIT  a commit whose PARENT still has both records
    SPOKE_ACCEPTANCE_FILES   the two record file names, comma-separated
    SPOKE_ACCEPTANCE_QUOTE   text that must appear in the paired prompt

Nothing is ever written to the store: the records are read with
`git show`, and the working copy is a scratch tree with `.git` stripped.

The model itself is STUBBED: no network call, no API key, nothing that
could pass by luck. What is under test is the pipeline, not the model's
judgement, which is exactly what a stub replaces.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from pathlib import Path
import pytest
from spoke.config import Config
from spoke.store import Store
from spoke.checks.contradict import find_contradictions, plan_clusters

ENV_STORE = "SPOKE_ACCEPTANCE_STORE"
ENV_COMMIT = "SPOKE_ACCEPTANCE_COMMIT"
ENV_FILES = "SPOKE_ACCEPTANCE_FILES"
ENV_QUOTE = "SPOKE_ACCEPTANCE_QUOTE"


def _fixture_spec():
    """(store, commit, (file_a, file_b), quote) -- or a skip naming what
    is missing. A half-configured replay is a skip, not a guess: the
    alternative is a test that silently checks less than it claims."""
    store = os.environ.get(ENV_STORE)
    commit = os.environ.get(ENV_COMMIT)
    files = os.environ.get(ENV_FILES, "")
    quote = os.environ.get(ENV_QUOTE, "")
    missing = [n for n, v in ((ENV_STORE, store), (ENV_COMMIT, commit),
                              (ENV_FILES, files), (ENV_QUOTE, quote)) if not v]
    if missing:
        pytest.skip("real-store replay not configured; set " + ", ".join(missing))
    names = tuple(n.strip() for n in files.split(",") if n.strip())
    if len(names) != 2:
        pytest.skip(f"{ENV_FILES} must name exactly two records, comma-separated")
    path = Path(store).expanduser()
    if not path.exists():
        pytest.skip(f"no store at {path}")
    return path, commit, names, quote


@pytest.fixture(scope="module")
def replay():
    return _fixture_spec()


@pytest.fixture(scope="module")
def scratch_store(tmp_path_factory, replay):
    real_store, commit, names, _ = replay
    scratch = tmp_path_factory.mktemp("contradict_acceptance") / "store"
    # Read-only copy: the store's history is only ever queried with
    # `git show`, and the copy strips .git entirely so nothing here can
    # commit back into it.
    shutil.copytree(real_store, scratch, ignore=shutil.ignore_patterns(".git"))
    for name in names:
        r = subprocess.run(["git", "-C", str(real_store), "show", f"{commit}^:{name}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip(f"could not read {name} at {commit}^: {r.stderr.strip()}")
        (scratch / name).write_text(r.stdout)
    return scratch


def test_both_records_reach_the_model_in_one_prompt(scratch_store, replay):
    _, _, names, quote = replay
    a, b = names
    cfg = Config(scratch_store, (), 30, "groq", None)
    store = Store(scratch_store)

    # Sanity-check the premise before asserting on the pipeline: the two
    # records must actually co-cluster, or this test would pass for the
    # wrong reason (never reaching a real prompt at all).
    plan = plan_clusters(store)
    assert any(set(g) >= {a, b} for g in plan.all_groups), (
        "the fixture records did not cluster -- the pipeline has nothing to prompt about")

    calls: list[str] = []

    def stub_complete(prompt, _cfg, _client):
        # No network, no API key: this replaces the model entirely. It
        # answers only the one pair the acceptance criterion is about;
        # every other pair gets "no contradiction" so the test stays fast
        # and deterministic regardless of what else is in the store.
        calls.append(prompt)
        if a in prompt and b in prompt:
            return json.dumps([{
                "file_a": a, "file_b": b,
                "quote_a": quote, "quote_b": quote,
                "reason": f"{a} and {b} disagree about the same fact",
            }])
        return "[]"

    run = find_contradictions(store, cfg, None, complete_fn=stub_complete)

    # Proof the pipeline actually reached the model with BOTH records in
    # one prompt -- the thing this acceptance test exists to verify.
    paired = [p for p in calls if a in p and b in p]
    assert paired, f"no prompt named both {a} and {b}"
    assert quote in paired[0], "the record's own text did not reach the prompt"

    # Proof the finding names both files -- a human reading the CLI output
    # does not need to open either file to know what clashed.
    found = [c for c in run.findings if set(c.files) == {a, b}]
    assert len(found) == 1, run.findings
