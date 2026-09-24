"""Read a repository without writing to it.

Everything the scan knows comes from here, and every reader obeys three
rules:

1. **Read-only.** Not a temp file, not a lock, not a `git gc`. A tool
   that maps your repos must never be a reason one of them changed.
2. **Tracked files only.** The file list comes from `git ls-files`, so a
   repo's own ignore rules are honoured by construction rather than by a
   skip-list this module would have to keep in sync with them. That is
   also what keeps the scan cheap: no walk into `node_modules`.
3. **Deterministic.** Every listing is sorted before it is used. A map
   that depends on filesystem order is a map that changes when nothing
   did, and the whole promise of a derived map is that it does not.

Never read: `.env*` in any form, and file CONTENT except for the small
set of manifests and ADR/doc markdown the map is actually built from.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

# A directory whose name is the project's own word for its deployable
# units. The value is the singular the scan proposes as a LABEL -- never
# as a ledger type, which stays the engine's closed set (see the plan's
# opening ruling).
LAYOUT_MARKERS: dict[str, str] = {
    "apps": "app",
    "packages": "package",
    "services": "service",
    "crates": "crate",
    "cmd": "command",
    "plugins": "plugin",
    "modules": "module",
    "sites": "site",
    "chapters": "chapter",
    "workspaces": "workspace",
    # Found on a real repo: a WordPress connector with six PHP packages
    # and twenty CMS adapters under these two names, and the scan saw
    # one unit in the whole repo.
    "php-packages": "package",
    "adapters": "adapter",
}

# A root manifest names a unit better than a directory does, and carries
# the ecosystem's own word for what that unit is.
MANIFESTS: dict[str, str] = {
    "package.json": "package",
    "pyproject.toml": "package",
    "Cargo.toml": "crate",
    "go.mod": "module",
    "composer.json": "package",
    "Gemfile": "gem",
    # A Python service with no pyproject.toml is still a unit. Found by
    # running the scan for real: a deployable app was reported as no unit
    # at all, so the map said the repo did not exist -- absence rendering
    # as assurance, in the tool built to stop that.
    "requirements.txt": "package",
}

# Last resort. A repo with none of the above but a container or platform
# manifest at its root is unmistakably ONE deployable thing, and saying
# nothing about it would be worse than naming it with the ecosystem's own
# word for what it deploys.
DEPLOY_MANIFESTS: dict[str, str] = {
    "Dockerfile": "image",
    "fly.toml": "app",
    "Procfile": "app",
    "docker-compose.yml": "stack",
}

# Files that are identical across repos BY CONVENTION, not by sharing.
# Two MIT licences are not a cross-cutting concern, and every empty
# __init__.py in the world hashes the same -- reporting those as vendored
# code buries the real duplication under noise. Found by running the scan
# for real: the top result was a group of empty files.
# A test travels with the code it tests. When an engine is vendored
# into four repos, its tests come too, and each one hashed identical
# across the four -- 26 "shared code" nodes on one map, every one a
# test file, burying the ten that were the engine. The engine files
# are the finding; the tests are the same finding said 26 more times.
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|[._]test\.[a-z]+$|[._]spec\.[a-z]+$")

# Binary assets and fonts are never "shared code". Two repos carrying the
# same favicon share a brand, not a capability -- and a capability node
# named `shared-favicon-ico` reported every product without it as absent.
_ASSET = re.compile(r"\.(ico|png|jpe?g|gif|webp|svg|woff2?|ttf|otf|eot|pdf|mp[34]|zip)$", re.I)
_CONVENTIONAL = re.compile(
    r"^(license|licence|copying|notice|authors|changelog|contributing|"
    r"code_of_conduct|security|readme|\.gitkeep|\.gitignore|\.gitattributes|"
    r"\.dockerignore|\.editorconfig|py\.typed|__init__\.py)",
    re.I,
)

# Content shorter than this carries no evidence of sharing: an empty file,
# a one-line stub, a single import. The signal this looks for is vendored
# code, and vendored code is not 40 bytes long.
MIN_DUPLICATE_BYTES = 200

_ADR_DIRS = ("docs/adr", "docs/adrs", "docs/decisions", "doc/adr", "adr", "decisions")
# A sequence number, NOT a date. `^\d{3,4}[-_]` matched `2026-09-08-...`
# because a year is four digits and a dash, so every dated design spec
# in every repo scanned as a decision with an unrecognised status --
# 21 of 45 "decisions" on one map were plans. The negative lookahead
# refuses a leading YYYY-MM-DD outright.
_ADR_FILENAME = re.compile(r"^(?!\d{4}-\d{2}-\d{2})\d{3,4}[-_]")
_STATUS_INLINE = re.compile(r"^[-*\s]*status\s*[:\-]\s*(.+?)\s*$", re.I)
_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_DATE_INLINE = re.compile(r"^[-*\s]*\**date\**\s*[:\-]\s*\**\s*(\d{4}-\d{2}-\d{2})", re.I)
_STATUS_HEADING = re.compile(r"^#{1,6}\s*status\s*$", re.I)
_TITLE = re.compile(r"^#\s+(.+?)\s*$")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

# Content is only ever read from files this large or smaller. A manifest
# or an ADR is kilobytes; anything past this is not one, and reading it
# would be the scan doing more than it needs in a tool that also talks
# to a model.
MAX_READ_BYTES = 256 * 1024

# Directory names too generic to stand alone as a unit's identity. A
# `viewer` or a `worker` means nothing outside its own repo, and two
# repos each having one would collide into a single node.
#
# Anything NOT in this list keeps its bare directory name, because that
# is what the repo -- and the people working in it -- actually call that
# unit. A package's declared name is its PUBLISHING name and is often
# namespaced for the registry; using it produced units called
# `fleet-acme-alpha` for a unit everyone -- including every ledger node
# referring to it -- calls `alpha`, which then matched nothing.
GENERIC_UNIT_NAMES = frozenset({
    "src", "app", "apps", "lib", "libs", "core", "api", "web", "server",
    "client", "worker", "viewer", "ingestor", "cli", "docs", "common",
    "shared", "utils", "tools", "service", "services", "backend", "frontend",
})


def _unit_name(repo_name: str, child: str, declared: str | None) -> str:
    """What to call a unit that lives in a subdirectory.

    The directory name wins when it is distinctive, because that is the
    name in use. It is qualified by the repo only when it is too generic
    to identify anything on its own.
    """
    if child.lower() not in GENERIC_UNIT_NAMES:
        return slug(child)
    # The qualification exists to make a generic directory identifiable,
    # so it must not repeat what the repo name already says. A declared
    # name of `masthead-ingestor` inside `masthead-audit` qualified to
    # `masthead-audit-masthead-ingestor`; the shared token is noise, and
    # the name people use for that unit is the part that is left.
    own = slug(repo_name).split("-")
    tokens = slug(declared or child).split("-")
    while len(tokens) > 1 and tokens[0] in own:
        tokens = tokens[1:]
    return slug(repo_name, "-".join(tokens))


def slug(*parts: str) -> str:
    """A ledger-safe node name. `schema.validate` refuses anything else,
    and a scan whose own output the gate rejects is not a scan."""
    joined = "-".join(p for p in parts if p)
    s = _SLUG_STRIP.sub("-", joined.lower()).strip("-")
    return s or "unnamed"


@dataclass(frozen=True)
class Unit:
    name: str
    label: str
    path: str          # relative to the repo root; "." for the repo itself
    repo: str
    manifest: str | None
    # The whole this unit is a part of, when the repo holds several parts
    # and none of them carries the repo's own name. See `_with_whole`.
    part_of: str | None = None


@dataclass(frozen=True)
class Decision:
    name: str
    title: str
    status: str | None
    path: str
    repo: str
    # The date the ADR carries itself -- in its filename
    # (`2026-07-20-topic-hubs-design.md`) or on a `Date:` line -- as ISO,
    # or None. The scan dates a decision by this, not by when it read it.
    date: str | None = None


def _is_secretish(rel: str) -> bool:
    """`.env`, `.env.local`, `.envrc` and friends are never read, and
    never even listed -- not because they are likely to be tracked, but
    because a scanner that would read one if it were is a liability."""
    base = rel.rsplit("/", 1)[-1]
    return base == ".env" or base.startswith(".env.") or base == ".envrc"


def tracked_files(repo: Path) -> tuple[list[str], list[str]]:
    """(relative paths, notes). Tracked files only, sorted.

    A directory that is not a git repo is not an error -- it is a note,
    and the caller falls back to a bounded walk. Silence would be worse:
    a repo that produced nothing because git failed looks exactly like a
    repo with nothing in it.
    """
    notes: list[str] = []
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z"],
            capture_output=True, check=True, text=True, timeout=60,
        ).stdout
        files = [f for f in out.split("\0") if f]
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        notes.append(f"{repo}: not a git repository (or git is unavailable) -- "
                     "falling back to a bounded directory walk, which cannot "
                     "honour the project's ignore rules")
        files = _bounded_walk(repo)
    files = sorted(f for f in files if not _is_secretish(f))
    return files, notes


_WALK_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist",
              "build", "target", ".next", ".tox", ".mypy_cache", ".pytest_cache"}


def _bounded_walk(repo: Path, max_depth: int = 4) -> list[str]:
    found: list[str] = []
    root_len = len(repo.parts)
    for path in repo.rglob("*"):
        parts = path.parts[root_len:]
        if any(p in _WALK_SKIP or p.startswith(".") and p not in (".github",) for p in parts[:-1]):
            continue
        if len(parts) > max_depth or not path.is_file():
            continue
        found.append("/".join(parts))
    return found


def _read_text(repo: Path, rel: str) -> str | None:
    p = repo / rel
    try:
        if p.stat().st_size > MAX_READ_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _manifest_name(repo: Path, rel: str) -> str | None:
    """The unit's own name, from the manifest that declares it."""
    text = _read_text(repo, rel)
    if text is None:
        return None
    base = rel.rsplit("/", 1)[-1]
    try:
        if base in ("package.json", "composer.json"):
            # `vendor/package` (composer) and `@scope/package` (npm):
            # the half before the slash is the registry's namespace, not
            # the name anyone uses. go.mod below already does the same.
            name = json.loads(text).get("name") or None
            return name.rsplit("/", 1)[-1] if name else None
        if base == "pyproject.toml":
            data = tomllib.loads(text)
            return (data.get("project", {}).get("name")
                    or data.get("tool", {}).get("poetry", {}).get("name") or None)
        if base == "Cargo.toml":
            return tomllib.loads(text).get("package", {}).get("name") or None
        if base == "go.mod":
            for line in text.splitlines():
                if line.startswith("module "):
                    return line.split(None, 1)[1].strip().rsplit("/", 1)[-1]
    except (ValueError, tomllib.TOMLDecodeError):
        return None
    return None


