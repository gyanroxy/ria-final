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
        # The dashboard server, the dialer and the agent all write here at once
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            # One row per call: the invoice it was about and what the customer
            # said. The dashboard queues it, the dialer places it, the agent
            # fills in the outcome. promised_date is the promise-to-pay (PTP) date.
            # status: queued -> dialing -> live -> done (or cancelled)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS collection_calls (
                    call_id TEXT PRIMARY KEY,
                    invoice_no TEXT,
                    customer_name TEXT,
                    business TEXT,
                    phone TEXT,
                    amount REAL,
                    due_date TEXT,
                    company TEXT,
                    status TEXT,
                    outcome TEXT,
                    promised_date TEXT,
                    promised_amount REAL,
                    notes TEXT,
                    language TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    answered_at TIMESTAMP,
                    ended_at TIMESTAMP
                )
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(collection_calls)")}
            if "status" not in columns:
                # Databases from before the dashboard: every call there has finished
                conn.execute("ALTER TABLE collection_calls ADD COLUMN status TEXT")
                conn.execute("UPDATE collection_calls SET status = 'done'")
            conn.commit()

    COLLECTION_FIELDS = frozenset((
        "invoice_no", "customer_name", "business", "phone", "amount", "due_date", "company",
        "status", "outcome", "promised_date", "promised_amount", "notes", "language",
        "answered_at", "ended_at",
    ))

    def save_collection_call(self, call_id: str, **fields) -> None:
        """Creates or updates a call; only the given fields change."""
        unknown = set(fields) - self.COLLECTION_FIELDS
        if unknown:
            raise ValueError(f"unknown collection_calls fields: {sorted(unknown)}")
        columns = ["call_id", *fields]
        updates = ", ".join(f"{c} = excluded.{c}" for c in fields) or "call_id = call_id"
        with self._get_connection() as conn:
            conn.execute(
                f"INSERT INTO collection_calls ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)}) "
                f"ON CONFLICT(call_id) DO UPDATE SET {updates}",
                (call_id, *fields.values()),
            )
            conn.commit()

    def end_collection_call(self, call_id: str) -> None:
        """Stamps the end time; a call that ended without an outcome is DISCONNECTED."""
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE collection_calls SET status = 'done',
                    ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP),
                    outcome = COALESCE(outcome, 'DISCONNECTED')
                WHERE call_id = ?
                """,
                (call_id,),
            )
            conn.commit()

    def get_collection_call(self, call_id: str) -> dict | None:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM collection_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_calls(self, limit: int = 2000) -> list[dict]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM collection_calls ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def queued_calls(self, limit: int) -> list[dict]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM collection_calls WHERE status = 'queued' "
                "ORDER BY created_at, rowid LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def claim_call(self, call_id: str) -> bool:
        """Moves a queued call to dialing; False if someone else got it first."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE collection_calls SET status = 'dialing' "
                "WHERE call_id = ? AND status = 'queued'",
                (call_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def recover_stale_calls(self, max_minutes: int) -> None:
        """Closes calls a previous dialer left behind (server restarted mid-call)."""
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE collection_calls SET status = 'done', ended_at = CURRENT_TIMESTAMP,
                    outcome = COALESCE(outcome, 'FAILED'),
                    notes = COALESCE(notes, 'dialer restarted during the call')
                WHERE status = 'dialing'
                    OR (status = 'live' AND answered_at < datetime('now', ?))
                """,
                (f"-{max_minutes + 5} minutes",),
            )
            conn.commit()

    def cancel_call(self, call_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE collection_calls SET status = 'cancelled', outcome = 'CANCELLED' "
                "WHERE call_id = ? AND status = 'queued'",
                (call_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def latest_call_for_phone(self, phone: str) -> dict | None:
        """The most recent invoice discussed with this number (for inbound callbacks)."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM collection_calls WHERE phone = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (phone,),
            ).fetchone()
            return dict(row) if row else None

    def has_opted_out(self, phone: str) -> bool:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM collection_calls WHERE phone = ? AND outcome = 'OPT-OUT' LIMIT 1",
                (phone,),
            ).fetchone()
            return row is not None

    def called_recently(self, phone: str, invoice_no: str, hours: int = 20) -> bool:
        """True if this invoice is queued, on a call, or was discussed recently."""
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM collection_calls
                WHERE phone = ? AND invoice_no = ? AND (
                    status IN ('queued', 'dialing', 'live')
                    OR (answered_at IS NOT NULL AND created_at >= datetime('now', ?))
                )
                LIMIT 1
                """,
                (phone, invoice_no, f"-{hours} hours"),
            ).fetchone()
            return row is not None
