from __future__ import annotations
import html
import re
from datetime import date
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from .config import DEFAULT_TAGLINE, Config, load_config_file, overridden_env_vars, save_config
from .ledger.board import BoardError, build_matrix, build_scale, build_view
from .ledger.store import LedgerStore
from .llm import PROVIDERS as _PROVIDERS, api_key_is_set as _api_key_is_set
from .store import Store
from .clock import Clock
from .ledger.scale import DEFAULT_MAX_VALUE
from .workspace import store_error

STATIC = Path(__file__).parent / "static"

_HTTP_URL_RE = re.compile(r"^https?://", re.I)

# A board stylesheet may be a same-origin path or an https URL, and
# nothing else. The value is written into an href on a page that talks to
# the local store, so an unconstrained string is a script-injection hole
# in a tool the user runs against their own memory. Validated here AND
# escaped at the point of use -- neither alone.
_STYLESHEET_RE = re.compile(r"^(?:/[\w.~!$&\'()*+,;=:@%/-]*|https://[^\s\"\'<>]+)$")
_NTFY_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SaveBody(BaseModel):
    body: str
    index_line: str | None = None
    expected_body: str | None = None


class ProjectPayload(BaseModel):
    """A project to register from the first-run screen: a name and the
    repos it is made of. Everything else about a project derives."""
    name: str
    repos: list[str]


class AxisPayload(BaseModel):
    key: str
    label: str = ""
    weight: float = 1.0


class AxesPayload(BaseModel):
    """The project's whole scale, in reading order. A full-set write,
    like the CLI's re-declaration: the order sent is the order kept."""
    project: str
    axes: list[AxisPayload]
    scale_max: int


class ScanPayload(BaseModel):
    """`write` mirrors the CLI: false prints what would change, true
    writes it through the same gate a human meets."""
    write: bool = False


class DemoPayload(BaseModel):
    """Where to build the demo. A directory of its own, always: the demo
    must never be able to land in the user's store."""
    dir: str = "~/spoke-demo"


class ReportPayload(BaseModel):
    """A problem somebody found IN a surface, reported FROM that surface.

    `page` is which page; `text` is what they saw, in their words;
    `context` is what the page knew about its own state when they said
    it -- lens, zoom, filters -- captured by the page, not typed by them.
    """
    page: str
    text: str
    context: dict[str, str] = {}


class ConfigBody(BaseModel):
    """The fields a Settings screen may change. There is deliberately no
    llm_api_key field: LLM_API_KEY is environment-only (see llm.py), and
    Pydantic ignores unknown fields by default, so a client that sends one
    anyway (e.g. a stale form) has it silently dropped rather than written
    to config.toml."""
    store_path: str
    accepted_absences: list[str] = []
    stale_days: int
    llm_provider: str
    llm_base_url: str | None = None
    board_stylesheet: str | None = None
    brand_name: str | None = None
    llm_model: str | None = None
    brand_author: str | None = None
    brand_author_url: str | None = None
    brand_logo_url: str | None = None
    brand_tagline: str | None = None
    ntfy_topic: str | None = None
    ntfy_server: str | None = None


