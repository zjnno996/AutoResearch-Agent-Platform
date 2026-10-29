"""Optional deployment policy that pins every model call to one provider.

The private launcher environment enables this policy. Without it, explicit
provider configurations retain their normal behavior for other installations.
"""
from __future__ import annotations

from dataclasses import replace
import os
from urllib.parse import urlsplit


def provider_lock_enabled() -> bool:
    return os.environ.get('EMNETS_PROVIDER_LOCK', '').strip().lower() in {'1', 'true', 'yes'}


def unlimited_tokens_enabled() -> bool:
    return provider_lock_enabled() and os.environ.get('EMNETS_UNLIMITED_TOKENS', '').lower() in {'1', 'true', 'yes'}


def quality_first_enabled() -> bool:
    """Explicit deployment preference; independent benchmark scorers opt out."""
    return unlimited_tokens_enabled() and os.environ.get('EMNETS_QUALITY_FIRST', '').strip().lower() in {'1', 'true', 'yes'}


def deployment_policy_identity() -> dict:
    identity = {'model_lock': provider_lock_enabled(), 'unlimited_tokens': unlimited_tokens_enabled()}
    if quality_first_enabled():
        identity['quality_first'] = True
    return identity


def apply_output_policy(body: dict) -> dict:
    """The verified local service allocates output from remaining context."""
    if unlimited_tokens_enabled():
        limits = ('max_tokens', 'max_completion_tokens', 'max_output_tokens', 'max_new_tokens', 'thinking_token_budget')
        for key in limits:
            body.pop(key, None)
        if isinstance(body.get('chat_template_kwargs'), dict):
            # Copy nested settings: the caller's persisted configuration is
            # also used by later requests and must not be modified in place.
            body['chat_template_kwargs'] = {key: value for key, value in body['chat_template_kwargs'].items()
                                           if key not in limits}
    return body


def locked_provider_settings() -> dict:
    if not provider_lock_enabled():
        return {}
    base = os.environ.get('EMNETS_API_BASE', '').strip().rstrip('/')
    model = os.environ.get('EMNETS_MODEL', '').strip()
    key_name = os.environ.get('EMNETS_API_KEY_ENV', 'RESEARCHCLAW_API_KEY')
    credential = os.environ.get(key_name, '')
    parsed = urlsplit(base)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or not model or not credential):
        raise ValueError('The locked EMNETS provider needs a valid API base, model and private API-key environment variable')
    return {'provider': 'openai-compatible', 'base_url': base, 'api_key': '', 'api_key_env': key_name,
            'primary_model': model, 'coding_model': model, 'image_model': model, 'fallback_models': []}


def provider_request_options(options: dict | None) -> dict:
    extra = dict(options or {})
    if not provider_lock_enabled():
        return extra
    old_thinking = extra.pop('thinking', None)
    old_effort = extra.pop('reasoning_effort', None)
    template = dict(extra.get('chat_template_kwargs') or {})
    if 'enable_thinking' not in template:
        template['enable_thinking'] = isinstance(old_thinking, dict) and old_thinking.get('type') == 'enabled'
    extra['chat_template_kwargs'] = template
    if unlimited_tokens_enabled():
        if os.environ.get('EMNETS_MODEL', '').startswith('Qwen3.8-'):
            if quality_first_enabled():
                template.update(enable_thinking=True, reasoning_effort='xhigh')
                extra.update(temperature=1.0, top_p=.95, presence_penalty=0.0,
                             top_k=20, min_p=0.0, repetition_penalty=1.0)
            # Native Qwen3.8 uses template effort (not a token quota). Follow
            # its published sampling recipe instead of legacy phase temp=.2.
            if template.get('enable_thinking'):
                effort = 'xhigh' if old_effort == 'high' else old_effort
                template.setdefault('reasoning_effort', effort if effort in {'low', 'medium', 'xhigh'} else 'xhigh')
                extra.setdefault('temperature', 1.0)
                extra.setdefault('top_p', .95)
                extra.setdefault('presence_penalty', 0.0)
            else:
                extra.setdefault('temperature', .7)
                extra.setdefault('top_p', .80)
                extra.setdefault('presence_penalty', 1.5)
            extra.setdefault('top_k', 20)
            extra.setdefault('min_p', 0.0)
            extra.setdefault('repetition_penalty', 1.0)
        return apply_output_policy(extra)
    configured_budget = int(os.environ.get('EMNETS_THINKING_TOKEN_BUDGET', '4096'))
    if configured_budget < 0:
        raise ValueError('EMNETS thinking budget must be nonnegative')
    extra.setdefault('thinking_token_budget', configured_budget if template['enable_thinking'] else 0)
    return extra


def apply_provider_policy(config):
    settings = locked_provider_settings()
    if not settings:
        return config
    return replace(config, base_url=settings['base_url'], api_key=os.environ[settings['api_key_env']],
                   primary_model=settings['primary_model'],
                   fallback_models=[], fallback_url='', fallback_api_key='',
                   extra_headers={},
                   extra_body=provider_request_options(config.extra_body), strip_thinking=True)
