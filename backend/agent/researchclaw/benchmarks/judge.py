"""Explicit provider options around the unchanged official scoring implementation."""
from __future__ import annotations

from contextlib import contextmanager
import os
from typing import Any, Iterator
from urllib.parse import urlsplit


def judge_options() -> dict[str, Any]:
    thinking = os.environ.get("JUDGE_THINKING", "").strip()
    if not thinking:
        return {}
    if thinking not in {"enabled", "disabled"}:
        raise ValueError("JUDGE_THINKING must be enabled or disabled, or unset for provider defaults")
    model = os.environ.get('JUDGE_MODEL_NAME', '')
    if model.startswith('Qwen'):
        return {'chat_template_kwargs': {'enable_thinking': thinking == 'enabled'}}
    if (urlsplit(os.environ.get("JUDGE_API_BASE", "")).hostname != "api.deepseek.com"
            or not model.startswith("deepseek-")):
        raise ValueError("JUDGE_THINKING requires a supported Qwen or official DeepSeek endpoint")
    return {"thinking": {"type": thinking}}


@contextmanager
def configured_official_judge(scorer: Any, options: dict[str, Any]) -> Iterator[None]:
    """Configure only owned SDK instances; keep prompts, rubric and budgets intact."""
    if not options:
        yield
        return
    original_factory = scorer.LLMAgent
    def factory(*args: Any, **kwargs: Any) -> Any:
        agent = original_factory(*args, **kwargs)
        create = agent.client.chat.completions.create
        def configured_create(*call_args: Any, **call_kwargs: Any) -> Any:
            call_kwargs["extra_body"] = {**call_kwargs.get("extra_body", {}), **options}
            return create(*call_args, **call_kwargs)
        agent.client.chat.completions.create = configured_create
        return agent
    scorer.LLMAgent = factory
    try:
        yield
    finally:
        scorer.LLMAgent = original_factory
