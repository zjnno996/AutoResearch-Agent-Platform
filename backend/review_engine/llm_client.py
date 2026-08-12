"""LLM client management — loads config from config.arc.yaml and manages clients."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT_DIR / "backend" / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

_llm_clients: dict[str, Any] = {}
_llm_configs: list[dict[str, Any]] = []
_llm_model_options: list[dict[str, str]] = []
_primary_model_name: str = "deepseek-v4-pro"


def _load_config() -> dict[str, Any]:
    """Load the ARC config file."""
    config_path = ROOT_DIR / "config.arc.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml as _yaml
        with open(config_path, encoding="utf-8") as f:
            return _yaml.safe_load(f) or {}
    except Exception:
        return {}


def _build_model_configs() -> None:
    """Parse all model configurations from config and build model_options list."""
    global _llm_configs, _llm_model_options, _primary_model_name

    from researchclaw.llm import resolve_provider_base_url

    config = _load_config()
    web_llm = config.get("web_chat_llm", {}) or {}
    fallbacks = config.get("web_chat_llm_fallbacks", []) or []

    if not isinstance(web_llm, dict):
        web_llm = {}
    if not isinstance(fallbacks, list):
        fallbacks = []

    provider = str(web_llm.get("provider", "openai-compatible") or "openai-compatible")
    base_url = resolve_provider_base_url(provider, str(web_llm.get("base_url", "") or ""))
    api_key = str(
        web_llm.get("api_key", "")
        or os.environ.get(str(web_llm.get("api_key_env", "RESEARCHCLAW_API_KEY")), "")
        or ""
    )
    primary_model = str(web_llm.get("primary_model", "deepseek-v4-pro") or "deepseek-v4-pro")
    extra_body = web_llm.get("extra_body", {}) or {}
    timeout_sec = int(web_llm.get("timeout_sec", 120) or 120)
    max_retries = int(web_llm.get("max_retries", 2) or 2)
    strip_thinking = web_llm.get("strip_thinking", True)

    _primary_model_name = primary_model
    seen_models: set[str] = set()
    configs: list[dict[str, Any]] = []
    model_options: list[dict[str, str]] = []

    def add_model(model: str, base: str, key: str, prov: str, extra: dict,
                  timeout: int, retries: int, strip: bool, label_prefix: str = ""):
        if not model:
            return
        normalized_base = base.rstrip("/")
        if model in seen_models:
            # The same served model may be available on an independent endpoint.
            # Preserve it as an endpoint failover instead of silently dropping it.
            for existing in configs:
                if existing["model"] == model and existing["base_url"] != normalized_base:
                    if not existing.get("fallback_url"):
                        existing["fallback_url"] = normalized_base
                        existing["fallback_api_key"] = key
                    break
            return
        seen_models.add(model)
        display = f"{label_prefix} {model}" if label_prefix else model
        model_options.append({"value": model, "label": display, "baseUrl": normalized_base})
        configs.append({
            "model": model,
            "base_url": normalized_base,
            "api_key": key,
            "provider": prov,
            "extra_body": extra,
            "timeout_sec": timeout,
            "max_retries": retries,
            "strip_thinking": strip,
            "fallback_url": "",
            "fallback_api_key": "",
        })

    add_model(primary_model, base_url, api_key, provider, extra_body,
              timeout_sec, max_retries, strip_thinking)

    if isinstance(fallbacks, list):
        for fb in fallbacks:
            if not isinstance(fb, dict):
                continue
            fb_provider = str(fb.get("provider", "openai-compatible") or "openai-compatible")
            fb_base = resolve_provider_base_url(fb_provider, str(fb.get("base_url", "") or ""))
            fb_model = str(fb.get("primary_model", "") or "")
            fb_key = str(
                fb.get("api_key", "")
                or os.environ.get(str(fb.get("api_key_env", "RESEARCHCLAW_API_KEY")), "")
                or api_key
            )
            fb_extra = fb.get("extra_body", {}) or {}
            fb_timeout = int(fb.get("timeout_sec", timeout_sec) or timeout_sec)
            fb_retries = int(fb.get("max_retries", max_retries) or max_retries)
            fb_strip = fb.get("strip_thinking", strip_thinking)
            fb_label = str(fb.get("label", "") or "")
            add_model(fb_model, fb_base, fb_key, fb_provider, fb_extra,
                      fb_timeout, fb_retries, fb_strip, fb_label)

    _llm_configs = configs
    _llm_model_options = model_options


def register_config_section(section_name: str) -> str | None:
    """Register models from an arbitrary config section (e.g., 'llm') as available.

    Returns the primary model name from that section, or None if the section
    doesn't exist or has no model configured.
    """
    from researchclaw.llm import resolve_provider_base_url

    config = _load_config()
    section = config.get(section_name, {}) or {}
    if not isinstance(section, dict):
        return None

    if not _llm_configs:
        _build_model_configs()

    provider = str(section.get("provider", "openai-compatible") or "openai-compatible")
    base_url = resolve_provider_base_url(provider, str(section.get("base_url", "") or ""))
    api_key = str(
        section.get("api_key", "")
        or os.environ.get(str(section.get("api_key_env", "RESEARCHCLAW_API_KEY")), "")
        or ""
    )
    model_name = str(section.get("primary_model", "") or "")
    if not model_name:
        return None
    extra_body = section.get("extra_body", {}) or {}
    timeout_sec = int(section.get("timeout_sec", 120) or 120)
    max_retries = int(section.get("max_retries", 2) or 2)
    strip_thinking = section.get("strip_thinking", True)

    # Endpoint failovers are configured in web_chat_llm_fallbacks. They may
    # intentionally serve the same model name from a different host.
    fallback_url = ""
    fallback_api_key = ""
    for fallback in config.get("web_chat_llm_fallbacks", []) or []:
        if not isinstance(fallback, dict):
            continue
        if str(fallback.get("primary_model", "") or "") != model_name:
            continue
        candidate = resolve_provider_base_url(
            str(fallback.get("provider", "openai-compatible") or "openai-compatible"),
            str(fallback.get("base_url", "") or ""),
        ).rstrip("/")
        if candidate and candidate != base_url.rstrip("/"):
            fallback_url = candidate
            fallback_api_key = str(
                fallback.get("api_key", "")
                or os.environ.get(str(fallback.get("api_key_env", "RESEARCHCLAW_API_KEY")), "")
                or api_key
            )
            break

    # Already registered — overwrite config (the llm section is authoritative)
    for i, c in enumerate(_llm_configs):
        if c["model"] == model_name:
            _llm_configs[i] = {
                "model": model_name,
                "base_url": base_url.rstrip("/"),
                "api_key": api_key,
                "provider": provider,
                "extra_body": extra_body,
                "timeout_sec": timeout_sec,
                "max_retries": max_retries,
                "strip_thinking": strip_thinking,
                "fallback_url": fallback_url,
                "fallback_api_key": fallback_api_key,
            }
            # Also update model_options label
            for opt in _llm_model_options:
                if opt.get("value") == model_name:
                    opt["baseUrl"] = base_url.rstrip("/")
            # Clear cached client so it gets recreated
            if model_name in _llm_clients:
                del _llm_clients[model_name]
            return model_name

    _llm_model_options.append({"value": model_name, "label": model_name, "baseUrl": base_url})
    _llm_configs.append({
        "model": model_name,
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "provider": provider,
        "extra_body": extra_body,
        "timeout_sec": timeout_sec,
        "max_retries": max_retries,
        "strip_thinking": strip_thinking,
        "fallback_url": fallback_url,
        "fallback_api_key": fallback_api_key,
    })
    return model_name


def get_client_for_model(model_name: str | None = None) -> Any:
    """Get (or create) an LLM client for the given model name."""
    from researchclaw.llm.client import LLMClient, LLMConfig

    if not _llm_configs:
        _build_model_configs()

    target = model_name or _primary_model_name

    if target in _llm_clients:
        return _llm_clients[target]

    cfg = None
    for c in _llm_configs:
        if c["model"] == target:
            cfg = c
            break

    if cfg is None:
        for c in _llm_configs:
            if c["model"] == _primary_model_name:
                cfg = c
                break

    if cfg is None:
        raise RuntimeError(f"No configuration found for model '{target}'")

    client_config = LLMConfig(
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        primary_model=cfg["model"],
        fallback_models=[],
        timeout_sec=cfg["timeout_sec"],
        max_retries=cfg["max_retries"],
        extra_body=cfg["extra_body"],
        strip_thinking=cfg["strip_thinking"],
        fallback_url=cfg.get("fallback_url", ""),
        fallback_api_key=cfg.get("fallback_api_key", ""),
    )
    client = LLMClient(client_config)
    _llm_clients[target] = client
    return client


def get_model_options() -> list[dict[str, str]]:
    """Get the list of available model options."""
    if not _llm_model_options:
        _build_model_configs()
    return _llm_model_options


def get_primary_model_name() -> str:
    """Get the primary model name."""
    if not _llm_configs:
        _build_model_configs()
    return _primary_model_name



def get_preferred_review_model_name() -> str:
    """Prefer the configured Qwen model for multimodal review and debate."""
    if not _llm_model_options:
        _build_model_configs()
    for option in _llm_model_options:
        value = option.get("value", "")
        if "qwen" in value.lower():
            return value
    raise RuntimeError("AutoReview requires a configured Qwen model")
