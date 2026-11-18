# DECISIONS.md — RentOk LLM Gateway

## What I built (in 3–4 sentences)

A small Python + Flask + SQLite HTTP service that sits between callers
and an LLM provider. Callers authenticate with a gateway-issued virtual
key (never the real provider key); each key has a dollar budget, and the
gateway refuses — with HTTP 402 — any request that would go over it,
before calling the provider. Every request is logged to SQLite
(key, model, tokens in/out, cost, timestamp), so `/usage` can answer
"how much has key X spent?", and when the primary provider fails the
gateway retries once and then falls back to a clearly-labelled mock
provider instead of failing the caller.

## Moving parts & request lifecycle

Moving parts (5 small files, each one job):

| Piece | File | Job |
|---|---|---|
| Routes + budget logic | `app.py` | Flask endpoints, auth, budget check, charge |
| Database | `db.py` | All SQL: keys table, usage table, spend query |
| Pricing | `pricing.py` | Token estimate + cost table |
| Real provider | `providers.py` | OpenAI-compatible HTTP call, 10s timeout, 1 retry |
| Mock provider | `mock.py` | Deterministic fallback reply |

One request, end to end:

```
Client
  |  POST /v1/chat/completions, header x-api-key: rk_live_...
  v
Gateway (app.py)
  |  1. Auth: hash the presented key (SHA-256), look it up in SQLite.
  |     Missing/invalid key -> 401, stop here.
  |  2. Take the per-key lock (threading.Lock for this key id).
  |  3. Budget pre-check: spent = SELECT SUM(cost_usd) WHERE key_id
  |     AND status='success'. Estimate this request's cost from the
  |     prompt (~chars/4 tokens) + 64 reserved completion tokens.
  |     spent + estimate > budget -> log a 'rejected' row ($0) and
  |     return 402. No provider call is made.
  |  4. Provider: POST to {OPENAI_BASE_URL}/chat/completions with the
  |     real provider key in a server-side Authorization header.
  |     1 retry, only on 5xx/timeout. If it still fails (or no provider
  |     key is configured) -> mock provider.
  |  5. Charge + log: cost from the provider's real token counts
  |     (or the estimate, for the mock), INSERT one row into
  |     usage_logs with status='success'. Release the lock.
  v
Client <- OpenAI-shaped JSON + provider / fallback / cost_usd
```

The lock is held for steps 2–5, so the check and the charge are one
unit — a second request on the same key waits instead of sneaking past.

## My 5 most important decisions

### 1. Python + Flask + SQLite, plain `sqlite3` — no ORM, no framework magic
- **Options:** (a) Node + Express (my first attempt), (b) Python + Flask
  + SQLite, (c) Java + Spring Boot + Postgres, (d) Python + FastAPI +
  SQLAlchemy.
- **Picked:** (b).
- **Why:** I am strongest in Python and SQL, and the brief says "don't
  ship anything you can't explain" — I can explain every line of this
  code and every SQL query on a follow-up call. Flask is just Python
  functions for each endpoint. Plain `sqlite3` means the SQL is visible
  (`CREATE TABLE`, `INSERT`, `SELECT SUM(cost_usd)`), which is exactly
  the skill the gateway's logging is built on. SQLite needs no server,
  no password, no native build step, so `pip install` + `python app.py`
  works anywhere.
- **Tradeoff I accept:** SQLite is one file on one machine — no
  replication, one writer at a time, and it resets on hosts with an
  ephemeral filesystem. For a take-home that is fine; Postgres is the
  obvious upgrade (see "Where it breaks"). Java/Spring would have been
  more "enterprise" but far more boilerplate, slower to deploy, and
  harder for me to defend line by line.

### 2. Non-streaming only
- **Options:** streaming (SSE) or non-streaming (one JSON response).
- **Picked:** non-streaming.
- **Why:** the gateway must charge the *actual* cost, and actual token
  counts only exist when the provider finishes. Streaming would force
  me to charge on an estimate and then correct it later — or worse,
  stream tokens to a caller whose budget runs out mid-reply. A single
  JSON response keeps the budget check honest: estimate before, charge
  actual after, one log row.