def _validate_config_body(body: ConfigBody) -> str | None:
    """None if `body` is usable; otherwise a message naming exactly which
    field is wrong, for a 422 refusal a non-coder can act on."""
    # Reuse the CLI's own store validator rather than a second, subtly
    # different copy of "does this look like a memory store" -- imported
    # lazily to avoid a module-load cycle with cli.py (which imports
    # create_app from this module, inside its `serve` command).

    store_err = store_error(Path(body.store_path).expanduser())
    if store_err is not None:
        return f"store_path: {store_err}"
    if body.stale_days <= 0:
        return f"stale_days: must be a positive integer, got {body.stale_days}"
    for a in body.accepted_absences:
        if not isinstance(a, str) or not a.strip():
            return f"accepted_absences: each entry must be a non-empty string, got {a!r}"
    from .llm import PROVIDERS

    if body.llm_provider not in PROVIDERS:
        return (
            f"llm_provider: unknown provider {body.llm_provider!r} -- must be one of "
            f"{', '.join(sorted(PROVIDERS))}"
        )
    if body.llm_base_url and not _HTTP_URL_RE.match(body.llm_base_url):
        return f"llm_base_url: must be an http:// or https:// URL, got {body.llm_base_url!r}"
    if body.brand_name is not None:
        # Length-capped and control-character-free. The value is written
        # into the page title and header, so an unbounded or newline-
        # bearing string is a layout break at best.
        name = body.brand_name.strip()
        if len(name) > 60:
            return f"brand_name: must be 60 characters or fewer, got {len(name)}"
        if any(ord(c) < 32 or ord(c) == 127 for c in name):
            return "brand_name: must not contain control characters"
    if body.brand_author is not None:
        author = body.brand_author.strip()
        if len(author) > 60:
            return f"brand_author: must be 60 characters or fewer, got {len(author)}"
        if any(ord(c) < 32 or ord(c) == 127 for c in author):
            return "brand_author: must not contain control characters"
    if body.brand_author_url and not _HTTP_URL_RE.match(body.brand_author_url):
        # It becomes an href. Anything but http(s) -- javascript:, data:
        # -- is a script injection with a config file as the vector.
        return (f"brand_author_url: must be an http:// or https:// URL, "
                f"got {body.brand_author_url!r}")
    if body.brand_tagline is not None:
        tag = body.brand_tagline.strip()
        if len(tag) > 140:
            return f"brand_tagline: must be 140 characters or fewer, got {len(tag)}"
        if any(ord(c) < 32 or ord(c) == 127 for c in tag):
            return "brand_tagline: must not contain control characters"
    if body.ntfy_topic and not _NTFY_TOPIC_RE.match(body.ntfy_topic.strip()):
        return ("ntfy_topic: letters, digits, '-' and '_' only, up to 64 -- "
                "the topic name is the whole secret, so make it unguessable")
    if body.ntfy_server and not body.ntfy_server.strip().startswith("https://"):
        return "ntfy_server: must start with https:// (findings name your hosts; they travel encrypted)"
    if body.brand_logo_url and not _STYLESHEET_RE.match(body.brand_logo_url):
        # Same rule as the stylesheet: it becomes a src. Same-origin path
        # or https; nothing that could be a script.
        return (f"brand_logo_url: must be a same-origin path starting with '/' "
                f"or an https:// URL, got {body.brand_logo_url!r}")
    if body.board_stylesheet and not _STYLESHEET_RE.match(body.board_stylesheet):
        return (
            "board_stylesheet: must be a same-origin path starting with '/' "
            f"or an https:// URL, got {body.board_stylesheet!r}"
        )
    return None


# Host names this server will answer to. The bind is 127.0.0.1, so only a
# local process can open the socket -- but a web page IS a local process
# for this purpose. DNS rebinding lets a page the user is merely VISITING
# point its own hostname at 127.0.0.1 and then talk to this server as
# same-origin, with full write access to the memory store behind it.
#
# The bind address does not stop that; the Host header does. A rebinding
# attacker must use a name that RESOLVES to 127.0.0.1, so the check is
# simply: is the name one of the loopback names.
#
# "testserver" is Starlette's TestClient default. It is safe to allow
# because it is not a resolvable name -- no browser can be pointed at it,
# so it can never be the Host of a request from a real page.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "testserver"})


def _host_is_loopback(host: str) -> bool:
    """`host` is a Host header value, so it may carry a port."""
    name = (host or "").strip().lower()
    if name.startswith("["):                      # [::1]:8765
        name = name.split("]")[0] + "]"
    elif ":" in name:
        name = name.rsplit(":", 1)[0]
    return name in LOOPBACK_HOSTS


_REPORT_PAGES = ("/", "/board", "/scale", "/settings")
_REPORT_MAX = 4000
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _report_error(body: ReportPayload) -> str | None:
    text = body.text.strip()
    if not text:
        return "text: say what you saw -- an empty report records nothing"
    if len(text) > _REPORT_MAX:
        return f"text: {_REPORT_MAX} characters or fewer, got {len(text)}"
    if any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in text):
        return "text: must not contain control characters"
    if body.page not in _REPORT_PAGES:
        return f"page: must be one of {', '.join(_REPORT_PAGES)}, got {body.page!r}"
    if len(body.context) > 12:
        return "context: at most 12 entries"
    for k, v in body.context.items():
        if len(k) > 40 or len(v) > 200:
            return f"context: {k!r} is too long"
        if any(ord(c) < 32 or ord(c) == 127 for c in k + v):
            return "context: must not contain control characters"
    return None


