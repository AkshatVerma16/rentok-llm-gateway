"""All database work for the gateway — plain SQL, no ORM.

Why plain sqlite3 and not SQLAlchemy / Postgres:
- The brief allows any datastore, and SQLite is a real SQL database
  (a file on disk) with zero setup: no server to install, no password,
  no native build step. `pip install -r requirements.txt` is enough.
- Every query is a visible SQL string, so Akshat can explain it:
  CREATE TABLE, INSERT, SELECT SUM(cost_usd).
- Tradeoff: one file, one machine, one writer at a time. That is fine
  for a take-home gateway and is listed in DECISIONS.md.

Tables:
  api_keys   — one row per virtual key. Only the SHA-256 hash of the
               key is stored; the full key is shown once at creation
               and never stored anywhere.
  usage_logs — one row per request (including rejected ones, which
               cost $0). Spend is always SELECT SUM(cost_usd) over
               rows with status = 'success'.
"""

import json
import sqlite3

# --- SQL statements, written out so they can be read and explained ---

CREATE_API_KEYS_SQL = """
CREATE TABLE IF NOT EXISTS api_keys (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    key_hash    TEXT NOT NULL UNIQUE,
    key_prefix  TEXT NOT NULL,
    budget_usd  REAL NOT NULL,
    models      TEXT,              -- JSON list of allowed models, or NULL = any
    created_at  TEXT NOT NULL
)
"""

CREATE_USAGE_LOGS_SQL = """
CREATE TABLE IF NOT EXISTS usage_logs (
    id                 TEXT PRIMARY KEY,
    key_id             TEXT NOT NULL,
    model              TEXT NOT NULL,
    provider           TEXT NOT NULL,   -- 'openai', 'mock' or 'none'
    prompt_tokens      INTEGER NOT NULL,
    completion_tokens  INTEGER NOT NULL,
    total_tokens       INTEGER NOT NULL,
    cost_usd           REAL NOT NULL,
    status             TEXT NOT NULL,   -- 'success', 'rejected' or 'error'
    created_at         TEXT NOT NULL
)
"""

INSERT_KEY_SQL = """
INSERT INTO api_keys (id, name, key_hash, key_prefix, budget_usd, models, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

FIND_KEY_BY_HASH_SQL = "SELECT * FROM api_keys WHERE key_hash = ?"
FIND_KEY_BY_ID_SQL = "SELECT * FROM api_keys WHERE id = ?"
LIST_KEYS_SQL = "SELECT * FROM api_keys ORDER BY created_at"

INSERT_USAGE_SQL = """
INSERT INTO usage_logs
    (id, key_id, model, provider, prompt_tokens, completion_tokens,
     total_tokens, cost_usd, status, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# The spend question — "how much has key X spent?" — is this one query.
# Only 'success' rows count: rejected and error rows cost $0.
SPENT_SQL = """
SELECT COALESCE(SUM(cost_usd), 0) FROM usage_logs
WHERE key_id = ? AND status = 'success'
"""

USAGE_FOR_KEY_SQL = "SELECT * FROM usage_logs WHERE key_id = ? ORDER BY created_at"


def connect(db_path):
    """Open a new SQLite connection. One connection per call keeps this
    simple and thread-safe (Flask runs threaded)."""
    import os
    # SQLite cannot create the file if its folder does not exist yet.
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["id"]
    return conn


def init_db(db_path):
    """Create both tables if they do not exist yet."""
    with connect(db_path) as conn:
        conn.execute(CREATE_API_KEYS_SQL)
        conn.execute(CREATE_USAGE_LOGS_SQL)


def _row_to_key(row):
    """Turn a database row into a plain dict, decoding the JSON models list."""
    if row is None:
        return None
    key = dict(row)
    key["models"] = json.loads(key["models"]) if key["models"] else None
    return key


def create_key(db_path, key_id, name, key_hash, key_prefix, budget_usd, models, created_at):
    """Insert one virtual key. `models` is a list of model names or None."""
    models_json = json.dumps(models) if models else None
    with connect(db_path) as conn:
        conn.execute(INSERT_KEY_SQL,
                     (key_id, name, key_hash, key_prefix, budget_usd,
                      models_json, created_at))


def find_key_by_hash(db_path, key_hash):
    with connect(db_path) as conn:
        row = conn.execute(FIND_KEY_BY_HASH_SQL, (key_hash,)).fetchone()
    return _row_to_key(row)


def find_key_by_id(db_path, key_id):
    with connect(db_path) as conn:
        row = conn.execute(FIND_KEY_BY_ID_SQL, (key_id,)).fetchone()
    return _row_to_key(row)


def list_keys(db_path):
    with connect(db_path) as conn:
        rows = conn.execute(LIST_KEYS_SQL).fetchall()
    return [_row_to_key(row) for row in rows]


def log_usage(db_path, log_id, key_id, model, provider, prompt_tokens,
              completion_tokens, total_tokens, cost_usd, status, created_at):
    """Append one usage row. Logging is synchronous (the request waits
    for it) so a response is never sent for a request that was not
    logged — see DECISIONS.md for the tradeoff."""
    with connect(db_path) as conn:
        conn.execute(INSERT_USAGE_SQL,
                     (log_id, key_id, model, provider, prompt_tokens,
                      completion_tokens, total_tokens, cost_usd, status,
                      created_at))


def spent_usd(db_path, key_id):
    """Total actually charged to a key, in USD."""
    with connect(db_path) as conn:
        row = conn.execute(SPENT_SQL, (key_id,)).fetchone()
    return float(row[0])


def usage_for_key(db_path, key_id):
    with connect(db_path) as conn:
        rows = conn.execute(USAGE_FOR_KEY_SQL, (key_id,)).fetchall()
    return [dict(row) for row in rows]


def public_key(key, spent):
    """Public view of a key: never includes the hash or the full key."""
    return {
        "id": key["id"],
        "name": key["name"],
        "key_prefix": key["key_prefix"],
        "budget_usd": key["budget_usd"],
        "spent_usd": round(spent, 6),
        "models": key["models"],
        "created_at": key["created_at"],
    }
