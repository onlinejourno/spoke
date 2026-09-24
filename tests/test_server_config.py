import subprocess
from pathlib import Path
from fastapi.testclient import TestClient
from spoke.config import Config, load_config
from spoke.server import create_app


def _store(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "MEMORY.md").write_text("- [Alpha](alpha.md) — hook\n")
    (tmp_path / "alpha.md").write_text("---\nname: alpha\ndescription: d\n---\n\noriginal\n")
    return tmp_path


def _cfg(store_dir):
    return Config(store_dir, ("one",), 30, "groq", None)


def test_get_config_returns_current_values_and_path(tmp_path):
    store_dir = _store(tmp_path / "store")
    cfg_path = tmp_path / "config.toml"
    cfg = _cfg(store_dir)
    c = TestClient(create_app(cfg, config_path=cfg_path))
    r = c.get("/api/config")
    assert r.status_code == 200
    data = r.json()
    assert data["store_path"] == str(store_dir)
    assert data["accepted_absences"] == ["one"]
    assert data["stale_days"] == 30
    assert data["llm_provider"] == "groq"
    assert data["config_path"] == str(cfg_path)


def test_post_config_writes_and_new_values_load_back(tmp_path):
    store_dir = _store(tmp_path / "store")
    other_store = _store(tmp_path / "other-store")
    cfg_path = tmp_path / "config.toml"
    c = TestClient(create_app(_cfg(store_dir), config_path=cfg_path))
    r = c.post("/api/config", json={
        "store_path": str(other_store),
        "accepted_absences": ["two", "three"],
        "stale_days": 15,
        "llm_provider": "groq",
        "llm_base_url": "https://example.test/v1",
    })
    assert r.status_code == 200, r.text
    # loads back from disk
    on_disk = load_config(cfg_path)
    assert on_disk.store_path == other_store
    assert on_disk.accepted_absences == ("two", "three")
    assert on_disk.stale_days == 15
    assert on_disk.llm_base_url == "https://example.test/v1"
    # loads back through the API too
    got = c.get("/api/config").json()
    assert got["store_path"] == str(other_store)
    assert got["stale_days"] == 15


def test_post_config_refuses_nonexistent_store_path(tmp_path):
    store_dir = _store(tmp_path / "store")
    cfg_path = tmp_path / "config.toml"
    c = TestClient(create_app(_cfg(store_dir), config_path=cfg_path))
    r = c.post("/api/config", json={
        "store_path": str(tmp_path / "does-not-exist"),
        "accepted_absences": [],
        "stale_days": 30,
        "llm_provider": "groq",
    })
    assert r.status_code == 422
    assert "store_path" in r.text
    assert not cfg_path.exists()


def test_post_config_refuses_store_path_that_is_a_file(tmp_path):
    store_dir = _store(tmp_path / "store")
    cfg_path = tmp_path / "config.toml"
    not_a_dir = tmp_path / "just-a-file.txt"
    not_a_dir.write_text("hi")
    c = TestClient(create_app(_cfg(store_dir), config_path=cfg_path))
    r = c.post("/api/config", json={
        "store_path": str(not_a_dir),
        "accepted_absences": [],
        "stale_days": 30,
        "llm_provider": "groq",
    })
    assert r.status_code == 422
    assert "store_path" in r.text


def test_post_config_refuses_store_path_with_no_md_files(tmp_path):
    store_dir = _store(tmp_path / "store")
    cfg_path = tmp_path / "config.toml"
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    c = TestClient(create_app(_cfg(store_dir), config_path=cfg_path))
    r = c.post("/api/config", json={
        "store_path": str(empty_dir),
        "accepted_absences": [],
        "stale_days": 30,
        "llm_provider": "groq",
    })
    assert r.status_code == 422
    assert "store_path" in r.text


def test_post_config_refuses_non_positive_stale_days(tmp_path):
    store_dir = _store(tmp_path / "store")
    cfg_path = tmp_path / "config.toml"
    c = TestClient(create_app(_cfg(store_dir), config_path=cfg_path))
    r = c.post("/api/config", json={
        "store_path": str(store_dir),
        "accepted_absences": [],
        "stale_days": 0,
        "llm_provider": "groq",
    })
    assert r.status_code == 422
    assert "stale_days" in r.text


def test_post_config_refuses_negative_stale_days(tmp_path):
    store_dir = _store(tmp_path / "store")
    cfg_path = tmp_path / "config.toml"
    c = TestClient(create_app(_cfg(store_dir), config_path=cfg_path))
    r = c.post("/api/config", json={
        "store_path": str(store_dir),
        "accepted_absences": [],
        "stale_days": -5,
        "llm_provider": "groq",
    })
    assert r.status_code == 422
    assert "stale_days" in r.text


