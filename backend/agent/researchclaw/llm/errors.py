"""Retain useful provider diagnostics without logging credentials or requests."""
from __future__ import annotations

import io
import json
import re
import urllib.error
from typing import Any


class ModelTransportError(RuntimeError):
    """A tool loop exhausted transport retries, preserving its current files."""


class GenerationDeadlineExceeded(TimeoutError):
    """A live generation reached its wall deadline, distinct from a socket failure."""


def capture_provider_error(error: urllib.error.HTTPError, secrets: tuple[str, ...] = ()) -> str:
    if hasattr(error, "provider_error"):
        return str(error.provider_error.get("message", ""))
    def redact(value: Any) -> str:
        text = str(value)
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?i)\b(?:sk|key)-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
        text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", text)
        return text[:400]
    try:
        raw = error.read(8192)
    except Exception:
        raw = b""
    # Preserve compatibility with existing callers that inspect the body.
    error.fp = io.BytesIO(raw)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = {}
    payload = parsed.get("error", parsed) if isinstance(parsed, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    details = {"http_status": error.code,
               "code": redact(payload.get("code", payload.get("type", "unknown"))),
               "message": redact(payload.get("message", str(error.reason) or "Provider returned no structured error message")),
               "request_id": redact(next((error.headers.get(key) for key in ("x-request-id", "request-id", "x-trace-id")
                                           if error.headers and error.headers.get(key)), ""))}
    error.provider_error = details
    error.msg = f"provider_code={details['code']}; {details['message']}; request_id={details['request_id']}"
    return details["message"]
