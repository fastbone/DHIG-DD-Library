"""SQLite schema and access helpers.

Three things live here:

* ``documents`` — one row per file, carrying the manifest card once indexed.
* ``units`` + ``units_fts`` — the searchable atoms (a PDF page, a slide, a
  spreadsheet sheet), each with a citation anchor and a char range into the
  document's text mirror on disk.
* ``jobs`` / ``qa_log`` / ``scope_rounds`` / ``artifacts`` — operational and
  audit state.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from typing import Any, Iterable

from .config import settings

_local = threading.local()

# SQLite permits exactly one writer. Ingest is multi-threaded, so every write
# goes through this lock: without it, an explicit BEGIN in one thread races the
# autocommit writes in another and rows are silently lost. Reads (WAL snapshots)
# are not serialised.
_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id              TEXT PRIMARY KEY,
    sha256          TEXT NOT NULL,
    rel_path        TEXT NOT NULL,
    abs_path        TEXT NOT NULL,
    filename        TEXT NOT NULL,
    ext             TEXT NOT NULL,
    family          TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    mtime           REAL,
    status          TEXT NOT NULL DEFAULT 'pending',
    error           TEXT,
    n_units         INTEGER DEFAULT 0,
    n_chars         INTEGER DEFAULT 0,
    ocr_used        INTEGER DEFAULT 0,
    dupe_group      TEXT,
    title           TEXT,
    doc_type        TEXT,
    workstream      TEXT,
    parties         TEXT,
    period_covered  TEXT,
    key_figures     TEXT,
    summary         TEXT,
    supersedes_hint TEXT,
    languages       TEXT,
    card_flags      TEXT,
    carded_at       REAL,
    extracted_at    REAL,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docs_status     ON documents(status);
CREATE INDEX IF NOT EXISTS idx_docs_workstream ON documents(workstream);
CREATE INDEX IF NOT EXISTS idx_docs_sha        ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_docs_dupe       ON documents(dupe_group);

-- Documents are content-addressed, so byte-identical files collapse into one
-- row. Every path a document was found at is recorded here instead; extra
-- occurrences are the exact duplicates in the data room.
CREATE TABLE IF NOT EXISTS occurrences (
    abs_path    TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    mtime       REAL,
    size_bytes  INTEGER,
    seen_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_occ_doc ON occurrences(doc_id);

CREATE TABLE IF NOT EXISTS units (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,
    anchor      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_units_doc ON units(doc_id, ordinal);

CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(
    text,
    doc_id UNINDEXED,
    anchor UNINDEXED,
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    total       INTEGER DEFAULT 0,
    done        INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    message     TEXT,
    detail      TEXT,
    started_at  REAL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS qa_log (
    id          TEXT PRIMARY KEY,
    question    TEXT NOT NULL,
    answer      TEXT,
    citations   TEXT,
    verdicts    TEXT,
    tool_calls  TEXT,
    usage       TEXT,
    model       TEXT,
    duration_s  REAL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    filename    TEXT NOT NULL,
    path        TEXT NOT NULL,
    size_bytes  INTEGER,
    qa_id       TEXT,
    created_at  REAL NOT NULL
);

-- Question refinement. One row per round of grounded clarifying questions that
-- turns a general question into a research brief before the analyst is paid to
-- run. A separate table rather than columns on qa_log for the same reason
-- user_budgets is one: the schema is a single pass of CREATE TABLE IF NOT
-- EXISTS with no migration step, so a new column would never appear on a
-- database that already exists. The link runs the same direction as
-- artifacts.qa_id — written across every round of the session once the
-- accepted brief is finally asked.
CREATE TABLE IF NOT EXISTS scope_rounds (
    id          TEXT PRIMARY KEY,
    scope_id    TEXT NOT NULL,
    round       INTEGER NOT NULL,
    question    TEXT NOT NULL,   -- the user's original question, unchanged
    answers     TEXT,            -- JSON: the answers that produced this round
    payload     TEXT NOT NULL,   -- JSON: the coerced round object
    transcript  TEXT,            -- JSON: messages[] to rehydrate the next round
    ready       INTEGER NOT NULL DEFAULT 0,
    -- The combined coverage score, duplicated out of `payload` so "which
    -- questions did we run thin?" is a query rather than a JSON unpack.
    coverage    INTEGER,
    usage       TEXT,
    model       TEXT,
    effort      TEXT,
    actor       TEXT,
    qa_id       TEXT,            -- the answer this brief produced, once run
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scope_session ON scope_rounds(scope_id, round);
CREATE INDEX IF NOT EXISTS idx_scope_qa      ON scope_rounds(qa_id);

-- --- accounts and access ------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'analyst',   -- admin | analyst
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    created_by    TEXT,
    last_login_at REAL,
    must_change_password INTEGER NOT NULL DEFAULT 0
);

-- Sessions are stored by token digest, so the table cannot be replayed.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf         TEXT NOT NULL,
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    user_agent   TEXT,
    ip           TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- Anthropic API keys, encrypted with the instance key (see security.py).
CREATE TABLE IF NOT EXISTS api_keys (
    id             TEXT PRIMARY KEY,
    label          TEXT NOT NULL,
    nonce          BLOB NOT NULL,
    ciphertext     BLOB NOT NULL,
    last4          TEXT NOT NULL,
    is_active      INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    created_by     TEXT,
    last_used_at   REAL,
    last_test_at   REAL,
    last_test_ok   INTEGER,
    last_test_note TEXT
);

CREATE TABLE IF NOT EXISTS archives (
    id            TEXT PRIMARY KEY,
    filename      TEXT NOT NULL,
    stored_path   TEXT NOT NULL,
    extract_dir   TEXT,
    size_bytes    INTEGER NOT NULL,
    sha256        TEXT,
    n_files       INTEGER DEFAULT 0,
    n_skipped     INTEGER DEFAULT 0,
    bytes_written INTEGER DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'uploaded',  -- uploaded|extracting|extracted|failed
    error         TEXT,
    uploaded_by   TEXT,
    created_at    REAL NOT NULL,
    extracted_at  REAL
);

-- Remote corpus sources (SharePoint Online document libraries today). One row
-- per connected library; the client secret is encrypted with the instance key
-- under its own AAD (see security.py). The mirror on disk is an ordinary corpus
-- root, so nothing downstream of ingest knows a document came from here.
CREATE TABLE IF NOT EXISTS sync_connections (
    id                   TEXT PRIMARY KEY,
    kind                 TEXT NOT NULL DEFAULT 'sharepoint',
    label                TEXT NOT NULL,
    site_url             TEXT NOT NULL,
    library              TEXT,               -- library (drive) name; NULL = the default one
    tenant               TEXT NOT NULL,
    client_id            TEXT NOT NULL,
    secret_nonce         BLOB NOT NULL,
    secret_ciphertext    BLOB NOT NULL,
    secret_last4         TEXT NOT NULL,
    secret_expires_at    REAL,               -- Entra secrets expire; warn before they do
    drive_id             TEXT,               -- resolved from site_url, re-resolvable
    drive_name           TEXT,
    mirror_dir           TEXT NOT NULL,
    only_supported_types INTEGER NOT NULL DEFAULT 1,
    mirror_deletions     INTEGER NOT NULL DEFAULT 1,
    interval_minutes     INTEGER NOT NULL DEFAULT 0,   -- 0 = manual only
    status               TEXT NOT NULL DEFAULT 'new',  -- new|ok|syncing|failed
    error                TEXT,
    last_sync_at         REAL,
    last_sync_seconds    REAL,
    last_test_at         REAL,
    last_test_ok         INTEGER,
    last_test_note       TEXT,
    n_files              INTEGER DEFAULT 0,
    n_deleted            INTEGER DEFAULT 0,
    bytes_total          INTEGER DEFAULT 0,
    created_at           REAL NOT NULL,
    created_by           TEXT
);

CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    actor   TEXT,
    action  TEXT NOT NULL,
    detail  TEXT,
    ip      TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);

-- The activity log, kept rather than only streamed. The live SSE feed is a
-- 400-entry ring in memory, so the one failure worth reporting scrolls away and
-- a restart loses it entirely. `context` carries the structured detail — path,
-- extension, size, exception type, traceback — that makes an error reportable
-- instead of merely visible.
CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    level   TEXT NOT NULL,            -- info | success | warn | error
    source  TEXT,                     -- ingest | sweep | sync | upload | ask | server | …
    message TEXT NOT NULL,
    context TEXT,                     -- JSON object, or NULL
    job_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_ts    ON logs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level, ts DESC);

-- Every paid API call, attributed to whoever caused it. The running meters in
-- pricing.py are per-process and vanish on restart; a weekly limit has to be
-- answerable from disk, and "what did this cost and who spent it" is a question
-- worth being able to answer months later regardless.
CREATE TABLE IF NOT EXISTS spend (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    username TEXT,                    -- NULL when nothing initiated it (never blocked)
    budget   TEXT NOT NULL,           -- ask | index
    kind     TEXT NOT NULL,           -- analyst | scoper | verifier | carder
    model    TEXT,
    cost_usd REAL NOT NULL,
    input_tokens       INTEGER DEFAULT 0,
    output_tokens      INTEGER DEFAULT 0,
    cache_read_tokens  INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    ref      TEXT                     -- qa id or job id
);
CREATE INDEX IF NOT EXISTS idx_spend_user ON spend(username, budget, ts);
CREATE INDEX IF NOT EXISTS idx_spend_ts   ON spend(ts DESC);

-- Per-account weekly caps. A separate table rather than columns on `users`
-- because the schema is one pass of CREATE TABLE IF NOT EXISTS with no migration
-- step, so a new column would never appear on a database that already exists.
-- NULL means "inherit the instance default"; see budget.UNLIMITED for the rest.
CREATE TABLE IF NOT EXISTS user_budgets (
    username   TEXT PRIMARY KEY,
    ask_usd    REAL,
    index_usd  REAL,
    grace_week TEXT,                  -- ISO week in which the ask grace was claimed
    updated_at REAL,
    updated_by TEXT
);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(settings.db_path, timeout=30.0, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


def init() -> None:
    conn().executescript(SCHEMA)


def rows(sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
    return conn().execute(sql, tuple(params)).fetchall()


def one(sql: str, params: Iterable = ()) -> sqlite3.Row | None:
    return conn().execute(sql, tuple(params)).fetchone()


def scalar(sql: str, params: Iterable = (), default: Any = 0) -> Any:
    row = one(sql, params)
    if row is None:
        return default
    value = row[0]
    return default if value is None else value


def execute(sql: str, params: Iterable = ()) -> sqlite3.Cursor:
    with _write_lock:
        return conn().execute(sql, tuple(params))


def doc_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for field in ("parties", "key_figures", "card_flags", "languages"):
        raw = d.get(field)
        if raw:
            try:
                d[field] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d[field] = []
        else:
            d[field] = []
    return d


# --- units ---------------------------------------------------------------


def replace_units(doc_id: str, units: list[dict]) -> None:
    """Swap in a fresh set of units for a document (idempotent re-ingest)."""
    with _write_lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            old = [r[0] for r in c.execute("SELECT id FROM units WHERE doc_id=?", (doc_id,))]
            if old:
                c.executemany("DELETE FROM units_fts WHERE rowid=?", [(i,) for i in old])
                c.execute("DELETE FROM units WHERE doc_id=?", (doc_id,))
            for u in units:
                cur = c.execute(
                    "INSERT INTO units(doc_id, ordinal, anchor, kind, char_start, char_end)"
                    " VALUES(?,?,?,?,?,?)",
                    (doc_id, u["ordinal"], u["anchor"], u["kind"], u["char_start"], u["char_end"]),
                )
                c.execute(
                    "INSERT INTO units_fts(rowid, text, doc_id, anchor) VALUES(?,?,?,?)",
                    (cur.lastrowid, u["text"], doc_id, u["anchor"]),
                )
            c.execute("COMMIT")
        except Exception:
            try:
                c.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise


def unit_count_mismatches() -> list[dict]:
    """Documents whose recorded n_units disagrees with the units table."""
    return [
        dict(r)
        for r in rows(
            "SELECT id, rel_path, n_units,"
            " (SELECT COUNT(*) FROM units u WHERE u.doc_id = d.id) AS actual"
            " FROM documents d WHERE status IN ('extracted','carded')"
            " AND n_units != (SELECT COUNT(*) FROM units u WHERE u.doc_id = d.id)"
        )
    ]


# --- FTS query building --------------------------------------------------

_TOKEN = re.compile(r'"[^"]*"|\S+')
_KEYWORDS = {"AND", "OR", "NOT", "NEAR"}


def fts_query(raw: str) -> str:
    """Turn arbitrary user/model text into a valid FTS5 MATCH expression.

    Prefix with ``raw:`` to pass an expression straight through.
    """
    raw = (raw or "").strip()
    if raw.lower().startswith("raw:"):
        return raw[4:].strip()
    parts: list[str] = []
    for tok in _TOKEN.findall(raw):
        if tok.upper() in _KEYWORDS:
            parts.append(tok.upper())
            continue
        if tok.startswith('"') and tok.endswith('"') and len(tok) > 1:
            inner = tok[1:-1]
        else:
            inner = tok
        prefix = inner.endswith("*")
        inner = inner.rstrip("*")
        inner = re.sub(r'["]', "", inner)
        inner = re.sub(r"[^\w\s\-./&%]", " ", inner, flags=re.UNICODE).strip()
        if not inner:
            continue
        parts.append(f'"{inner}"' + ("*" if prefix else ""))
    return " ".join(parts) if parts else '""'


# --- jobs ----------------------------------------------------------------


def job_upsert(job_id: str, **fields: Any) -> None:
    existing = one("SELECT id FROM jobs WHERE id=?", (job_id,))
    if existing is None:
        fields.setdefault("started_at", time.time())
        fields.setdefault("status", "running")
        fields.setdefault("kind", "job")
        cols = ", ".join(["id", *fields.keys()])
        marks = ", ".join(["?"] * (len(fields) + 1))
        execute(f"INSERT INTO jobs({cols}) VALUES({marks})", (job_id, *fields.values()))
    else:
        sets = ", ".join(f"{k}=?" for k in fields)
        execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))


def job(job_id: str) -> dict | None:
    row = one("SELECT * FROM jobs WHERE id=?", (job_id,))
    return dict(row) if row else None


def recent_jobs(limit: int = 10) -> list[dict]:
    return [dict(r) for r in rows("SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?", (limit,))]


# --- audit ---------------------------------------------------------------


def audit(action: str, actor: str | None = None, detail: str | None = None, ip: str | None = None) -> None:
    execute(
        "INSERT INTO audit(ts, actor, action, detail, ip) VALUES(?,?,?,?,?)",
        (time.time(), actor, action, detail, ip),
    )


def recent_audit(limit: int = 200) -> list[dict]:
    return [dict(r) for r in rows("SELECT * FROM audit ORDER BY ts DESC LIMIT ?", (limit,))]


# --- activity log --------------------------------------------------------

LOG_LEVELS = ("info", "success", "warn", "error")
LOG_CONTEXT_MAX = 20_000
_log_writes = 0


def _clip_value(value, budget: int):
    """Bound one context value to roughly `budget` characters, keeping its shape.

    Every branch returns something bounded, including the fallback — that is what
    makes the caller's loop terminate rather than give up and store nothing.
    """
    if isinstance(value, str):
        if len(value) <= budget:
            return value
        return value[:budget] + f"… [{len(value) - budget} more characters]"
    if isinstance(value, list):
        # Bounded by total characters, not by item count: 100 long paths is 40 KB.
        kept: list[str] = []
        spent = 0
        for item in value:
            text = str(item)[: max(80, budget // 8)]
            if spent + len(text) > budget:
                break
            kept.append(text)
            spent += len(text)
        if len(kept) < len(value):
            kept.append(f"… [{len(value) - len(kept)} more]")
        return kept
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # Anything else (a dict, an object) becomes a clipped string. Unbounded nesting
    # is the one shape that could otherwise defeat the budget.
    return _clip_value(json.dumps(value, default=str), budget)


def _log_context_json(context: dict) -> str:
    """Serialise a context object, shrinking the *object* if it is too large.

    Slicing the serialised JSON instead would cut mid-string and store something
    unparseable, and an unparseable context is worse than a shortened one: on read
    it degrades to an opaque blob, losing the traceback and the path list at
    exactly the moment a bug report needs them. So oversize values are trimmed
    individually — with a marker saying so — and the result is always valid JSON.

    The budget is applied and then *checked*, repeatedly. One pass is not enough
    because JSON escaping is not size-preserving: a traceback is mostly newlines,
    and each one doubles on the way out, so a first guess that looks like it fits
    can serialise to twice the cap.
    """
    payload = json.dumps(context, default=str)
    if len(payload) <= LOG_CONTEXT_MAX:
        return payload

    n = max(len(context), 1)
    # Reserve for framing — braces, quotes, commas and the key names themselves —
    # rather than handing the whole cap to the values.
    framing = sum(len(str(k)) + 6 for k in context) + 32
    budget = max(120, (LOG_CONTEXT_MAX - framing) // n)
    for _ in range(8):
        shrunk = {k: _clip_value(v, budget) for k, v in context.items()}
        payload = json.dumps(shrunk, default=str)
        if len(payload) <= LOG_CONTEXT_MAX:
            return payload
        budget //= 2
        if budget < 40:
            break
    # Only reachable with a pathological number of keys, where even 40 characters
    # each will not fit. Keep it valid and say what happened.
    return json.dumps(
        {"truncated": True, "keys": sorted(str(k) for k in context)[:200], "note":
         f"context exceeded {LOG_CONTEXT_MAX} characters and could not be shortened"}
    )


def log_record(
    level: str,
    message: str,
    source: str | None = None,
    context: dict | None = None,
    job_id: str | None = None,
) -> int | None:
    """Persist one activity-log line, returning its row id.

    Never raises — a log line is not worth breaking a job over. The id is what
    lets the streamed copy of this line and the stored one be recognised as the
    same thing, so the browser can merge a live feed with a history query without
    showing anything twice.
    """
    global _log_writes

    try:
        payload = _log_context_json(context) if context else None
        cur = execute(
            "INSERT INTO logs(ts, level, source, message, context, job_id) VALUES(?,?,?,?,?,?)",
            (time.time(), level if level in LOG_LEVELS else "info", source,
             str(message)[:4000], payload, job_id),
        )
        _log_writes += 1
        # Trimmed on a counter rather than on every insert: a sweep over a large
        # data room writes thousands of lines and each trim is a scan.
        if _log_writes % 500 == 0:
            log_trim()
        return cur.lastrowid
    except Exception:  # noqa: BLE001 — logging is never worth an exception
        return None


def log_trim(keep: int | None = None) -> int:
    """Drop the oldest rows beyond the retention cap. Returns rows removed."""
    keep = keep if keep is not None else settings.log_retention
    if keep <= 0:
        return 0
    # The subquery is the id of the keep-th newest row, so everything strictly
    # older goes. It is NULL while fewer than `keep` rows exist, and `id < NULL`
    # matches nothing — which is the behaviour wanted.
    cur = execute(
        "DELETE FROM logs WHERE id < (SELECT id FROM logs ORDER BY id DESC LIMIT 1 OFFSET ?)",
        (keep - 1,),
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def log_query(
    *,
    levels: Iterable[str] | None = None,
    source: str | None = None,
    query: str | None = None,
    before_id: int | None = None,
    after_id: int | None = None,
    limit: int = 200,
) -> list[dict]:
    """Newest first.

    ``before_id`` pages backwards through the history; ``after_id`` fetches only
    what is newer than a line the caller already holds, which is how a browser
    fills the gap after its event stream dropped and reconnected.
    """
    where = ["1=1"]
    params: list = []
    wanted = [lv for lv in (levels or []) if lv in LOG_LEVELS]
    if wanted:
        where.append(f"level IN ({','.join('?' * len(wanted))})")
        params.extend(wanted)
    if source:
        where.append("source = ?")
        params.append(source)
    if query:
        where.append("(message LIKE ? OR context LIKE ?)")
        params.extend([f"%{query}%"] * 2)
    if before_id:
        where.append("id < ?")
        params.append(before_id)
    if after_id:
        where.append("id > ?")
        params.append(after_id)
    params.append(max(1, min(limit, 2000)))
    out: list[dict] = []
    for r in rows(
        f"SELECT * FROM logs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", params
    ):
        d = dict(r)
        raw = d.pop("context", None)
        if raw:
            try:
                d["context"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d["context"] = {"raw": raw}
        else:
            d["context"] = None
        out.append(d)
    return out


def log_counts() -> dict:
    """Totals per level, so the UI can show "12 errors" without paging."""
    counts = {lv: 0 for lv in LOG_LEVELS}
    for r in rows("SELECT level, COUNT(*) AS n FROM logs GROUP BY level"):
        if r["level"] in counts:
            counts[r["level"]] = r["n"]
    counts["total"] = sum(counts[lv] for lv in LOG_LEVELS)
    counts["sources"] = [
        r["source"] for r in rows(
            "SELECT DISTINCT source FROM logs WHERE source IS NOT NULL ORDER BY source"
        )
    ]
    return counts


# --- spend ledger --------------------------------------------------------


def spend_record(
    username: str | None,
    budget: str,
    kind: str,
    model: str | None,
    cost_usd: float,
    *,
    usage: dict | None = None,
    ref: str | None = None,
) -> None:
    """Append one paid call. Never raises — accounting must not fail a request."""
    u = usage or {}
    try:
        execute(
            "INSERT INTO spend(ts, username, budget, kind, model, cost_usd, input_tokens,"
            " output_tokens, cache_read_tokens, cache_write_tokens, ref)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), username, budget, kind, model, float(cost_usd),
             u.get("input_tokens", 0), u.get("output_tokens", 0),
             u.get("cache_read_tokens", 0), u.get("cache_write_tokens", 0), ref),
        )
    except Exception:  # noqa: BLE001 — a missed ledger row is not worth a 500
        pass


def spend_since(since: float, username: str | None = None, budget: str | None = None) -> float:
    """Total USD spent since a timestamp, optionally for one user and budget."""
    where = ["ts >= ?"]
    params: list = [since]
    if username is not None:
        where.append("username = ?")
        params.append(username)
    if budget is not None:
        where.append("budget = ?")
        params.append(budget)
    return float(scalar(f"SELECT SUM(cost_usd) FROM spend WHERE {' AND '.join(where)}", params, 0.0))


def spend_by_user(since: float) -> dict[str, dict[str, float]]:
    """``{username: {"ask": x, "index": y, "total": z}}`` since a timestamp.

    One query rather than two per account, because the accounts list renders a
    figure for every user and a per-user query there is a loop over the table.
    """
    out: dict[str, dict[str, float]] = {}
    for r in rows(
        "SELECT username, budget, SUM(cost_usd) AS total FROM spend"
        " WHERE ts >= ? AND username IS NOT NULL GROUP BY username, budget",
        (since,),
    ):
        entry = out.setdefault(r["username"], {"ask": 0.0, "index": 0.0, "total": 0.0})
        if r["budget"] in entry:
            entry[r["budget"]] = float(r["total"] or 0.0)
        entry["total"] += float(r["total"] or 0.0)
    return out


def log_clear(levels: Iterable[str] | None = None) -> int:
    wanted = [lv for lv in (levels or []) if lv in LOG_LEVELS]
    if wanted:
        cur = execute(
            f"DELETE FROM logs WHERE level IN ({','.join('?' * len(wanted))})", wanted
        )
    else:
        cur = execute("DELETE FROM logs")
    return max(cur.rowcount or 0, 0)
