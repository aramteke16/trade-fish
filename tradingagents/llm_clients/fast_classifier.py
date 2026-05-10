"""Fast Moonshot classifier client for low-latency intraday decisions.

Built for the mid-day news-event monitor: a binary HOLD/EXIT classifier needs
~500 ms turnaround, not the 8-30 s that K2.5/K2.6 take with thinking enabled.

Implementation: thin wrapper around the OpenAI SDK that calls Moonshot's API
with ``extra_body={"thinking": {"type": "disabled"}}``. This is documented at
https://platform.kimi.ai/docs/api/chat — disabling thinking on Kimi K2.5
collapses latency from ~8s to ~500ms while preserving the same model weights
and pricing.

We bypass LangChain here on purpose. LangChain's ``ChatOpenAI`` doesn't expose
``extra_body`` cleanly, and for a one-shot classifier call we don't need
LangChain's tool-binding / structured-output / callback machinery.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from openai import OpenAI

from .openai_client import _api_key_from_db

logger = logging.getLogger(__name__)


@dataclass
class FastClassifierConfig:
    model: str = "kimi-k2.5"
    base_url: str = "https://api.moonshot.ai/v1"
    api_key_env: str = "MOONSHOT_API_KEY"
    timeout_sec: float = 8.0           # Hard ceiling — paranoia against API hangs.
    max_output_tokens: int = 80        # Two-line response is plenty.


class FastClassifier:
    """Single-purpose Moonshot client for low-latency binary classification.

    Use this for HOLD/EXIT, RELEVANT/IGNORE, BUY/SKIP-style one-shot calls
    where thinking mode adds latency without improving the answer.
    """

    def __init__(self, config: Optional[FastClassifierConfig] = None):
        self.cfg = config or FastClassifierConfig()
        api_key = os.getenv(self.cfg.api_key_env) or _api_key_from_db(self.cfg.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"FastClassifier requires {self.cfg.api_key_env} in env or DB config."
            )
        self._client = OpenAI(
            api_key=api_key,
            base_url=self.cfg.base_url,
            timeout=self.cfg.timeout_sec,
        )

    def classify(self, prompt: str, system: Optional[str] = None) -> str:
        """Run a single classification call and return the assistant's text.

        Returns "" on any error so callers can default to a safe action
        (typically HOLD) without crashing the polling loop.
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            resp = self._client.chat.completions.create(
                model=self.cfg.model,
                messages=messages,
                max_tokens=self.cfg.max_output_tokens,
                extra_body={"thinking": {"type": "disabled"}},
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning("FastClassifier call failed (%s); returning empty.", e)
            return ""