def test_post_config_refuses_empty_string_in_accepted_absences(tmp_path):
    store_dir = _store(tmp_path / "store")
    cfg_path = tmp_path / "config.toml"
    c = TestClient(create_app(_cfg(store_dir), config_path=cfg_path))
    r = c.post("/api/config", json={
        "store_path": str(store_dir),
        "accepted_absences": ["ok", "   "],
        "stale_days": 30,
        "llm_provider": "groq",
    })
    assert r.status_code == 422
    assert "accepted_absences" in r.text


def test_post_config_refuses_non_http_base_url(tmp_path):
    store_dir = _store(tmp_path / "store")
    cfg_path = tmp_path / "config.toml"
    c = TestClient(create_app(_cfg(store_dir), config_path=cfg_path))
    r = c.post("/api/config", json={
        "store_path": str(store_dir),
        "accepted_absences": [],
        "stale_days": 30,
        "llm_provider": "groq",
        "llm_base_url": "ftp://example.test",
    })
    assert r.status_code == 422
    assert "llm_base_url" in r.text


def test_post_config_refused_when_no_config_path_known(tmp_path):
    store_dir = _store(tmp_path / "store")
    c = TestClient(create_app(_cfg(store_dir)))  # no config_path
    r = c.post("/api/config", json={
        "store_path": str(store_dir),
        "accepted_absences": [],
        "stale_days": 30,
        "llm_provider": "groq",
    })
    assert r.status_code == 422
    assert "config" in r.text.lower()


def test_get_config_response_has_no_key_like_field(tmp_path):
    store_dir = _store(tmp_path / "store")
    cfg_path = tmp_path / "config.toml"
    c = TestClient(create_app(_cfg(store_dir), config_path=cfg_path))
    data = c.get("/api/config").json()
    for k in data:
        assert "key" not in k.lower(), f"GET /api/config leaked a key-like field: {k}"


def test_post_config_body_carrying_llm_api_key_is_ignored_not_written(tmp_path):
    store_dir = _store(tmp_path / "store")
    cfg_path = tmp_path / "config.toml"
    c = TestClient(create_app(_cfg(store_dir), config_path=cfg_path))
    r = c.post("/api/config", json={
        "store_path": str(store_dir),
        "accepted_absences": [],
        "stale_days": 30,
        "llm_provider": "groq",
        "llm_api_key": "sk-should-never-be-written",
    })
    assert r.status_code == 200, r.text
    text = cfg_path.read_text()
    assert "sk-should-never-be-written" not in text
    assert "api_key" not in text.lower()


def test_settings_page_served(tmp_path):
    store_dir = _store(tmp_path / "store")
    cfg_path = tmp_path / "config.toml"
    c = TestClient(create_app(_cfg(store_dir), config_path=cfg_path))
    r = c.get("/settings")
    assert r.status_code == 200
    assert "<title>" in r.text


def test_settings_page_discloses_comments_are_not_preserved(tmp_path):
    # FIX 3: saving rewrites config.toml wholesale, so a user who copied
    # config.example.toml loses its comments on first save. Nothing here
    # tries to preserve them -- the page must simply say so, near Save.
    store_dir = _store(tmp_path / "store")
    cfg_path = tmp_path / "config.toml"
    c = TestClient(create_app(_cfg(store_dir), config_path=cfg_path))
    r = c.get("/settings")
    assert r.status_code == 200
    assert "not preserved" in r.text.lower()


# --- FIX 1: an env override must never become a permanent file setting ---


