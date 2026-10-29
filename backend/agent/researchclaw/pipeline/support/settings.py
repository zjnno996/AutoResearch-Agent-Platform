"""Repository-level reviewer configuration resolution."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient, LLMConfig
from researchclaw.pipeline.support.paths import repository_root

logger = logging.getLogger(__name__)


def _load_root_arc_section(section_name: str) -> dict[str, Any]:
    try:
        root_config = repository_root() / "config.arc.yaml"
        if not root_config.exists():
            return {}
        raw = yaml.safe_load(root_config.read_text(encoding="utf-8")) or {}
        section = raw.get(section_name, {}) if isinstance(raw, dict) else {}
        return section if isinstance(section, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _create_idea_judge_llm(config: RCConfig) -> tuple[LLMClient | None, str]:
    """Create the strong LLM-as-Judge client for Stage 8 idea scoring."""
    section = _load_root_arc_section("idea_judge_llm")
    # An explicitly selected judge must use its own provider parameters rather
    # than the repository's default model's request body.
    if os.environ.get("RESEARCHCLAW_IDEA_JUDGE_MODEL"):
        section = {}
    model = (
        os.environ.get("RESEARCHCLAW_IDEA_JUDGE_MODEL", "")
        or str(section.get("primary_model", "") or "")
        or str(getattr(config.llm, "primary_model", "") or "")
        or "Qwen3.5-122B-A10B-FP8"
    )
    base_url = (
        os.environ.get("RESEARCHCLAW_IDEA_JUDGE_BASE_URL", "")
        or str(section.get("base_url", "") or "")
        or str(getattr(config.llm, "base_url", "") or "")
    )
    api_key_env = str(section.get("api_key_env", "RESEARCHCLAW_IDEA_JUDGE_API_KEY") or "RESEARCHCLAW_IDEA_JUDGE_API_KEY")
    api_key = (
        os.environ.get("RESEARCHCLAW_IDEA_JUDGE_API_KEY", "")
        or os.environ.get(api_key_env, "")
        or str(section.get("api_key", "") or "")
        or str(getattr(config.llm, "api_key", "") or "")
    )
    if not api_key:
        current_key = str(getattr(config.llm, "api_key", "") or "")
        current_base = str(getattr(config.llm, "base_url", "") or "")
        if current_base and current_key:
            api_key = current_key
            base_url = current_base
    if not api_key:
        logger.info("S8: Idea LLM-as-Judge skipped — no Qwen3 judge API key configured")
        return None, model
    timeout = int(section.get("timeout_sec", os.environ.get("RESEARCHCLAW_IDEA_JUDGE_TIMEOUT", 90)) or 90)
    max_retries = int(section.get("max_retries", os.environ.get("RESEARCHCLAW_IDEA_JUDGE_MAX_RETRIES", 1)) or 1)
    extra_body = section.get("extra_body", None)
    if not isinstance(extra_body, dict):
        extra_body = dict(getattr(config.llm, "extra_body", {}) or {})
    client = LLMClient(LLMConfig(
        base_url=base_url,
        api_key=api_key,
        primary_model=model,
        fallback_models=[],
        max_tokens=getattr(config.llm, "max_tokens", 4096),
        reasoning_token_reserve=getattr(config.llm, "reasoning_token_reserve", 0),
        timeout_sec=timeout,
        max_retries=max_retries,
        temperature=0,
        strip_thinking=True,
        extra_body=extra_body,
    ))
    return client, model
