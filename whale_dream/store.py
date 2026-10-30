"""Private SQLite journal. Short synchronous transactions never span an await."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock


class Store:
    def __init__(self, path, bot, now):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # MaiBot builds prompts in a worker thread, while capture and dreaming
        # run on the event loop. Serialize the entire transaction, not just SQL
        # statements, so other threads cannot read or overwrite partial edits.
        self.lock = RLock()
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO state VALUES (1, ?)", (json.dumps({
                "schema": 1, "bot": bot, "enabled_at": now, "epoch": 0,
                "frozen": False, "disabled": False, "scopes": {}, "sources": {},
                "records": {}, "versions": [], "rounds": [], "promotions": [],
                "jobs": {}, "exports": {}, "exposures": [], "invalid_sources": [],
            }),))
        if self.read()["bot"] != bot:
            raise ValueError("dream store belongs to another bot")
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
            os.chmod(self.path, 0o600)

    def read(self):
        with self.lock:
            return json.loads(self.db.execute("SELECT value FROM state WHERE id=1").fetchone()[0])

    @contextmanager
    def edit(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                state = self.read()
                yield state
                self.db.execute("UPDATE state SET value=? WHERE id=1", (json.dumps(state, ensure_ascii=False),))
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def close(self):
        with self.lock:
            self.db.close()