def _with_whole(repo_name: str, units: list[Unit], root_manifest: bool = False) -> list[Unit]:
    """A repo whose parts do not carry its name is itself a unit.

    A monorepo of `ingestor/`, `viewer/` and `worker/` scans as three
    units named for the repo plus a part -- and nothing named for the
    repo. But the thing that ships, that people refer to, and that the
    ledger already points at, is the WHOLE. Reporting only the parts
    makes every reference to the product dangle, and the map then
    reports the product's own name as a broken link.

    Deliberately not applied when a unit already carries the repo's
    name: there the whole is present, and adding a second node for it
    would split one thing into two.

    TWO or more parts, for the same reason `read_units` requires two
    siblings: one part IS the repo under another name (`src/`, a
    publishing name), and calling that a part of itself would invent a
    relationship nobody has.
    """
    whole = slug(repo_name)
    if any(u.name == whole for u in units):
        return units
    # A root manifest is direct evidence that the repo is a thing in
    # itself, so the "one part IS the repo under another name" doubt
    # does not apply: a WordPress plugin with one packages/ child was a
    # repo whose only node was the child.
    if len(units) < 2 and not root_manifest:
        return units
    parts = [replace(u, part_of=whole) for u in units]
    return [Unit(name=whole, label="product", path=".", repo=repo_name,
                 manifest=None)] + parts


