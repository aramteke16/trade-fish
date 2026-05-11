import os
from typing import Any, Optional

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from .base_client import BaseLLMClient, normalize_content
from .validators import validate_model

_ENV_TO_CONFIG_KEY = {
    "MOONSHOT_API_KEY": "moonshot_api_key",
    "OPENAI_API_KEY": "openai_api_key",
    "XAI_API_KEY": "xai_api_key",
    "DEEPSEEK_API_KEY": "deepseek_api_key",
    "DASHSCOPE_API_KEY": "dashscope_api_key",
    "ZHIPU_API_KEY": "zhipu_api_key",
    "OPENROUTER_API_KEY": "openrouter_api_key",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "GOOGLE_API_KEY": "google_api_key",
}


def _api_key_from_db(env_var: str) -> Optional[str]:
    """Load an API key from the DB config when the env var is not set."""
    config_key = _ENV_TO_CONFIG_KEY.get(env_var)
    if not config_key:
        return None
    try:
        from tradingagents.web.config_service import get_config_value
        val = get_config_value(config_key)
        return val if val else None
    except Exception:
        return None


def _input_to_messages(input_: Any) -> list:
    """Normalise a langchain LLM input to a list of message objects.

    Accepts a list of messages, a ``ChatPromptValue`` (from a
    ChatPromptTemplate), or anything else (treated as no messages).
    Used by providers that need to walk the outgoing message history;
    in particular thinking-mode propagation must work for both bare-list
    invocations and ChatPromptTemplate-driven ones, so treating only
    ``list`` here would silently skip half the call sites.
    """
    if isinstance(input_, list):
        return input_
    if hasattr(input_, "to_messages"):
        return input_.to_messages()
    return []


class NormalizedChatOpenAI(ChatOpenAI):
    """ChatOpenAI with normalized content output and thinking-mode support.

    The Responses API returns content as a list of typed blocks
    (reasoning, text, etc.). ``invoke`` normalizes to string for
    consistent downstream handling. ``with_structured_output`` defaults
    to function-calling so the Responses-API parse path is avoided
    (langchain-openai's parse path emits noisy
    PydanticSerializationUnexpectedValue warnings per call without
    affecting correctness).

    Thinking-mode round-trip: when a provider (DeepSeek, Moonshot Kimi,
    etc.) returns ``reasoning_content`` in an assistant message, that
    field must be echoed back on the next request or the API returns
    HTTP 400. ``_create_chat_result`` captures the field on receive
    and ``_get_request_payload`` re-attaches it on send. This is a
    no-op for providers that don't use thinking mode.
    """

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))

    def with_structured_output(self, schema, *, method=None, **kwargs):
        if method is None:
            method = "function_calling"
        return super().with_structured_output(schema, method=method, **kwargs)

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        outgoing = payload.get("messages", [])
        for message_dict, message in zip(outgoing, _input_to_messages(input_)):
            if not isinstance(message, AIMessage):
                continue
            reasoning = message.additional_kwargs.get("reasoning_content")
            if reasoning is not None:
                message_dict["reasoning_content"] = reasoning
        return payload

    def _create_chat_result(self, response, generation_info=None):
        chat_result = super()._create_chat_result(response, generation_info)
        response_dict = (
            response
            if isinstance(response, dict)
            else response.model_dump(
                exclude={"choices": {"__all__": {"message": {"parsed"}}}}
            )
        )
        for generation, choice in zip(
            chat_result.generations, response_dict.get("choices", [])
        ):
            reasoning = choice.get("message", {}).get("reasoning_content")
            if reasoning is not None:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return chat_result


class DeepSeekChatOpenAI(NormalizedChatOpenAI):
    """DeepSeek-specific overrides.

    deepseek-reasoner has no tool_choice. Structured output via
    function-calling is unavailable, so we raise NotImplementedError
    and let the agent factories fall back to free-text generation
    (see ``tradingagents/agents/utils/structured.py``).

    Thinking-mode round-trip is handled by the base class.
    """

    def with_structured_output(self, schema, *, method=None, **kwargs):
        if self.model_name == "deepseek-reasoner":
            raise NotImplementedError(
                "deepseek-reasoner does not support tool_choice; structured "
                "output is unavailable. Agent factories fall back to "
                "free-text generation automatically."
            )
        return super().with_structured_output(schema, method=method, **kwargs)