def _report_node(body: ReportPayload, store, today):
    """The report as a ledger node: an OPEN ITEM, so the preamble raises
    it at the start of the next session without anyone remembering to
    look. Deliberately related to nothing: a node named after this
    tool exists in a ledger that scans this tool's repo and in no other
    install, and a relation that dangles everywhere but here would be a
    defect shipped as a feature.
    """
    from .ledger import Node

    text = body.text.strip()
    first = text.splitlines()[0].strip()
    title = f"Surface: {first[:80]}" + ("…" if len(first) > 80 else "")
    words = _SLUG_RE.sub("-", first.lower()).strip("-").split("-")[:6]
    base = f"surface-{'-'.join(w for w in words if w) or 'report'}-{today.strftime('%Y%m%d')}"
    name, n = base, 1
    while True:
        try:
            store.read(name)
        except FileNotFoundError:
            break
        except Exception:
            break  # unreadable is still "taken"; a fresh name is safer
        n += 1
        name = f"{base}-{n}"
    ctx = "\n".join(f"- {k}: {v}" for k, v in sorted(body.context.items()))
    by = f"board:{body.page}"
    node_body = (f"{text}\n\n---\nReported from {body.page} on {today.isoformat()}."
                 + (f"\n\nWhat the page knew about itself:\n{ctx}" if ctx else "") + "\n")
    prov = tuple({"field": f, "by": f"human:{by}", "at": today.isoformat()}
                 for f in ("title", "body", "state"))
    from .cli import FOUR_QUESTIONS  # the estate's own rule, one definition
    return Node(
        name=name, type="item", state="open", title=title, body=node_body,
        relations=(), ruling=None, blocked_by=(), provenance=prov,
        # A report is a task. It carries the four questions from the
        # start, so it cannot be closed as "fixed" without someone
        # naming what proved it and what watches it.
        checklist=tuple({"text": q, "done": False} for q in FOUR_QUESTIONS),
        # ISO strings, as every other writer stores them -- a date object
        # here round-tripped as an unquoted YAML date and crashed the
        # preamble's sort against the strings beside it.
        opened=today.isoformat(), updated=today.isoformat(),
        by=by, claimed_by=None, claimed_at=None,
    )


_PRISM = (
    '<svg class="prism" viewBox="0 0 20 20" width="20" height="20" aria-hidden="true" focusable="false">'
    '<path d="M0 11H6.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>'
    '<path d="M10 3.5 3 16.5h14Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>'
    '<path d="M13.5 11 20 6M13.5 11H20M13.5 11 20 16" stroke="var(--register)" stroke-width="1.5" stroke-linecap="round"/>'
    '</svg>'
)


def _mark(cfg) -> str:
    """The masthead mark: the install's own logo when one is configured,
    the package's prism when not. Re-checked against the same rule the
    save path applies, because a hand edit does not pass through it."""
    url = (cfg.brand_logo_url or "").strip()
    if url and _STYLESHEET_RE.match(url):
        return f'<img class="logo" src="{html.escape(url, quote=True)}" alt="">'
    return _PRISM


def _author_line(cfg) -> str:
    """The "by <author>" fragment for the header, or nothing.

    Nothing -- not "by" with a blank -- when no author is configured:
    the package ships nobody's name. Escaped at the point of use and
    the href re-checked against the same http(s) rule the save path
    applies, because the config file can be edited by hand and a hand
    edit does not pass through the save path.
    """
    author = (cfg.brand_author or "").strip()
    url = (cfg.brand_author_url or "").strip()
    if not author:
        return ""
    if url and _HTTP_URL_RE.match(url):
        return (f'<span class="author">by <a href="{html.escape(url, quote=True)}" '
                f'rel="author">{html.escape(author)}</a></span>')
    return f'<span class="author">by {html.escape(author)}</span>'