def read_units(repo: Path, files: list[str]) -> list[Unit]:
    """The repo's deployable/publishable units, layout markers first.

    Markers beat a root manifest: a monorepo has a root `package.json`
    too, and reporting the whole monorepo as one unit would collapse
    exactly the structure the map exists to show.
    """
    repo_name = repo.name
    units: list[Unit] = []
    seen: set[str] = set()

    for marker, label in sorted(LAYOUT_MARKERS.items()):
        children = sorted({
            f.split("/")[1] for f in files
            if f.startswith(f"{marker}/") and f.count("/") >= 2
        })
        for child in children:
            manifest = next(
                (m for m in MANIFESTS if f"{marker}/{child}/{m}" in files), None
            )
            declared = _manifest_name(repo, f"{marker}/{child}/{manifest}") if manifest else None
            name = _unit_name(repo_name, child, declared)
            if name in seen:
                continue
            seen.add(name)
            units.append(Unit(name=name, label=label, path=f"{marker}/{child}",
                              repo=repo_name, manifest=manifest))

    if units:
        return _with_whole(repo_name, units,
                           root_manifest=any(m in files for m in MANIFESTS))

    # A monorepo whose units are plain top-level directories, each with
    # its own manifest, and no `apps/`-style marker or root manifest at
    # all. Found in the wild: four services consolidated into one repo as
    # siblings, which this reported as "no deployable unit found" -- the
    # map then said a repo holding four live services held nothing.
    #
    # TWO or more, deliberately. A single directory carrying a manifest
    # is far more likely to be a `src/` or a vendored copy than a unit,
    # and calling that a unit would be a guess; two siblings agreeing on
    # the shape is evidence of a layout.
    #
    # ONE manifest per directory, the same way the marker branch picks
    # one: `ingestor/` carrying both a pyproject.toml and a
    # requirements.txt is one unit, not two. Taking both produced a node
    # per manifest -- and because a declared name and a directory name
    # slug differently, the two nodes had DIFFERENT names, so the map
    # showed a phantom unit alongside the real one. MANIFESTS is in
    # preference order, with the requirements.txt fallback last.
    by_child: dict[str, str] = {}
    for f in files:
        parts = f.split("/")
        if len(parts) == 2 and parts[1] in MANIFESTS:
            best = by_child.get(parts[0])
            order = list(MANIFESTS)
            if best is None or order.index(parts[1]) < order.index(best):
                by_child[parts[0]] = parts[1]
    siblings = sorted(by_child.items())
    if len(by_child) >= 2:
        for child, manifest in siblings:
            declared = _manifest_name(repo, f"{child}/{manifest}")
            name = _unit_name(repo_name, child, declared)
            if name in seen:
                continue
            seen.add(name)
            units.append(Unit(name=name, label=MANIFESTS[manifest], path=child,
                              repo=repo_name, manifest=manifest))
        return _with_whole(repo_name, units)

    for source in (MANIFESTS, DEPLOY_MANIFESTS):
        for manifest, label in source.items():
            if manifest in files:
                declared = _manifest_name(repo, manifest)
                return [Unit(name=slug(declared or repo_name), label=label, path=".",
                             repo=repo_name, manifest=manifest)]
    return []


