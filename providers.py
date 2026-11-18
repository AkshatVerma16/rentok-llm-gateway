"""Talking to the real (primary) LLM provider.

Uses only Python's standard library (urllib) — no extra dependency.
The provider key is sent only as a server-side Authorization header.
It is never logged, never returned to the caller, and never written
to the database.
"""

import json
import urllib.error
import urllib.request


class ProviderError(Exception):
    """Raised when the primary provider call fails. `status` is the
    HTTP status when the provider answered, or None on a timeout /
    network error."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _call_primary_once(model, messages, base_url, api_key, timeout_seconds=10):
    """One attempt against the provider. Raises ProviderError on failure."""
    url = f"{base_url}/chat/completions"
    body = json.dumps(
        {"model": model, "messages": messages, "stream": False}
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        # The provider answered with an error status. Read only a short
        # slice for diagnostics — never forward raw provider bodies
        # (they can contain account details) and never log the key.
        detail = err.read().decode("utf-8", errors="replace")[:200]
        raise ProviderError(f"Provider returned HTTP {err.code}: {detail}",
                            status=err.code) from err
    except (urllib.error.URLError, TimeoutError) as err:
        # Network error or timeout (urllib raises URLError wrapping a
        # timeout, or TimeoutError directly).
        raise ProviderError(f"Provider request failed: {err}") from err


def call_primary(model, messages, base_url, api_key):
    """Primary provider with exactly ONE retry, only on 5xx or timeout —
    the failures that are likely transient. A 4xx (bad model, bad
    request, bad provider key) will fail the same way twice, so retrying
    it only burns time and the caller's patience. Returns the parsed
    provider JSON. Raises ProviderError after the retry is exhausted."""
    import time

    last_error = None
    for attempt in (1, 2):
        try:
            return _call_primary_once(model, messages, base_url, api_key)
        except ProviderError as err:
            last_error = err
            retryable = err.status is None or err.status >= 500
            print(f"[provider] attempt {attempt} failed: {err}")
            if not retryable or attempt == 2:
                break
            time.sleep(0.3)  # tiny backoff before the single retry
    raise last_error
