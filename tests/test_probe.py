import httpx
import pytest
from spoke.probe import is_safe_url, probe_url, probe_pr


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "http://localhost/x", "http://169.254.169.254/latest/meta-data",
    "http://10.0.0.1/", "http://192.168.1.1/", "http://172.16.0.1/", "http://[::1]/",
    "http://100.64.0.1/", "file:///etc/passwd", "http://0.0.0.0/",
])
def test_blocks_dangerous_urls(url):
    assert is_safe_url(url) is False


def test_allows_ordinary_https():
    assert is_safe_url("https://example.com/page") is True


def test_probe_reports_404(monkeypatch):
    def handler(request):
        return httpx.Response(404)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    r = probe_url("https://example.com/gone", client)
    assert r.ok is False and "404" in r.detail


def test_probe_refuses_unsafe_without_fetching():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    r = probe_url("http://169.254.169.254/", client)
    assert r.ok is False and "blocked" in r.detail
    assert calls == []


def test_redirect_to_a_blocked_address_is_refused_and_not_fetched():
    fetched = []

    def handler(request):
        fetched.append(str(request.url))
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    r = probe_url("https://example.com/safe", client)
    assert r.ok is False
    assert "blocked" in r.detail
    assert not any("169.254.169.254" in u for u in fetched), f"metadata endpoint was fetched: {fetched}"


def test_relative_redirect_is_resolved_then_validated():
    def handler(request):
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert probe_url("https://example.com/a", client).ok is True


def test_redirect_chain_is_capped():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://example.com/next"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    r = probe_url("https://example.com/start", client)
    assert r.ok is False
    assert "too many redirects" in r.detail


def test_probe_pr_reads_merge_state(monkeypatch):
    def handler(request):
        assert "api.github.com" in str(request.url)
        return httpx.Response(200, json={"state": "closed", "merged": True})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    r = probe_pr("example-org/digest#96", client)
    assert r.ok is True and "merged" in r.detail


def test_probe_pr_on_a_200_that_is_not_json_reports_a_failed_probe_not_a_crash():
    """A captive portal, an HTML rate-limit page, or a proxy error page can
    all return 200 with a body that isn't JSON. r.json() must not raise
    json.JSONDecodeError out of here and crash check_staleness / stale /
    doctor -- it must come back as the same shape a failed probe already
    returns, with a detail saying why."""
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    r = probe_pr("example-org/digest#96", client)
    assert r.ok is False
    assert "not json" in r.detail.lower() or "json" in r.detail.lower()


@pytest.mark.parametrize("make_response", [
    lambda: httpx.Response(200, json=[1, 2, 3]),
    lambda: httpx.Response(200, json=None),
    lambda: httpx.Response(200, json="str"),
    lambda: httpx.Response(200, json=42),
    lambda: httpx.Response(200, content=b""),
    lambda: httpx.Response(200, text="<html><body>rate limited</body></html>"),
    lambda: httpx.Response(204),
], ids=["list", "null", "string", "number", "empty-body", "html", "204"])
def test_probe_pr_on_valid_but_wrong_shape_or_unusable_bodies_fails_without_crashing(make_response):
    """A 200 whose body is valid JSON but not an OBJECT -- a list, null, a
    bare string, a bare number -- reaches data.get("merged") just like a
    dict would, and must not raise AttributeError. Grouped with the
    already-handled not-JSON-at-all cases (empty body, HTML, a bodyless
    204) because all of them are "a response we cannot interpret", not a
    crash site."""
    client = httpx.Client(transport=httpx.MockTransport(lambda r: make_response()))
    r = probe_pr("example-org/digest#96", client)
    assert r.ok is False
    assert isinstance(r.detail, str)


def test_probe_pr_with_unexpected_keys_still_succeeds():
    """A dict is a dict even when it doesn't look like the GitHub PR schema
    we expect -- .get(..., "unknown") already handles unexpected keys
    correctly, and the new isinstance(dict) guard must not break that."""
    def handler(request):
        return httpx.Response(200, json={"totally": "unexpected", "shape": True})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    r = probe_pr("example-org/digest#96", client)
    assert r.ok is True
    assert r.detail == "unknown"


# --- Three states, not two -------------------------------------------------
# A probe has three honest answers, not two: the citation is reachable, it
# is gone, or THIS PROBE CANNOT TELL. Collapsing the third into "dead" is
# what makes most live citation-dead findings false -- and, because a
# dead citation used to suppress the record's age check, each false one
# silently switched a real check off.

@pytest.mark.parametrize("status", [401, 403])
def test_auth_gated_status_is_unverifiable_not_dead(status):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status)))
    r = probe_url("https://claude.ai/artifact/Ao9Htm14kPkxiqRAh9bMdW", client)
    assert r.ok is False
    assert r.verifiable is False, "an auth-gated URL discloses nothing about its own existence"


@pytest.mark.parametrize("status", [405, 406])
def test_method_refusal_is_unverifiable_not_dead(status):
    # A 405 to a HEAD request is the server answering -- proof of life
    # reported as death. Six live findings were exactly this.
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status)))
    r = probe_url("https://accounts.example/", client)
    assert r.ok is False
    assert r.verifiable is False


def test_plain_404_is_still_dead():
    # The flip side: the third state must not swallow the real finding.
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    r = probe_url("https://example.com/gone", client)
    assert r.ok is False and r.verifiable is True


def test_a_reachable_url_is_verifiable():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    r = probe_url("https://example.com/here", client)
    assert r.ok is True and r.verifiable is True


def test_ssrf_blocked_is_unverifiable_not_dead():
    # localhost / 0.0.0.0 / .flycast / example.invalid appear in memories as
    # illustrations, not citations. They can never resolve, so calling them
    # dead is a finding that can never be closed.
    r = probe_url("http://localhost:8000", httpx.Client())
    assert r.ok is False and r.verifiable is False


def test_placeholder_url_is_unverifiable_and_never_fetched():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    r = probe_url("https://example-desk.example/api/v1/stringer/<slug", client)
    assert r.ok is False and r.verifiable is False
    assert calls == [], "a template is not an address; do not request it"
