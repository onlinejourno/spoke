from pathlib import Path
import textwrap
import pytest
from spoke.config import (
    Config,
    ConfigError,
    load_config,
    load_config_file,
    overridden_env_vars,
    save_config,
)


def test_loads_store_path_and_accepted_absences(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(textwrap.dedent("""
        [store]
        path = "~/somewhere/memory"
        accepted_absences = ["in-redesign-status", "example-org-portfolio", "allowlists"]

        [checks]
        stale_days = 30

        [llm]
        provider = "groq"
    """))
    cfg = load_config(cfg_file)
    assert cfg.store_path == Path("~/somewhere/memory").expanduser()
    assert cfg.accepted_absences == ("in-redesign-status", "example-org-portfolio", "allowlists")
    assert cfg.stale_days == 30
    assert cfg.llm_provider == "groq"


def test_env_overrides_store_path(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[store]\npath = "/from/file"\n')
    monkeypatch.setenv("SPOKE_STORE_PATH", "/from/env")
    assert load_config(cfg_file).store_path == Path("/from/env")


def test_missing_store_path_is_an_error_not_a_default(tmp_path):
    # There is deliberately no default store path. Falling back to one would
    # point a second project at the first project's memories, silently.
    with pytest.raises(ConfigError) as e:
        load_config(tmp_path / "absent.toml")
    assert "SPOKE_STORE_PATH" in str(e.value)


def test_other_settings_still_default_when_absent(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[store]\npath = "/somewhere/memory"\n')
    cfg = load_config(cfg_file)
    assert cfg.stale_days > 0
    assert cfg.llm_provider


def test_accepted_absences_pass_through_unfiltered(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[store]\npath = "/somewhere/memory"\n'
        'accepted_absences = ["one", "allowlists", "two"]\n'
    )
    cfg = load_config(cfg_file)
    assert cfg.accepted_absences == ("one", "allowlists", "two")


def test_require_store_false_defers_to_the_registry(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("[checks]\nstale_days = 30\n")
    cfg = load_config(f, require_store=False)
    assert str(cfg.store_path) == "/__spoke_store_unset__"
    assert not cfg.store_path.exists()


def test_save_config_round_trips_through_load_config(tmp_path, monkeypatch):
    monkeypatch.delenv("SPOKE_STORE_PATH", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    cfg_file = tmp_path / "config.toml"
    cfg = Config(
        store_path=tmp_path / "store",
        accepted_absences=("one", "two"),
        stale_days=45,
        llm_provider="groq",
        llm_base_url="https://example.test/v1",
    )
    save_config(cfg_file, cfg)
    assert load_config(cfg_file) == cfg


def test_save_config_round_trips_with_no_base_url(tmp_path, monkeypatch):
    monkeypatch.delenv("SPOKE_STORE_PATH", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    cfg_file = tmp_path / "config.toml"
    cfg = Config(
        store_path=tmp_path / "store",
        accepted_absences=(),
        stale_days=30,
        llm_provider="groq",
        llm_base_url=None,
    )
    save_config(cfg_file, cfg)
    assert load_config(cfg_file) == cfg


def test_save_config_writes_atomically_via_temp_file_and_replace(tmp_path, monkeypatch):
    # Same mechanism as projects.save_registry: a sibling temp file plus
    # os.replace(), so a crash mid-write cannot leave a truncated config.
    import spoke.config as config_mod

    calls = []
    real_replace = config_mod.os.replace

    def spy_replace(src, dst):
        calls.append((Path(src).parent, Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(config_mod.os, "replace", spy_replace)
    cfg_file = tmp_path / "config.toml"
    cfg = Config(tmp_path / "store", (), 30, "groq", None)
    save_config(cfg_file, cfg)
    assert len(calls) == 1
    tmp_dir, dst = calls[0]
    assert tmp_dir == tmp_path
    assert dst == cfg_file


def test_save_config_creates_parent_directory(tmp_path):
    cfg_file = tmp_path / "nested" / "dir" / "config.toml"
    cfg = Config(tmp_path / "store", (), 30, "groq", None)
    save_config(cfg_file, cfg)
    assert cfg_file.exists()


def test_save_config_never_writes_llm_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "secret-should-not-appear")
    cfg_file = tmp_path / "config.toml"
    cfg = Config(tmp_path / "store", (), 30, "groq", "https://example.test")
    save_config(cfg_file, cfg)
    text = cfg_file.read_text()
    assert "secret-should-not-appear" not in text
    assert "api_key" not in text.lower()


# --- load_config_file: the raw file, with NO env overrides applied ---


def test_load_config_file_ignores_env_overrides(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[store]\npath = "/from/file"\n\n[llm]\nprovider = "groq"\n')
    monkeypatch.setenv("SPOKE_STORE_PATH", "/from/env")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://env-injected.example.com")
    file_cfg = load_config_file(cfg_file)
    assert file_cfg.store_path == Path("/from/file")
    assert file_cfg.llm_provider == "groq"
    assert file_cfg.llm_base_url is None
    # load_config(), unlike load_config_file(), still applies overrides --
    # this function must never change that.
    assert load_config(cfg_file).store_path == Path("/from/env")
    assert load_config(cfg_file).llm_provider == "openai"


def test_load_config_file_never_raises_on_missing_file_or_store_path(tmp_path):
    # Unlike load_config(), this is never the thing that decides whether a
    # store is configured -- it only reports what the file says, even when
    # that's nothing (e.g. the real path comes from the project registry).
    assert load_config_file(tmp_path / "absent.toml").store_path == Path("")
    f = tmp_path / "config.toml"
    f.write_text("[checks]\nstale_days = 10\n")
    file_cfg = load_config_file(f)
    assert file_cfg.store_path == Path("")
    assert file_cfg.stale_days == 10


# --- overridden_env_vars: which fields the environment is overriding ---


def test_overridden_env_vars_reports_only_set_variables(monkeypatch):
    monkeypatch.delenv("SPOKE_STORE_PATH", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert overridden_env_vars() == {}
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert overridden_env_vars() == {"llm_provider": "LLM_PROVIDER"}


def test_a_settings_save_keeps_the_notify_topic(tmp_path, monkeypatch):
    """Config carried no [notify]; save_config wrote none. One Save on the
    Settings screen dropped the doctor's topic and every later run said
    'notify: not configured' to a terminal nobody reads."""
    for v in ("SPOKE_STORE_PATH", "LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(v, raising=False)
    cfg_file = tmp_path / "config.toml"
    cfg = Config(store_path=tmp_path / "store", accepted_absences=(), stale_days=30,
                 llm_provider="groq", llm_base_url=None,
                 ntfy_topic="estate-0b60926f", ntfy_server="https://ntfy.example")
    save_config(cfg_file, cfg)
    assert load_config(cfg_file) == cfg
    assert "[notify]" in cfg_file.read_text()
    # and the doctor reads the same file the same way
    from spoke.notify import load_notify_config
    monkeypatch.delenv("SPOKE_NTFY_TOPIC", raising=False)
    monkeypatch.delenv("SPOKE_NTFY_SERVER", raising=False)
    n = load_notify_config(cfg_file)
    assert (n.topic, n.server) == ("estate-0b60926f", "https://ntfy.example")
