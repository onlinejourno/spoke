from __future__ import annotations
import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

# One TOML quoting rule, not two byte-identical copies guarding two
# different atomic-write paths.
from .config import Config, _toml_str
from .ledger.scale import DEFAULT_MAX_VALUE, Axis


class RegistryError(RuntimeError):
    """Raised when the project registry cannot be used or a request to it is
    invalid. Never swallowed: guessing which project was meant is the defect
    this layer exists to remove."""


@dataclass(frozen=True)
class Project:
    name: str
    repos: tuple[Path, ...]
    memory_store: Path | None = None
    # Absences this project has explicitly accepted -- e.g. a wikilink target
    # that is intentionally missing. This travels WITH the project so the
    # same project reports the same defects regardless of which directory
    # (and therefore which config.toml, if any) the command was run from.
    # See config_for() for how this combines with the base config's own
    # accepted_absences.
    #
    # Three distinct states, not two:
    #   None        -- NOT DECLARED. The project has no opinion; config_for()
    #                  falls back to the base config's accepted_absences.
    #   ()           -- DECLARED EMPTY. The project explicitly accepts
    #                  nothing; config_for() must NOT fall back to base.
    #   (a, b, ...)  -- DECLARED, populated. Replaces the base config's list
    #                  outright (see config_for()'s docstring for why).
    # Collapsing "not declared" and "declared empty" into one falsy value
    # (the old default was ()) made an explicit "I accept nothing" silently
    # read as "inherit whatever the base config accepts" -- exactly the kind
    # of quiet, wrong behaviour this project exists to catch elsewhere.
    accepted_absences: tuple[str, ...] | None = None
    # The project's OWN word for each engine node type, e.g.
    # {"product": "app"}. The ledger's `type` stays the engine's closed
    # six -- lenses, flags and propagation are type-agnostic only because
    # that set does not grow -- and this is what every surface RENDERS
    # instead. So a reader of a project that calls its units "chapters"
    # never sees the word "product", while `resolve_lens` still works
    # without knowing any project's vocabulary.
    #
    # The scan PROPOSES entries here from a repo's own layout; a key
    # present means the human accepted it, and the scan stops proposing.
    vocabulary: dict[str, str] = field(default_factory=dict)
    # The capability axes this project is assessed on, in the order they
    # are rendered, each with a weight. Declared per project on purpose:
    # a hardcoded axis list would be this package carrying one
    # organisation's assessment framework, the same defect as a
    # hardcoded outlet name. See ledger/scale.py.
    axes: tuple = ()
    # The top of the score range. Declared with the axes because it IS
    # part of the scale: a project scoring 0-4 rendered on a 0-5 ramp
    # shows a full mark as 80%, which is a lie by rendering. The package
    # default is the package's; a project's scale is the project's.
    scale_max: int = DEFAULT_MAX_VALUE

    def word_for(self, node_type: str) -> str:
        """The word to show a reader for `node_type`."""
        return self.vocabulary.get(node_type, node_type)


def load_registry(path: Path) -> dict[str, Project]:
    if not Path(path).exists():
        return {}
    text = Path(path).read_text()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RegistryError(
            f"registry file {path} is corrupt and cannot be parsed as TOML "
            f"({e}). Fix or remove this file."
        ) from e
    out: dict[str, Project] = {}
    for name, body in (data.get("projects") or {}).items():
        repos = tuple(Path(r).expanduser() for r in (body.get("repos") or []))
        ms = body.get("memory_store")
        # None (key absent) stays None -- NOT DECLARED. A present key,
        # even `accepted_absences = []`, becomes a tuple -- DECLARED,
        # possibly empty. `or []` here would collapse both back into one
        # falsy state, which is exactly the bug this three-state type exists
        # to fix.
        raw_absences = body.get("accepted_absences")
        absences = tuple(raw_absences) if raw_absences is not None else None
        vocab = {
            str(k): str(v) for k, v in (body.get("vocabulary") or {}).items()
        }
        raw_axes = body.get("axes") or []
        if not isinstance(raw_axes, list):
            raise RegistryError(
                f"project {name!r}: 'axes' must be a list of "
                "{ key, label, weight } tables"
            )
        axes = tuple(
            Axis(
                key=str(a.get("key", "")),
                label=str(a.get("label") or a.get("key", "")),
                weight=float(a.get("weight", 1)),
            )
            for a in raw_axes
            if isinstance(a, dict) and a.get("key")
        )
        raw_max = body.get("scale_max", DEFAULT_MAX_VALUE)
        try:
            scale_max = int(raw_max)
        except (TypeError, ValueError):
            raise RegistryError(f"project {name!r}: 'scale_max' must be a whole number, got {raw_max!r}")
        if scale_max < 1:
            raise RegistryError(f"project {name!r}: 'scale_max' must be at least 1, got {scale_max}")
        out[name] = Project(
            name, repos, Path(ms).expanduser() if ms else None, absences, vocab, axes,
            scale_max,
        )
    return out