- **Tradeoff:** callers wait for the full reply instead of seeing
  tokens as they arrive. For a minimal gateway that is the right cut;
  streaming is a "one more week" item.

### 3. Budget in USD, charged at the *requested* model's rate — even on mock fallback
- **Options:** cap by request count, by tokens, or by dollars.
- **Picked:** dollars, priced from a small table (gpt-4o-mini
  $0.15/1M in, $0.60/1M out; gpt-4o $2.50/$10.00).
- **Why:** dollars are what a team actually cares about — a token cap
  means nothing if the model changes price. And charging the requested
  model's rate even when the mock answers matters: if fallback were
  free, a caller could dodge their budget by forcing a fallback, and
  the budget could never be exhausted in mock mode (the 402 path
  would be untestable). Only an explicit `model: "mock"` request is $0.
- **Tradeoff:** the price table is a snapshot — if OpenAI changes
  prices, my costs drift from real spend. The pre-flight estimate also
  reserves only 64 completion tokens, so a very long reply can push a
  key slightly over budget before the *next* request blocks it. The
  budget is never silently exceeded by a large amount, but it is not
  a hard real-time cap on a single reply's length.

### 4. Synchronous logging (log before responding)
- **Options:** (a) write the usage row to SQLite inside the request
  (sync), (b) queue it and log in the background (async).
- **Picked:** sync.
- **Why:** correctness beats speed here. With sync logging, a response
  is never sent for a request that was not logged — the `/usage`
  numbers always match what the caller saw. Async logging is faster
  under load, but a crash or a dropped queue entry would silently
  under-count spend, which is exactly the failure a budget system
  must not have.
- **Tradeoff:** each request pays one SQLite write (~milliseconds).
  Under heavy load this becomes the bottleneck; the fix is a
  write-ahead log + a durable queue, not skipping the log.

### 5. Fallback to an *obviously fake* mock, not a silent second model
- **Options:** (a) retry only, then fail fast with 502, (b) fall back
  to a second real provider, (c) fall back to a local/mock model.
- **Picked:** (c), with one retry first, and the mock reply says in
  plain text that no real LLM answered (plus `provider: "mock"` and
  `fallback: true` in the JSON).
- **Why:** a second real provider needs a second paid key I do not
  have, and failing fast on every provider blip makes the gateway
  feel broken. The mock keeps callers working through an outage, and
  because it is clearly labelled, nobody mistakes it for a real
  answer — a fallback that pretends to be real is worse than an error.
- **Tradeoff:** during an outage, callers get a useless echo instead
  of a real answer. That is honest, but it is not a real fallback for
  production traffic — a second provider or a local model (Ollama)
  would be.

## First-principles question: why enforce budgets at the gateway instead of trusting callers?

Because the gateway is the only place that can see the truth. A
caller can lie about how much they spent, forget to count, or simply
have a bug that sends the same prompt in a loop — and the provider
will happily bill the account owner for all of it. The provider key
belongs to the team, not to the caller, so the team is the one who
pays. The gateway sits between the caller and the provider key, so it
is the only component that (a) knows every request across all callers,
(b) knows the real token counts the provider reports, and (c) can
actually say no before money is spent. Trusting callers would mean
the budget is a suggestion, not a control. Enforcing it at the
gateway turns it into a control: one place, one set of numbers, one
place to say no.

## Concurrency: what happens if two requests on the same near-exhausted key hit at once?