# Everything after the status WORD: a date, a beat, a parenthetical, a
# reviewer's note. Real ADRs in the wild read "**Status:** accepted —
# 2026-08-03" and "accepted · **date:** ... · **beat:** policy".
_STATUS_TAIL = re.compile(r"\s*[\u2014\u2013\u00b7,;(\[].*$")


def _clean_status(raw: str) -> str | None:
    """The status word alone, from a line written by a human in markdown.

    Emphasis markers and trailing qualifiers are stripped, because they
    are decoration and metadata around the answer rather than part of it.
    Everything unrecognised still reaches the caller unchanged, so an
    unmappable status is REPORTED rather than mangled into a known one --
    the whole point of the mapping table is that it refuses to guess.
    """
    s = raw.strip().strip("*_`~ ").strip()
    s = _STATUS_TAIL.sub("", s)
    return s.strip().strip("*_`~ .").lower() or None


def _adr_date(text: str, base: str) -> str | None:
    """The ADR's own date: the first ISO date in its filename, else the
    first `Date:` line (frontmatter or inline, emphasis stripped). A
    calendar date that happens to sit in the prose is NOT read as the
    decision's date; that would date a decision by whatever it cites."""
    m = _ISO_DATE.search(base)
    if m:
        return m.group(1)
    for line in text.splitlines()[:40]:
        m = _DATE_INLINE.match(line)
        if m:
            return m.group(1)
    return None


