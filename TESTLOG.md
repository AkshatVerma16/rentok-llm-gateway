# TESTLOG — real local test runs

Date: 17 Nov 2026 (evening, IST). Machine: Python 3.12.3, Flask 3.1.0.
Full gateway keys are redacted below — only the `rk_live_…` prefix is shown.
No provider key was set, so tests 1–6 ran in mock mode; test 7 proves
the fallback path with a live-mode configuration.

## Setup (from a clean folder)

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python -c "import flask; print(flask.__version__)"
# Flask 3.1.0, Python 3.12.3
```

## Bug found on first run — fixed before testing

First `python app.py` crashed immediately:

```
sqlite3.OperationalError: unable to open database file
```

Cause: `db.py` created the tables but never created the `data/`
folder, and SQLite cannot create a file in a missing folder. Fix:
`db.py connect()` now does `os.makedirs(parent, exist_ok=True)`
before opening the database. Re-ran from a clean folder — server
started.

## Test 1 — Health check — PASS

```bash
curl -s http://localhost:3000/health
```

```json
{"default_model": "gpt-4o-mini", "mode": "mock", "provider": "mock", "status": "ok", "uptime_seconds": 2}
```

Home page (`GET /`) also returned the plain HTML page with curl examples.

## Test 2 — Create a key + chat call + usage — PASS

```bash
curl -s -X POST http://localhost:3000/admin/keys \
  -H "x-admin-token: dev-admin-token" -H "Content-Type: application/json" \
  -d '{"name":"test-tiny","budget_usd":0.01}'
```

```json
{"api_key": "rk_live_d5ef… (redacted)", "budget_usd": 0.01,
 "created_at": "2026-11-17T21:45:23.604544+00:00",
 "id": "key_eddbbfbb30ba5768", "key_prefix": "rk_live_d5ef",
 "models": null, "name": "test-tiny"}
```

```bash
curl -s -X POST http://localhost:3000/v1/chat/completions \
  -H "x-api-key: rk_live_d5ef…" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hello, what is 2+2?"}]}'
```

```json
{"choices": [{"finish_reason": "stop", "index": 0,
  "message": {"content": "Mock gateway response — no real LLM answered this request … You asked: \"Hello, what is 2+2?\". Configure OPENAI_API_KEY to get real model output.", "role": "assistant"}}],
 "cost_usd": 3.2e-05, "created": 1794951926, "fallback": true,
 "id": "chatcmpl-50661783d7a585b9", "model": "gpt-4o-mini",
 "object": "chat.completion", "provider": "mock",
 "usage": {"prompt_tokens": 7, "completion_tokens": 52, "total_tokens": 59}}
```

```bash
curl -s "http://localhost:3000/usage?key=rk_live_d5ef…"
```

```json
{"budget_usd": 0.01, "key_id": "key_eddbbfbb30ba5768", "name": "test-tiny",
 "rejected_requests": 0, "remaining_usd": 0.009968, "requests": 1,
 "spent_usd": 3.2e-05, "tokens_in": 7, "tokens_out": 52, "total_tokens": 59}
```

Admin without a token was also checked — `POST /admin/keys` with no
`x-admin-token` returned 401 `unauthorized`.

## Test 3 — Invalid / missing key → 401 — PASS

Missing `x-api-key` header:

```json
{"error": {"code": "invalid_api_key", "message": "Missing x-api-key header.", "type": "invalid_api_key"}}
```

Wrong key (`rk_live_wrongkey123`):

```json
{"error": {"code": "invalid_api_key", "message": "Invalid x-api-key.", "type": "invalid_api_key"}}
```

## Test 4 — Budget exhaustion → 402, spend stops — PASS

Key `test-exhaust`, budget `$0.00008` (prefix `rk_live_5b15`, id
`key_d06f4ee27cfde555`). Five identical chat calls:

| Call | HTTP | Result |
|---|---|---|
| 1 | 200 | mock, cost $0.000032 |
| 2 | 200 | mock, cost $0.000032 |
| 3 | 402 | `budget_exceeded` |
| 4 | 402 | `budget_exceeded` |
| 5 | 402 | `budget_exceeded` |

Usage after:

```json
{"budget_usd": 8e-05, "key_id": "key_d06f4ee27cfde555", "name": "test-exhaust",
 "rejected_requests": 3, "remaining_usd": 1.6e-05, "requests": 2,
 "spent_usd": 6.4e-05, "tokens_in": 14, "tokens_out": 104, "total_tokens": 118}
