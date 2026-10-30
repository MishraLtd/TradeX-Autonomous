"""Durable state plus a process lock. One database per broker account on local disk."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3
import time
from .models import dumps


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at REAL, trade_id TEXT, kind TEXT, data TEXT);
        """)

    @contextmanager
    def locked(self):
        with open(str(self.path) + ".lock", "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another engine operation is using this account database") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def all(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM trades ORDER BY rowid")]

    def save(self, trade, kind):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO trades VALUES (?,?)", (trade["id"], dumps(trade)))
            self.db.execute("INSERT INTO events(at,trade_id,kind,data) VALUES (?,?,?,?)",
                            (time.time(), trade["id"], kind, dumps(trade)))

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, dumps(value)))

    def close(self):
        self.db.close()