class MoonshotChatOpenAI(NormalizedChatOpenAI):
    """Moonshot/Kimi-specific overrides.

    All kimi-k2* variants (K2, K2.5, K2.6, kimi-k2-thinking) have thinking
    always enabled. Both structured-output methods fail with thinking on:
      - function_calling: API rejects with "tool_choice incompatible with thinking"
      - json_mode: model leaks intermediate thinking JSON and ignores JSON
        constraints for certain outputs (e.g. SKIP responses)

    We raise NotImplementedError for all kimi-k2* so bind_structured() routes
    them to the free-text path, which uses a format suffix to guide the model
    to produce the exact markdown headers the plan_extractor regexes look for.

    Non-thinking variants (moonshot-v1-*) work fine with the default path.
    """

    _THINKING_MODELS = ("kimi-k2",)  # matches kimi-k2, kimi-k2.5, kimi-k2.6, kimi-k2-thinking

    def with_structured_output(self, schema, *, method=None, **kwargs):
        if any(self.model_name.startswith(m) for m in self._THINKING_MODELS):
            raise NotImplementedError(
                f"{self.model_name} has thinking always enabled; structured output "
                "(tool_choice and json_mode) is incompatible. Using free-text generation."
            )
        return super().with_structured_output(schema, method=method, **kwargs)

# Kwargs forwarded from user config to ChatOpenAI
_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "reasoning_effort",
    "api_key", "callbacks", "http_client", "http_async_client",
)

# Provider base URLs and API key env vars
_PROVIDER_CONFIG = {
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "qwen": ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    "glm": ("https://api.z.ai/api/paas/v4/", "ZHIPU_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),
    "moonshot": ("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"),
}


class OpenAIClient(BaseLLMClient):
    """Client for OpenAI, Ollama, OpenRouter, and xAI providers.

    For native OpenAI models, uses the Responses API (/v1/responses) which
    supports reasoning_effort with function tools across all model families
    (GPT-4.1, GPT-5). Third-party compatible providers (xAI, OpenRouter,
    Ollama) use standard Chat Completions.
    """

    def __init__(
        self,
        model: str,
        base_url: Optional[str] = None,
        provider: str = "openai",
        **kwargs,
    ):
        super().__init__(model, base_url, **kwargs)
        self.provider = provider.lower()

    def get_llm(self) -> Any:
        """Return configured ChatOpenAI instance."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}

        # Provider-specific base URL and auth. An explicit base_url on the
        # client (e.g. a corporate proxy) takes precedence over the
        # provider default so users can route through their own gateway.
        if self.provider in _PROVIDER_CONFIG:
            default_base, api_key_env = _PROVIDER_CONFIG[self.provider]
            llm_kwargs["base_url"] = self.base_url or default_base
            if api_key_env:
                api_key = os.environ.get(api_key_env) or _api_key_from_db(api_key_env)
                if api_key:
                    llm_kwargs["api_key"] = api_key
            else:
                llm_kwargs["api_key"] = "ollama"
        elif self.base_url:
            llm_kwargs["base_url"] = self.base_url

        # Forward user-provided kwargs
        for key in _PASSTHROUGH_KWARGS:
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        # Enable token streaming only for providers that don't use
        # reasoning_content round-trip. Moonshot (all Kimi models) and
        # DeepSeek Reasoner break when streamed because reasoning_content
        # from assistant messages must be echoed back on subsequent requests.
        _NO_STREAM_PROVIDERS = ("moonshot", "deepseek")
        if "callbacks" in llm_kwargs and llm_kwargs["callbacks"]:
            if self.provider not in _NO_STREAM_PROVIDERS:
                llm_kwargs["streaming"] = True

        # Native OpenAI: use Responses API for consistent behavior across
        # all model families. Third-party providers use Chat Completions.
        if self.provider == "openai":
            llm_kwargs["use_responses_api"] = True

        # Provider-specific subclasses handle thinking-mode quirks so the
        # base NormalizedChatOpenAI stays free of provider-specific branches.
        if self.provider == "deepseek":
            chat_cls = DeepSeekChatOpenAI
        elif self.provider == "moonshot":
            chat_cls = MoonshotChatOpenAI
        else:
            chat_cls = NormalizedChatOpenAI
        return chat_cls(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for the provider."""
        return validate_model(self.provider, self.model)
