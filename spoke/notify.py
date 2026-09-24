"""ntfy push notification for `doctor`.

Turns a run with new defects into something that reaches a human, rather
than something that only helps when someone remembers to open a terminal
and run `doctor` themselves -- the exact failure this project exists to
remove one level up (see doctor.py).

Reuses `probe.py`'s `is_safe_url` guard rather than `probe_url` itself:
`probe_url` is a HEAD request that discards its body, built for checking
whether a cited link is still alive. A notification is a POST carrying a
real message body, so this module writes its own send path -- but it
validates the destination with the same SSRF guard before ever touching
the network, rather than quietly skipping that check because the shape of
the request differs.
"""
from __future__ import annotations
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from .probe import is_safe_url

# The only default here is the public ntfy.sh server. There is deliberately
# no default TOPIC (see load_notify_config) and nothing here names this
# estate -- tests/test_no_estate_identity.py forbids exactly that.
DEFAULT_NTFY_SERVER = "https://ntfy.sh"


@dataclass(frozen=True)
class NotifyConfig:
    topic: str | None
    server: str


@dataclass(frozen=True)
class NotifyResult:
    # False only when no topic is configured -- i.e. a send was never
    # attempted at all. True whenever a POST was actually made (or refused
    # by the SSRF guard), whether or not it succeeded.
    attempted: bool
    ok: bool
    detail: str


def load_notify_config(path: Path | None) -> NotifyConfig:
    """Read `[notify]` from `path` (if it exists), overridable by
    SPOKE_NTFY_TOPIC / SPOKE_NTFY_SERVER -- the same file-then-env
    pattern load_config() uses for everything else.

    No topic anywhere (file or env) means notification is off. That is a
    stated absence the caller must print, never a silent one -- there is
    no fallback topic and no fallback host beyond the public ntfy.sh
    default, both deliberately: a baked-in topic would page whoever
    inherits this default, and a baked-in estate host is exactly the kind
    of identity this project's config-only rule forbids.
    """
    data: dict = {}
    if path is not None and Path(path).exists():
        data = tomllib.loads(Path(path).read_text())
    notify = data.get("notify", {})

    raw_topic = os.environ.get("SPOKE_NTFY_TOPIC") or notify.get("ntfy_topic")
    topic = raw_topic.strip() if isinstance(raw_topic, str) else raw_topic
    topic = topic or None

    server = (os.environ.get("SPOKE_NTFY_SERVER")
              or notify.get("ntfy_server")
              or DEFAULT_NTFY_SERVER)
    return NotifyConfig(topic=topic, server=str(server).rstrip("/"))


# Above this many instances of the SAME kind, the notification collapses
# them to one line rather than listing each -- see format_message. Exactly
# this many or fewer still lists individually, unchanged.
_COLLAPSE_THRESHOLD = 3
_MAX_EXAMPLES = 3

# Per-kind collapse alone still grows linearly with the number of DISTINCT
# kinds: 40 kinds of 3 each is 120 uncollapsed lines even though no single
# kind exceeds the threshold above. This is the whole-body ceiling: at most
# this many kind-lines are ever emitted, and any remaining kinds are named
# and counted in one trailing line instead -- see format_message.
_MAX_KIND_LINES = 12


# Lower leads. `unmet` is a claim the world contradicted or nobody checked;
# a dead citation is a fact that stopped being reachable; a PR state the
# probe could not see is usually a private repo; a citation this probe
# cannot judge at all (auth-gated, method-refused, not a public address)
# ranks below those, because it names a limit of the probe rather than a
# defect in the store; "unverified too long" is the standing background of
# any store.
_KIND_RANK = {"unmet": 0, "citation-dead": 1, "citation-state": 2,
              "citation-unverifiable": 3, "unverified-too-long": 4}


