"""SQLite persistence. Amounts are decimal strings, never binary floats."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS participants(
      id TEXT PRIMARY KEY, name TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS movements(
      id TEXT PRIMARY KEY, effective_at TEXT NOT NULL,
      type TEXT NOT NULL, participant_id TEXT NOT NULL REFERENCES participants(id),
      amount_usd TEXT NOT NULL, units TEXT NOT NULL, unit_value TEXT NOT NULL,
      fx TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL CHECK(status IN ('posted','pending','reversed')),
      source_key TEXT NOT NULL UNIQUE, reverses_id TEXT REFERENCES movements(id),
      created_at TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'USD',
      original_amount TEXT, close_id TEXT,
      metadata_json TEXT NOT NULL DEFAULT '{}');
    CREATE INDEX IF NOT EXISTS idx_movements_date ON movements(effective_at);
    CREATE TABLE IF NOT EXISTS account_snapshots(
      id TEXT PRIMARY KEY, as_of TEXT NOT NULL,
      cash_ars TEXT NOT NULL, cash_usd TEXT NOT NULL,
      securities_ars TEXT NOT NULL, mep TEXT NOT NULL, total_usd TEXT NOT NULL,
      units TEXT NOT NULL, unit_value TEXT NOT NULL,
      source TEXT NOT NULL, raw_hash TEXT NOT NULL,
      metadata_json TEXT NOT NULL DEFAULT '{}');
    CREATE INDEX IF NOT EXISTS idx_snapshots_date ON account_snapshots(as_of);
    CREATE TABLE IF NOT EXISTS operations(
      account_id TEXT NOT NULL, number TEXT NOT NULL,
      payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
      PRIMARY KEY(account_id,number));
    CREATE TABLE IF NOT EXISTS quotes(
      symbol TEXT NOT NULL, date TEXT NOT NULL, price TEXT NOT NULL,
      source TEXT NOT NULL, PRIMARY KEY(symbol,date));
    CREATE TABLE IF NOT EXISTS positions(
      snapshot_id TEXT NOT NULL REFERENCES account_snapshots(id),
      symbol TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
      category TEXT NOT NULL DEFAULT 'Otros', currency TEXT NOT NULL,
      quantity TEXT NOT NULL, value_usd TEXT NOT NULL,
      PRIMARY KEY(snapshot_id,symbol,currency));
    CREATE TABLE IF NOT EXISTS runs(
      id TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
      status TEXT NOT NULL, summary_json TEXT NOT NULL DEFAULT '{}');
    CREATE TABLE IF NOT EXISTS migration_log(
      source_hash TEXT PRIMARY KEY, source_path TEXT NOT NULL,
      imported_at TEXT NOT NULL, counts_json TEXT NOT NULL,
      query_code TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS audit_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
      action TEXT NOT NULL, entity TEXT NOT NULL, entity_key TEXT NOT NULL,
      before_json TEXT, after_json TEXT);
    PRAGMA user_version = 1;
    """)
    conn.executemany("INSERT OR IGNORE INTO participants(id,name) VALUES(?,?)",
                     [("tommy", "Tommy"), ("juli", "Juli"), ("pili", "Pili")])
    conn.commit()


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def audit(conn, action: str, entity: str, entity_key: str,
          before=None, after=None) -> None:
    conn.execute("""INSERT INTO audit_log(at,action,entity,entity_key,before_json,after_json)
                    VALUES(?,?,?,?,?,?)""",
                 (utcnow(), action, entity, str(entity_key),
                  json_text(before) if before is not None else None,
                  json_text(after) if after is not None else None))


def upsert_operation(conn, account_id, number, payload) -> str:
    """Insert/correct aggregate broker operations without multiplying executions."""
    account_id, number = str(account_id), str(number)
    serialized = json_text(payload)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    old = conn.execute("SELECT * FROM operations WHERE account_id=? AND number=?",
                       (account_id, number)).fetchone()
    if old and old["payload_hash"] == digest:
        return "unchanged"
    action = "updated" if old else "inserted"
    conn.execute("""INSERT INTO operations(account_id,number,payload_json,payload_hash)
      VALUES(?,?,?,?) ON CONFLICT(account_id,number) DO UPDATE SET
      payload_json=excluded.payload_json,payload_hash=excluded.payload_hash""",
                 (account_id, number, serialized, digest))
    audit(conn, action, "operation", f"{account_id}:{number}",
          dict(old) if old else None, payload)
    return action


def upsert_quote(conn, symbol, date, price, source="IOL") -> str:
    old = conn.execute("SELECT * FROM quotes WHERE symbol=? AND date=?",
                       (str(symbol), str(date))).fetchone()
    if old and old["price"] == str(price):
        return "unchanged"
    action = "updated" if old else "inserted"
    conn.execute("""INSERT INTO quotes(symbol,date,price,source) VALUES(?,?,?,?)
      ON CONFLICT(symbol,date) DO UPDATE SET price=excluded.price,source=excluded.source""",
                 (str(symbol), str(date), str(price), str(source)))
    audit(conn, action, "quote", f"{symbol}:{date}", dict(old) if old else None,
          {"price": str(price), "source": source})
    return action
