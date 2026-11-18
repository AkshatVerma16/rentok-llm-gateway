# RentOk take-home — Minimal LLM Gateway (Python + Flask + SQLite)
# ------------------------------------------------------------------
# A small HTTP service that sits between callers and an LLM provider
# and adds the things a team needs to run LLMs safely: virtual API
# keys, per-key budgets that actually block, usage/spend logging,
# and fallback.
#
# Request lifecycle (full trace in DECISIONS.md):
#   client -> auth (x-api-key) -> per-key lock -> budget pre-check
#   -> provider (OpenAI-compatible, 1 retry) -> fallback (mock)
#   -> charge actual cost + log usage -> response
#
# Non-streaming only, by design: the gateway needs the provider's
# final token counts before it charges the key, and streaming would
# force it to charge on estimates. See DECISIONS.md.
#
# Secrets: the provider key lives only in the OPENAI_API_KEY env var
# and is sent only as a server-side Authorization header. It is never
# logged, never returned, and never written to the database.

import hashlib
import hmac
import os
import secrets
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, request

import db
from mock import mock_reply
from pricing import (
    DEFAULT_MODEL,
    cost_usd,
    estimate_tokens,
    estimated_cost_usd,
)
from providers import ProviderError, call_primary

# ---- config (env vars only; nothing secret is hardcoded) ----
PORT = int(os.environ.get("PORT", "3000"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get(
    "OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-admin-token")
DATABASE_PATH = os.environ.get(
    "DATABASE_PATH", os.path.join(os.path.dirname(__file__), "data", "gateway.db"))

if not os.environ.get("ADMIN_TOKEN"):
    print('[config] ADMIN_TOKEN not set — using dev default '
          '"dev-admin-token". Set it in production.')
if not OPENAI_API_KEY:
    print("[config] OPENAI_API_KEY not set — running in MOCK mode "
          "(fallback provider answers all calls).")

PROVIDER_MODE = "live" if OPENAI_API_KEY else "mock"
STARTED_AT = time.time()

# ---- state ----
# One threading.Lock per virtual key. The lock is held for the whole
# check -> provider call -> charge sequence for that key, so two
# concurrent requests on a near-exhausted key cannot both pass the
# budget check (see DECISIONS.md, "Concurrency"). Different keys run
# in parallel; the same key runs one request at a time.
_key_locks = {}
_key_locks_guard = threading.Lock()


def get_key_lock(key_id):
    """Return the lock for a key, creating it the first time."""
    with _key_locks_guard:
        lock = _key_locks.get(key_id)
        if lock is None:
            lock = threading.Lock()
            _key_locks[key_id] = lock
        return lock


db.init_db(DATABASE_PATH)

app = Flask(__name__)


# ---- small helpers ----

def new_id(prefix):
    """Random id like key_a1b2c3d4e5f6a7b8."""
    return f"{prefix}_{secrets.token_hex(8)}"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def sha256_hex(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def error_response(status, code, message, **extra):
    """One shape for every error: {"error": {"message", "type", "code", ...}}"""
    body = {"error": {"message": message, "type": code, "code": code}}
    body["error"].update(extra)
    return jsonify(body), status


def authenticate_gateway_key():
    """Check the x-api-key header. Returns (key_dict, error_reason).
    The presented key is hashed and looked up — the full key is never
    stored, so a stolen database does not hand out usable keys."""
    presented = request.headers.get("x-api-key")
    if not presented:
        return None, "missing"
    found = db.find_key_by_hash(DATABASE_PATH, sha256_hex(presented))
    return found, (None if found else "invalid")


def require_admin():
    """Check the x-admin-token header with a timing-safe compare, so
    the token cannot be guessed byte-by-byte. Returns an error
    response, or None when the caller is an admin."""
    presented = request.headers.get("x-admin-token", "")
    if not hmac.compare_digest(presented, ADMIN_TOKEN):
        return error_response(401, "unauthorized",
                              "Invalid or missing x-admin-token.")
    return None


def log_usage(key_id, model, provider, prompt_tokens, completion_tokens,
              cost, status):
    """Append one usage row to SQLite (synchronous — see DECISIONS.md)."""
    db.log_usage(
        DATABASE_PATH,
        log_id=new_id("usage"),
        key_id=key_id,
        model=model,
        provider=provider,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost_usd=cost,
        status=status,
        created_at=now_iso(),
    )


# ---- routes ----

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "provider": "openai" if OPENAI_API_KEY else "mock",
        "mode": PROVIDER_MODE,
        "default_model": DEFAULT_MODEL,
        "uptime_seconds": int(time.time() - STARTED_AT),
    })


@app.route("/", methods=["GET"])
def home():
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>RentOk LLM Gateway</title></head>
<body style="font-family:system-ui;max-width:720px;margin:40px auto;padding:0 16px">
<h1>RentOk LLM Gateway</h1>
<p>Minimal LLM gateway: virtual API keys, per-key budgets, usage logging, provider fallback. Non-streaming only.</p>
<p><a href="/health">GET /health</a> — status and provider mode.</p>
<h2>Try it</h2>
<pre>curl -X POST http://localhost:{PORT}/admin/keys \\
  -H "x-admin-token: dev-admin-token" \\
  -H "Content-Type: application/json" \\
  -d '{{"name":"demo","budget_usd":1}}'</pre>
<pre>curl -X POST http://localhost:{PORT}/v1/chat/completions \\
  -H "x-api-key: &lt;key from above&gt;" \\
  -H "Content-Type: application/json" \\
  -d '{{"model":"gpt-4o-mini","messages":[{{"role":"user","content":"Hello!"}}]}}'</pre>
<pre>curl "http://localhost:{PORT}/usage?key=&lt;key&gt;"</pre>
<p>Full docs: README.md, design reasoning: DECISIONS.md.</p>
</body></html>"""


# ---- admin: virtual keys ----

@app.route("/admin/keys", methods=["POST"])
def create_virtual_key():
    admin_error = require_admin()
    if admin_error:
        return admin_error
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if not name or not isinstance(name, str):
        return error_response(400, "invalid_request",
                              'Field "name" (string) is required.')
    budget = data.get("budget_usd", 1.0)
    try:
        budget = float(budget)
    except (TypeError, ValueError):
        return error_response(400, "invalid_request",
                              'Field "budget_usd" must be a positive number.')
    if budget <= 0:
        return error_response(400, "invalid_request",
                              'Field "budget_usd" must be a positive number.')
    models = data.get("models")
    if models is not None and (not isinstance(models, list)
                               or any(not isinstance(m, str) for m in models)):
        return error_response(400, "invalid_request",
                              'Field "models" must be an array of model name strings.')

    # The full key is generated here, shown ONCE in this response, and
    # never stored. Only its SHA-256 hash is saved in SQLite.
    full_key = f"rk_live_{secrets.token_hex(16)}"
    record = {
        "id": new_id("key"),
        "name": name,
        "key_prefix": full_key[:12],
        "budget_usd": budget,
        "models": models,
        "created_at": now_iso(),
    }
    db.create_key(DATABASE_PATH, record["id"], name, sha256_hex(full_key),
                  record["key_prefix"], budget, models, record["created_at"])
    return jsonify({
        "id": record["id"],
        "name": name,
        "api_key": full_key,
        "key_prefix": record["key_prefix"],
        "budget_usd": budget,
        "models": models,
        "created_at": record["created_at"],
    }), 201


@app.route("/admin/keys", methods=["GET"])
def list_virtual_keys():
    admin_error = require_admin()
    if admin_error:
        return admin_error
    keys = db.list_keys(DATABASE_PATH)
    return jsonify({
        "keys": [db.public_key(k, db.spent_usd(DATABASE_PATH, k["id"]))
                 for k in keys],
    })


# ---- usage ----

@app.route("/usage", methods=["GET"])
def usage():
    presented = request.args.get("key")
    if not presented:
        return error_response(400, "invalid_request",
                              'Query param "key" (full gateway key or key id) is required.')
    # Accept the full key (hashed lookup) or a bare key id.
    key = db.find_key_by_hash(DATABASE_PATH, sha256_hex(presented))
    if key is None:
        key = db.find_key_by_id(DATABASE_PATH, presented)
    if key is None:
        return error_response(404, "key_not_found",
                              "No key matches the given key or key id.")
    rows = db.usage_for_key(DATABASE_PATH, key["id"])
    ok_rows = [r for r in rows if r["status"] == "success"]
    spent = db.spent_usd(DATABASE_PATH, key["id"])
    return jsonify({
        "key_id": key["id"],
        "name": key["name"],
        "budget_usd": key["budget_usd"],
        "spent_usd": round(spent, 6),
        "remaining_usd": round(max(0.0, key["budget_usd"] - spent), 6),
        "requests": len(ok_rows),
        "rejected_requests": len([r for r in rows if r["status"] == "rejected"]),
        "tokens_in": sum(r["prompt_tokens"] for r in ok_rows),
        "tokens_out": sum(r["completion_tokens"] for r in ok_rows),
        "total_tokens": sum(r["total_tokens"] for r in ok_rows),
    })


# ---- chat completions (the gateway itself) ----

@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    key, reason = authenticate_gateway_key()
    if key is None:
        message = ("Missing x-api-key header." if reason == "missing"
                   else "Invalid x-api-key.")
        return error_response(401, "invalid_api_key", message)

    data = request.get_json(silent=True) or {}
    model = data.get("model", DEFAULT_MODEL)
    messages = data.get("messages")
    if (not isinstance(messages, list) or len(messages) == 0
            or any(not isinstance(m, dict)
                   or not isinstance(m.get("content"), str) for m in messages)):
        return error_response(400, "invalid_request",
                              'Field "messages" must be a non-empty array of '
                              "{role, content} objects.")
    if key["models"] is not None and model not in key["models"]:
        return error_response(403, "model_not_allowed",
                              f'Key {key["key_prefix"]}… is not allowed to use '
                              f'model "{model}".')

    # Everything from here on runs under the per-key lock: the budget
    # check, the provider call, and the charge are one serialized unit,
    # so two concurrent requests on a near-exhausted key cannot both pass.
    lock = get_key_lock(key["id"])
    with lock:
        prompt_text = "\n".join(
            f'{m.get("role", "")}: {m.get("content", "")}' for m in messages)
        prompt_tokens = estimate_tokens(prompt_text)
        estimated_cost = estimated_cost_usd(model, prompt_tokens)
        spent = db.spent_usd(DATABASE_PATH, key["id"])

        # Pre-flight budget check — BEFORE any provider call, so a
        # blocked request costs the caller (and the provider) nothing.
        if spent + estimated_cost > key["budget_usd"]:
            log_usage(key["id"], model, "none", prompt_tokens, 0, 0.0,
                      "rejected")
            return error_response(
                402, "budget_exceeded",
                "Budget exceeded for this key. Create a key with a higher "
                "budget or wait for a reset.",
                spent_usd=round(spent, 6),
                budget_usd=key["budget_usd"],
                estimated_cost_usd=round(estimated_cost, 6))

        # ---- provider (primary, then fallback) ----
        content = None
        completion_tokens = 0
        actual_prompt_tokens = prompt_tokens
        provider = "openai"
        fallback = False

        try:
            if model == "mock" or not OPENAI_API_KEY:
                # No provider key, or the caller explicitly asked for
                # the mock. An explicit mock request is not a "fallback".
                content = mock_reply(messages)
                completion_tokens = estimate_tokens(content)
                provider = "mock"
                fallback = model != "mock"
            else:
                try:
                    provider_data = call_primary(model, messages,
                                                 OPENAI_BASE_URL, OPENAI_API_KEY)
                    choices = provider_data.get("choices") or []
                    first = choices[0] if choices else {}
                    content = (first.get("message") or {}).get("content", "")
                    provider_usage = provider_data.get("usage") or {}
                    actual_prompt_tokens = provider_usage.get(
                        "prompt_tokens", prompt_tokens)
                    completion_tokens = provider_usage.get(
                        "completion_tokens", estimate_tokens(content))
                except ProviderError as err:
                    # Primary failed after its retry: fall back to the
                    # mock rather than failing the caller. The response
                    # says so explicitly.
                    print(f"[fallback] primary failed ({err}) — using mock")
                    content = mock_reply(messages)
                    completion_tokens = estimate_tokens(content)
                    provider = "mock"
                    fallback = True
        except Exception as err:  # only reachable if the mock itself throws
            print(f"[error] provider + fallback failed: {err}")
            log_usage(key["id"], model, "none", prompt_tokens, 0, 0.0, "error")
            return error_response(502, "provider_unavailable",
                                  "Upstream provider and fallback both failed. "
                                  "Try again shortly.")

        # ---- charge actual cost + log ----
        # Charge at the *requested* model's rate, even on mock fallback
        # (see pricing.py) — otherwise fallback could dodge the budget.
        actual_cost = cost_usd(model, actual_prompt_tokens, completion_tokens)
        log_usage(key["id"], model, provider, actual_prompt_tokens,
                  completion_tokens, actual_cost, "success")

        return jsonify({
            "id": f"chatcmpl-{secrets.token_hex(8)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": actual_prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": actual_prompt_tokens + completion_tokens,
            },
            # Gateway additions (not part of the OpenAI schema): callers
            # can see what actually answered and what it cost them.
            "provider": provider,
            "fallback": fallback,
            "cost_usd": round(actual_cost, 6),
        })


# ---- fallback handlers ----

@app.errorhandler(404)
def not_found(err):
    return error_response(404, "not_found",
                          f"No route {request.method} {request.path}.")


@app.errorhandler(500)
def internal_error(err):
    print(f"[error] {err}")
    return error_response(500, "internal_error", "Internal server error.")


if __name__ == "__main__":
    print(f"RentOk LLM Gateway listening on http://localhost:{PORT} "
          f"(mode={PROVIDER_MODE}, db={DATABASE_PATH})")
    # threaded=True so concurrent requests on different keys run in
    # parallel (the per-key lock only serialises the same key).
    app.run(host="0.0.0.0", port=PORT, threaded=True)