def format_message(project: str | None, findings) -> str:
    """A message a human can act on from a phone lock screen: the project,
    the total count, and the findings grouped by kind.

    Detection is unaffected by this -- `check` and `doctor`'s console
    listing still print every finding individually (see cli.py). This
    function only shapes what goes into the notification BODY: a run that
    is 108 counts of one underlying fact (nothing has ever been verified)
    and 2 of another must not read as 110 lines. A kind with
    `_COLLAPSE_THRESHOLD` or fewer instances is still listed line by line,
    exactly as before; past that it collapses to one line naming the kind,
    the count, and up to `_MAX_EXAMPLES` example files -- enough to
    recognise the pattern, not a wall of text that becomes wallpaper.

    That per-kind collapse bounds each kind's own contribution but not the
    number of DISTINCT kinds shown, which grows the body linearly however
    small each kind is. So there is a second, whole-body cap on top: kinds
    are ordered by descending finding count (the biggest problems surface
    first) and at most `_MAX_KIND_LINES` of them are shown; any remainder
    is rolled into one trailing "... and N more kinds" line naming their
    combined count, never silently dropped. While that cap is in effect,
    EVERY shown kind renders as exactly one line -- even one at or under
    `_COLLAPSE_THRESHOLD` -- because the per-kind individual-listing style
    has no line budget of its own: 12 kinds of exactly 3 listed
    individually would be 36 lines, blowing straight through the cap this
    exists to enforce. The opening line's total count is always the TRUE
    total, computed before any capping, regardless of what is shown below it.
    """
    label = project or "spoke"
    findings = list(findings)
    lines = [f"{label}: {len(findings)} new defect(s)"]
    by_kind: dict[str, list] = {}
    for f in findings:
        by_kind.setdefault(f.kind, []).append(f)

    # Severity first, then volume. Ordered by count alone, one unmet
    # expectation -- the world contradicting a written claim -- sat last
    # behind seventy "not re-verified in a while" records and was the
    # line the phone truncated. The thing to act on leads.
    ordered_kinds = sorted(by_kind, key=lambda k: (_KIND_RANK.get(k, 9), -len(by_kind[k]), k))
    capped = len(ordered_kinds) > _MAX_KIND_LINES
    shown_kinds = ordered_kinds[:_MAX_KIND_LINES] if capped else ordered_kinds
    omitted_kinds = ordered_kinds[_MAX_KIND_LINES:] if capped else []

    for kind in shown_kinds:
        group = by_kind[kind]
        if kind == "unmet" or (not capped and len(group) <= _COLLAPSE_THRESHOLD):
            for f in group:
                lines.append(f"- {f.kind}: {f.file}: {f.detail}")
        else:
            examples = ", ".join(f.file for f in group[:_MAX_EXAMPLES])
            lines.append(f"{kind}: {len(group)} records (e.g. {examples})")

    if omitted_kinds:
        omitted_count = sum(len(by_kind[k]) for k in omitted_kinds)
        lines.append(
            f"… and {len(omitted_kinds)} more kinds ({omitted_count} findings) "
            "— run `spoke doctor` for the full list"
        )
    return "\n".join(lines)


def send_notification(cfg: NotifyConfig, project: str | None, findings, client) -> NotifyResult:
    """Send exactly one ntfy push for `findings` (the caller is responsible
    for only calling this with NEW findings, and only when there are any --
    see cli.py's doctor command). Never raises: every failure mode -- not
    configured, blocked by the SSRF guard, a non-2xx response, a transport
    error -- comes back as a NotifyResult the caller prints and acts on."""
    if not cfg.topic:
        return NotifyResult(attempted=False, ok=False, detail="not configured")

    url = f"{cfg.server}/{cfg.topic}"
    if not is_safe_url(url):
        return NotifyResult(attempted=True, ok=False, detail=f"blocked by SSRF guard: {url}")

    title = f"spoke: {len(findings)} new defect(s)"
    body = format_message(project, findings)
    # An unmet expectation is the world contradicting a claim somebody
    # wrote down, or a claim nobody has checked; it outranks a dead link
    # on a phone. ntfy reads Priority and Tags from headers.
    unmet = any(getattr(f, "kind", "") == "unmet" for f in findings)
    headers = {"Title": title}
    if unmet:
        headers["Priority"] = "high"
        headers["Tags"] = "warning"
    try:
        r = client.post(
            url,
            content=body.encode("utf-8"),
            headers=headers,
            follow_redirects=False,
        )
    except Exception as exc:  # noqa: BLE001 -- any transport failure must be
        # reported, not raised: a watcher whose notifier crashes must still
        # print a visible failure and let `doctor` exit non-zero, not blow
        # up before it can say so.
        return NotifyResult(attempted=True, ok=False, detail=f"error: {exc}")

    if r.status_code >= 400:
        return NotifyResult(attempted=True, ok=False, detail=f"HTTP {r.status_code}")
    return NotifyResult(attempted=True, ok=True, detail=f"HTTP {r.status_code}")
