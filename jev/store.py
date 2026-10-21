"""SQLite Persistence Engine for Idempotency, Outbox, and Crash Recovery."""

import sqlite3
import json
import time
import logging
from typing import Optional, List, Dict, Any
from pathlib import Path

logger = logging.getLogger("jevtrader.store")

class StateStore:
    def __init__(self, db_path: str = "data/jevtrader.sqlite3"):
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self):
        ddl = """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trades (
            trade_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            tp1 REAL NOT NULL,
            tp2 REAL NOT NULL,
            qty_entry REAL NOT NULL,
            initial_risk_usd REAL NOT NULL,
            status TEXT NOT NULL,
            is_tp1_filled INTEGER NOT NULL DEFAULT 0,
            is_be_locked INTEGER NOT NULL DEFAULT 0,
            created_ms INTEGER NOT NULL,
            updated_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS decisions (
            decision_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            model_name TEXT NOT NULL,
            action_prob REAL NOT NULL,
            wall_prob REAL NOT NULL,
            cost_usd REAL NOT NULL,
            latency_sec REAL NOT NULL,
            request_json TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orders (
            order_link_id TEXT PRIMARY KEY,
            trade_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            purpose TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL,
            state TEXT NOT NULL,
            created_ms INTEGER NOT NULL,
            updated_ms INTEGER NOT NULL,
            FOREIGN KEY(trade_id) REFERENCES trades(trade_id)
        );

        CREATE TABLE IF NOT EXISTS cooldowns (
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            until_ms INTEGER NOT NULL,
            reason TEXT NOT NULL,
            PRIMARY KEY(symbol, direction)
        );

        CREATE TABLE IF NOT EXISTS events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        """
        with self._get_conn() as conn:
            conn.executescript(ddl)

    def record_decision(self, decision_id: str, symbol: str, direction: str,
                        model_name: str, action_prob: float, wall_prob: float,
                        cost_usd: float, latency_sec: float, request_json: str,
                        response_json: str):
        now_ms = int(time.time() * 1000)
        with self._get_conn() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO decisions 
            (decision_id, symbol, direction, model_name, action_prob, wall_prob, cost_usd, latency_sec, request_json, response_json, created_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (decision_id, symbol, direction, model_name, action_prob, wall_prob, cost_usd, latency_sec, request_json, response_json, now_ms))

    def create_trade(self, trade_id: str, symbol: str, direction: str,
                     entry_price: float, stop_loss: float, tp1: float, tp2: float,
                     qty: float, initial_risk_usd: float):
        now_ms = int(time.time() * 1000)
        with self._get_conn() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO trades
            (trade_id, symbol, direction, entry_price, stop_loss, tp1, tp2, qty_entry, initial_risk_usd, status, created_ms, updated_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
            """, (trade_id, symbol, direction, entry_price, stop_loss, tp1, tp2, qty, initial_risk_usd, now_ms, now_ms))

    def update_trade_be(self, trade_id: str):
        now_ms = int(time.time() * 1000)
        with self._get_conn() as conn:
            conn.execute("""
            UPDATE trades 
            SET is_tp1_filled = 1, is_be_locked = 1, status = 'TP1_HIT_BE_LOCKED', updated_ms = ?
            WHERE trade_id = ?
            """, (now_ms, trade_id))

    def close_trade(self, trade_id: str, reason_status: str):
        now_ms = int(time.time() * 1000)
        with self._get_conn() as conn:
            conn.execute("""
            UPDATE trades SET status = ?, updated_ms = ? WHERE trade_id = ?
            """, (reason_status, now_ms, trade_id))

    def get_open_trades(self) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM trades WHERE status IN ('OPEN', 'TP1_HIT_BE_LOCKED')")
            return [dict(r) for r in cur.fetchall()]

    def is_cooldown_active(self, symbol: str, direction: str) -> bool:
        now_ms = int(time.time() * 1000)
        with self._get_conn() as conn:
            cur = conn.execute("SELECT until_ms FROM cooldowns WHERE symbol = ? AND direction = ?", (symbol, direction))
            row = cur.fetchone()
            if row and row['until_ms'] > now_ms:
                return True
        return False

    def set_cooldown(self, symbol: str, direction: str, duration_sec: int, reason: str):
        now_ms = int(time.time() * 1000)
        until_ms = now_ms + (duration_sec * 1000)
        with self._get_conn() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO cooldowns (symbol, direction, until_ms, reason)
            VALUES (?, ?, ?, ?)
            """, (symbol, direction, until_ms, reason))

    def log_event(self, kind: str, payload: Dict[str, Any]):
        now_ms = int(time.time() * 1000)
        with self._get_conn() as conn:
            conn.execute("INSERT INTO events (ts_ms, kind, payload_json) VALUES (?, ?, ?)",
                         (now_ms, kind, json.dumps(payload)))