def save_registry(path: Path, projects: dict[str, Project]) -> None:
    path = Path(path)
    # The default registry lives under ~/.claude/spoke/, which does
    # not exist on a fresh machine. Create it before writing so the very
    # first `projects add` does not crash with an unhandled
    # FileNotFoundError -- a traceback on someone's first command is the
    # worst possible first impression, and this project's own rule is that
    # failures are clean and actionable.
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for name in sorted(projects):
        p = projects[name]
        # Quote the table key. A bare key ("[projects.my.project]") is TOML
        # for a NESTED table -- it reloads as project "my" holding a
        # sub-table "project", not as one project named "my.project". A
        # second dotted sibling then silently rewrites the first one's
        # entry under the shared "my" prefix, destroying its repos after a
        # success message. Quoting makes the key a single string, which
        # tomllib reads back correctly regardless of dots in the name.
        lines.append(f"[projects.{_toml_str(name)}]")
        lines.append("repos = [" + ", ".join(_toml_str(r) for r in p.repos) + "]")
        if p.memory_store is not None:
            lines.append(f"memory_store = {_toml_str(p.memory_store)}")
        if p.accepted_absences is not None:
            # Written whenever DECLARED -- including declared EMPTY, which
            # writes `accepted_absences = []`. Omitted only when NOT
            # DECLARED (None), so load_registry's `body.get(...)` sees no
            # key at all and reconstructs None, not an empty tuple.
            lines.append(
                "accepted_absences = ["
                + ", ".join(_toml_str(a) for a in p.accepted_absences)
                + "]"
            )
        if p.vocabulary:
            # An inline table keeps one project's whole entry inside its
            # own [projects."name"] block -- a [projects."name".vocabulary]
            # sub-table would have to be emitted after every other key of
            # every project, and one misordered line silently reassigns it
            # to the wrong project.
            lines.append(
                "vocabulary = { "
                + ", ".join(
                    f"{_toml_str(k)} = {_toml_str(v)}"
                    for k, v in sorted(p.vocabulary.items())
                )
                + " }"
            )
        if p.scale_max != DEFAULT_MAX_VALUE:
            lines.append(f"scale_max = {p.scale_max}")
        if p.axes:
            # An array of inline tables, and DECLARATION ORDER is
            # preserved rather than sorted: the order is the reading
            # order of the scale, which the project chose.
            lines.append(
                "axes = ["
                + ", ".join(
                    "{ key = " + _toml_str(a.key)
                    + ", label = " + _toml_str(a.label)
                    + f", weight = {a.weight:g} }}"
                    for a in p.axes
                )
                + "]"
            )
        lines.append("")
    content = "\n".join(lines)
    # Write to a sibling temp file and rename into place: os.replace() is
    # atomic on POSIX, so a crash mid-write leaves the original file intact
    # instead of a truncated registry that then fails to parse on next load.
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(content)
    os.replace(tmp, path)


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _validate_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        raise RegistryError(
            f"project name {name!r} is invalid: a name must match "
            f"{_NAME_RE.pattern!r} -- start with a letter or digit, then "
            "letters, digits, '.', '_' or '-'. The name becomes a TOML "
            "table key and a CLI argument, so this is enforced rather than "
            "left to corrupt the registry."
        )


def _validate_no_control_chars(path: Path) -> None:
    s = str(path)
    if _CONTROL_CHAR_RE.search(s):
        raise RegistryError(
            f"path {s!r} contains a control character (e.g. a newline or "
            "carriage return); that cannot appear in a real repo path and "
            "would corrupt the registry file"
        )


