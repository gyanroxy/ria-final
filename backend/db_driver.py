import os
import sqlite3
from contextlib import contextmanager

# Always next to this file, regardless of the directory the agent is started from
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_db.sqlite")


class DatabaseDriver:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._get_connection() as conn:
            # Leads captured by the voice agent when a visitor asks for a demo/trial
            conn.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    business_name TEXT,
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def has_recent_lead(self, phone: str, hours: int = 24) -> bool:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM leads WHERE phone = ? AND created_at >= datetime('now', ?) LIMIT 1",
                (phone, f"-{hours} hours"),
            ).fetchone()
            return row is not None

    def create_lead(self, name: str, phone: str, business_name: str = "", notes: str = "") -> int:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO leads (name, phone, business_name, notes) VALUES (?, ?, ?, ?)",
                (name, phone, business_name, notes)
            )
            conn.commit()
            return cursor.lastrowid

    def append_lead_note(self, phone: str, note: str) -> bool:
        """Adds a note to this phone number's most recent lead; False if there is none."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE leads SET notes = COALESCE(NULLIF(notes, '') || ' | ', '') || ?
                WHERE id = (SELECT id FROM leads WHERE phone = ? ORDER BY id DESC LIMIT 1)
                """,
                (note, phone),
            )
            conn.commit()
            return cursor.rowcount > 0
