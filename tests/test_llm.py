import httpx
import pytest
from spoke.config import Config
from spoke.llm import LLMUnavailable, complete


def _cfg(**kw):
    fields = dict(store_path=__import__("pathlib").Path("/tmp"), accepted_absences=(),
                  stale_days=30, llm_provider="groq",
                  llm_base_url="https://api.test/v1", llm_model="pinned-model")
    fields.update(kw)
    return Config(**fields)


def test_raises_when_no_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(LLMUnavailable):
        complete("hi", _cfg(), client)


def test_never_falls_back_to_a_default_key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(LLMUnavailable):
        complete("hi", _cfg(), client)


def test_sends_prompt_and_returns_text(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert complete("hi", _cfg(), client) == "answer"
    assert seen["auth"] == "Bearer k"


@pytest.mark.parametrize("bogus", ["", "   ", "\n", "none", "None", "NULL", "undefined"])
def test_placeholder_keys_are_treated_as_absent(monkeypatch, bogus):
    monkeypatch.setenv("LLM_API_KEY", bogus)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(LLMUnavailable):
        complete("hi", _cfg(), client)


def test_key_is_stripped_before_use(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "  k\n")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert complete("hi", _cfg(), client) == "answer"
    assert seen["auth"] == "Bearer k"


# --- the provider setting, which used to be inert ---------------------

def test_the_provider_decides_the_endpoint(monkeypatch):
    """It used to decide nothing: the Groq URL was a literal in llm.py, so
    `provider = "openai"` with an OpenAI key sent that key, as a bearer
    token, to api.groq.com -- a host the user had not chosen."""
    from spoke.llm import resolve_endpoint

    monkeypatch.delenv("LLM_MODEL", raising=False)
    base, _ = resolve_endpoint(_cfg(llm_provider="openai", llm_base_url=None))
    assert base == "https://api.openai.com/v1"
    base, _ = resolve_endpoint(_cfg(llm_provider="groq", llm_base_url=None))
    assert base == "https://api.groq.com/openai/v1"


def test_an_unknown_provider_is_refused_not_quietly_served_by_the_default(monkeypatch):
    from spoke.llm import resolve_endpoint

    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(LLMUnavailable) as e:
        resolve_endpoint(_cfg(llm_provider="anthropic", llm_base_url=None))
    assert "known providers" in str(e.value) and "groq" in str(e.value)


def test_a_cloud_provider_left_unpinned_refuses_and_names_the_free_tier(monkeypatch):
    """The estate's own fail-closed ruling: a default model is how an
    unnoticed bill starts."""
    from spoke.llm import resolve_endpoint

    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(LLMUnavailable) as e:
        resolve_endpoint(_cfg(llm_model=None, llm_base_url=None))
    msg = str(e.value)
    assert "LLM_MODEL" in msg
    assert msg.index("llama-3.1-8b-instant") < msg.index("llama-3.3-70b-versatile")


def test_a_local_provider_may_default_because_it_cannot_bill_you(monkeypatch):
    from spoke.llm import resolve_endpoint

    monkeypatch.delenv("LLM_MODEL", raising=False)
    base, model = resolve_endpoint(_cfg(llm_provider="ollama", llm_base_url=None,
                                        llm_model=None))
    assert base.startswith("http://localhost") and model


def test_custom_requires_you_to_name_the_endpoint(monkeypatch):
    from spoke.llm import resolve_endpoint

    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(LLMUnavailable) as e:
        resolve_endpoint(_cfg(llm_provider="custom", llm_base_url=None))
    assert "base_url" in str(e.value)


def test_every_listed_provider_speaks_the_shape_complete_speaks():
    """A menu entry that fails at the first request is worse than its
    absence, so nothing goes in PROVIDERS that is not OpenAI-compatible."""
    from spoke.llm import PROVIDERS

    assert "anthropic" not in PROVIDERS
    for name, p in PROVIDERS.items():
        assert p.local or p.default_model is None, name


def test_the_key_presence_is_reportable_but_the_value_is_not(monkeypatch):
    from spoke.llm import api_key_is_set

    monkeypatch.setenv("LLM_API_KEY", "sk-secret")
    assert api_key_is_set() is True
    monkeypatch.setenv("LLM_API_KEY", "  ")
    assert api_key_is_set() is False
    monkeypatch.setenv("LLM_API_KEY", "none")
    assert api_key_is_set() is False