def _normalize_repo(r: Path) -> Path:
    # Absolute but NOT resolved: Claude slugs the launch directory exactly as
    # given, so following a symlink here would change the slug and derive
    # the wrong store. os.path.abspath normalises '..' without touching
    # symlinks. Store paths (used for dedup in derive_stores) are resolved
    # elsewhere; repo paths, which the slug is computed from, are not.
    return Path(os.path.abspath(Path(r).expanduser()))


def extend_project(path: Path, name: str, repos: list[Path]) -> tuple[Project, tuple[Path, ...]]:
    """Add `repos` to an EXISTING project. Returns (project, the repos
    that were actually new).

    Distinct from `add_project` on purpose: that creates a project from
    four fields, and calling it on an existing name would either refuse
    (as it does) or rebuild the project and silently drop the two fields
    it does not take -- `vocabulary` and `axes`. Growing a project must
    touch its repo list and nothing else. Found the day a product with
    a live outage turned out not to be on the map: the only way to add
    it was `remove` and re-`add`, which would have dropped the project's
    memory store, accepted absences and vocabulary with it.

    Idempotent: a repo already in the project is reported, not duplicated.
    """
    from dataclasses import replace

    _validate_name(name)
    if not repos:
        raise RegistryError(f"nothing to add to project {name!r}: pass at least one --repo")
    reg = load_registry(path)
    if name not in reg:
        raise RegistryError(
            f"project {name!r} is not registered; `projects add` creates one"
        )
    norm = []
    for r in repos:
        n = _normalize_repo(r)
        _validate_no_control_chars(n)
        norm.append(n)
    existing = reg[name]
    added = tuple(r for r in norm if r not in existing.repos)
    if added:
        reg[name] = replace(existing, repos=existing.repos + added)
        save_registry(path, reg)
    return reg[name], added


def add_project(
    path: Path,
    name: str,
    repos: list[Path],
    memory_store: Path | None = None,
    accepted_absences: list[str] | None = None,
) -> Project:
    _validate_name(name)
    if not repos:
        raise RegistryError(
            f"project {name!r} needs at least one repo path: a project is a "
            "name and a set of repo paths, and everything else derives from them"
        )
    norm_repos = tuple(_normalize_repo(r) for r in repos)
    for r in norm_repos:
        _validate_no_control_chars(r)
    norm_store = Path(memory_store).expanduser() if memory_store is not None else None
    if norm_store is not None:
        _validate_no_control_chars(norm_store)
    # None (the default -- caller passed nothing) stays None: NOT DECLARED,
    # config_for() falls back to the base config. An explicit `[]` becomes
    # `()`: DECLARED EMPTY, config_for() must not fall back. `... or []`
    # would collapse both into one falsy state -- the bug this fixes.
    norm_absences = None if accepted_absences is None else tuple(accepted_absences)
    for a in (norm_absences or ()):
        if _CONTROL_CHAR_RE.search(a):
            raise RegistryError(
                f"accepted absence {a!r} contains a control character (e.g. "
                "a newline or carriage return); that would corrupt the registry file"
            )
    reg = load_registry(path)
    if name in reg:
        raise RegistryError(
            f"project {name!r} is already registered with repos "
            f"{[str(r) for r in reg[name].repos]}; remove it first or choose another name"
        )
    p = Project(name, norm_repos, norm_store, norm_absences)
    reg[name] = p
    save_registry(path, reg)
    return p


def remove_project(path: Path, name: str) -> None:
    """Forget a project's registration. This only removes the entry from
    the registry -- it never touches that project's repos or its memory
    store. Deregistering is not deleting: the records a project points at
    are exactly as untouched afterward as before."""
    reg = load_registry(path)
    if name not in reg:
        raise RegistryError(
            f"unknown project {name!r}: nothing to remove. Registered: "
            + ", ".join(sorted(reg))
        )
    del reg[name]
    save_registry(path, reg)


_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
# CLAUDE_PROJECTS is a plain module constant -- it is what every caller
# falls back to when it means "the default" and SPOKE_CLAUDE_PROJECTS is
# unset, and it is what a caller can compare against to recognise "the
# real tree" (as tests/conftest.py's isolation meta-test does). It is
# deliberately NOT what the functions below default their own
# `claude_projects` parameter to -- see default_claude_projects().
CLAUDE_PROJECTS = Path("~/.claude/projects").expanduser()