```

Spend stopped at $0.000064 (two successful calls) — the three 402s
were logged as `rejected` at $0 and charged nothing. The budget
actually blocks.

## Test 5 — Concurrency: two parallel calls, near-exhausted key — PASS

Key `test-concurrent`, budget `$0.00008` (id `key_7918012d5e8fc5ef`).
One warm-up call (200, spent $0.000032), then two calls in parallel
via `ThreadPoolExecutor`:

```
warm-up: 200
parallel results: [200, 402]
successes: 1  blocked (402): 1
usage: {"budget_usd": 8e-05, "key_id": "key_7918012d5e8fc5ef",
  "name": "test-concurrent", "rejected_requests": 1, "remaining_usd": 1.6e-05,
  "requests": 2, "spent_usd": 6.4e-05, "tokens_in": 14, "tokens_out": 104,
  "total_tokens": 118}
```

Exactly one of the two parallel calls succeeded — the per-key lock works.

## Test 6 — SQLite holds the truth — PASS

```bash
python3 -c "import sqlite3; c=sqlite3.connect('data/gateway.db'); \
  print(c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()); \
  print(c.execute(\"SELECT key_id, COUNT(*), SUM(cost_usd) FROM usage_logs WHERE status='success' GROUP BY key_id\").fetchall())"
```

```
tables: [('api_keys',), ('usage_logs',)]
spend per key: [('key_7918012d5e8fc5ef', 2, 6.45e-05),
                ('key_d06f4ee27cfde555', 2, 6.45e-05),
                ('key_eddbbfbb30ba5768', 1, 3.225e-05)]
```

(The API rounds spend to 6 decimals for display; the database holds
the full value, e.g. $0.00003225 per call. Only `key_hash` values are
stored for keys — no full key appears in the database.)

`GET /admin/keys` listed all three test keys with prefix and spend
only — no full keys.

## Test 7 — Fallback: live mode, dead provider → mock — PASS

Second server on port 3001 with a fake provider key and an unreachable
provider URL, to prove the retry-then-fallback path:

```bash
PORT=3001 OPENAI_API_KEY=fake-key-for-testing \
  OPENAI_BASE_URL=http://127.0.0.1:9/v1 \
  DATABASE_PATH=/tmp/gateway-fallback.db venv/bin/python app.py
curl -s http://localhost:3001/health
# {"default_model": "gpt-4o-mini", "mode": "live", "provider": "openai", "status": "ok", "uptime_seconds": 2}
```

Chat call returned in 0.34s (connection refused → retry → fallback):

```json
{"choices": [{"message": {"content": "Mock gateway response — no real LLM answered this request …", "role": "assistant"}}],
 "cost_usd": 3.2e-05, "fallback": true, "model": "gpt-4o-mini",
 "provider": "mock",
 "usage": {"prompt_tokens": 7, "completion_tokens": 52, "total_tokens": 59}}
```

The gateway did not hang, did not crash, and did not leak the fake
provider key. The response says clearly that it is a mock fallback.

## Summary

| Case | Result |
|---|---|
| Health + home page | PASS |
| Create key, chat call, usage | PASS |
| Missing / invalid key → 401 | PASS |
| Admin without token → 401 | PASS |
| Budget exhaustion → 402, spend stops | PASS |
| Two parallel calls, near-exhausted key → 1×200 + 1×402 | PASS |
| SQLite tables + SUM(cost_usd) spend | PASS |
| Live mode, dead provider → labelled mock fallback | PASS |
| Demo-key seeding survives a restart with an empty DB | PASS |

## 18 Nov 2026 — demo keys seeded on boot (after two live failures)

On the live Render service the demo key (`rk_live_dd0b…`) and the
budget-test key (`rk_live_df77…`) both returned `key_not_found` —
the free host had restarted (uptime back to ~3 minutes) and wiped
the SQLite file. Fix: `seed_demo_keys()` in `app.py` re-creates the
two demo keys from env vars on every boot (`INSERT OR IGNORE` in
`db.ensure_key`).

Local test, 18 Nov 2026 (venv, fresh DB dirs, mock mode):
- Demo key chat → 200, cost $0.00003; usage spent $0.00003 /
  remaining $0.99997 — PASS
- Budget-test key → 200, 200, 402 — PASS
- Server killed, restarted with an EMPTY database directory:
  demo key chat → 200 again; budget-test key present with $0 spent
  — PASS (keys survive a restart; usage history resets, as documented)

Test databases were deleted after testing (`data/` and
`/tmp/gateway-fallback.db`), so the delivered project starts clean.
