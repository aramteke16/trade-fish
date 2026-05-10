from typing import Optional

from .base_client import BaseLLMClient

# Providers that use the OpenAI-compatible chat completions API
_OPENAI_COMPATIBLE = (
    "openai", "xai", "deepseek", "qwen", "glm", "ollama", "openrouter", "moonshot",
)


def create_llm_client(
    provider: str,
    model: str,
    base_url: Optional[str] = None,
    **kwargs,
) -> BaseLLMClient:
    """Create an LLM client for the specified provider.

    Provider modules are imported lazily so that simply importing this
    factory (e.g. during test collection) does not pull in heavy LLM SDKs
    or fail when their API keys are absent.

    Args:
        provider: LLM provider name
        model: Model name/identifier
        base_url: Optional base URL for API endpoint
        **kwargs: Additional provider-specific arguments

    Returns:
        Configured BaseLLMClient instance

    Raises:
        ValueError: If provider is not supported
    """
    provider_lower = provider.lower()

    if provider_lower in _OPENAI_COMPATIBLE:
        from .openai_client import OpenAIClient
        return OpenAIClient(model, base_url, provider=provider_lower, **kwargs)

    if provider_lower == "anthropic":
        # Check if claude_mode is set to "cli" in kwargs
        claude_mode = kwargs.pop("claude_mode", None)
        if claude_mode == "cli":
            from .claude_connector import ClaudeConnector
            project_root = kwargs.pop("project_root", None)
            return ClaudeConnector(model, base_url, mode="cli", project_root=project_root, **kwargs)
        from .anthropic_client import AnthropicClient
        return AnthropicClient(model, base_url, **kwargs)

    if provider_lower == "google":
        from .google_client import GoogleClient
        return GoogleClient(model, base_url, **kwargs)

    if provider_lower == "azure":
        from .azure_client import AzureOpenAIClient
        return AzureOpenAIClient(model, base_url, **kwargs)

    raise ValueError(f"Unsupported LLM provider: {provider}")
