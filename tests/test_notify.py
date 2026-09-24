"""Unit tests for spoke/notify.py: config loading and the actual send.

CLI-level behaviour (the four `doctor` scenarios: not configured, sent,
skipped, failed) lives in tests/test_cli_doctor.py alongside the rest of
`doctor`'s output contract.
"""
import textwrap
import httpx
import pytest
from spoke.notify import (
    NotifyConfig,
    NotifyResult,
    format_message,
    load_notify_config,
    send_notification,
)
from spoke.checks import Finding


def test_no_config_file_and_no_env_is_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("SPOKE_NTFY_TOPIC", raising=False)
    monkeypatch.delenv("SPOKE_NTFY_SERVER", raising=False)
    cfg = load_notify_config(tmp_path / "no-such-config.toml")
    assert cfg.topic is None
    assert cfg.server == "https://ntfy.sh"


def test_topic_from_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SPOKE_NTFY_TOPIC", raising=False)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(textwrap.dedent("""
        [notify]
        ntfy_topic = "my-topic"
        ntfy_server = "https://ntfy.example.test"
    """))
    cfg = load_notify_config(cfg_file)
    assert cfg.topic == "my-topic"
    assert cfg.server == "https://ntfy.example.test"


def test_env_topic_overrides_file(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[notify]\nntfy_topic = "file-topic"\n')
    monkeypatch.setenv("SPOKE_NTFY_TOPIC", "env-topic")
    cfg = load_notify_config(cfg_file)
    assert cfg.topic == "env-topic"


def test_whitespace_only_topic_is_treated_as_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("SPOKE_NTFY_TOPIC", raising=False)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[notify]\nntfy_topic = "   "\n')
    cfg = load_notify_config(cfg_file)
    assert cfg.topic is None


def test_no_estate_host_or_topic_defaults_are_baked_in():
    """The only default is the public ntfy.sh server -- never a topic, and
    never anything naming this estate. This is checked structurally too by
    tests/test_no_estate_identity.py; this test pins the *behaviour*."""
    import spoke.notify as notify_mod
    assert notify_mod.DEFAULT_NTFY_SERVER == "https://ntfy.sh"
    cfg = NotifyConfig(topic=None, server=notify_mod.DEFAULT_NTFY_SERVER)
    assert cfg.topic is None


def test_format_message_collapses_a_kind_with_many_instances():
    """One underlying fact (e.g. "nothing has ever been verified") must not
    read as N separate lines. A kind with more than 3 instances collapses
    to one line naming the kind, the count, and up to 3 example files --
    never the full list, which is the wallpaper effect this exists to
    avoid."""
    many = [Finding("unverified-too-long", f"a{i}.md", f"stale{i}") for i in range(108)]
    few = [Finding("broken-wikilink", "b1.md", "ghost1"), Finding("broken-wikilink", "b2.md", "ghost2")]
    msg = format_message("estate", many + few)
    assert "estate" in msg
    assert "110" in msg  # total new count in the opening line
    assert "unverified-too-long: 108 records" in msg
    # at most 3 example filenames for the collapsed kind
    examples_shown = sum(1 for i in range(108) if f"a{i}.md" in msg)
    assert examples_shown <= 3
    # the rare kind (<=3 instances) is still listed individually
    assert "b1.md" in msg and "ghost1" in msg
    assert "b2.md" in msg and "ghost2" in msg
    # never all 108 filenames
    assert not all(f"a{i}.md" in msg for i in range(108))


def test_format_message_lists_three_or_fewer_of_a_kind_individually():
    findings = [Finding("broken-wikilink", f"a{i}.md", f"ghost{i}") for i in range(3)]
    msg = format_message("estate", findings)
    for i in range(3):
        assert f"a{i}.md" in msg
        assert f"ghost{i}" in msg
    assert "records" not in msg  # not collapsed at exactly the threshold


def test_format_message_with_no_project_names_the_tool():
    findings = [Finding("broken-wikilink", "a.md", "ghost")]
    msg = format_message(None, findings)
    assert "spoke" in msg


def test_format_message_collapses_at_four_but_not_at_three():
    """Pins the exact boundary of the per-kind collapse: `_COLLAPSE_THRESHOLD`
    is 3, so 3 instances of a kind still list individually and 4 collapse
    to one summary line. Existing coverage only exercised 3 and 108."""
    three = [Finding("broken-wikilink", f"c{i}.md", f"ghost{i}") for i in range(3)]
    msg_three = format_message("estate", three)
    assert "records" not in msg_three
    for i in range(3):
        assert f"c{i}.md" in msg_three

    four = [Finding("broken-wikilink", f"d{i}.md", f"ghost{i}") for i in range(4)]
    msg_four = format_message("estate", four)
    assert "broken-wikilink: 4 records" in msg_four
    # collapsed -- individual filenames no longer appear as list entries
    assert not all(f"d{i}.md" in msg_four for i in range(4))


def test_format_message_caps_the_whole_body_across_many_distinct_kinds():
    """Per-kind collapse alone still grows with the number of DISTINCT
    kinds: 40 kinds of 3 each is 120 findings and, without a whole-body
    cap, 120 uncollapsed lines (no single kind exceeds the per-kind
    threshold of 3). Fix 3 caps total kind-lines at 12 plus one trailing
    "N more kinds" summary line -- at most 13 lines after the header."""
    findings = [
        Finding(f"kind-{k}", f"{k}-{i}.md", f"detail{i}")
        for k in range(40) for i in range(3)
    ]
    msg = format_message("estate", findings)
    lines = msg.split("\n")
    header, body_lines = lines[0], lines[1:]

    assert "estate" in header
    assert "120" in header  # true total, unaffected by capping

    assert len(body_lines) <= 13
    assert len(body_lines) == 13  # 12 shown kinds + 1 trailing summary

    trailing = body_lines[-1]
    assert "28 more kinds" in trailing
    assert "84 findings" in trailing  # 28 omitted kinds * 3 findings each


def test_send_notification_without_topic_is_not_attempted():
    cfg = NotifyConfig(topic=None, server="https://ntfy.sh")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = send_notification(cfg, "estate", [Finding("k", "f", "d")], client)
    assert res.attempted is False
    assert res.ok is False
    assert "not configured" in res.detail
    assert calls == []


def test_send_notification_success_posts_to_topic_url():
    cfg = NotifyConfig(topic="my-topic", server="https://ntfy.sh")
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode("utf-8")
        return httpx.Response(200)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    findings = [Finding("broken-wikilink", "a.md", "ghost")]
    res = send_notification(cfg, "estate", findings, client)
    assert res.attempted is True
    assert res.ok is True
    assert seen["url"] == "https://ntfy.sh/my-topic"
    assert "estate" in seen["body"]
    assert "1" in seen["body"]


def test_send_notification_reports_http_failure():
    cfg = NotifyConfig(topic="my-topic", server="https://ntfy.sh")

    def handler(request):
        return httpx.Response(500)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = send_notification(cfg, "estate", [Finding("k", "f", "d")], client)
    assert res.attempted is True
    assert res.ok is False
    assert "500" in res.detail


def test_send_notification_blocks_unsafe_server_without_fetching():
    """The server URL is configurable -- so it must go through the same
    SSRF guard as everything else that fetches a configured URL."""
    cfg = NotifyConfig(topic="x", server="http://169.254.169.254")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = send_notification(cfg, "estate", [Finding("k", "f", "d")], client)
    assert res.ok is False
    assert "blocked" in res.detail
    assert calls == []


def test_send_notification_reports_transport_error():
    cfg = NotifyConfig(topic="my-topic", server="https://ntfy.sh")

    def handler(request):
        raise httpx.ConnectError("boom")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = send_notification(cfg, "estate", [Finding("k", "f", "d")], client)
    assert res.attempted is True
    assert res.ok is False
    assert "boom" in res.detail