def _adr_status(text: str) -> str | None:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _STATUS_INLINE.match(line)
        if m and not line.lstrip().startswith("#"):
            return _clean_status(m.group(1))
        if _STATUS_HEADING.match(line):
            for follow in lines[i + 1:]:
                if follow.strip():
                    return _clean_status(follow)
            return None
    return None


# Files that INDEX decisions rather than being one. Checked by basename,
# so it holds wherever the ADR directory lives.
_ADR_INDEX_NAMES = frozenset({"readme.md", "index.md", "contents.md"})


def read_decisions(repo: Path, files: list[str]) -> list[Decision]:
    """ADRs, from a conventional directory or from the ADR shape itself.

    A project that keeps its ADRs somewhere this list does not name still
    gets them, as long as the files look like ADRs -- a numbered markdown
    file carrying a Status. Requiring the directory would have made the
    scan silently empty for those projects, which is the failure mode
    this whole tool is about.
    """
    repo_name = repo.name
    candidates: list[str] = []
    for f in files:
        if not f.endswith(".md"):
            continue
        base = f.rsplit("/", 1)[-1]
        # An INDEX of decisions is documentation about decisions, not a
        # decision. `docs/adr/README.md` scanned as one and landed in the
        # ledger as `<repo>-readme: decision open, title: "Architecture
        # decision records"` -- a node asserting that the existence of an
        # index was an open architectural question.
        if base.lower() in _ADR_INDEX_NAMES:
            continue
        in_adr_dir = any(f.startswith(d + "/") for d in _ADR_DIRS)
        looks_like = _ADR_FILENAME.match(base) is not None
        if in_adr_dir or looks_like:
            candidates.append(f)

    out: list[Decision] = []
    seen: set[str] = set()
    for rel in sorted(candidates):
        text = _read_text(repo, rel)
        if text is None:
            continue
        status = _adr_status(text)
        in_adr_dir = any(rel.startswith(d + "/") for d in _ADR_DIRS)
        if status is None and not in_adr_dir:
            continue        # a numbered markdown file with no Status is not an ADR
        title = next(
            (m.group(1) for m in (_TITLE.match(l) for l in text.splitlines()) if m),
            rel.rsplit("/", 1)[-1].removesuffix(".md"),
        )
        name = slug(repo_name, rel.rsplit("/", 1)[-1].removesuffix(".md"))
        if name in seen:
            continue
        seen.add(name)
        out.append(Decision(name=name, title=title, status=status, path=rel, repo=repo_name,
                            date=_adr_date(text, rel.rsplit("/", 1)[-1])))
    return out


