from spoke.store import Store
from spoke.checks.relate import cluster


def test_empty_store_returns_no_clusters(tmp_path):
    (tmp_path / "MEMORY.md").write_text("")
    assert cluster(Store(tmp_path)) == []


def test_single_record_store_returns_no_clusters(tmp_path):
    (tmp_path / "MEMORY.md").write_text("")
    (tmp_path / "a.md").write_text('---\nname: a\ndescription: d\n---\n\nbody\n')
    assert cluster(Store(tmp_path)) == []


def _rec(d, name, meta_type, body):
    (d / name).write_text(f"---\nname: {name[:-3]}\ndescription: d\nmetadata:\n  type: {meta_type}\n---\n\n{body}\n")


def test_records_sharing_a_wikilink_cluster_together(tmp_path):
    (tmp_path / "MEMORY.md").write_text("")
    _rec(tmp_path, "a.md", "project", "see [[shared]]")
    _rec(tmp_path, "b.md", "project", "also [[shared]]")
    _rec(tmp_path, "c.md", "reference", "unrelated entirely")
    _rec(tmp_path, "shared.md", "project", "x")
    groups = cluster(Store(tmp_path))
    together = [g for g in groups if "a.md" in g][0]
    assert "b.md" in together
    assert "c.md" not in together


def test_records_sharing_a_type_and_an_entity_cluster(tmp_path):
    (tmp_path / "MEMORY.md").write_text("")
    _rec(tmp_path, "a.md", "user", "works on the Northwind account currently")
    _rec(tmp_path, "b.md", "user", "left the Northwind account in June")
    # Add a third unrelated record so "Northwind" is not ubiquitous (df=2/3 < rarity_threshold).
    _rec(tmp_path, "c.md", "project", "Something else about Forage platform")
    groups = cluster(Store(tmp_path))
    assert any({"a.md", "b.md"} <= set(g) for g in groups)


def test_clusters_are_capped(tmp_path):
    (tmp_path / "MEMORY.md").write_text("")
    for i in range(30):
        _rec(tmp_path, f"r{i}.md", "project", "all mention Atlas")
    assert all(len(g) <= 12 for g in cluster(Store(tmp_path), max_size=12))


def test_a_name_beginning_with_a_stopword_is_still_extracted(tmp_path):
    """The entity is "The Beacon": the leading "The" is a stopword, and
    dropping it along with the stopword is the defect this guards."""
    (tmp_path / "MEMORY.md").write_text("")
    _rec(tmp_path, "a.md", "user", "Currently in an engagement with The Beacon.")
    _rec(tmp_path, "b.md", "user", "Done with The Beacon, now independent.")
    _rec(tmp_path, "c.md", "project", "Something else entirely, about Atlas deployments.")
    groups = cluster(Store(tmp_path))
    assert any({"a.md", "b.md"} <= set(g) for g in groups), groups
    assert not any({"a.md", "c.md"} <= set(g) for g in groups), groups


def test_a_ubiquitous_signal_does_not_connect_everything(tmp_path):
    (tmp_path / "MEMORY.md").write_text("")
    for i in range(30):
        _rec(tmp_path, f"r{i}.md", "project", "every record mentions Atlas here")
    _rec(tmp_path, "x.md", "project", "every record mentions Atlas here, and also Zebracorn Protocol")
    _rec(tmp_path, "y.md", "project", "every record mentions Atlas here, and also Zebracorn Protocol")
    groups = cluster(Store(tmp_path))
    # Atlas is in every record, so it must not group anything. Zebracorn is rare, so it must.
    assert any({"x.md", "y.md"} <= set(g) for g in groups), groups
    biggest = max((len(g) for g in groups), default=0)
    assert biggest <= 12


def test_a_rare_shared_signal_outranks_a_common_one(tmp_path):
    (tmp_path / "MEMORY.md").write_text("")
    # 20 records all share a common entity; two of them ALSO share a rare one.
    for i in range(20):
        (tmp_path / f"r{i}.md").write_text(
            f'---\nname: r{i}\ndescription: d\nmetadata:\n  type: project\n---\n\nCommonplace mentions Atlas here.\n')
    (tmp_path / "p.md").write_text(
        '---\nname: p\ndescription: d\nmetadata:\n  type: project\n---\n\nCommonplace mentions Atlas here, plus Zebracorn Protocol.\n')
    (tmp_path / "q.md").write_text(
        '---\nname: q\ndescription: d\nmetadata:\n  type: project\n---\n\nCommonplace mentions Atlas here, plus Zebracorn Protocol.\n')
    groups = cluster(Store(tmp_path))
    assert any({"p.md", "q.md"} <= set(g) for g in groups), groups


def test_type_is_read_from_top_level_when_not_nested(tmp_path):
    (tmp_path / "MEMORY.md").write_text("")
    # Older format: type at the YAML top level rather than under metadata.
    (tmp_path / "a.md").write_text(
        '---\nname: a\ndescription: d\ntype: user\n---\n\nEngagement with Northwind is current.\n')
    (tmp_path / "b.md").write_text(
        '---\nname: b\ndescription: d\nmetadata:\n  type: user\n---\n\nDone with Northwind, now independent.\n')
    (tmp_path / "c.md").write_text(
        '---\nname: c\ndescription: d\nmetadata:\n  type: project\n---\n\nAtlas deployment notes and nothing else.\n')
    groups = cluster(Store(tmp_path))
    assert any({"a.md", "b.md"} <= set(g) for g in groups), groups
    assert not any({"a.md", "c.md"} <= set(g) for g in groups), groups


def test_two_record_store_does_not_cluster_on_a_ubiquitous_signal(tmp_path):
    # Both records share only `type:user`, which is present in 100% of the store,
    # so it is no evidence at all and must not group them.
    (tmp_path / "MEMORY.md").write_text("")
    (tmp_path / "a.md").write_text(
        '---\nname: a\ndescription: d\nmetadata:\n  type: user\n---\n\nAlpha writes about widgets.\n')
    (tmp_path / "b.md").write_text(
        '---\nname: b\ndescription: d\nmetadata:\n  type: user\n---\n\nBeta writes about sprockets.\n')
    assert cluster(Store(tmp_path)) == []
