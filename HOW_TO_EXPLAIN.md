# HOW TO EXPLAIN — 2-minute walkthrough for the follow-up call

Read this out loud once or twice before the call. Every paragraph is
one idea, in plain English. If you forget a detail, say the idea —
not the exact words.

## The 30-second version

"I built a small gateway in Python with Flask and SQLite. A caller
sends a chat request with a virtual key I issued. The gateway checks
the key, checks that the key still has budget left, forwards the
request to the real LLM provider, and logs what it cost in SQLite.
If the provider fails, it retries once and then answers with a
clearly-labelled mock reply instead of crashing. If the key is out of
budget, it refuses with a 402 before spending anything."

## One paragraph per piece

**Flask routes (`app.py`).** "Flask is just Python functions, one per
endpoint. `POST /v1/chat/completions` is the main one — it takes the
same JSON OpenAI takes, `model` and `messages`. `POST /admin/keys`
creates a virtual key, `GET /usage` shows spend, `GET /health` says
whether the server is up and whether it is in live or mock mode."

**SQL tables (`db.py`).** "Two tables in SQLite. `api_keys` has one row
per virtual key — id, name, the SHA-256 hash of the key, the first 12
characters as a prefix, the budget, and which models it may use. The
full key is never stored. `usage_logs` has one row per request — key
id, model, provider, tokens in, tokens out, total tokens, cost, status,
and time. Spend is one query: `SELECT SUM(cost_usd) FROM usage_logs
WHERE key_id = ? AND status = 'success'`."

**Budget check.** "Before I call the provider, I read how much the key
has already spent, estimate this request — prompt tokens as characters
divided by 4, plus 64 tokens reserved for the reply — and if spent plus
estimate is more than the budget, I return 402 and log a rejected row
that costs zero. No provider call happens, so nothing is spent. After
the provider answers, I charge the real cost from the provider's token
counts and write the usage row."

**Concurrency.** "If two requests on the same key arrive together, both
could see budget left and both could pass — that is the race. I stop it
with one lock per key: the whole check, provider call and charge runs
inside the lock, so the second request waits and then re-reads the
spend. I tested it — two parallel calls on a nearly-empty key, exactly
one succeeded. Different keys have different locks, so they still run
in parallel. This only works in one process; with two servers I would
do the check inside a database transaction instead."

**Fallback.** "The provider is called with a 10-second timeout. If it
returns a 5xx or times out, I retry once — a 4xx I do not retry, because
it will fail the same way twice. If it still fails, or if no provider
key is set at all, I answer with the mock provider. The reply says in
plain text that no real model answered, and the JSON has
`provider: "mock"` and `fallback: true`, so nobody mistakes it for a
real answer. I still charge the requested model's price, otherwise the
budget could be dodged by forcing a fallback."

**Why Python + SQLite.** "I picked Python and Flask because I can
explain every line, and plain SQLite because the SQL is visible — no
ORM hiding the queries. The brief says don't ship anything you can't
explain, so the stack I can defend beats a fancier one I can't."

**Why non-streaming.** "Streaming would force me to charge on an
estimate, because the final token count only exists when the reply
finishes. Non-streaming means I charge the real cost, once, after the
call."

## Likely questions and short answers

**"Why enforce the budget at the gateway instead of trusting callers?"**
"Because the gateway is the only place that sees every request and the
real token counts, and the only place that can say no before money is
spent. A caller can lie, forget, or have a bug that loops — the
provider will still bill us."

**"What happens if the provider is down?"**
"One retry on 5xx/timeout, then the mock fallback with a clear label.
Only if the mock itself fails do I return a 502."

**"Where does it break?"**
"One process, one SQLite file — it does not scale out, the database
resets on hosts with an ephemeral filesystem, and my price table goes
stale if OpenAI changes prices. Postgres plus a transactional budget
check is the first upgrade."

**"What did you deliberately not build?"**
"Streaming, caching, a smart router, and a frontend. The brief says a
rock-solid core beats a broken everything, and UI is not scored."

**"What are you least confident about?"**
"SQLite. It is perfect for this size, but it is the first thing I
would replace in production — Postgres, for the reasons above. I can
argue both sides; they are in DECISIONS.md."
