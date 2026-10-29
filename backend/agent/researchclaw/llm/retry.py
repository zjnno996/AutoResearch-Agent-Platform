"""Transport retry policy shared by harnesses; programming errors fail promptly."""

from __future__ import annotations

import ssl
import http.client
import urllib.error

from researchclaw.llm.tool_client import ModelResponseError
from researchclaw.llm.errors import ModelTransportError


def retryable_model_error(exc: Exception) -> bool:
    if isinstance(exc, ModelTransportError):
        return True
    if isinstance(exc, ModelResponseError):
        return exc.retryable
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 409, 425, 429) or 500 <= exc.code <= 599
    if isinstance(exc, urllib.error.URLError):
        return not isinstance(exc.reason, ssl.SSLCertVerificationError)
    if isinstance(exc, (http.client.IncompleteRead, http.client.RemoteDisconnected)):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError))