def test_saving_does_not_bake_an_env_override_into_the_file(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    store = _store(tmp_path / "store")
    cfg_file.write_text(f'[store]\npath = "{store}"\n\n[llm]\nprovider = "groq"\n')
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    c = TestClient(create_app(load_config(cfg_file), cfg_file))
    body = c.get("/api/config").json()
    # save back exactly what the screen would send, changing only stale_days
    payload = {**body["file"], "stale_days": 45}
    assert c.post("/api/config", json=payload).status_code == 200
    assert 'provider = "groq"' in cfg_file.read_text()
    assert "openai" not in cfg_file.read_text()


def test_get_config_reports_which_fields_the_environment_is_overriding(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    store = _store(tmp_path / "store")
    cfg_file.write_text(f'[store]\npath = "{store}"\n\n[llm]\nprovider = "groq"\n')
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    body = TestClient(create_app(load_config(cfg_file), cfg_file)).get("/api/config").json()
    assert body["file"]["llm_provider"] == "groq"
    assert body["effective"]["llm_provider"] == "openai"
    assert body["overridden"]["llm_provider"] == "LLM_PROVIDER"


def test_get_config_reports_no_override_when_env_var_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("SPOKE_STORE_PATH", raising=False)
    store_dir = _store(tmp_path / "store")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'[store]\npath = "{store_dir}"\n\n[llm]\nprovider = "groq"\n')
    body = TestClient(create_app(load_config(cfg_path), cfg_path)).get("/api/config").json()
    assert body["overridden"] == {}
    assert body["file"] == body["effective"]


# --- FIX 2: a save that changes store_path must say the server needs a
# restart, not just "Saved." -- the running Store is never hot-reloaded. ---


def test_a_saved_store_path_is_read_by_the_same_process(tmp_path):
    """This used to assert a `restart_required` flag naming both paths,
    because the store was built once at start and a changed path was not
    read until restart -- saying "Saved." alone would have been absence
    rendering as assurance. The store is resolved per use now; the first-
    run screen depends on it. So the honest assertion is the stronger
    one: after the save, /api/records lists the NEW store's records."""
    store_dir = _store(tmp_path / "store")
    other_store = _store(tmp_path / "other-store")
    (other_store / "only-here.md").write_text("---\nname: only-here\n---\nbody\n")
    cfg_path = tmp_path / "config.toml"
    c = TestClient(create_app(_cfg(store_dir), config_path=cfg_path))
    assert "only-here.md" not in {r["name"] for r in c.get("/api/records").json()}

    r = c.post("/api/config", json={
        "store_path": str(other_store), "accepted_absences": [],
        "stale_days": 20, "llm_provider": "groq",
    })
    assert r.status_code == 200, r.text
    assert "restart_required" not in r.json()
    assert c.get("/api/health").json()["store"] == str(other_store)
    assert "only-here.md" in {r["name"] for r in c.get("/api/records").json()}


def test_the_brand_name_is_configuration_not_source(tmp_path):
    """The package ships no organisation's name -- the identity gate
    enforces that -- and the person who installed it must still be able
    to put theirs on it without touching code."""
    cfg = Config(_store(tmp_path / "store"), (), 30, "groq", None,
                 brand_name="Acme Reports")
    c = TestClient(create_app(cfg))
    for path in ("/", "/board", "/scale", "/settings"):
        page = c.get(path).text
        assert "Acme Reports" in page, path
        assert "<!--BRAND-->" not in page, path


def test_the_default_brand_is_the_tools_own_name(tmp_path):
    c = TestClient(create_app(Config(_store(tmp_path / "store"), (), 30, "groq", None)))
    assert "Spoke" in c.get("/board").text


def test_a_brand_name_is_escaped_where_it_is_written_into_the_page(tmp_path):
    """Validated on the way in AND escaped on the way out. A config file
    is not a trusted input just because it is local."""
    cfg = Config(_store(tmp_path / "store"), (), 30, "groq", None,
                 brand_name='<script>alert(1)</script>')
    page = TestClient(create_app(cfg)).get("/board").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_a_brand_name_with_a_newline_or_absurd_length_is_refused(tmp_path):
    store = _store(tmp_path / "store")
    c = TestClient(create_app(Config(store, (), 30, "groq", None),
                              config_path=tmp_path / "config.toml"))
    body = {"store_path": str(store), "accepted_absences": [], "stale_days": 30,
            "llm_provider": "groq"}
    assert c.post("/api/config", json={**body, "brand_name": "a\nb"}).status_code == 422
    assert c.post("/api/config", json={**body, "brand_name": "x" * 61}).status_code == 422
    assert c.post("/api/config", json={**body, "brand_name": "Acme"}).status_code == 200


def test_every_page_carries_the_brand_the_stylesheet_hook_and_a_cache_bust(tmp_path):
    """These used to reach two pages of four. `_page` replaces a comment
    that is a no-op when absent, so a page missing one renders perfectly
    and silently loses the feature -- absence as assurance, in the
    module that exists to prevent it."""
    store = _store(tmp_path / "store")
    cfg = Config(store, (), 30, "groq", None,
                 board_stylesheet="/static/app.css", brand_name="Acme")
    c = TestClient(create_app(cfg))
    for path in ("/", "/settings", "/board", "/scale"):
        page = c.get(path).text
        assert "Acme" in page, path
        assert 'href="/static/app.css"' in page, path      # the configured sheet
        assert "?v=" in page, path                          # cache-busted
        for placeholder in ("<!--BRAND-->", "<!--ASSET_V-->", "<!--BOARD_STYLESHEET-->"):
            assert placeholder not in page, (path, placeholder)


def test_the_cache_bust_covers_every_asset_not_just_the_boards(tmp_path):
    """The stamp was a hardcoded tuple of four filenames. Editing app.css
    could not change it, so the records screen shipped stale for ever."""
    store = _store(tmp_path / "store")
    c = TestClient(create_app(Config(store, (), 30, "groq", None)))
    from spoke.server import STATIC
    before = c.get("/").text
    target = STATIC / "app.css"
    stat = target.stat()
    try:
        import os
        # Past the NEWEST asset, not merely past app.css's own mtime. The
        # stamp is a max() over all nine files, so a fixed bump proves
        # nothing unless app.css already happened to be the newest -- true
        # on a fresh checkout, false the moment any other asset is edited.
        # The test then went red for a reason that had nothing to do with
        # what it claims to check.
        newest = max(f.stat().st_mtime for f in STATIC.iterdir() if f.is_file())
        os.utime(target, (stat.st_atime, newest + 120))
        assert c.get("/").text != before
    finally:
        os.utime(target, (stat.st_atime, stat.st_mtime))


def test_the_real_credential_never_appears_anywhere_in_the_config_response(
    tmp_path, monkeypatch
):
    """The name-based guard above is cheap and worth keeping, but it only
    knows about field NAMES. This one knows about the value: whatever
    LLM_API_KEY actually holds must not appear anywhere in the response,
    under any field name at all."""
    monkeypatch.setenv("LLM_API_KEY", "sk-this-must-never-be-returned")
    store_dir = _store(tmp_path / "store")
    c = TestClient(create_app(_cfg(store_dir), config_path=tmp_path / "config.toml"))
    r = c.get("/api/config")
    assert "sk-this-must-never-be-returned" not in r.text
    # ... and the screen can still tell you one is set
    assert r.json()["llm_credential_present"] is True


def test_the_settings_screen_is_told_which_providers_exist(tmp_path):
    """It used to offer a free-text box for a setting that did nothing.
    A typo produced no error and no effect."""
    store_dir = _store(tmp_path / "store")
    c = TestClient(create_app(_cfg(store_dir), config_path=tmp_path / "config.toml"))
    providers = c.get("/api/config").json()["llm_providers"]
    assert "groq" in providers and "ollama" in providers
    assert "anthropic" not in providers


def test_saving_an_unknown_provider_is_refused(tmp_path):
    store_dir = _store(tmp_path / "store")
    c = TestClient(create_app(_cfg(store_dir), config_path=tmp_path / "config.toml"))
    r = c.post("/api/config", json={
        "store_path": str(store_dir), "accepted_absences": [], "stale_days": 30,
        "llm_provider": "definitely-not-a-provider",
    })
    assert r.status_code == 422 and "llm_provider" in r.text


def test_no_author_means_no_author_line_at_all(tmp_path):
    """Not "by" with a blank after it: the package ships nobody's name."""
    c = TestClient(create_app(Config(_store(tmp_path / "store"), (), 30, "groq", None)))
    for path in ("/", "/board", "/scale"):
        page = c.get(path).text
        assert "<!--AUTHOR-->" not in page, path
        assert 'class="author"' not in page, path


def test_an_author_with_a_url_is_a_link_and_without_one_is_text(tmp_path):
    store = _store(tmp_path / "store")
    linked = Config(store, (), 30, "groq", None,
                    brand_author="Acme", brand_author_url="https://acme.example")
    page = TestClient(create_app(linked)).get("/board").text
    assert 'by <a href="https://acme.example" rel="author">Acme</a>' in page

    plain = Config(store, (), 30, "groq", None, brand_author="Acme")
    page = TestClient(create_app(plain)).get("/board").text
    assert "by Acme" in page and "<a href" not in page.split('class="author"')[1][:40]


def test_a_hand_edited_author_url_that_is_not_http_gets_no_href(tmp_path):
    """The save path refuses it, but a config file can be edited by hand
    and a hand edit does not pass through the save path. Rendering must
    apply the same rule or the config file is a script-injection vector."""
    cfg = Config(_store(tmp_path / "store"), (), 30, "groq", None,
                 brand_author="Acme", brand_author_url="javascript:alert(1)")
    page = TestClient(create_app(cfg)).get("/board").text
    assert "javascript:" not in page
    assert "by Acme" in page


def test_the_author_is_escaped_where_it_is_written_into_the_page(tmp_path):
    cfg = Config(_store(tmp_path / "store"), (), 30, "groq", None,
                 brand_author='<b>x</b>', brand_author_url='https://a.example/?q="><script>')
    page = TestClient(create_app(cfg)).get("/board").text
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page
    assert '"><script>' not in page


def test_author_saves_through_settings_and_refuses_a_non_http_url(tmp_path):
    store = _store(tmp_path / "store")
    c = TestClient(create_app(Config(store, (), 30, "groq", None),
                              config_path=tmp_path / "config.toml"))
    body = {"store_path": str(store), "accepted_absences": [], "stale_days": 30,
            "llm_provider": "groq"}
    assert c.post("/api/config", json={**body, "brand_author": "Acme",
                                       "brand_author_url": "javascript:alert(1)"}).status_code == 422
    assert c.post("/api/config", json={**body, "brand_author": "a\nb"}).status_code == 422
    r = c.post("/api/config", json={**body, "brand_author": "Acme",
                                    "brand_author_url": "https://acme.example"})
    assert r.status_code == 200, r.text
    got = c.get("/api/config").json()["file"]
    assert got["brand_author"] == "Acme"
    assert got["brand_author_url"] == "https://acme.example"
    text = (tmp_path / "config.toml").read_text()
    assert 'author = "Acme"' in text and 'author_url = "https://acme.example"' in text


def test_the_centre_of_the_board_says_hub_and_not_the_product_name(tmp_path):
    """The brand sat above "Hub" in the centre of the canvas, which read
    as though the hub were named Spoke. The name lives in the masthead,
    once, beside the mark."""
    c = TestClient(create_app(Config(_store(tmp_path / "store"), (), 30, "groq", None)))
    js = c.get("/static/board.js").text
    centre = js[js.index("function drawCentre"):js.index("function text(")]
    assert '"Hub"' in centre
    assert ".brand" not in centre and "centre-brand" not in centre
    page = c.get("/board").text
    assert 'class="prism"' in page
    assert page.count('class="brand"') == 1


def test_the_tagline_is_under_the_name_on_every_page_and_defaults_to_the_packages_line(tmp_path):
    from spoke.config import DEFAULT_TAGLINE

    c = TestClient(create_app(Config(_store(tmp_path / "store"), (), 30, "groq", None)))
    for path in ("/", "/board", "/scale", "/setup"):
        page = c.get(path).text
        assert f'<span class="tagline">{DEFAULT_TAGLINE}</span>' in page, path
        assert "<!--TAGLINE-->" not in page
    own = Config(_store(tmp_path / "s2"), (), 30, "groq", None, brand_tagline="Our own <line>")
    page = TestClient(create_app(own)).get("/board").text
    assert "Our own &lt;line&gt;" in page and DEFAULT_TAGLINE not in page


def test_the_notify_topic_is_editable_on_settings_and_validated(tmp_path):
    store = _store(tmp_path / "store")
    cfg_path = tmp_path / "config.toml"
    c = TestClient(create_app(Config(store, (), 30, "groq", None), config_path=cfg_path))
    body = {"store_path": str(store), "accepted_absences": [], "stale_days": 30,
            "llm_provider": "groq"}
    assert c.post("/api/config", json={**body, "ntfy_topic": "has space"}).status_code == 422
    assert c.post("/api/config", json={**body, "ntfy_topic": "ok-1", "ntfy_server": "http://plain"}).status_code == 422
    assert c.post("/api/config", json={**body, "ntfy_topic": "ok-1", "ntfy_server": "https://n.example/"}).status_code == 200
    got = c.get("/api/config").json()["file"]
    assert got["ntfy_topic"] == "ok-1" and got["ntfy_server"] == "https://n.example"
    page = c.get("/settings").text
    assert 'id="ntfy_topic"' in page and 'id="bar"' in page, "the field and the shared masthead"


def test_the_records_page_explains_itself_and_carries_the_shared_bar(tmp_path):
    """It was two empty panes and 'Select a record'. Every other page
    says what it is for before anything is clicked; this one now does."""
    store = _store(tmp_path / "store")
    c = TestClient(create_app(Config(store, (), 30, "groq", None)))
    page = c.get("/").text
    assert 'id="bar"' in page and 'id="pages"' in page
    assert 'id="filter"' in page, "240 records need a filter"
    assert "Save and commit" in page and "changed on disk" in page, "the gate and the conflict are explained"
    assert 'id="howto"' in page
