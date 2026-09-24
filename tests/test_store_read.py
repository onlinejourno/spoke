import pytest
from spoke.store import Store


def _seed(d):
    (d / "MEMORY.md").write_text("- [Alpha](alpha.md) — the hook\n")
    (d / "alpha.md").write_text("---\nname: alpha\ndescription: d\n---\n\nbody\n")
    (d / "README.md").write_text("readme\n")


def test_lists_memory_files_only(tmp_path):
    _seed(tmp_path)
    names = [m.name for m in Store(tmp_path).list_records()]
    assert names == ["alpha.md"]


def test_attaches_index_line(tmp_path):
    _seed(tmp_path)
    assert Store(tmp_path).read("alpha.md").index_line == "- [Alpha](alpha.md) — the hook"


def test_read_missing_raises(tmp_path):
    _seed(tmp_path)
    with pytest.raises(FileNotFoundError):
        Store(tmp_path).read("nope.md")
