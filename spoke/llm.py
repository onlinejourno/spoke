"""The provider seam: which model, at which endpoint, on whose credential.

Before this module took `llm_provider` seriously, the setting was inert.
It was a required `Config` field, environment-overridable, persisted to
config.toml and editable on the Settings screen -- and read by nothing
that made a decision. Both real choices were literals in this file: the
Groq base URL, and `llama-3.3-70b-versatile` as a function default.

That is not merely a dead knob. Setting `provider = "openai"` and
exporting an OpenAI key sent that key, as a bearer token, to
`api.groq.com` -- a host the user had not chosen and had no reason to
expect. A setting that does nothing is untidy; a setting that silently
routes a credential to a different vendor is a defect.

**Cloud providers have no default model.** That is not a preference, it
is this project's fail-closed ruling, adopted after a service ran for
weeks against an unpinned premium model because a default filled the gap
silently. An unpinned cloud provider raises here, names the exact
environment variable to set, and names the free tier first. Local
providers may default, because a model running on your own machine
cannot generate a bill.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


class LLMUnavailable(RuntimeError):
    """No usable credential or endpoint. Never silently skipped."""


@dataclass(frozen=True)
class Provider:
    base_url: str
    #: Free-tier or otherwise no-cost models, named in the refusal so the
    #: cheapest viable option is the first one a reader sees.
    suggested: tuple[str, ...] = ()
    #: Only ever set for a provider that runs on the user's own hardware.
    default_model: str | None = None
    local: bool = False


# Only providers speaking the OpenAI `/chat/completions` shape, because
# that is the only shape `complete()` speaks. Listing one that does not
# -- Anthropic's own API, for instance -- would be a menu entry that
# fails at the first request, which is worse than its absence.
PROVIDERS: dict[str, Provider] = {
    "groq": Provider(
        "https://api.groq.com/openai/v1",
        suggested=("llama-3.1-8b-instant", "llama-3.3-70b-versatile"),
    ),
    "openai": Provider("https://api.openai.com/v1", suggested=("gpt-4o-mini",)),
    "openrouter": Provider("https://openrouter.ai/api/v1"),
    "together": Provider("https://api.together.xyz/v1"),
    "ollama": Provider("http://localhost:11434/v1", default_model="llama3.1",
                       local=True),
    "vllm": Provider("http://localhost:8000/v1", default_model="local-model",
                     local=True),
    # For anything else: name the endpoint yourself.
    "custom": Provider("", local=False),
}


def resolve_endpoint(cfg) -> tuple[str, str]:
    """(base_url, model) for `cfg`, or raise saying exactly what to set.

    Never falls back to a provider the caller did not name. An unknown
    provider is refused rather than quietly served by the default one,
    because the failure that would hide is a credential sent to the wrong
    vendor.
    """
    name = (getattr(cfg, "llm_provider", "") or "").strip().lower()
    provider = PROVIDERS.get(name)
    if provider is None:
        raise LLMUnavailable(
            f"unknown LLM provider {name!r} -- known providers: "
            f"{', '.join(sorted(PROVIDERS))}. Set [llm].provider in config.toml "
            "or LLM_PROVIDER."
        )

    base = (getattr(cfg, "llm_base_url", None) or provider.base_url or "").rstrip("/")
    if not base:
        raise LLMUnavailable(
            f"provider {name!r} names no endpoint of its own -- set [llm].base_url "
            "in config.toml, or LLM_BASE_URL."
        )

    model = (os.environ.get("LLM_MODEL")
             or getattr(cfg, "llm_model", None)
             or provider.default_model)
    if not model:
        hint = (f" Free tier first: {', '.join(provider.suggested)}."
                if provider.suggested else "")
        raise LLMUnavailable(
            f"no model pinned for provider {name!r}. Set [llm].model in "
            f"config.toml, or LLM_MODEL.{hint} A cloud provider is left "
            "unpinned deliberately: a default model is how an unnoticed bill "
            "starts."
        )
    return base, model


def api_key_is_set() -> bool:
    """Whether a usable credential is present. The VALUE never leaves
    this module -- the Settings screen shows presence, never the key."""
    key = (os.environ.get("LLM_API_KEY") or "").strip()
    return bool(key) and key.lower() not in ("none", "null", "undefined")


def complete(prompt: str, cfg, client, model: str | None = None) -> str:
    if not api_key_is_set():
        raise LLMUnavailable(
            "LLM_API_KEY is not set. The contradiction pass is skipped, and this is an "
            "error rather than a pass - set LLM_API_KEY, or point LLM_BASE_URL at a local model."
        )
    key = os.environ["LLM_API_KEY"].strip()
    base, resolved = resolve_endpoint(cfg)
    r = client.post(f"{base}/chat/completions",
                    headers={"authorization": f"Bearer {key}"},
                    json={"model": model or resolved,
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]
