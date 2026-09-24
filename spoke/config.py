from __future__ import annotations
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# No default store path. A personal machine path baked in as a default is the
# BYO violation this project forbids: a second project, or a second person,
# would silently review someone else's memories. The path must be stated, in
# config.toml or SPOKE_STORE_PATH, and its absence is an error rather than a
# quiet fallback.


DEFAULT_TAGLINE = "What was decided, deferred, blocked or found across your projects, in front of the next session."


class ConfigError(RuntimeError):
    """Raised when the configuration cannot be used. Never swallowed: a tool
    that starts against an unknown store is the failure this project exists
    to remove."""


@dataclass(frozen=True)
class Config:
    store_path: Path
    accepted_absences: tuple[str, ...]
    stale_days: int
    llm_provider: str
    llm_base_url: str | None
    # The model to use. There is deliberately NO default for a cloud
    # provider -- see llm.resolve_endpoint. Left unset with a local
    # provider, that provider's own default is used, because a model on
    # your own hardware cannot generate a bill.
    llm_model: str | None = None
    # An optional stylesheet the board links AFTER its own, so an
    # installer can restyle the map to their own design system without
    # the package carrying anybody's brand (see board.css). Defaulted so
    # every existing Config(...) call site keeps working unchanged.
    board_stylesheet: str | None = None
    # What this INSTALL calls itself, shown in the page title and header.
    # Defaulted, not hardcoded: the package must ship no organisation's
    # name (tests/test_no_estate_identity.py enforces it), while the
    # person who installed it must be able to put theirs on it WITHOUT
    # touching code -- the estate's own editability rule. So the brand
    # lives in config.toml and on the Settings screen, and travels with
    # the install rather than with the source.
    brand_name: str = "Spoke"
    # Who made this install, and where to find them -- rendered as a
    # "by <author>" link in the header. Same rule as brand_name: the
    # package ships nobody's name, so both default empty and the link
    # is not rendered at all until the installer fills them in. The
    # URL is validated to http(s) on the way in (server.py) AND escaped
    # on the way out (_page), because a config file is not a trusted
    # input just because it is local.
    brand_author: str = ""
    brand_author_url: str = ""
    # The install's mark, replacing the package's own. A URL the page
    # links -- never a file the package carries -- so an estate with a
    # served brand bundle points here and copies nothing. http(s) or a
    # same-origin path, validated on save and re-checked at render.
    brand_logo_url: str = ""
    # One line under the name saying what this is. Defaults to the
    # package's own line, which names no one; an install can say it in
    # its own words from the settings screen.
    brand_tagline: str = DEFAULT_TAGLINE
    # [notify]: where `doctor` pushes NEW findings. Empty topic = off,
    # stated at every run. Held on Config so a Settings save round-trips
    # it -- the first version did not, and one Save silently switched
    # the doctor's alerts off. No default topic, ever: a baked-in one
    # would page whoever inherits it.
    ntfy_topic: str = ""
    ntfy_server: str = ""


# The env vars load_config() lets override a file value, keyed by the
# Config field they affect. A Settings screen must know exactly which
# fields these are so it can (a) edit the FILE value, never the
# env-overridden effective value, and (b) tell the user when a field
# they're looking at is currently being overridden. Shared here so
# server.py never has to re-list them by hand and risk the list drifting
# from what load_config() actually reads.
ENV_OVERRIDES: dict[str, str] = {
    "store_path": "SPOKE_STORE_PATH",
    "llm_provider": "LLM_PROVIDER",
    "llm_base_url": "LLM_BASE_URL",
    "llm_model": "LLM_MODEL",
}


def load_config(path: Path | None = None, require_store: bool = True) -> Config:
    data: dict = {}
    if path is not None and Path(path).exists():
        data = tomllib.loads(Path(path).read_text())
    store = data.get("store", {})
    checks = data.get("checks", {})
    llm = data.get("llm", {})
    raw_path = os.environ.get("SPOKE_STORE_PATH") or store.get("path")
    if not raw_path or not str(raw_path).strip():
        if require_store:
            raise ConfigError(
                "no store path configured: set [store].path in config.toml, or the "
                "SPOKE_STORE_PATH environment variable. There is deliberately no "
                "default -- pointing at the wrong project's memories silently is "
                "worse than refusing to start."
            )
        # The project registry will supply the real store. This placeholder
        # cannot exist, so if it ever leaks past config_for() the CLI's store
        # validator refuses it loudly and names it, rather than reading
        # somewhere real.
        raw_path = "/__spoke_store_unset__"
    return Config(
        store_path=Path(raw_path).expanduser(),
        accepted_absences=tuple(store.get("accepted_absences", [])),
        stale_days=int(checks.get("stale_days", 30)),
        board_stylesheet=(data.get("board", {}) or {}).get("stylesheet") or None,
        brand_name=((data.get("brand", {}) or {}).get("name") or "Spoke"),
        brand_author=str((data.get("brand", {}) or {}).get("author") or ""),
        brand_author_url=str((data.get("brand", {}) or {}).get("author_url") or ""),
        brand_logo_url=str((data.get("brand", {}) or {}).get("logo_url") or ""),
        brand_tagline=str((data.get("brand", {}) or {}).get("tagline") or DEFAULT_TAGLINE),
        ntfy_topic=str((data.get("notify", {}) or {}).get("ntfy_topic") or ""),
        ntfy_server=str((data.get("notify", {}) or {}).get("ntfy_server") or ""),
        llm_provider=os.environ.get("LLM_PROVIDER") or llm.get("provider", "groq"),
        llm_base_url=os.environ.get("LLM_BASE_URL") or llm.get("base_url"),
        llm_model=os.environ.get("LLM_MODEL") or llm.get("model"),
    )