They cannot both pass. Each key has its own `threading.Lock`, and the
whole sequence — budget check, provider call, charge, log — runs
inside that lock for that key. The second request waits, then re-reads
the spend (which now includes the first request's charge) and is
rejected with 402 if there is no room left.

I tested this (see TESTLOG.md): a key with $0.00008 budget, one
warm-up call, then two parallel calls — exactly one returned 200 and
one returned 402. Requests on *different* keys are not affected; they
run in parallel, because they have different locks.

What I knowingly did *not* handle: this lock only works inside one
process. If the gateway ran on two servers (or two gunicorn workers
with separate memory), two requests could still race — each process
has its own locks. The real fix is a database-level atomic
check-and-reserve (e.g. `UPDATE ... WHERE spent + cost <= budget`
inside a transaction, or a Postgres row lock). For a single-process
take-home, the in-process lock is the honest, sufficient answer.

## Fallback policy (and why)

1. **Retry once, only on 5xx or timeout.** These are the failures
   that are likely transient — a 4xx (bad model, bad request, bad
   provider key) will fail the same way twice, so retrying it only
   burns time. 300ms backoff before the retry.
2. **Then fall back to the mock provider.** The caller still gets a
   200 with a clearly-labelled mock reply, charged at the requested
   model's rate (so the budget still applies).
3. **Fail fast with 502 only if the mock itself fails** — which in
   practice cannot happen, since the mock is pure string code. A 502
   here means something is deeply wrong, and pretending otherwise
   would be dishonest.

Timeout: 10 seconds per provider attempt. A provider that hangs
forever is worse than a provider that errors — it holds the per-key
lock and blocks that key's other requests.

## What I deliberately did NOT build (and why)

- **No streaming.** See Decision 2 — it would break honest charging.
- **No semantic caching.** It saves money on repeated prompts, but it
  adds a similarity threshold, a cache store, and a whole new class of
  correctness questions ("is this cached answer still right?"). The
  core had to be rock-solid first.
- **No smart router (cheap vs. capable model per request).** Same
  reason — routing policy is a product decision, and a wrong router
  silently gives a hard question to a cheap model. One default model
  (gpt-4o-mini) is enough for the brief.
- **No polished frontend / admin dashboard.** The brief says "we are
  not scoring UI" — a plain HTML home page with curl examples and
  `GET /usage` is plenty. Zero hours on design.
- **No multi-tenant auth, roles, or per-user dashboards.** Simple
  virtual keys + one admin token is what the brief asks for.
- **No tests suite (pytest).** I tested the real server end to end
  with curl and a small concurrency script instead (TESTLOG.md), and
  I named the missing suite here rather than pretending it exists.
  A pytest suite around the budget check is the first test I would add.
- **No stretch goal (Ollama / router / caching).** The brief says a
  rock-solid core with no stretch beats a broken everything.

## The one decision I am least confident about

**SQLite as the datastore** — and I can argue both sides.

*For SQLite:* zero setup, real SQL, the whole gateway runs with one
`pip install`, and the data model (two tables, a handful of rows per
test) genuinely fits in a file. For a weekend take-home, a Postgres
server would have been setup overhead with no benefit to the demo.

*Against SQLite:* it is the first thing that breaks in production.
One file, one machine, one writer at a time; on a free-tier host with
an ephemeral filesystem the usage history vanishes on every redeploy;
and my per-key lock (Decision: concurrency) does not work across
processes, so scaling out means re-doing the budget check in the
database. If this gateway were going to serve a real team tomorrow,
Postgres + a transactional check-and-reserve would be the right call,
and I would make that switch first.

I kept SQLite because the brief rewards a small thing that fully
works over a big thing that half-works — but this is the decision I
would most expect to be challenged on, and the challenge would be fair.

## Where it breaks, and what I would do with one more week

- **Scale:** one process, one SQLite file, sync logging. Under real
  load the per-key lock serialises each key and SQLite serialises
  writes. Fix: Postgres, a transactional budget reserve, async
  logging to a durable queue, and a Redis lock if I ever run two
  processes.
- **Prices go stale:** the cost table is hardcoded. Fix: fetch prices
  from the provider, or at least read them from config, and record
  the price-table version on each usage row.
- **Token estimates:** `len(text)/4` is rough — code, Hindi/Hinglish,
  and non-English text tokenise very differently. Fix: use a real
  tokenizer (tiktoken) for estimates, and always prefer the
  provider's reported counts.
- **No streaming, no cache, no router:** the three cuts above, in
  that order — streaming first (callers expect it), then a cheap
  exact-match cache for repeated prompts, then a router.
- **Tests:** add a pytest suite — budget-block, concurrency, and
  fallback tests as real tests, not just the manual TESTLOG.
- **Security hardening:** rate-limit the admin endpoints, rotate the
  admin token, give keys an expiry date, and add per-key daily caps
  on top of the total budget.