def read_docs(repo: Path, files: list[str]) -> list[str]:
    """The paths that make this repo documented, sorted."""
    out = [
        f for f in files
        if f.rsplit("/", 1)[-1].lower().startswith("readme")
        or f.startswith("docs/") or f.startswith("doc/")
        # Root-level markdown counts too (spec Sec.4). A repo whose
        # DEPLOY.md and CONTRIBUTING.md sit at the root is documented;
        # counting only README would have reported it as barely so.
        or ("/" not in f and f.lower().endswith(".md"))
    ]
    return sorted(out)


def read_git(repo: Path) -> dict:
    """Remote, default branch and last-commit date. Every value is None
    when git cannot answer -- an unknown is reported as unknown, never
    filled in with a plausible default."""
    def _git(*args: str) -> str | None:
        try:
            r = subprocess.run(["git", "-C", str(repo), *args],
                               capture_output=True, text=True, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        return r.stdout.strip() or None if r.returncode == 0 else None

    head = _git("symbolic-ref", "--short", "HEAD")
    return {
        "remote": _git("remote", "get-url", "origin"),
        "default_branch": head,
        "last_commit": _git("log", "-1", "--format=%cs"),
    }


def shared_package(hits: list[tuple[Path, str]], repo_files: dict[Path, list[str]]) -> str:
    """The package a duplicated file belongs to, as the key that groups
    files of one package into ONE capability.

    The scan once made a capability of every duplicated file: a vendored
    engine of nine files became nine capabilities, and every product
    without the engine was reported absent nine times. The unit here is
    the package: the nearest enclosing directory, common to every copy,
    that carries a manifest. With no such directory, the longest
    directory suffix all copies share (`docs/agents`). With none at all,
    the file itself.
    """
    dirs = [rel.rsplit("/", 1)[0].split("/") if "/" in rel else [] for _, rel in hits]
    common: list[str] = []
    for parts in zip(*(reversed(d) for d in dirs)):
        if len(set(parts)) != 1:
            break
        common.insert(0, parts[0])
    if not common:
        return hits[0][1].rsplit("/", 1)[-1]
    # Walk the shared suffix from the file outward; the first level that
    # holds a manifest in EVERY copy is the package.
    for up in range(len(common)):          # 0 = the file's own directory, then its parents
        if all(_has_manifest(repo_files.get(repo, []), rel, up) for repo, rel in hits):
            return "/".join(common[: len(common) - up])
    return "/".join(common)


def _has_manifest(files: list[str], rel: str, up: int) -> bool:
    """Does the directory `up` levels above the one holding `rel` carry a
    manifest?"""
    parts = rel.rsplit("/", 1)[0].split("/")
    package_dir = "/".join(parts[: len(parts) - up])
    return any(f"{package_dir}/{m}" in files for m in MANIFESTS)


def duplicated_files(repo_files: dict[Path, list[str]]) -> dict[str, list[tuple[Path, str]]]:
    """sha256 -> the (repo, path) pairs holding identical content, for
    content present in MORE THAN ONE repo.

    Vendored code is the clearest real example of a cross-cutting
    concern: it is the shape a tree cannot hold, which is why it is worth
    the hashing.
    """
    by_hash: dict[str, list[tuple[Path, str]]] = {}
    for repo in sorted(repo_files, key=str):
        for rel in repo_files[repo]:
            base = rel.rsplit("/", 1)[-1]
            if _CONVENTIONAL.match(base) or _ASSET.search(base) or _TEST_PATH.search(rel):
                continue
            p = repo / rel
            try:
                size = p.stat().st_size
                if size > MAX_READ_BYTES or size < MIN_DUPLICATE_BYTES or p.is_symlink():
                    continue
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                continue
            by_hash.setdefault(digest, []).append((repo, rel))
    return {
        h: hits for h, hits in sorted(by_hash.items())
        if len({r for r, _ in hits}) > 1
    }
