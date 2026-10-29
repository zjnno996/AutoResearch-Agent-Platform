"""Wire-format adapter for the shared tool loop, including usage accounting."""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from typing import Any

from researchclaw.llm.client import LLMClient


class ModelResponseError(ValueError):
    """The transport succeeded but did not return a usable assistant turn."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def _validate_calls(calls: Any) -> None:
    if not isinstance(calls, list):
        raise ModelResponseError("Tool calls must be a list")
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        if (not isinstance(function, dict) or not isinstance(function.get("name"), str)
                or not function["name"] or not isinstance(function.get("arguments"), str)
                or not isinstance(call.get("id"), str) or not call["id"]):
            raise ModelResponseError("Malformed model tool call")


def tool_request(config: Any, model: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    responses = LLMClient._is_responses_api_model(model)
    tokens = 16384 if model.startswith(("o3", "o4", "gpt-5")) else 8192
    configured_tokens = getattr(config, "max_tokens", 0)
    if type(configured_tokens) is int and configured_tokens > tokens:
        tokens = configured_tokens
    if responses:
        inputs: list[dict[str, Any]] = []
        instructions = []
        for message in messages:
            role = message["role"]
            if role == "system":
                instructions.append(message.get("content", ""))
            elif role == "tool":
                inputs.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                               "output": str(message.get("content", ""))})
            elif message.get("_response_items"):
                inputs.extend(message["_response_items"])
            else:
                if message.get("content"):
                    inputs.append({"role": role, "content": message["content"]})
                for call in message.get("tool_calls", []):
                    inputs.append({"type": "function_call", "call_id": call["id"],
                                   "name": call["function"]["name"],
                                   "arguments": call["function"]["arguments"]})
        body: dict[str, Any] = {"model": model, "input": inputs,
                                "instructions": "\n\n".join(instructions),
                                "max_output_tokens": tokens, "store": False,
                                "include": ["reasoning.encrypted_content"]}
        if tools:
            body["tools"] = [{"type": "function", **tool["function"]} for tool in tools]
            body["tool_choice"] = "auto"
        endpoint = "/v1/responses"
    else:
        token_key = "max_completion_tokens" if model.startswith(("o3", "o4", "gpt-5")) else "max_tokens"
        body = {"model": model, "messages": messages, token_key: tokens}
        if tools:
            body.update(tools=tools, tool_choice="auto")
        endpoint = "/v1/chat/completions"
    for key, value in (getattr(config, "extra_body", {}) or {}).items():
        if key not in {"model", "messages", "input", "instructions", "tools", "tool_choice"}:
            body[key] = value
    from researchclaw.llm.provider_policy import apply_output_policy
    return LLMClient._join_endpoint(config.base_url or "https://api.openai.com/v1", endpoint), apply_output_policy(body)


def tool_response(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ModelResponseError("Model response must be a JSON object")
    if raw.get("error"):
        error = raw["error"]
        code = str(error.get("code", error.get("type", "unknown"))) if isinstance(error, dict) else "unknown"
        raise ModelResponseError(f"Tool model returned an API error ({code})", retryable=code in (
            "server_error", "rate_limit_exceeded", "overloaded_error", "timeout"))
    if "choices" in raw:
        choices = raw["choices"]
        message = choices[0].get("message", {}) if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        if not isinstance(message, dict) or not (message.get("content") or message.get("tool_calls")):
            raise ModelResponseError("Model returned an empty assistant turn")
        _validate_calls(message.get("tool_calls") or [])
        return raw
    text = []
    calls = []
    output = raw.get("output", [])
    if not isinstance(output, list):
        raise ModelResponseError("Model output must be a list")
    for item in output:
        if not isinstance(item, dict):
            raise ModelResponseError("Malformed model output item")
        if item.get("type") == "message":
            text.extend(part.get("text", "") for part in item.get("content", []) if part.get("type") == "output_text")
        elif item.get("type") == "function_call":
            calls.append({"id": item.get("call_id"), "type": "function",
                          "function": {"name": item.get("name"), "arguments": item.get("arguments")}})
    _validate_calls(calls)
    if not text and not calls:
        raise ModelResponseError("Model returned no assistant text or tool calls")
    usage = raw.get("usage") or {}
    return {"choices": [{"message": {"role": "assistant", "content": "\n".join(text), "tool_calls": calls},
                          "finish_reason": "tool_calls" if calls else raw.get("status", "stop")}],
            "model": raw.get("model", ""),
            "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                      "completion_tokens": usage.get("output_tokens", 0), "total_tokens": usage.get("total_tokens", 0)},
            "_response_items": raw.get("output", [])}


def call_tool_model(config: Any, model: str, messages: list[dict[str, Any]],
                    tools: list[dict[str, Any]]) -> dict[str, Any]:
    """One validated, measured request shared by all native tool loops.

    Retry belongs to the caller, so each attempt produces one usage record.
    Preserve usage even when a successful HTTP response has invalid output.
    """
    from researchclaw.observability.tracing import trace_llm_call

    from researchclaw.llm.provider_policy import apply_provider_policy, provider_lock_enabled
    config = apply_provider_policy(config)
    if provider_lock_enabled():
        model = config.primary_model

    url, body = tool_request(config, model, messages, tools)
    from researchclaw.llm.budget import claim_model_call
    claim_model_call()
    from researchclaw.observability.tracing import provider_activity
    provider_activity("started", model=model, timeout_sec=getattr(config, "timeout_sec", 600),
                      reasoning_effort=(getattr(config, "extra_body", {}) or {}).get("reasoning_effort", "provider_default"))
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {config.api_key}"}
    headers.update(getattr(config, "extra_headers", {}) or {})
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
    started = time.monotonic()
    data: dict[str, Any] = {}
    error = ""
    try:
        with LLMClient._urlopen(request, timeout=getattr(config, "timeout_sec", 600)) as response:
            raw = json.loads(response.read().decode("utf-8"))
        data = raw if isinstance(raw, dict) else {}
        data = tool_response(raw)
        return data
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError):
            from researchclaw.llm.errors import capture_provider_error
            capture_provider_error(exc, (config.api_key,))
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
        raise
    finally:
        provider_activity("error" if error else "completed", error_type=error.split(":", 1)[0] if error else "")
        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        choices = data.get("choices") or [{}]
        choice = choices[0] if isinstance(choices, list) and isinstance(choices[0], dict) else {}
        message = choice.get("message")
        message = message if isinstance(message, dict) else {}
        trace_llm_call(model=model, messages=messages,
                       max_tokens=body.get("max_tokens", body.get("max_completion_tokens", body.get("max_output_tokens", 8192))),
                       temperature=body.get("temperature", 0), json_mode=False,
                       status="error" if error else "ok", latency_sec=time.monotonic() - started,
                       response_model=data.get("model", model), prompt_tokens=usage.get("prompt_tokens", 0),
                       completion_tokens=usage.get("completion_tokens", 0), total_tokens=usage.get("total_tokens", 0),
                       response_content=str(message.get("content") or ""), request_body=body, error=error,
                       finish_reason=choice.get("finish_reason", ""))


def assistant_message(data: dict[str, Any]) -> dict[str, Any]:
    """Retain provider reasoning and Responses items for the next tool turn."""
    choices = data.get("choices") or []
    message = {"role": "assistant", **choices[0].get("message", {})} if choices else {"role": "assistant", "content": ""}
    if data.get("_response_items"):
        message["_response_items"] = data["_response_items"]
    return message
