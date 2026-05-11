"""Shared helpers for invoking an agent with structured output and a graceful fallback.

The Portfolio Manager, Trader, and Research Manager all follow the same
canonical pattern:

1. At agent creation, wrap the LLM with ``with_structured_output(Schema)``
   so the model returns a typed Pydantic instance. If the provider does
   not support structured output (rare; mostly older Ollama models), the
   wrap is skipped and the agent uses free-text generation instead.
2. At invocation, run the structured call and render the result back to
   markdown. If the structured call itself fails for any reason
   (malformed JSON from a weak model, transient provider issue), fall
   back to a plain ``llm.invoke`` so the pipeline never blocks.

Centralising the pattern here keeps the agent factories small and ensures
all three agents log the same warnings when fallback fires.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Optional[Any]:
    """Return ``llm.with_structured_output(schema)`` or ``None`` if unsupported.

    For thinking-mode models (Kimi K2.6, DeepSeek Reasoner) this is expected
    and logged at INFO. For unexpected failures, logs at WARNING.
    """
    try:
        return llm.with_structured_output(schema)
    except NotImplementedError:
        # Expected for thinking-mode models — not an error condition.
        logger.info(
            "%s: using free-text generation (structured output unavailable for this model)",
            agent_name,
        )
        return None
    except AttributeError as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name, exc,
        )
        return None


def _append_suffix(prompt: Any, suffix: str) -> Any:
    """Append a format-instruction suffix to a prompt for the free-text path.

    Handles the two common prompt shapes:
    - str: appended directly with a blank line separator
    - list of message dicts: a new user message is appended
    - anything else (ChatPromptValue etc): returned untouched
    """
    if isinstance(prompt, str):
        return prompt + "\n\n" + suffix
    if isinstance(prompt, list):
        return prompt + [{"role": "user", "content": suffix}]
    return prompt


def invoke_structured_or_freetext(
    structured_llm: Optional[Any],
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
    freetext_suffix: Optional[str] = None,
) -> str:
    """Run the structured call and render to markdown; fall back to free-text on any failure.

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape). The same value is forwarded to the free-text path so the
    fallback sees the same input the structured call did.

    ``freetext_suffix`` is appended to the prompt when taking the free-text
    path. Use it to instruct thinking-mode models (Kimi K2, DeepSeek Reasoner)
    to produce the exact markdown headers that the plan_extractor regexes
    look for, so field extraction doesn't return all-null values.
    """
    if structured_llm is not None:
        try:
            result = structured_llm.invoke(prompt)
            return render(result)
        except Exception as exc:
            logger.warning(
                "%s: structured-output failed (%s: %s); falling back to free text — extraction may return nulls",
                agent_name, type(exc).__name__, exc,
            )

    effective_prompt = _append_suffix(prompt, freetext_suffix) if freetext_suffix else prompt
    response = plain_llm.invoke(effective_prompt)
    return response.content
