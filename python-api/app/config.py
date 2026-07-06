import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_extra_body: dict[str, object] | None
    max_group_tokens: int
    token_encoding: str
    storage_dir: str
    job_workers: int
    frontend_origins: list[str]


@dataclass(frozen=True)
class _ProviderConfig:
    base_url: str
    api_key: str
    model: str
    extra_body: dict[str, object] | None = None


class _ProviderAdapter:
    """Base adapter: knows a provider's defaults, reads the generic
    API_KEY/BASE_URL/MODEL env vars, and resolves them into a _ProviderConfig.
    Subclasses only need to set the class attributes below (and override
    extra_body() for provider-specific request options)."""

    name: str
    default_base_url: str
    default_model: str
    default_api_key: str | None = None  # set for providers that accept a dummy key

    def resolve(self) -> _ProviderConfig:
        # `or` (not dict.get's default) so an empty string left over from a
        # copied .env.example still falls back to the provider's default.
        api_key = os.environ.get("API_KEY") or self.default_api_key
        if not api_key:
            raise RuntimeError(f"API_KEY environment variable is required when LLM_PROVIDER={self.name}")
        base_url = os.environ.get("BASE_URL") or self.default_base_url
        model = os.environ.get("MODEL") or self.default_model
        return _ProviderConfig(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            model=model,
            extra_body=self.extra_body(),
        )

    def extra_body(self) -> dict[str, object] | None:
        return None


class _OllamaAdapter(_ProviderAdapter):
    name = "ollama"
    default_base_url = "http://localhost:11434/v1"
    default_model = "qwen2.5:3b-instruct-q4_K_M"
    default_api_key = "ollama"  # ignored by Ollama, but the client requires one

    def extra_body(self) -> dict[str, object] | None:
        num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "30000"))
        return {"options": {"num_ctx": num_ctx}}


class _DeepSeekAdapter(_ProviderAdapter):
    name = "deepseek"
    default_base_url = "https://api.deepseek.com"
    default_model = "deepseek-chat"


class _OpenAIAdapter(_ProviderAdapter):
    name = "openai"
    default_base_url = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini"


# Registry of supported providers. Adding one is: write a small adapter class
# (defaults + optional extra_body()) and register it here — the generic
# API_KEY/BASE_URL/MODEL env vars are shared across all of them.
# Note: only genuinely OpenAI-compatible backends can go here. epub_translator's
# LLM client is hardcoded to the OpenAI chat-completions wire format, and
# Anthropic's Messages API has no official OpenAI-compatible endpoint, so
# Claude can't be added this way without rewriting the underlying library.
_PROVIDERS: dict[str, _ProviderAdapter] = {
    adapter.name: adapter for adapter in (_OllamaAdapter(), _DeepSeekAdapter(), _OpenAIAdapter())
}


def _load() -> Settings:
    provider_name = os.environ.get("LLM_PROVIDER", "openai").strip().lower()

    adapter = _PROVIDERS.get(provider_name)
    if adapter is None:
        supported = ", ".join(sorted(_PROVIDERS))
        raise RuntimeError(f"Unsupported LLM_PROVIDER: {provider_name!r} (expected one of: {supported})")
    provider_config = adapter.resolve()

    return Settings(
        llm_provider=provider_name,
        llm_base_url=provider_config.base_url,
        llm_api_key=provider_config.api_key,
        llm_model=provider_config.model,
        llm_extra_body=provider_config.extra_body,
        max_group_tokens=int(os.environ.get("MAX_GROUP_TOKENS", "1000")),
        token_encoding=os.environ.get("TOKEN_ENCODING", "cl100k_base"),
        storage_dir=os.environ.get("STORAGE_DIR", "./data"),
        job_workers=int(os.environ.get("JOB_WORKERS", "4")),
        frontend_origins=[o.strip() for o in os.environ.get("FRONTEND_ORIGIN", "").split(",") if o.strip()],
    )


settings = _load()
