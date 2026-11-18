# AI-LOG.md — How AI was used to build this gateway

## Which AI tools/models I used, and for what

- **ChatGPT — research first, before any code was written.**
  Before I asked Muse to build anything, I researched this
  assignment on ChatGPT: what an LLM gateway is, how the model /
  request flow works, what components it needs (virtual keys,
  budgets, usage logging, fallback), and what stack would fit.
  That research is how I decided what to build and what to ask for.
- **Claude AI — help during research and planning as well.**
  I also took help from Claude AI while understanding the
  assignment and the design, alongside ChatGPT.
- **Muse (Meta's personal AI agent, Muse Spark) — the main build tool.**
  After that research, I asked Muse to build it: reading the
  assignment PDF, planning the endpoints and the database tables,
  writing the first draft of the Python code, running the local
  tests, and writing DECISIONS.md and this file. It worked like a
  coding partner I could direct — I told it what to build and in
  which stack, and it wrote the code while I reviewed each piece.
- **No other AI tool was used for the code itself.** No separate
  code-generation tool, no AI search. The provider itself (OpenAI)
  was never called during the build — all testing used the built-in
  mock provider, because no provider key was available.

## What I did myself

The code was developed with Muse's assistance, that is fine to say — but 

I did a lot of the work myself, and I did not just accept the code
as it came:

- I researched the assignment myself first (ChatGPT and Claude,
  above) and decided the design and the stack before the build.
- I chose Python + Flask + SQLite myself, because that is the
  stack I know and can explain. I stopped the first Node.js
  build myself for that reason.
- I set the project up on my own Windows system on 18 Nov 2026:
  downloaded the project, created the virtual environment,
  installed the requirements, and ran `python app.py` myself.
- I tested it myself, step by step, on my own system: I opened
  the home page, created a demo key (`rk_live_ae5e…`, budget
  $1.00), made a chat call with it, and checked `/usage`.
  The call cost $0.00003 (3 tokens in, 49 out), and usage showed
  spent $0.00003, remaining $0.99997, 1 request. Only when all
  of that was OK on my system did I go ahead with the next step.
- I reviewed parts of the code myself, checked the files in the
  project folder, and asked for changes where something was not
  clear to me — for example, which files are actually used and
  which folders (`venv`, `data`, `__pycache__`) should not be
  uploaded.

## One place the AI was wrong or misleading, and how I caught it

The first version of the gateway was started in Node.js + Express,
and its `db` layer assumed the `data/` folder already existed. When
I rebuilt in Python, the same assumption came back: the first run of
`python app.py` crashed immediately with:

```
sqlite3.OperationalError: unable to open database file
```

The code had created the tables but never created the folder the
database file lives in. I caught it because I ran the server myself
instead of trusting that it worked — the crash was on the very first
`GET /health`. The fix was small and is in `db.py`: `connect()` now
creates the parent folder (`os.makedirs(parent, exist_ok=True)`)
before opening the database. Lesson: always run the thing from a
clean folder; "it worked on the last machine" is not a test.

A second, smaller one: the first draft of the pricing code charged
mock fallback replies at $0. That sounded right ("the mock is free")
but it was wrong — it would have let a caller dodge their budget by
forcing a fallback, and the 402 path could never be tested in mock
mode. I caught it while writing the budget test, because the
exhaustion test would never have reached 402. The fix (charge the
*requested* model's rate on fallback, $0 only for an explicit
`model: "mock"`) is in `pricing.py` and `app.py`.

## One place I overrode the AI's suggestion, and why

**I overrode the stack itself.** The AI's first build was Node.js +
Express with a JSON-file store — a reasonable, fast choice, and the
code worked. I stopped that build and asked for a rebuild in
Python + Flask + SQLite.

Why: I am good in Python, SQL and Java. The brief
says "don't ship anything you can't explain" and that I must be
ready to walk through any decision on a follow-up call. With Node I
would have had working code I could not confidently explain; with
Python + SQLite I can explain every endpoint, every SQL query, and
every budget decision in my own words. Explainability is what the
assignment scores, so the stack that I can defend beats the stack
that was built first. The Node version was left untouched (in
`../rentok-llm-gateway/`) as the record of that first attempt.

A second override, smaller: the AI suggested using SQLAlchemy to
make the database code shorter. I said no — plain `sqlite3` with
visible SQL strings. An ORM would have hidden the exact queries the
assignment wants me to be able to explain, and it would have been
one more thing I could not defend line by line.

## How I stayed in control of code I didn't type by hand

Especially the three dangerous places — secrets, budget logic, and
the provider call:

- **Secrets:** I checked that the provider key is read only from the
  `OPENAI_API_KEY` environment variable, is sent only as a server-side
  `Authorization` header, and never appears in a log, a response, or
  the database. `.env` is in `.gitignore` and only `.env.example`
  (placeholders) is in the repo. Gateway keys are stored only as
  SHA-256 hashes — I verified this by reading `db.py` and by checking
  the SQLite file directly: it contains hashes and prefixes, never a
  full key.
- **Budget logic:** I did not trust the code — I tested it. I created
  a $0.00008 key, called it until it returned 402, and checked that
  spend stopped increasing and `rejected_requests` went up (TESTLOG.md).
  I also ran two parallel calls on a near-exhausted key and confirmed
  exactly one succeeded. If the lock or the pre-check had been wrong,
  those tests would have shown it.
- **Provider call:** I read `providers.py` line by line: 10-second
  timeout, exactly one retry, only on 5xx/timeout, and the fallback
  to the mock. I tested the fallback for real by pointing the gateway
  at a dead provider URL with a fake key — it returned a labelled
  mock reply with `fallback: true` instead of hanging or crashing.
- **General rule I followed:** I read every file after it was
  written, ran the server from a clean folder, and hit every endpoint
  with curl myself before calling anything "done". On 18 Nov I did
  this again on my own Windows laptop — I set it up, ran it, created
  a key, made a chat call, and checked the usage myself, and only
  proceeded when everything matched. Anything I could not explain,
  I asked to have simplified or removed.

## Something I had to learn from scratch this weekend, and how

**Per-key concurrency — the check-then-act race.** I knew Python
threads, but I had never thought about what happens when two requests
check the same budget at the same time. The AI explained it with a
small example (two requests both see "$0.00004 left", both pass, the
key is overspent), and I learned the fix by building it: one
`threading.Lock` per key, held from the budget check through the
charge, so the second request re-reads the spend after the first one
is logged. I verified it with the parallel-call test rather than
taking the explanation on trust. I also learned its limit the same
way — the lock only works inside one process — which is why
DECISIONS.md names the database-transaction fix for a multi-process
setup instead of pretending the problem is fully solved.