def default_claude_projects() -> Path:
    """The Claude projects tree a caller gets when it does not name one
    explicitly: SPOKE_CLAUDE_PROJECTS if set, else CLAUDE_PROJECTS.

    FINDING 5: `derive_stores`, `project_for_path` and `config_for` used
    to spell `claude_projects: Path = CLAUDE_PROJECTS` directly as their
    parameter default -- a plain module constant, bound once, at IMPORT
    time. Only `cli.py`'s own `_claude_projects()` read the env var, and
    only because every CLI command passes its result in explicitly; a
    caller of the library functions themselves (several tests in
    tests/test_projects.py included) that omitted the argument fell
    through to the real `~/.claude/projects` -- read-only, and today's
    fixture data happens not to collide with it, but that is luck, not a
    guarantee, and conftest.py's isolation meta-test certified a coverage
    it did not actually have.

    This function is called from inside each of those three functions,
    not baked into their signatures, precisely so it re-reads the env var
    on every call -- a test that sets SPOKE_CLAUDE_PROJECTS via
    monkeypatch part-way through is honoured immediately, the same way
    `cli._claude_projects()` already behaves.
    """
    return Path(os.environ.get("SPOKE_CLAUDE_PROJECTS") or CLAUDE_PROJECTS).expanduser()


def slug_for(repo_path: Path) -> str:
    """Claude keys memory to the directory a session was launched from,
    slugging the absolute path by replacing every non-alphanumeric character
    with '-'. Verified against real project directories."""
    return _NON_ALNUM.sub("-", str(Path(repo_path)))


def derive_stores(project: Project, claude_projects: Path | None = None) -> list[Path]:
    """Memory stores for a project, derived from its repo paths.

    Derived rather than configured because a derived value cannot drift from
    what it describes. Paths are resolved and deduplicated: several repo dirs
    may symlink to one canonical store, and counting it twice would double
    every record. Stores holding no records are dropped -- a repo that has
    never had a session contributes nothing.

    `claude_projects` defaults to `default_claude_projects()` -- resolved
    HERE, at call time, not baked into the signature -- so a caller that
    omits it still honours SPOKE_CLAUDE_PROJECTS (Finding 5).
    """
    if claude_projects is None:
        claude_projects = default_claude_projects()
    if project.memory_store is not None:
        p = Path(project.memory_store).expanduser()
        return [p.resolve()] if p.is_dir() and any(p.glob("*.md")) else []
    seen: list[Path] = []
    for repo in project.repos:
        cand = Path(claude_projects) / slug_for(repo) / "memory"
        if not cand.is_dir():
            continue
        real = cand.resolve()
        if not any(real.glob("*.md")):
            continue
        if real not in seen:
            seen.append(real)
    return seen


def _path_is_or_within(path: Path, repo: Path) -> bool:
    """True if `path` equals `repo` or is nested inside it. Compared with
    os.path.abspath, matching the normalisation add_project already applies
    to repo paths -- and deliberately NOT resolved: Claude slugs the launch
    directory exactly as given, so following a symlink here would compare
    the wrong identity against the registry."""
    p = os.path.abspath(str(path))
    r = os.path.abspath(str(repo))
    return p == r or p.startswith(r + os.sep)


def project_for_path(
    registry: dict[str, Project],
    path: Path,
    claude_projects: Path | None = None,
) -> Project | None:
    """Which registered project owns `path`?

    `claude_projects` defaults to `default_claude_projects()` at call time
    (Finding 5) -- see that function's docstring.

    A SessionStart hook knows a directory (the transcript's containing
    folder, a slugged directory under ~/.claude/projects/) but the CLI's
    --project takes a registered NAME, not a path. This is what connects
    the two.

    Matches in priority order, and only within one priority level at a
    time -- if a level produces more than one match, that is ambiguous and
    the function refuses rather than picking one, even if a lower level
    would have produced a unique answer:

      1. `path` is one of a project's repo paths, or lives inside one.
      2. `path` is a ~/.claude/projects/<slug> directory whose slug names
         one of the project's repo paths -- what the hook actually has.
      3. `path` resolves to the same memory store as one of the project's
         derived stores.

    Returns None when nothing matches at any level, or when several
    projects match at the first level that matches at all.
    """
    if claude_projects is None:
        claude_projects = default_claude_projects()
    path = Path(path)

    # 1. repo path, or a directory inside one.
    matches = [
        proj for proj in registry.values()
        if any(_path_is_or_within(path, repo) for repo in proj.repos)
    ]
    if matches:
        return matches[0] if len(matches) == 1 else None

    # 2. a ~/.claude/projects/<slug> directory naming one of the project's
    # repos. Only considered when `path`'s parent really is the claude
    # projects directory -- a bare name match elsewhere would be a
    # coincidence, not a real signal.
    claude_projects = Path(claude_projects)
    if os.path.abspath(str(path.parent)) == os.path.abspath(str(claude_projects)):
        slug = path.name
        matches = [
            proj for proj in registry.values()
            if any(slug_for(repo) == slug for repo in proj.repos)
        ]
        if matches:
            return matches[0] if len(matches) == 1 else None

    # 3. the same memory store as one of the project's derived stores.
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    matches = [
        proj for proj in registry.values()
        if resolved in {s.resolve() for s in derive_stores(proj, claude_projects)}
    ]
    if matches:
        return matches[0] if len(matches) == 1 else None

    return None


