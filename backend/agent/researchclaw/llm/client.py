"""Lightweight OpenAI-compatible LLM client — stdlib only.

Features:
  - Model fallback chain (Qwen3 by default, configurable)
  - Auto-detect max_tokens vs max_completion_tokens per model
  - Cloudflare User-Agent bypass
  - Exponential backoff retry with jitter
  - JSON mode support
  - Streaming disabled (sync only)
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Any

logger = logging.getLogger(__name__)

# Models that require max_completion_tokens instead of max_tokens
_NEW_PARAM_MODELS = frozenset(
    {
        "o3",
        "o3-mini",
        "o4-mini",
        "gpt-5.4",
    }
)

# Models routed through the Responses API (v1/responses instead of v1/chat/completions)
_RESPONSES_API_MODELS = frozenset(
    {
        "gpt-5",
        "gpt-5.1",
        "gpt-5.2",
        "gpt-5.5",
    }
)

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class LLMResponse:
    """Parsed response from the LLM API."""

    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    finish_reason: str = ""
    truncated: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    request: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMConfig:
    """Configuration for the LLM client."""

    base_url: str
    api_key: str
    primary_model: str = "Qwen3.5-122B-A10B-FP8"
    fallback_models: list[str] = field(default_factory=list)
    max_tokens: int = 4096
    temperature: float = 0.7
    max_retries: int = 5
    retry_base_delay: float = 3.0
    timeout_sec: int = 600
    user_agent: str = _DEFAULT_USER_AGENT
    # MetaClaw bridge: extra headers for proxy requests
    extra_headers: dict[str, str] = field(default_factory=dict)
    # MetaClaw bridge: fallback URL if primary (proxy) is unreachable
    fallback_url: str = ""
    fallback_api_key: str = ""
    # Provider-specific OpenAI-compatible request body fields.
    extra_body: dict[str, Any] = field(default_factory=dict)
    # Strip visible reasoning tags by default for clients that should behave
    # like a normal chat assistant rather than a reasoning trace exporter.
    strip_thinking: bool = False


class LLMClient:
    """Stateless OpenAI-compatible chat completion client."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._model_chain = [config.primary_model] + list(config.fallback_models)
        self._anthropic = None  # Will be set by from_rc_config if needed

    @classmethod
    def from_rc_config(cls, rc_config: Any) -> LLMClient:
        from researchclaw.llm import resolve_provider_base_url

        provider = getattr(rc_config.llm, "provider", "openai")
        configured_base_url = str(getattr(rc_config.llm, "base_url", "") or "")

        api_key = str(
            rc_config.llm.api_key
            or os.environ.get(rc_config.llm.api_key_env, "")
            or ""
        )

        base_url = resolve_provider_base_url(provider, configured_base_url)

        # Preserve original URL/key before MetaClaw bridge override
        # (needed for Anthropic adapter which should always talk directly
        # to the Anthropic API, not through the OpenAI-compatible proxy).
        original_base_url = base_url
        original_api_key = api_key

        # MetaClaw bridge: if enabled, point to proxy and set up fallback
        bridge = getattr(rc_config, "metaclaw_bridge", None)
        fallback_url = ""
        fallback_api_key = ""

        if bridge and getattr(bridge, "enabled", False):
            fallback_url = base_url
            fallback_api_key = api_key
            base_url = bridge.proxy_url
            if bridge.fallback_url:
                fallback_url = bridge.fallback_url
            if bridge.fallback_api_key:
                fallback_api_key = bridge.fallback_api_key

        config = LLMConfig(
            base_url=base_url,
            api_key=api_key,
            primary_model=rc_config.llm.primary_model or "Qwen3.5-122B-A10B-FP8",
            fallback_models=list(rc_config.llm.fallback_models or []),
            timeout_sec=getattr(rc_config.llm, "timeout_sec", 600),
            max_retries=getattr(rc_config.llm, "max_retries", 5),
            retry_base_delay=getattr(rc_config.llm, "retry_base_delay", 3.0),
            extra_body=dict(getattr(rc_config.llm, "extra_body", {}) or {}),
            strip_thinking=bool(getattr(rc_config.llm, "strip_thinking", False)),
            fallback_url=fallback_url,
            fallback_api_key=fallback_api_key,
        )
        client = cls(config)

        # Detect Anthropic provider — use original URL/key (not the
        # MetaClaw proxy URL which is OpenAI-compatible only).
        if provider == "anthropic":
            from .anthropic_adapter import AnthropicAdapter

            client._anthropic = AnthropicAdapter(
                original_base_url, original_api_key, config.timeout_sec
            )
        return client

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
        system: str | None = None,
        strip_thinking: bool | None = None,
    ) -> LLMResponse:
        """Send a chat completion request with retry and fallback.

        Args:
            messages: List of {role, content} dicts.
            model: Override model (skips fallback chain).
            max_tokens: Override max token count.
            temperature: Override temperature.
            json_mode: Request JSON response format.
            system: Prepend a system message.
            strip_thinking: If True, strip <think>…</think> reasoning
                tags from the response content.  Use this when the
                output will be written to paper/script artifacts but
                NOT for general chat calls (to avoid corrupting
                legitimate content).

        Returns:
            LLMResponse with content and metadata.
        """
        if system:
            messages = [{"role": "system", "content": system}] + messages

        models = [model] if model else self._model_chain
        max_tok = max_tokens or self.config.max_tokens
        temp = temperature if temperature is not None else self.config.temperature

        last_error: Exception | None = None

        for idx, m in enumerate(models):
            attempt_max_tok = max_tok
            if m.startswith("deepseek-v4-pro"):
                attempt_max_tok = max(attempt_max_tok, 256)
            _trace_start = time.monotonic()
            try:
                resp = self._call_with_retry(m, messages, attempt_max_tok, temp, json_mode)
                should_strip = self.config.strip_thinking if strip_thinking is None else strip_thinking
                if should_strip:
                    from researchclaw.utils.thinking_tags import strip_thinking_tags
                    resp = LLMResponse(
                        content=strip_thinking_tags(resp.content),
                        model=resp.model,
                        prompt_tokens=resp.prompt_tokens,
                        completion_tokens=resp.completion_tokens,
                        total_tokens=resp.total_tokens,
                        finish_reason=resp.finish_reason,
                        truncated=resp.truncated,
                        raw=resp.raw,
                        request=resp.request,
                    )
                try:
                    from researchclaw.observability.tracing import trace_llm_call
                    trace_llm_call(
                        model=m,
                        messages=messages,
                        max_tokens=attempt_max_tok,
                        temperature=temp,
                        json_mode=json_mode,
                        status="ok",
                        latency_sec=time.monotonic() - _trace_start,
                        response_model=resp.model,
                        prompt_tokens=resp.prompt_tokens,
                        completion_tokens=resp.completion_tokens,
                        total_tokens=resp.total_tokens,
                        finish_reason=resp.finish_reason,
                        request_body=resp.request,
                        response_content=resp.content,
                    )
                except Exception:  # noqa: BLE001
                    pass
                return resp
            except Exception as exc:  # noqa: BLE001
                try:
                    from researchclaw.observability.tracing import trace_llm_call
                    trace_llm_call(
                        model=m,
                        messages=messages,
                        max_tokens=max_tok,
                        temperature=temp,
                        json_mode=json_mode,
                        status="error",
                        latency_sec=time.monotonic() - _trace_start,
                        error=str(exc),
                        request_body={
                            "model": m,
                            "messages": messages,
                            "max_tokens": attempt_max_tok,
                            "temperature": temp,
                            "json_mode": json_mode,
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass
                logger.warning("Model %s failed: %s. Trying next.", m, exc)
                last_error = exc
                _is_rate_or_conn = (
                    isinstance(exc, urllib.error.HTTPError) and exc.code == 429
                ) or isinstance(exc, (urllib.error.URLError, OSError, ConnectionError))
                if _is_rate_or_conn and idx < len(models) - 1:
                    import random
                    _backoff = self.config.retry_base_delay * (2 ** idx) + random.uniform(2, 8)
                    logger.info(
                        "Rate-limit / connection error on %s; waiting %.1fs before next model.",
                        m, _backoff,
                    )
                    time.sleep(_backoff)

        raise RuntimeError(
            f"All models failed. Last error: {last_error}"
        ) from last_error

    def preflight(self) -> tuple[bool, str]:
        """Quick connectivity check - one minimal chat call.

        Returns (success, message).
        Distinguishes: 401 (bad key), 403 (model forbidden),
                       404 (bad endpoint), 429 (rate limited), timeout.
        """
        is_reasoning = any(
            self.config.primary_model.startswith(p) for p in _NEW_PARAM_MODELS
        )
        min_tokens = 64 if is_reasoning else 1
        try:
            _ = self.chat(
                [{"role": "user", "content": "ping"}],
                max_tokens=min_tokens,
                temperature=0,
            )
            return True, f"OK - model {self.config.primary_model} responding"
        except urllib.error.HTTPError as e:
            status_map = {
                401: "Invalid API key",
                403: f"Model {self.config.primary_model} not allowed for this key",
                404: f"Endpoint not found: {self.config.base_url}",
                429: "Rate limited - try again in a moment",
            }
            msg = status_map.get(e.code, f"HTTP {e.code}")
            return False, msg
        except (urllib.error.URLError, OSError) as e:
            return False, f"Connection failed: {e}"
        except RuntimeError as e:
            # chat() wraps errors in RuntimeError; extract original HTTPError
            cause = e.__cause__
            if isinstance(cause, urllib.error.HTTPError):
                status_map = {
                    401: "Invalid API key",
                    403: f"Model {self.config.primary_model} not allowed for this key",
                    404: f"Endpoint not found: {self.config.base_url}",
                    429: "Rate limited - try again in a moment",
                }
                msg = status_map.get(cause.code, f"HTTP {cause.code}")
                return False, msg
            return False, f"All models failed: {e}"

    def _call_with_retry(
        self,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> LLMResponse:
        """Call with exponential backoff retry."""
        for attempt in range(self.config.max_retries):
            try:
                return self._raw_call(
                    model, messages, max_tokens, temperature, json_mode
                )
            except urllib.error.HTTPError as e:
                status = e.code
                body = ""
                try:
                    body = e.read().decode()[:500]
                except Exception:  # noqa: BLE001
                    pass

                # Non-retryable errors
                if status == 403 and "not allowed to use model" in body:
                    raise  # Model not available — let fallback handle

                # 400 is normally non-retryable, but some providers
                # (Azure OpenAI) return 400 during overload / rate-limit.
                # Retry if the body hints at a transient issue.
                if status == 400:
                    print(f"[LLM 400] model={model} body={body[:300]}", flush=True)
                    _transient_400 = any(
                        kw in body.lower()
                        for kw in ("rate limit", "ratelimit", "overloaded",
                                   "temporarily", "capacity", "throttl",
                                   "too many", "retry")
                    )
                    if not _transient_400:
                        raise  # Genuine bad request — don't retry

                # Retryable: 429 (rate limit), transient 400, 500, 502, 503, 504,
                # 529 (Anthropic overloaded)
                if status in (400, 429, 500, 502, 503, 504, 529):
                    if attempt >= self.config.max_retries - 1:
                        raise
                    delay = self.config.retry_base_delay * (2**attempt)
                    # Add jitter
                    import random

                    delay += random.uniform(0, delay * 0.3)
                    logger.info(
                        "Retry %d/%d for %s (HTTP %d). Waiting %.1fs.",
                        attempt + 1,
                        self.config.max_retries,
                        model,
                        status,
                        delay,
                    )
                    time.sleep(delay)
                    continue

                raise  # Other HTTP errors
            except (urllib.error.URLError, OSError, ConnectionError) as e:
                if attempt < self.config.max_retries - 1:
                    import random
                    delay = self.config.retry_base_delay * (2 ** attempt)
                    delay += random.uniform(0, delay * 0.3)
                    logger.info(
                        "Retry %d/%d for %s (connection error: %s). Waiting %.1fs.",
                        attempt + 1, self.config.max_retries, model, e, delay,
                    )
                    time.sleep(delay)
                    continue
                raise

        # Should not reach here, but just in case
        return self._raw_call(model, messages, max_tokens, temperature, json_mode)

    @staticmethod
    def _should_bypass_proxy(url: str) -> bool:
        host = urllib.parse.urlparse(url).hostname or ""
        if host in {"localhost"} or host.endswith(".local"):
            return True
        try:
            addr = ip_address(host)
        except ValueError:
            return False
        return addr.is_private or addr.is_loopback or addr.is_link_local

    @classmethod
    def _urlopen(cls, req: urllib.request.Request, timeout: int):
        url = getattr(req, "full_url", "")
        if cls._should_bypass_proxy(url):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            return opener.open(req, timeout=timeout)
        return urllib.request.urlopen(req, timeout=timeout)

    @classmethod
    def _stream_read(cls, req: urllib.request.Request, timeout: int) -> dict:
        """Send a streaming request and reassemble into a standard response dict."""
        with cls._urlopen(req, timeout=timeout) as resp:
            chunks: list[str] = []
            finish_reason = None
            model_name = ""
            usage = {}
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not model_name:
                    model_name = event.get("model", "")
                if event.get("usage"):
                    usage = event["usage"]
                for choice in event.get("choices", []):
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        chunks.append(delta["content"])
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
            content = "".join(chunks)
            return {
                "choices": [{"message": {"content": content, "role": "assistant"},
                             "finish_reason": finish_reason}],
                "model": model_name,
                "usage": usage,
            }

    @classmethod
    def _is_responses_api_model(cls, model: str) -> bool:
        """Check if a model requires the Responses API endpoint."""
        if any(model.startswith(prefix) for prefix in _NEW_PARAM_MODELS):
            return False
        return any(model.startswith(prefix) for prefix in _RESPONSES_API_MODELS)

    @staticmethod
    def _join_endpoint(base_url: str, endpoint: str) -> str:
        """Join an OpenAI-compatible base URL with an endpoint path.

        Configs commonly use either https://host or https://host/v1.
        Avoid producing /v1/v1/... when the latter form is supplied.
        """
        base = base_url.rstrip("/")
        endpoint_path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        if base.endswith("/v1") and endpoint_path.startswith("/v1/"):
            endpoint_path = endpoint_path[3:]
        return f"{base}{endpoint_path}"

    def _responses_call(
        self,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> LLMResponse:
        """Call the OpenAI Responses API (v1/responses)."""
        instructions = None
        input_items: list[dict[str, Any]] = []
        for msg in messages:
            if msg["role"] == "system":
                instructions = msg["content"]
            else:
                input_items.append({"role": msg["role"], "content": msg["content"]})

        min_output_tokens = 1024 if model.startswith("gpt-5.5") else 256
        body: dict[str, Any] = {
            "model": model,
            "input": input_items if input_items else [{"role": "user", "content": ""}],
            "max_output_tokens": max(max_tokens, min_output_tokens),
            "temperature": temperature,
        }
        if model.startswith("gpt-5.5"):
            body["reasoning"] = {"effort": "high"}
            body["text"] = {"verbosity": "medium"}
        if instructions:
            body["instructions"] = instructions

        if json_mode:
            if input_items and input_items[0].get("role") == "system":
                input_items[0]["content"] = (
                    "You MUST respond with valid JSON only. Do not include any "
                    "text outside the JSON object.\n\n" + input_items[0]["content"]
                )
            elif instructions:
                instructions = (
                    "You MUST respond with valid JSON only. Do not include any "
                    "text outside the JSON object.\n\n" + instructions
                )
                body["instructions"] = instructions
            else:
                body["instructions"] = (
                    "You MUST respond with valid JSON only. Do not include any "
                    "text outside the JSON object."
                )
            text_options = body.get("text", {})
            if not isinstance(text_options, dict):
                text_options = {}
            text_options["format"] = {"type": "json_object"}
            body["text"] = text_options

        for key, value in self.config.extra_body.items():
            if key not in {"model", "messages", "input", "instructions"}:
                body[key] = value

        request_body = json.loads(json.dumps(body))
        payload = json.dumps(body).encode("utf-8")
        url = self._join_endpoint(self.config.base_url, "/v1/responses")

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "User-Agent": self.config.user_agent,
        }
        headers.update(self.config.extra_headers)

        req = urllib.request.Request(url, data=payload, headers=headers)

        try:
            with self._urlopen(req, self.config.timeout_sec) as resp:
                raw_data = json.loads(resp.read())
        except (urllib.error.URLError, OSError) as exc:
            if self.config.fallback_url:
                logger.warning(
                    "Primary endpoint unreachable, falling back to %s: %s",
                    self.config.fallback_url,
                    exc,
                )
                fallback_url = self._join_endpoint(self.config.fallback_url, "/v1/responses")
                fallback_key = self.config.fallback_api_key or self.config.api_key
                fallback_headers = {
                    "Authorization": f"Bearer {fallback_key}",
                    "Content-Type": "application/json",
                    "User-Agent": self.config.user_agent,
                }
                fb_body = dict(body)
                fb_payload = json.dumps(fb_body).encode("utf-8")
                fallback_req = urllib.request.Request(
                    fallback_url, data=fb_payload, headers=fallback_headers
                )
                with self._urlopen(fallback_req, self.config.timeout_sec) as fallback_resp:
                    raw_data = json.loads(fallback_resp.read())
            else:
                raise

        data: dict[str, Any] = raw_data  # type: ignore[no-redef]

        # Handle API error responses
        if data.get("error"):
            error_info = data["error"]
            error_msg = error_info.get("message", str(error_info))
            error_type = error_info.get("type", "api_error")
            import io
            raise urllib.error.HTTPError(
                "", 500, f"{error_type}: {error_msg}", {},
                io.BytesIO(error_msg.encode()),
            )

        # Extract text content from Responses API output format
        content_parts: list[str] = []
        for item in data.get("output", []):
            if item.get("type") == "message":
                for content_item in item.get("content", []):
                    if content_item.get("type") == "output_text":
                        text = content_item.get("text", "")
                        if text:
                            content_parts.append(text)

        content = "\n".join(content_parts)
        usage = data.get("usage", {})
        status = data.get("status", "")
        incomplete_details = data.get("incomplete_details") or {}
        if status == "incomplete" and not content:
            reason = incomplete_details.get("reason", "unknown") if isinstance(incomplete_details, dict) else "unknown"
            import io
            error_msg = f"Responses API returned incomplete response without text (reason={reason})"
            raise urllib.error.HTTPError(
                url, 500, error_msg, {}, io.BytesIO(error_msg.encode())
            )

        return LLMResponse(
            content=content,
            model=data.get("model", model),
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            finish_reason=status,
            truncated=(status == "incomplete"),
            raw=data,
            request=request_body,
        )

    def _raw_call(
        self,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> LLMResponse:
        """Make a single API call."""

        # Use Anthropic adapter if configured
        if self._anthropic:
            data = self._anthropic.chat_completion(model, messages, max_tokens, temperature, json_mode)
        elif self._is_responses_api_model(model):
            return self._responses_call(model, messages, max_tokens, temperature, json_mode)
        else:
            # Original OpenAI logic
            # Copy messages to avoid mutating the caller's list (important for
            # retries and model-fallback — each attempt must start from the
            # original, un-modified messages).
            msgs = [dict(m) for m in messages]
            body: dict[str, Any] = {
                "model": model,
                "messages": msgs,
                "temperature": temperature,
            }

            # Use correct token parameter based on model.
            # Check _NEW_PARAM_MODELS first — "gpt-5.4" must NOT fall through
            # to _RESPONSES_API_MODELS whose "gpt-5" prefix would also match.
            if any(model.startswith(prefix) for prefix in _NEW_PARAM_MODELS):
                reasoning_min = 32768
                body["max_completion_tokens"] = max(max_tokens, reasoning_min)
            elif any(model.startswith(prefix) for prefix in _RESPONSES_API_MODELS):
                body["max_output_tokens"] = max(max_tokens, 32768)
            else:
                body["max_tokens"] = max_tokens

            if json_mode:
                # Many OpenAI-compatible providers (Claude, DeepSeek, etc.)
                # don't support the response_format parameter and return 400.
                # Fall back to a system-prompt injection for non-OpenAI models.
                _use_prompt_injection = (
                    model.startswith("claude")
                    or model.startswith("deepseek")
                    or "deepseek" in self.config.base_url.lower()
                )
                if _use_prompt_injection:
                    _json_hint = (
                        "You MUST respond with valid JSON only. "
                        "Do not include any text outside the JSON object."
                    )
                    # Prepend to existing system message or add as new one
                    if msgs and msgs[0]["role"] == "system":
                        msgs[0]["content"] = (
                            _json_hint + "\n\n" + msgs[0]["content"]
                        )
                    else:
                        msgs.insert(
                            0, {"role": "system", "content": _json_hint}
                        )
                else:
                    body["response_format"] = {"type": "json_object"}

            for key, value in self.config.extra_body.items():
                if key not in {"model", "messages"}:
                    body[key] = value

            body["stream"] = True
            request_body = json.loads(json.dumps(body))
            payload = json.dumps(body).encode("utf-8")
            url = self._join_endpoint(self.config.base_url, "/v1/chat/completions")

            headers = {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "User-Agent": self.config.user_agent,
            }
            headers.update(self.config.extra_headers)

            req = urllib.request.Request(url, data=payload, headers=headers)

            try:
                data = self._stream_read(req, self.config.timeout_sec)
            except (urllib.error.URLError, OSError) as exc:
                if self.config.fallback_url:
                    logger.warning(
                        "Primary endpoint unreachable, falling back to %s: %s",
                        self.config.fallback_url,
                        exc,
                    )
                    fallback_url = self._join_endpoint(
                        self.config.fallback_url, "/v1/chat/completions"
                    )
                    fallback_key = self.config.fallback_api_key or self.config.api_key
                    fallback_headers = {
                        "Authorization": f"Bearer {fallback_key}",
                        "Content-Type": "application/json",
                        "User-Agent": self.config.user_agent,
                    }
                    fb_body = dict(body)
                    fb_body["stream"] = True
                    fb_payload = json.dumps(fb_body).encode("utf-8")
                    fallback_req = urllib.request.Request(
                        fallback_url, data=fb_payload, headers=fallback_headers
                    )
                    data = self._stream_read(fallback_req, self.config.timeout_sec)
                else:
                    raise

        # Handle API error responses
        if data.get("error"):
            error_info = data["error"]
            error_msg = error_info.get("message", str(error_info))
            error_type = error_info.get("type", "api_error")
            import io
            raise urllib.error.HTTPError(
                "", 500, f"{error_type}: {error_msg}", {},
                io.BytesIO(error_msg.encode()),
            )

        # Validate response structure
        if "choices" not in data or not data["choices"]:
            raise ValueError(f"Malformed API response: missing choices. Got: {data}")

        choice = data["choices"][0]
        usage = data.get("usage", {})

        message = choice.get("message", {})
        content = message.get("content") or ""

        return LLMResponse(
            content=content,
            model=data.get("model", model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            finish_reason=choice.get("finish_reason", ""),
            truncated=(choice.get("finish_reason", "") == "length"),
            raw=data,
            request=locals().get("request_body", {}),
        )


def create_client_from_yaml(yaml_path: str | None = None) -> LLMClient:
    """Create an LLMClient from the ARC config file.

    Reads base_url and api_key from config.arc.yaml's llm section.
    """
    import yaml as _yaml

    if yaml_path is None:
        yaml_path = "config.yaml"

    with open(yaml_path, encoding="utf-8") as f:
        raw = _yaml.safe_load(f)

    llm_section = raw.get("llm", {})
    api_key = str(
        os.environ.get(
            llm_section.get("api_key_env", "OPENAI_API_KEY"),
            llm_section.get("api_key", ""),
        )
        or ""
    )

    return LLMClient(
        LLMConfig(
            base_url=llm_section.get("base_url", "https://api.openai.com/v1"),
            api_key=api_key,
            primary_model=llm_section.get("primary_model", "Qwen3.5-122B-A10B-FP8"),
            fallback_models=llm_section.get(
                "fallback_models", []
            ),
        )
    )
