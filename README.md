# RentOk LLM Gateway — Python + Flask + SQLite

A minimal LLM gateway: virtual API keys, per-key budgets that actually
block, usage/spend logging, and provider fallback. Built for the RentOk
Backend Intern take-home assignment.

- **Stack:** Python 3.11+ (tested on 3.12.3), Flask 3.1.0, SQLite via the
  Python standard library (`sqlite3`) — no ORM, no database server.
- **Mode:** non-streaming only (see DECISIONS.md for why).
- **Provider:** OpenAI-compatible API when `OPENAI_API_KEY` is set,
  otherwise a built-in mock provider (so it works with no key at all).

## Quickstart

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python app.py
# Server starts on http://localhost:3000 (mode=mock if no provider key)
```

For production, use gunicorn (already in `requirements.txt`):

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4
```

## Environment variables

| Variable | Required | Default | What it is |
|---|---|---|---|
| `PORT` | No | `3000` | Port the server listens on |
| `OPENAI_API_KEY` | No | *(empty)* | Real provider key. Empty = mock mode |
| `OPENAI_BASE_URL` | No | `https://api.openai.com/v1` | OpenAI-compatible provider base URL |
| `ADMIN_TOKEN` | No* | `dev-admin-token` | Token for `/admin/*` endpoints |
| `DATABASE_PATH` | No | `data/gateway.db` | Where the SQLite file lives |

\* Change `ADMIN_TOKEN` before deploying — the dev default is only a
fallback so the service runs locally. See `.env.example`.

## Endpoints

| Method | Endpoint | Auth | What it does |
|---|---|---|---|
| `GET` | `/` | — | Tiny HTML page with curl examples |
| `GET` | `/health` | — | Status, provider mode, default model |
| `POST` | `/admin/keys` | `x-admin-token` | Create a virtual key (full key shown once) |
| `GET` | `/admin/keys` | `x-admin-token` | List keys (prefix only, never full keys) |
| `GET` | `/usage?key=<key>` | key or key id | Budget, spent, remaining, requests, tokens |
| `POST` | `/v1/chat/completions` | `x-api-key` | Proxy a chat completion (OpenAI-compatible) |

All errors use one shape: `{"error": {"message": ..., "type": ..., "code": ...}}`.

## Examples

### 1. Create a virtual key (budget $1.00)

```bash
curl -X POST http://localhost:3000/admin/keys \
  -H "x-admin-token: dev-admin-token" \
  -H "Content-Type: application/json" \
  -d '{"name":"demo","budget_usd":1}'
```

Response (save the `api_key` — it is shown **once**, only the hash is stored):

```json
{"id": "key_...", "name": "demo", "api_key": "rk_live_...",
 "key_prefix": "rk_live_...", "budget_usd": 1.0, "models": null,
 "created_at": "..."}
```

### 2. Call the gateway (mock mode — no provider key needed)

```bash
curl -X POST http://localhost:3000/v1/chat/completions \
  -H "x-api-key: rk_live_..." \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hello, what is 2+2?"}]}'
```

Response is OpenAI-shaped, plus three gateway fields (`provider`,
`fallback`, `cost_usd`):

```json
{"id": "chatcmpl-...", "object": "chat.completion", "created": 1794951926,
 "model": "gpt-4o-mini",
 "choices": [{"index": 0, "message": {"role": "assistant", "content": "Mock gateway response — ..."}, "finish_reason": "stop"}],
 "usage": {"prompt_tokens": 7, "completion_tokens": 52, "total_tokens": 59},
 "provider": "mock", "fallback": true, "cost_usd": 0.000032}
```

### 3. Check spend — "how much has key X spent?"

```bash
curl "http://localhost:3000/usage?key=rk_live_..."
```

```json
{"key_id": "key_...", "name": "demo", "budget_usd": 1.0,
 "spent_usd": 0.000032, "remaining_usd": 0.999968, "requests": 1,
 "rejected_requests": 0, "tokens_in": 7, "tokens_out": 52, "total_tokens": 59}
```

### 4. Budget-block demo

Create a key with a tiny budget (`$0.00008`), call it twice — the
third call is rejected with HTTP 402 and costs nothing:

```bash
curl -X POST http://localhost:3000/admin/keys \
  -H "x-admin-token: dev-admin-token" -H "Content-Type: application/json" \
  -d '{"name":"tiny","budget_usd":0.00008}'
# call /v1/chat/completions twice  -> 200, 200
# call it a third time            -> 402 {"error": {"code": "budget_exceeded", ...}}
```

## Project structure

```
app.py            Flask app: all routes, auth, budget check, provider + fallback
db.py             All SQL: tables, key lookup, usage logging, SELECT SUM(cost_usd)
pricing.py        Token estimate (~chars/4) + cost table (gpt-4o-mini, gpt-4o, mock)
providers.py      Real provider call (stdlib urllib, 10s timeout, 1 retry on 5xx/timeout)
mock.py           Built-in mock provider (deterministic, clearly labelled)
requirements.txt  Flask + gunicorn, pinned
.env.example      All env vars, placeholders only
DECISIONS.md      Design reasoning — the most important document
AI-LOG.md         How AI was used to build this
TESTLOG.md        Real test commands and outputs
```

## Deploy notes

- Set `OPENAI_API_KEY` and a long random `ADMIN_TOKEN` as environment
  variables on the host (Render / Railway / Fly.io / any VM). Never
  commit a real `.env` — it is in `.gitignore`.
- The SQLite file lives at `DATABASE_PATH`. On a host with an
  ephemeral filesystem it resets on redeploy — acceptable for this
  take-home; DECISIONS.md explains the Postgres upgrade path.
- Start command: `gunicorn app:app --bind 0.0.0.0:$PORT`.