def select_project(registry: dict[str, Project], requested: str | None) -> Project:
    """Pick the project to act on. Refuses rather than guesses: acting on an
    unstated assumption about which project was meant is the same defect as a
    default store path."""
    if not registry:
        raise RegistryError(
            "no projects registered: run `spoke projects add <name> "
            "--repo <path>` first. A project is a name and a set of repo paths."
        )
    if requested is not None:
        if requested not in registry:
            raise RegistryError(
                f"unknown project {requested!r}. Registered: "
                + ", ".join(sorted(registry))
            )
        return registry[requested]
    if len(registry) == 1:
        return next(iter(registry.values()))
    raise RegistryError(
        "several projects are registered and none was named -- refusing to "
        "guess which one you meant. Pass --project <name> or set "
        "SPOKE_PROJECT. Registered: " + ", ".join(sorted(registry))
    )


def _describe_store_path(p: Path) -> str:
    """Distinguish the three ways a candidate store can hold nothing: never
    created, created but empty, or (for a resolve() call) unreachable. Lets
    config_for's refusal tell a genuinely new project from a misconfigured
    path instead of collapsing both into one empty result."""
    if not p.is_dir():
        return "no such directory - no session has run there"
    if not any(p.resolve().glob("*.md")):
        return "exists but holds no records"
    return "ok"


def _no_store_message(project: Project, claude_projects: Path) -> str:
    if project.memory_store is not None:
        p = Path(project.memory_store).expanduser()
        return (
            f"project {project.name!r} has no memory store holding records: "
            f"its configured memory_store {p} ({_describe_store_path(p)})"
        )
    lines = [f"project {project.name!r} has no memory store holding records:"]
    for repo in project.repos:
        cand = Path(claude_projects) / slug_for(repo) / "memory"
        lines.append(f"  {repo}   -> {cand}        ({_describe_store_path(cand)})")
    return "\n".join(lines)


def config_for(
    project: Project,
    base: Config,
    claude_projects: Path | None = None,
) -> Config:
    """`claude_projects` defaults to `default_claude_projects()` at call
    time (Finding 5) -- see that function's docstring."""
    if claude_projects is None:
        claude_projects = default_claude_projects()
    stores = derive_stores(project, claude_projects)
    if not stores:
        raise RegistryError(_no_store_message(project, claude_projects))
    if len(stores) > 1:
        raise RegistryError(
            f"project {project.name!r} derives {len(stores)} memory stores: "
            + ", ".join(str(s) for s in stores)
            + ". Commands act on one store; set memory_store in the registry "
            "to name which."
        )
    # Replacement, not merging, when the project declares its own
    # accepted_absences: a project's absences are a property of the
    # PROJECT, not of wherever the command happened to be run from, so the
    # project's list -- if it has one -- is authoritative and entirely
    # replaces the base config's. Merging would make it impossible to ever
    # remove an absence the base config still lists (the union could only
    # grow), so the project's declared list wins outright rather than
    # being unioned with the base's.
    #
    # Falling back only on None (not on falsy) is deliberate: a project
    # that DECLARES an empty list is saying "I accept nothing", which must
    # stick, not silently become "inherit whatever the base config
    # accepts" just because () and None are both falsy in Python.
    absences = base.accepted_absences if project.accepted_absences is None else project.accepted_absences
    return replace(base, store_path=stores[0], accepted_absences=absences)