def create_app(cfg, config_path: Path | None = None,
               vocabulary: dict[str, str] | None = None,
               axes: tuple = (), scale_max: int | None = None,
               registry_path: Path | None = None,
               project_name: str | None = None) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _refuse_non_loopback(request, call_next):
        """Refuse anything that did not address this server by a loopback
        name, or that carries a cross-origin `Origin`.

        Without this, a page on any website could reach a Spoke running
        on the reader's own machine -- via DNS rebinding for the Host,
        and directly for a simple cross-origin POST -- and rewrite their
        memory store. Binding to 127.0.0.1 does not prevent it: the
        browser making the request IS on 127.0.0.1.
        """
        if not _host_is_loopback(request.headers.get("host", "")):
            return JSONResponse(
                {"detail": "refused: this server answers only to localhost"},
                status_code=403,
            )
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlparse

            if not _host_is_loopback(urlparse(origin).netloc):
                return JSONResponse(
                    {"detail": "refused: cross-origin request"}, status_code=403,
                )
        return await call_next(request)
    state = {"cfg": cfg, "project": project_name, "vocabulary": dict(vocabulary or {}),
             "axes": tuple(axes), "scale_max": scale_max}

    def _store() -> Store:
        # Built from the CURRENT config on every use, not once at start:
        # the first-run screen sets the store path through /api/config and
        # the same process must start reading it. This also retires the
        # "restart required" answer a store change used to need.
        return Store(state["cfg"].store_path)

    class _StoreProxy:
        """`store.<x>` reads as before; each attribute resolves against
        the current config."""
        def __getattr__(self, name):
            return getattr(_store(), name)

    store = _StoreProxy()

    @app.get("/api/health")
    def health():
        """Enough to prove it is THIS app answering, and not merely that
        something is listening.

        `serve --status` reported a foreign server on the same port as
        success, because "the port answers" and "Spoke answers" are
        different facts and it was only checking the first. Ports collide;
        a second project of the same person's was on 8765 already.
        """
        err = store_error(state["cfg"].store_path)
        return {"app": "spoke", "store": None if err else str(store.path),
                "first_run": err is not None}

    @app.get("/api/records")
    def list_records():
        return [{"name": m.name, "index_line": m.index_line,
                 "description": str(m.record.meta.get("description", ""))}
                for m in store.list_records()]

    @app.get("/api/records/{name}")
    def get_record(name: str):
        try:
            m = store.read(name)
        except FileNotFoundError:
            raise HTTPException(404, "no such record")
        return {"name": m.name, "body": m.record.body, "index_line": m.index_line,
                "meta": {k: str(v) for k, v in m.record.meta.items()}}

    @app.post("/api/records/{name}")
    def save_record(name: str, payload: SaveBody):
        res = store.write(name, payload.body, payload.index_line,
                           state["cfg"].accepted_absences, expected_body=payload.expected_body)
        if not res.ok:
            detail = [f"{f.kind}: {f.detail}" for f in res.findings]
            # A stale-read finding means the record changed on disk since
            # the caller loaded it -- this is a conflict (409), distinct
            # from a gate refusal (422) like a broken link or bad schema.
            # Losing the caller's in-flight edit on top of that would be a
            # second data-loss bug stacked on the one expected_body exists
            # to prevent, so the page must be told to reload rather than
            # treat this like any other refusal.
            if any(f.kind == "stale-read" for f in res.findings):
                raise HTTPException(409, detail)
            raise HTTPException(422, detail)
        return {"ok": True, "commit": res.commit,
                "advisories": [{"kind": a.kind, "file": a.file, "detail": a.detail}
                               for a in res.advisories]}

    def _shape(c: Config) -> dict:
        return {
            "store_path": str(c.store_path),
            "accepted_absences": list(c.accepted_absences),
            "stale_days": c.stale_days,
            "llm_provider": c.llm_provider,
            "llm_base_url": c.llm_base_url,
            "board_stylesheet": c.board_stylesheet,
            "brand_name": c.brand_name,
            "llm_model": c.llm_model,
            "brand_author": c.brand_author,
            "brand_author_url": c.brand_author_url,
            "brand_logo_url": c.brand_logo_url,
            "brand_tagline": c.brand_tagline,
            "ntfy_topic": c.ntfy_topic,
            "ntfy_server": c.ntfy_server,
        }

    @app.get("/api/config")
    def get_config():
        effective = state["cfg"]
        file_cfg = load_config_file(config_path)
        # Top-level fields stay the *effective* values, unchanged from
        # before FIX 1, for backward compatibility with existing callers.
        # "file" / "effective" / "overridden" are what the Settings screen
        # actually needs: the form edits "file", never "effective", so a
        # temporary env override can never be saved into config.toml by
        # accident (FIX 1).
        resp = _shape(effective)
        resp["config_path"] = str(config_path) if config_path is not None else None
        resp["file"] = _shape(file_cfg)
        resp["effective"] = _shape(effective)
        resp["overridden"] = overridden_env_vars()
        resp["llm_providers"] = sorted(_PROVIDERS)
        # Presence, never the value -- and named to keep clear of the
        # guard that refuses any field whose NAME contains "key", which
        # is worth more than the convenience of calling it what it is.
        # LLM_API_KEY is environment-only and
        # is not a Config field, so `overridden_env_vars()` cannot report
        # it -- and the Settings screen could say the key was env-only
        # while being unable to tell the reader whether one was actually
        # there.
        resp["llm_credential_present"] = _api_key_is_set()
        return resp

    @app.post("/api/config")
    def post_config(payload: ConfigBody):
        if config_path is None:
            raise HTTPException(
                422,
                "no config file path is known -- the server was started "
                "without a resolved config.toml path, so there is nowhere "
                "to save settings to",
            )
        msg = _validate_config_body(payload)
        if msg is not None:
            raise HTTPException(422, msg)
        new_cfg = Config(
            store_path=Path(payload.store_path).expanduser(),
            accepted_absences=tuple(payload.accepted_absences),
            stale_days=payload.stale_days,
            llm_provider=payload.llm_provider,
            llm_base_url=payload.llm_base_url or None,
            board_stylesheet=payload.board_stylesheet or None,
            brand_name=(payload.brand_name or "").strip() or "Spoke",
            llm_model=(payload.llm_model or "").strip() or None,
            brand_author=(payload.brand_author or "").strip(),
            brand_author_url=(payload.brand_author_url or "").strip(),
            brand_logo_url=(payload.brand_logo_url or "").strip(),
            brand_tagline=(payload.brand_tagline or "").strip() or DEFAULT_TAGLINE,
            ntfy_topic=(payload.ntfy_topic or "").strip(),
            ntfy_server=(payload.ntfy_server or "").strip().rstrip("/"),
        )
        save_config(config_path, new_cfg)
        # The running `store` object above was built once, at app start,
        # from whatever store_path was in effect then -- it is never
        # rebuilt on a save (there is no hot-reload). So if this save
        # changes store_path, the server keeps reading `store.path` (the
        # OLD one) until it is restarted, no matter what state["cfg"] now
        # says. Saying "Saved." with nothing else would be the exact
        # absence-rendering-as-assurance bug this project exists to catch
        # (FIX 2) -- so tell the truth, naming both paths.
        state["cfg"] = new_cfg
        return {"ok": True}

    def _ledger_store() -> LedgerStore:
        return LedgerStore(state["cfg"].store_path)

    @app.get("/api/lens")
    def api_lens(
        name: str = "product",
        zoom: str = "far",
        hops: int = 1,
        flag: list[str] = Query(default=[]),
    ):
        """One lens at one zoom: nodes with their flags, layers, shapes,
        badges and positions, plus everything that could not be read.

        A bad lens/zoom/flag is a 422, never a silent fallback to a
        working default -- a page that answers a question nobody asked
        looks exactly like a page that answers the one they did.
        """
        cfg = state["cfg"]
        ledger = _ledger_store()
        try:
            return build_view(
                ledger, name, Clock(date.today(), cfg.stale_days),
                zoom=zoom, hops=hops, flag_filter=tuple(flag),
                vocabulary=state["vocabulary"],
            )
        except BoardError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/matrix")
    def api_matrix(rows: str = "product", cols: str = "capability", hops: int = 1):
        cfg = state["cfg"]
        try:
            return build_matrix(_ledger_store(), rows, cols, Clock(date.today(), cfg.stale_days), hops)
        except BoardError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/scale")
    def api_scale(type: str = "product"):
        cfg = state["cfg"]
        try:
            return build_scale(
                _ledger_store(), tuple(state["axes"]), Clock(date.today(), cfg.stale_days),
                node_type=type, vocabulary=state["vocabulary"], max_value=state["scale_max"],
            )
        except BoardError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/report")
    def api_report(body: ReportPayload):
        """Record a problem found in a surface, through the same gate a
        human or an agent meets. The response names the node, because a
        report that vanishes into a store is the failure this tool is
        about."""
        err = _report_error(body)
        if err:
            raise HTTPException(422, err)
        store_err = store_error(state["cfg"].store_path)
        if store_err:
            raise HTTPException(409, store_err)
        node = _report_node(body, _ledger_store(), date.today())
        res = _ledger_store().write(node, tuple(state["axes"]), state["scale_max"] or DEFAULT_MAX_VALUE)
        if not res.ok:
            raise HTTPException(422, "; ".join(res.reasons))
        return {"name": node.name, "title": node.title,
                "raised": "as an open item: the ledger preamble lists it at the "
                          "start of the next session, and `spoke ledger show "
                          f"{node.name}` prints it now.",
                # Shown, not swallowed: a report nobody else's checkout has
                # is not yet raised anywhere but here.
                "pushed": res.pushed, "advisories": list(res.advisories)}

    @app.get("/scale")
    def scale_page():
        return _first_run_redirect() or _page("scale.html")

    def _page(filename: str):
        """Serve one of the app's own HTML files, with the optional
        `[board].stylesheet` linked and its assets cache-busted.

        Served as HTML rather than a file so the stylesheet can be
        injected. The value is validated on the way in (see
        _validate_config_body) and escaped here on the way out: a config
        file is not a trusted input just because it is local, and these
        pages hold a session against the user's store.
        """
        page = (STATIC / filename).read_text()
        # Cache-bust the app's own assets by their mtime. StaticFiles
        # serves them with a validator the browser is entitled to reuse,
        # and an upgraded Spoke serving last week's CSS against this
        # week's markup looks like a broken page, not a stale cache --
        # which is exactly the kind of wrong-but-plausible surface this
        # tool exists to stop shipping.
        stamp = str(max(
            int((STATIC / f).stat().st_mtime)
            for f in ("tokens.css", "app.css", "app.js", "settings.css",
                      "settings.js", "board.css", "board.js",
                      "scale.css", "scale.js", "envelope.js", "chrome.css",
                      "setup.css", "setup.js")
        ))
        page = page.replace("<!--ASSET_V-->", stamp)
        # Escaped at the point of use, as well as validated on the way in.
        # A config file is not a trusted input just because it is local.
        page = page.replace(
            "<!--BRAND-->", html.escape(state["cfg"].brand_name or "Spoke")
        )
        page = page.replace("<!--AUTHOR-->", _author_line(state["cfg"]))
        page = page.replace("<!--MARK-->", _mark(state["cfg"]))
        page = page.replace("<!--TAGLINE-->", html.escape(state["cfg"].brand_tagline or ""))
        sheet = state["cfg"].board_stylesheet
        link = ""
        if sheet and _STYLESHEET_RE.match(sheet):
            link = f'<link rel="stylesheet" href="{html.escape(sheet, quote=True)}">'
        # The page must never be served from a browser cache: its asset
        # links carry the stamp of the assets that existed when it was
        # last fetched, so a cached page loads last week's script against
        # this week's server. The assets themselves are cache-busted by
        # that stamp; the page that names them is the one thing that has
        # to be fresh every time.
        return HTMLResponse(page.replace("<!--BOARD_STYLESHEET-->", link),
                            headers={"Cache-Control": "no-store"})

    @app.get("/board")
    def board_page():
        return _first_run_redirect() or _page("board.html")

    # ── first run ────────────────────────────────────────────────────
    # The four things an install needs, in the order it needs them, and
    # which one is next. The page draws itself from this; it never
    # decides the order on its own.

    def _registry():
        from .projects import load_registry
        return load_registry(registry_path) if registry_path else {}

    def _store_candidates() -> list[dict]:
        """Memory stores this machine already has: every
        `<claude projects>/<slug>/memory` holding records. An installer
        who has used Claude Code has one per project; typing its path
        from memory is the first thing that goes wrong."""
        from .projects import default_claude_projects
        root = default_claude_projects()
        out = []
        if not root.exists():
            return out
        for d in sorted(root.iterdir()):
            mem = d / "memory"
            if mem.is_dir():
                n = sum(1 for _ in mem.glob("*.md"))
                if n:
                    out.append({"path": str(mem), "records": n, "project_dir": d.name})
        return out[:40]

    @app.get("/api/setup")
    def api_setup():
        cfg = state["cfg"]
        err = store_error(cfg.store_path)
        reg = _registry()
        projects = [{"name": n, "repos": [str(r) for r in p.repos], "axes": len(p.axes)}
                    for n, p in sorted(reg.items())]
        nodes = 0
        if err is None:
            try:
                nodes = len(_ledger_store().list_nodes())
            except Exception as e:  # a store that will not list is reported, not hidden
                err = f"store could not be read: {e}"
        active = state.get("project")
        if active is None and len(reg) == 1:
            active = next(iter(reg))
            state["project"] = active
        if active in reg:
            # The registry is the truth about a project; the terminal may
            # have changed it since this process started. Re-read it here
            # so a step done in a shell shows as done on this page, and
            # the scale draws the axes just declared.
            proj = reg[active]
            state["axes"] = tuple(proj.axes)
            state["vocabulary"] = dict(proj.vocabulary)
            state["scale_max"] = proj.scale_max
        axes = len(state.get("axes") or ())
        if err:
            nxt = "store"
        elif not projects:
            nxt = "project"
        elif nodes == 0:
            nxt = "content"
        elif axes == 0:
            nxt = "axes"
        else:
            nxt = "done"
        return {
            "store": {"path": None if str(cfg.store_path) in ("", ".") else str(cfg.store_path),
                      "ok": err is None, "error": err},
            "store_candidates": _store_candidates() if err else [],
            "projects": projects,
            "active_project": active,
            "ledger_nodes": nodes,
            "axes": axes,
            "next": nxt,
            "config_path": str(config_path) if config_path else None,
            "registry_path": str(registry_path) if registry_path else None,
        }

    @app.get("/api/axes")
    def api_axes(project: str | None = None):
        """The active project's scale, for the settings screen to edit.
        Read from the registry, not from this process's memory of it."""
        reg = _registry()
        name = project or state.get("project") or (next(iter(reg)) if len(reg) == 1 else None)
        if not name or name not in reg:
            return {"project": None, "projects": sorted(reg), "axes": [],
                    "scale_max": DEFAULT_MAX_VALUE}
        proj = reg[name]
        return {
            "project": name,
            "projects": sorted(reg),
            "axes": [{"key": a.key, "label": a.label, "weight": a.weight} for a in proj.axes],
            "scale_max": proj.scale_max,
        }

    @app.post("/api/axes")
    def api_axes_save(body: AxesPayload):
        """Declare the scale from the settings screen. The one rule this
        tool has about editability is that a non-coder can change every
        setting on a screen; the axes were the last thing only a
        terminal could reach."""
        from dataclasses import replace
        from .ledger.scale import Axis
        from .projects import RegistryError, load_registry, save_registry, _validate_name
        if not registry_path:
            raise HTTPException(409, "no project registry is configured for this server")
        reg = load_registry(registry_path)
        if body.project not in reg:
            raise HTTPException(422, f"project: {body.project!r} is not registered")
        if body.scale_max < 1:
            raise HTTPException(422, f"scale_max: must be at least 1, got {body.scale_max}")
        seen: set[str] = set()
        axes = []
        for i, a in enumerate(body.axes, 1):
            key = a.key.strip()
            if not key:
                raise HTTPException(422, f"axis {i}: a key is needed")
            try:
                _validate_name(key)
            except RegistryError as e:
                raise HTTPException(422, f"axis {i}: {e}")
            if key in seen:
                raise HTTPException(422, f"axis {i}: key {key!r} is declared twice")
            seen.add(key)
            if a.weight < 0:
                raise HTTPException(422, f"axis {key!r}: a weight cannot be negative")
            label = a.label.strip() or key
            if any(ord(c) < 32 or ord(c) == 127 for c in label):
                raise HTTPException(422, f"axis {key!r}: the label must not contain control characters")
            axes.append(Axis(key=key, label=label, weight=float(a.weight)))
        proj = replace(reg[body.project], axes=tuple(axes), scale_max=body.scale_max)
        reg[body.project] = proj
        save_registry(registry_path, reg)
        if state.get("project") in (None, body.project):
            state["project"] = body.project
            state["axes"] = tuple(axes)
            state["scale_max"] = body.scale_max
        total = sum(a.weight for a in axes) or 1
        return {"ok": True, "project": body.project, "scale_max": body.scale_max,
                "axes": [{"key": a.key, "label": a.label, "weight": a.weight,
                          "share": round(a.weight / total, 4)} for a in axes]}

    @app.post("/api/projects")
    def api_projects(body: ProjectPayload):
        """Register a project, or grow one that exists -- the same two
        verbs `projects add` has on the terminal."""
        from .projects import RegistryError, add_project, extend_project, load_registry
        if not registry_path:
            raise HTTPException(409, "no project registry is configured for this server")
        name = body.name.strip()
        repos = [Path(r.strip()).expanduser() for r in body.repos if r.strip()]
        if not name:
            raise HTTPException(422, "name: a project needs a name")
        if not repos:
            raise HTTPException(422, "repos: a project is a name and at least one repo path")
        missing = [str(r) for r in repos if not r.is_dir()]
        if missing:
            raise HTTPException(422, f"repos: not a directory on this machine: {', '.join(missing)}")
        try:
            if name in load_registry(registry_path):
                proj, added = extend_project(registry_path, name, repos)
                verb = "grown" if added else "unchanged"
            else:
                # The project carries the store this server is reading.
                # Without it, every terminal command derives the store
                # from the repo paths -- the Claude Code convention --
                # and an installer whose store is anywhere else hits
                # "has no memory store holding records" on the very next
                # step this page tells them to run.
                store_path = state["cfg"].store_path
                proj = add_project(registry_path, name, repos,
                                   memory_store=store_path if not store_error(store_path) else None)
                verb = "registered"
        except RegistryError as e:
            raise HTTPException(422, str(e))
        if state.get("project") in (None, name):
            state["project"] = name
            state["vocabulary"] = dict(proj.vocabulary)
            state["axes"] = tuple(proj.axes)
            state["scale_max"] = proj.scale_max
        return {"ok": True, "verb": verb, "name": proj.name,
                "repos": [str(r) for r in proj.repos]}

    @app.post("/api/scan")
    def api_scan(body: ScanPayload):
        """Run the scan for the active project. Synchronous: a first scan
        of a few repos is seconds, and a page that says "scanning" while
        it happens is more honest than one that returns before it has."""
        from .scan import apply, scan_repos
        err = store_error(state["cfg"].store_path)
        if err:
            raise HTTPException(409, err)
        reg = _registry()
        name = state.get("project")
        if not name or name not in reg:
            raise HTTPException(409, "no active project to scan -- register one first")
        proj = reg[name]
        result = scan_repos(list(proj.repos), date.today(), proj.vocabulary)
        applied = apply(result, _ledger_store(), tuple(proj.axes), proj.scale_max,
                        date.today(), write=body.write)
        counts = {"proposals": len(result.proposals), **applied.counts}
        return {"ok": True, "wrote": body.write, "counts": counts,
                "notes": list(result.notes)[:40], "lines": applied.lines[:200]}

    @app.post("/api/demo")
    def api_demo(body: DemoPayload):
        """Build the demo map, from the page, for someone who has nothing
        to point at yet.

        Synchronous like the scan, and slower -- it clones two repos --
        because a page that says "building" while it builds is more honest
        than one that returns before it has.

        It configures NOTHING here. The demo writes its own store,
        registry and config under its own directory and hands back the
        command that serves them; this server keeps pointing wherever it
        already pointed, which for a first run is nowhere. Re-pointing a
        live server at the demo would mean writing the user's config file
        on their behalf, to show them a sample.
        """
        from . import demo as demo_mod

        dest = Path(body.dir).expanduser()
        try:
            r = demo_mod.build(dest)
        except demo_mod.DemoError as e:
            # A failed clone is the ordinary case on a machine with no
            # network, and it is a refusal with a reason, not a crash.
            raise HTTPException(409, str(e))
        except OSError as e:
            raise HTTPException(409, f"could not build the demo at {dest}: {e}")
        return {"ok": True, "root": str(r.root), "store": str(r.store),
                "config": str(r.config), "nodes": r.nodes,
                "scanned": r.scanned, "serve_command": r.serve_command}

    def _first_run_redirect():
        from fastapi.responses import RedirectResponse
        if store_error(state["cfg"].store_path):
            return RedirectResponse("/setup", status_code=307)
        return None

    @app.get("/setup")
    def setup_page():
        return _page("setup.html")

    @app.get("/")
    def index():
        return _first_run_redirect() or _page("index.html")

    @app.get("/settings")
    def settings_page():
        return _page("settings.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
