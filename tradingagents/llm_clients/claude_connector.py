"""Claude Connector with API/CLI toggle.

Supports two modes for invoking Claude:
- "api": Standard langchain-anthropic direct API call (requires ANTHROPIC_API_KEY)
- "cli": Uses langchain-anthropic pointed at the Bedrock gateway proxy
         (uses ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BEDROCK_BASE_URL)

Both modes support full tool calling and structured output — the difference
is only which endpoint/credentials are used.
"""

import logging
import os
from typing import Any, Optional

from .base_client import BaseLLMClient, normalize_content

logger = logging.getLogger(__name__)


class ClaudeConnector(BaseLLMClient):
    """Claude connector with configurable api/cli mode.

    In "cli" mode, uses the Bedrock gateway proxy (no direct Anthropic API key needed).
    In "api" mode, uses standard Anthropic API directly.

    Both modes return a full ChatAnthropic instance with tool calling support.

    Args:
        model: Model name (e.g., "claude-sonnet-4-5-20250514")
        base_url: Optional base URL override
        mode: "api" for direct Anthropic API, "cli" for Bedrock gateway
        project_root: Project directory (used for env context)
        **kwargs: Additional args passed to ChatAnthropic
    """

    def __init__(
        self,
        model: str,
        base_url: Optional[str] = None,
        mode: str = "cli",
        project_root: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(model, base_url, **kwargs)
        self.mode = mode
        self.project_root = project_root or os.getcwd()

    def get_llm(self) -> Any:
        """Return the configured LLM instance based on mode.

        Both modes return a fully functional ChatAnthropic with tool support.
        The difference is the endpoint and auth credentials used.
        """
        from langchain_anthropic import ChatAnthropic
        from .anthropic_client import NormalizedChatAnthropic

        if self.mode == "cli":
            # Use Bedrock gateway — reads from environment variables
            bedrock_base_url = os.environ.get(
                "ANTHROPIC_BEDROCK_BASE_URL",
                "https://eng-ai-model-gateway.sfproxy.devx-preprod.aws-esvc1-useast2.aws.sfdc.cl/bedrock",
            )
            auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")

            if not auth_token:
                raise ValueError(
                    "ANTHROPIC_AUTH_TOKEN environment variable is required for CLI mode. "
                    "Source .env.claude-cli first: `source .env.claude-cli`"
                )

            logger.info("Using Claude via Bedrock gateway: %s", bedrock_base_url)

            llm_kwargs = {
                "model": self.model,
                "base_url": bedrock_base_url,
                "api_key": auth_token,
            }

            # Pass through any additional kwargs
            passthrough = ("timeout", "max_retries", "max_tokens", "callbacks", "effort")
            for key in passthrough:
                if key in self.kwargs:
                    llm_kwargs[key] = self.kwargs[key]

            return NormalizedChatAnthropic(**llm_kwargs)

        else:
            # Standard API mode — delegates to AnthropicClient
            from .anthropic_client import AnthropicClient
            client = AnthropicClient(self.model, self.base_url, **self.kwargs)
            return client.get_llm()

    def validate_model(self) -> bool:
        """Validate model — accept any Claude model."""
        from .validators import validate_model
        return validate_model("anthropic", self.model)