def load_config_file(path: Path | None) -> Config:
    """Read `path` as TOML and return exactly what the FILE says, with NO
    environment-variable overrides applied. This is what a Settings screen
    must show and edit (FIX 1): if it instead showed load_config()'s
    effective, env-influenced values, saving an unrelated field would
    silently bake a temporary env override into config.toml forever.

    A missing file, or a file with no store path, is not an error here --
    unlike load_config(), this function never raises ConfigError. Its only
    job is to report what config.toml itself contains, even when the real
    store path is actually coming from SPOKE_STORE_PATH or the project
    registry. An unset store path in the file is represented as Path("").
    """
    data: dict = {}
    if path is not None and Path(path).exists():
        data = tomllib.loads(Path(path).read_text())
    store = data.get("store", {})
    checks = data.get("checks", {})
    llm = data.get("llm", {})
    raw_path = store.get("path") or ""
    return Config(
        store_path=Path(raw_path).expanduser() if raw_path else Path(""),
        accepted_absences=tuple(store.get("accepted_absences", [])),
        stale_days=int(checks.get("stale_days", 30)),
        board_stylesheet=(data.get("board", {}) or {}).get("stylesheet") or None,
        brand_name=((data.get("brand", {}) or {}).get("name") or "Spoke"),
        brand_author=str((data.get("brand", {}) or {}).get("author") or ""),
        brand_author_url=str((data.get("brand", {}) or {}).get("author_url") or ""),
        brand_logo_url=str((data.get("brand", {}) or {}).get("logo_url") or ""),
        brand_tagline=str((data.get("brand", {}) or {}).get("tagline") or DEFAULT_TAGLINE),
        ntfy_topic=str((data.get("notify", {}) or {}).get("ntfy_topic") or ""),
        ntfy_server=str((data.get("notify", {}) or {}).get("ntfy_server") or ""),
        llm_provider=llm.get("provider", "groq"),
        llm_base_url=llm.get("base_url"),
        llm_model=llm.get("model"),
    )


def overridden_env_vars() -> dict[str, str]:
    """{field: env_var_name} for every Config field currently overridden by
    a set environment variable -- i.e. every entry of ENV_OVERRIDES whose
    variable is actually present in the environment right now."""
    return {field: var for field, var in ENV_OVERRIDES.items() if os.environ.get(var)}


def _toml_str(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_config(path: Path, cfg: Config) -> None:
    """Write `cfg` to `path` as TOML in the same [store]/[checks]/[llm]/
    [board]/[brand]/[notify] shape load_config reads, so load_config(path) == cfg afterward.

    Deliberately never writes an API key: LLM_API_KEY is environment-only
    (see llm.py), and Config itself has no field for it, so there is
    nothing here that could leak one into a file a Settings screen writes.

    Written atomically -- a sibling temp file then os.replace() -- the
    same mechanism projects.save_registry already uses, so a crash
    mid-write leaves the previous config.toml intact rather than a
    truncated file that then fails to parse on the next load.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "[store]",
        f"path = {_toml_str(str(cfg.store_path))}",
        "accepted_absences = ["
        + ", ".join(_toml_str(a) for a in cfg.accepted_absences)
        + "]",
        "",
        "[checks]",
        f"stale_days = {int(cfg.stale_days)}",
        "",
        "[llm]",
        f"provider = {_toml_str(cfg.llm_provider)}",
    ]
    if cfg.llm_base_url:
        lines.append(f"base_url = {_toml_str(cfg.llm_base_url)}")
    if cfg.llm_model:
        lines.append(f"model = {_toml_str(cfg.llm_model)}")
    if cfg.board_stylesheet:
        lines += ["", "[board]", f"stylesheet = {_toml_str(cfg.board_stylesheet)}"]
    brand_lines = []
    if cfg.brand_name and cfg.brand_name != "Spoke":
        brand_lines.append(f"name = {_toml_str(cfg.brand_name)}")
    if cfg.brand_author:
        brand_lines.append(f"author = {_toml_str(cfg.brand_author)}")
    if cfg.brand_author_url:
        brand_lines.append(f"author_url = {_toml_str(cfg.brand_author_url)}")
    if cfg.brand_logo_url:
        brand_lines.append(f"logo_url = {_toml_str(cfg.brand_logo_url)}")
    if cfg.brand_tagline and cfg.brand_tagline != DEFAULT_TAGLINE:
        brand_lines.append(f"tagline = {_toml_str(cfg.brand_tagline)}")
    if brand_lines:
        lines += ["", "[brand]", *brand_lines]
    notify_lines = []
    if cfg.ntfy_topic:
        notify_lines.append(f"ntfy_topic = {_toml_str(cfg.ntfy_topic)}")
    if cfg.ntfy_server:
        notify_lines.append(f"ntfy_server = {_toml_str(cfg.ntfy_server)}")
    if notify_lines:
        lines += ["", "[notify]", *notify_lines]
    lines.append("")
    content = "\n".join(lines)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(content)
    os.replace(tmp, path)
