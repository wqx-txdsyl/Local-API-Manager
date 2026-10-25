"""SQLite storage: providers, model routes, local keys, request logs, daily usage."""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB_PATH = os.environ.get("APIPLATFORM_DB", str(DATA / "apiplatform.db"))

_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,              -- openai | anthropic | codex
  base_url TEXT NOT NULL,
  api_key TEXT DEFAULT '',
  auth TEXT DEFAULT 'bearer',      -- bearer | x-api-key | none
  extra_headers TEXT DEFAULT '{}',
  chat_path TEXT DEFAULT '',
  enabled INTEGER DEFAULT 0,
  priority INTEGER DEFAULT 100,
  billing TEXT DEFAULT 'credit',   -- credit | subscription
  balance_type TEXT DEFAULT 'none',
  currency TEXT DEFAULT 'USD',
  balance TEXT DEFAULT '',
  balance_at REAL DEFAULT 0,
  models TEXT DEFAULT '',
  models_at REAL DEFAULT 0,
  models_error TEXT DEFAULT '',
  refresh_token TEXT DEFAULT '',
  account_id TEXT DEFAULT '',
  last_status TEXT DEFAULT '',
  last_check_at REAL DEFAULT 0,
  created_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS models (
  local_name TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  upstream_name TEXT NOT NULL,
  enabled INTEGER DEFAULT 1,
  source TEXT DEFAULT 'discovered',  -- discovered | manual
  PRIMARY KEY (local_name, provider_id)
);
CREATE TABLE IF NOT EXISTS keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  key TEXT NOT NULL UNIQUE,
  enabled INTEGER DEFAULT 1,
  created_at REAL DEFAULT 0,
  last_used REAL DEFAULT 0,
  req_total INTEGER DEFAULT 0,
  in_total INTEGER DEFAULT 0,
  out_total INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0,
  cost_cny REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  key_id INTEGER, key_name TEXT,
  provider_id TEXT, model TEXT, protocol TEXT, route TEXT,
  status INTEGER, ms INTEGER,
  in_tok INTEGER DEFAULT 0, out_tok INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0, cost_cny REAL DEFAULT 0,
  stream INTEGER DEFAULT 0, error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);
CREATE TABLE IF NOT EXISTS daily (
  day TEXT NOT NULL,
  key_id INTEGER, provider_id TEXT, model TEXT,
  req INTEGER DEFAULT 0, in_tok INTEGER DEFAULT 0, out_tok INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0, cost_cny REAL DEFAULT 0,
  PRIMARY KEY (day, key_id, provider_id, model)
);
"""


def conn():
    global _conn
    if _conn is None:
        c = sqlite3.connect(DB_PATH, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA)
        c.commit()
        _conn = c
    return _conn


def query(sql, args=()):
    with _lock:
        rows = conn().execute(sql, args).fetchall()
        return [dict(r) for r in rows]


def one(sql, args=()):
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql, args=()):
    with _lock:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


def executemany(sql, seq):
    with _lock:
        c = conn()
        c.executemany(sql, seq)
        c.commit()


def upsert_provider(p: dict):
    now = time.time()
    with _lock:
        c = conn()
        c.execute(
            """INSERT INTO providers (id,name,kind,base_url,api_key,auth,extra_headers,chat_path,
               enabled,priority,billing,balance_type,currency,refresh_token,account_id,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind,
                 base_url=excluded.base_url, api_key=CASE WHEN excluded.api_key='' THEN providers.api_key ELSE excluded.api_key END,
                 auth=excluded.auth, extra_headers=excluded.extra_headers, chat_path=excluded.chat_path,
                 enabled=excluded.enabled, priority=excluded.priority, billing=excluded.billing,
                 balance_type=excluded.balance_type, currency=excluded.currency,
                 refresh_token=CASE WHEN excluded.refresh_token='' THEN providers.refresh_token ELSE excluded.refresh_token END,
                 account_id=CASE WHEN excluded.account_id='' THEN providers.account_id ELSE excluded.account_id END""",
            (p["id"], p["name"], p["kind"], p["base_url"], p.get("api_key", "") or "",
             p.get("auth", "bearer"), json.dumps(p.get("extra_headers") or {}, ensure_ascii=False),
             p.get("chat_path", "") or "", 1 if p.get("enabled") else 0,
             p.get("priority", 100), p.get("billing", "credit"), p.get("balance_type", "none"),
             p.get("currency", "USD"), p.get("refresh_token", "") or "",
             p.get("account_id", "") or "", now))
        c.commit()


def get_provider(pid):
    r = one("SELECT * FROM providers WHERE id=?", (pid,))
    if not r:
        return None
    try:
        r["extra_headers"] = json.loads(r.get("extra_headers") or "{}")
    except Exception:
        r["extra_headers"] = {}
    try:
        r["models"] = json.loads(r.get("models") or "[]")
    except Exception:
        r["models"] = []
    return r


def list_providers():
    rows = query("SELECT * FROM providers ORDER BY priority, id")
    for r in rows:
        try:
            r["extra_headers"] = json.loads(r.get("extra_headers") or "{}")
        except Exception:
            r["extra_headers"] = {}
        try:
            r["models"] = json.loads(r.get("models") or "[]")
        except Exception:
            r["models"] = []
    return rows


def record_usage(key_row, provider_id, model, route, stage, status, ms, in_tok, out_tok,
                 cost_usd, cost_cny, stream, error, protocol):
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    execute(
        """INSERT INTO logs (ts,key_id,key_name,provider_id,model,protocol,route,status,ms,
           in_tok,out_tok,cost_usd,cost_cny,stream,error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now, key_row["id"], key_row["name"], provider_id, model, protocol, route, status, ms,
         in_tok, out_tok, cost_usd, cost_cny, 1 if stream else 0, (error or "")[:500]))
    if key_row:
        execute(
            """UPDATE keys SET req_total=req_total+1, in_total=in_total+?, out_total=out_total+?,
               cost_usd=cost_usd+?, cost_cny=cost_cny+?, last_used=? WHERE id=?""",
            (in_tok, out_tok, cost_usd, cost_cny, now, key_row["id"]))
    execute(
        """INSERT INTO daily (day,key_id,provider_id,model,req,in_tok,out_tok,cost_usd,cost_cny)
           VALUES (?,?,?,?,1,?,?,?,?)
           ON CONFLICT(day,key_id,provider_id,model) DO UPDATE SET req=req+1,
             in_tok=in_tok+excluded.in_tok, out_tok=out_tok+excluded.out_tok,
             cost_usd=cost_usd+excluded.cost_usd, cost_cny=cost_cny+excluded.cost_cny""",
        (day, key_row["id"] if key_row else 0, provider_id, model, in_tok, out_tok, cost_usd, cost_cny))
    # prune logs
    if int(now) % 60 == 0:
        execute("""DELETE FROM logs WHERE id < (SELECT COALESCE(MIN(id),0) FROM logs
                   WHERE id IN (SELECT id FROM logs ORDER BY id DESC LIMIT 50000))""")


def mask_secret(v):
    if not v:
        return ""
    v = str(v)
    return v[:6] + "…" + v[-4:] if len(v) > 14 else "…"
