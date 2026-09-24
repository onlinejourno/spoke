"""SSRF-guarded HTTP/PR probing for citations found in memory records.

Residual risk (deliberately not fully closed): `is_safe_url` resolves the
host and validates those addresses at check time, but the actual HTTP
connection (made by httpx/the OS resolver) re-resolves the host
independently. An attacker who controls DNS for the cited host with a
short TTL can therefore pass the guard's check and have the real
connection land on a different, unvalidated address ("DNS rebinding").
Closing this fully needs a custom transport that pins the exact address
validated by the guard, which is disproportionate for this tool's threat
model. Two things bound the residual risk instead of eliminating it:
inputs are the user's own memory files, not arbitrary third-party input,
and every probe is a HEAD request whose body is discarded. This guard is
NOT a complete SSRF defense — treat it as a best-effort filter, not a
guarantee.
"""
from __future__ import annotations
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

PR_REF = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)")


# Statuses that answer the request without disclosing whether the cited
# thing exists. A probe that meets one of these has NOT found the citation
# dead -- it has found that this probe cannot judge it:
#   401/403  the resource is auth-gated (a private claude.ai artifact
#            returns 403 whether it is live, private, or deleted)
#   405/406  the server answered and refused the METHOD -- proof of life
#            reported, until this existed, as death
_UNDISCLOSED = frozenset({401, 403, 405, 406})

# A citation that is a template, not an address: a markdown glob, an
# angle-bracket placeholder, a brace. Requesting one asks about a URL
# nobody ever published.
_PLACEHOLDER = re.compile(r"[*<>{}]")


@dataclass(frozen=True)
class ProbeResult:
    target: str
    ok: bool
    detail: str
    # Whether the probe was in a position to JUDGE the target at all.
    # Three states, not two: reachable (ok), gone (not ok, verifiable),
    # and unjudgeable (not ok, not verifiable). Collapsing the third into
    # "dead" is what makes most live citation-dead findings false --
    # and, while a dead citation suppressed the record's age check, each
    # false one silently switched a real check off. Defaulted True so
    # every existing construction keeps its old meaning.
    verifiable: bool = True


def _blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ipaddress.ip_network("100.64.0.0/10").supernet_of(
            ipaddress.ip_network(f"{ip}/32")):
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _blocked_ip(ip.ipv4_mapped)
    return False


def is_safe_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    if host.lower() in ("localhost",):
        return False
    try:
        return not _blocked_ip(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return not any(_blocked_ip(ipaddress.ip_address(i[4][0])) for i in infos)


_REDIRECT_CODES = (301, 302, 303, 307, 308)
_MAX_REDIRECTS = 5


def probe_url(url: str, client) -> ProbeResult:
    if _PLACEHOLDER.search(url):
        # Never fetched: a template is not an address.
        return ProbeResult(url, False, "placeholder URL, not a fixed address",
                           verifiable=False)
    if not is_safe_url(url):
        # localhost, 0.0.0.0, *.flycast, an example domain -- these appear
        # in memories as illustrations. They can never resolve from here,
        # so "dead" is a finding that can never be closed.
        return ProbeResult(url, False, "not a public address (blocked by SSRF guard)",
                           verifiable=False)
    current = url
    for _ in range(_MAX_REDIRECTS):
        try:
            r = client.head(current, follow_redirects=False)
        except Exception as exc:
            return ProbeResult(url, False, f"error: {exc}")
        if r.status_code in _REDIRECT_CODES:
            location = r.headers.get("location")
            if not location:
                return ProbeResult(url, False, f"HTTP {r.status_code}")
            target = urljoin(current, location)
            if not is_safe_url(target):
                return ProbeResult(url, False, f"redirect to a blocked address: {target}")
            current = target
            continue
        if r.status_code in _UNDISCLOSED:
            return ProbeResult(url, False, f"HTTP {r.status_code}", verifiable=False)
        ok = r.status_code < 400
        return ProbeResult(url, ok, f"HTTP {r.status_code}")
    return ProbeResult(url, False, "too many redirects")


def probe_pr(ref: str, client) -> ProbeResult:
    m = PR_REF.fullmatch(ref)
    if not m:
        return ProbeResult(ref, False, "not a PR reference")
    repo, num = m.groups()
    r = client.get(f"https://api.github.com/repos/{repo}/pulls/{num}")
    if r.status_code >= 400:
        return ProbeResult(ref, False, f"HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        # A 200 whose body isn't JSON -- a captive portal, an HTML
        # rate-limit page, a proxy error page -- is a response we cannot
        # interpret, so it is a FAILED probe, not a crash. httpx raises its
        # own JSONDecodeError here, which subclasses the stdlib
        # json.JSONDecodeError, itself a ValueError -- catching ValueError
        # covers both without importing either directly.
        return ProbeResult(ref, False, "response was not JSON")
    if not isinstance(data, dict):
        # Valid JSON that isn't an OBJECT -- a list, null, a bare string, a
        # bare number -- is just as unusable as non-JSON: a broken proxy or
        # a CDN error page can return any of these with a 200. Without this
        # check, data.get("merged") below raises AttributeError on anything
        # that isn't a dict, and this is a probe result, not a crash site.
        return ProbeResult(ref, False, "response body was not a JSON object")
    state = "merged" if data.get("merged") else data.get("state", "unknown")
    return ProbeResult(ref, True, state)
