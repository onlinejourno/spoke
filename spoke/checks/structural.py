from __future__ import annotations
import re
from pathlib import Path
from . import NOT_A_MEMORY, Finding, LINK, strip_code
from ..frontmatter import parse

INDEX_LINK = re.compile(r"\]\(([^)]+\.md)\)")


def _ledger_files(store: Path) -> list[Path]:
    ledger_dir = store / "ledger"
    if not ledger_dir.is_dir():
        return []
    return sorted(ledger_dir.glob("*.md"))


def check_wikilinks(store: Path, accepted: tuple[str, ...]) -> list[Finding]:
    # Memories and ledger nodes live in one store and form one graph: a
    # wikilink must resolve whether it points memory -> ledger or
    # ledger -> memory, and both directions must be scanned for links in
    # the first place. `names` is therefore the union of both namespaces,
    # and the scan loop below walks both `store/*.md` and
    # `store/ledger/*.md` -- scanning only the top level would silently
    # never report a broken link written *inside* a ledger node.
    ledger_files = _ledger_files(store)
    names = {p.stem for p in store.glob("*.md")} | {p.stem for p in ledger_files}
    findings: list[Finding] = []

    def _scan(f: Path, file_label: str) -> None:
        for target in LINK.findall(strip_code(f.read_text())):
            if "/" in target:
                continue  # a path, not a memory reference
            if target.endswith(".md"):
                findings.append(Finding("malformed-wikilink", file_label, target))
                continue
            if target in accepted or target in names:
                continue
            findings.append(Finding("broken-wikilink", file_label, target))

    for f in sorted(store.glob("*.md")):
        if f.name in NOT_A_MEMORY:
            continue
        _scan(f, f.name)

    for f in ledger_files:
        _scan(f, f"ledger/{f.name}")

    return findings


def _memory_files(store: Path) -> list[Path]:
    return sorted(p for p in store.glob("*.md") if p.name not in NOT_A_MEMORY)


def check_index(store: Path) -> list[Finding]:
    index = store / "MEMORY.md"
    if not index.exists():
        return [Finding("index-missing", "MEMORY.md", "no MEMORY.md in store")]
    text = index.read_text()
    targets = set(INDEX_LINK.findall(text))
    findings = [Finding("index-target-missing", "MEMORY.md", t)
                for t in sorted(targets) if not (store / t).exists()]
    findings += [Finding("unindexed", p.name, p.name)
                 for p in _memory_files(store) if p.name not in targets]
    return findings


def check_schema(store: Path) -> list[Finding]:
    findings: list[Finding] = []
    for p in _memory_files(store):
        if "_" in p.stem:
            findings.append(Finding("schema-snake-case-name", p.name, p.stem))
        record = parse(p.read_text())
        meta = record.meta
        if not meta:
            if record.unparsed:
                # Frontmatter block is present but not valid YAML -- distinct
                # from no frontmatter at all. Mislabelling this as "missing"
                # sends someone looking for text that is right there.
                findings.append(Finding("schema-unparseable-frontmatter", p.name,
                                         "frontmatter block is not valid YAML"))
            else:
                findings.append(Finding("schema-missing-frontmatter", p.name, ""))
            continue
        if "type" in meta:
            findings.append(Finding("schema-type-at-top-level", p.name, "move under metadata:"))
        for required in ("name", "description"):
            if required not in meta or not str(meta[required]).strip():
                findings.append(Finding("schema-missing-field", p.name, required))
    return findings


def check_ledger_schema(store: Path, axes: tuple = ()) -> list[Finding]:
    """Every ledger node that would be REFUSED if it were written today.

    Ledger nodes were validated on write and never again. That is exactly
    backwards for this store: it is a directory of markdown files whose
    whole point is that a human can edit them by hand, and a hand edit
    goes nowhere near `LedgerStore.write`. So a node could sit on disk
    permanently carrying `state: deferred` with no ruling, an unknown
    `type`, or a client node with a real name in its title -- all of them
    conditions the gate exists to refuse -- and every surface would
    render it as an ordinary node.

    The asymmetry was easy to miss and completely arbitrary:
    `check_wikilinks` deliberately scans BOTH trees, so a broken wikilink
    inside a ledger node was reported, while the node being malformed was
    not.

    Two kinds, because they need different work: `ledger-unreadable`
    cannot be parsed at all, `ledger-invalid` parses and fails the gate.

    `axes` is the project's declared capability axes. Empty means "the
    caller has no axis list", and scores are then checked for shape but
    not membership -- the same convention `validate()` itself uses.
    """
    # Imported here, not at module scope: spoke.store imports this module,
    # and ledger.store imports spoke.store for its git helpers, so a
    # module-level import of either would close a cycle. schema.py
    # imports neither.
    from ..ledger import LedgerError
    from ..ledger.schema import parse_node, validate

    out: list[Finding] = []
    for path in _ledger_files(store):
        rel = f"ledger/{path.name}"
        try:
            node = parse_node(path.read_text())
        except LedgerError as e:
            out.append(Finding("ledger-unreadable", rel, str(e)))
            continue
        except OSError as e:
            out.append(Finding("ledger-unreadable", rel, f"could not be read: {e}"))
            continue
        for reason in validate(node, tuple(axes)):
            out.append(Finding("ledger-invalid", rel, reason))
    return out


def check_all(store: Path, accepted: tuple[str, ...], axes: tuple = ()) -> list[Finding]:
    return (
        check_wikilinks(store, accepted)
        + check_index(store)
        + check_schema(store)
        + check_ledger_schema(store, axes)
    )
