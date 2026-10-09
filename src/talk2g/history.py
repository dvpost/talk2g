from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .config import project_home


class History:
    def __init__(self, home: Path | None = None):
        path = (home or project_home()) / ".data" / "history.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        path.chmod(0o600)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, at TEXT, text TEXT)"
        )

    def add(self, text: str):
        if not text.strip():
            return
        with self.connection:
            self.connection.execute(
                "INSERT INTO history(at,text) VALUES (?,?)", (datetime.now(UTC).isoformat(), text)
            )
            self.connection.execute(
                "DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 100)"
            )

    def recent(self) -> list[tuple[str, str]]:
        return self.connection.execute("SELECT at,text FROM history ORDER BY id DESC LIMIT 100").fetchall()

    def clear(self):
        with self.connection:
            self.connection.execute("DELETE FROM history")

    def close(self):
        self.connection.close()
