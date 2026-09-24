from __future__ import annotations
import io
from dataclasses import dataclass, field
from ruamel.yaml import YAML, YAMLError

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)

DELIM = "---\n"


@dataclass
class Record:
    meta: dict = field(default_factory=dict)
    body: str = ""
    _raw_meta: str = ""
    # Public: True when the frontmatter block exists but is not strictly valid YAML
    # (e.g. an unquoted ": " inside a plain scalar, which real records in
    # the store contain). In that case `meta` is empty and the block is
    # carried verbatim so round-trip stays byte-identical — the tool bends
    # to the store, not the other way round. Downstream checks (e.g. schema
    # validation) depend on this flag to distinguish "no frontmatter" from
    # "unparseable frontmatter"; do not remove without updating consumers.
    unparsed: bool = False

    @property
    def name(self) -> str:
        return str(self.meta.get("name", ""))


def parse(text: str) -> Record:
    if not text.startswith(DELIM):
        return Record(meta={}, body=text)
    rest = text[len(DELIM):]
    end = rest.find("\n" + DELIM)
    if end == -1:
        return Record(meta={}, body=text)
    raw_meta = rest[: end + 1]
    body = rest[end + 1 + len(DELIM):]
    try:
        meta = _yaml.load(io.StringIO(raw_meta)) or {}
    except YAMLError:
        return Record(meta={}, body=body, _raw_meta=raw_meta, unparsed=True)
    return Record(meta=meta, body=body, _raw_meta=raw_meta)


def serialise(record: Record) -> str:
    if record.unparsed:
        return DELIM + record._raw_meta + DELIM + record.body
    if not record.meta:
        return record.body
    buf = io.StringIO()
    _yaml.dump(record.meta, buf)
    dumped = buf.getvalue()
    # Prefer the original text when the mapping is unchanged, so formatting
    # and quoting survive exactly.
    meta_text = record._raw_meta if record._raw_meta and _yaml.load(io.StringIO(record._raw_meta)) == record.meta else dumped
    return DELIM + meta_text + DELIM + record.body
