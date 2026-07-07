from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import settings


class JobStore:
    """Persists job rows to a SQLite file so the job list survives API restarts."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                source_filename TEXT NOT NULL,
                target_language TEXT NOT NULL,
                submit_kind TEXT NOT NULL,
                concurrency INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                progress REAL NOT NULL,
                error TEXT,
                warning TEXT,
                source_path TEXT,
                target_path TEXT
            )
            """
        )
        self._conn.commit()
        for column, ddl in (
            ("owner_user_id", "TEXT"),
            ("user_prompt", "TEXT"),
            ("estimated_tokens", "INTEGER"),
            ("price_cents", "INTEGER"),
            ("currency", "TEXT"),
            ("stripe_checkout_session_id", "TEXT"),
            ("stripe_payment_status", "TEXT"),
        ):
            self._ensure_column("jobs", column, ddl)

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        with self._lock:
            existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                self._conn.commit()

    def upsert(self, row: dict) -> None:
        params = {
            "id": row["id"],
            "source_filename": row["source_filename"],
            "target_language": row["target_language"],
            "submit_kind": row["submit_kind"],
            "concurrency": row["concurrency"],
            "created_at": row["created_at"],
            "status": row["status"],
            "progress": row["progress"],
            "error": row.get("error"),
            "warning": row.get("warning"),
            "source_path": row.get("source_path"),
            "target_path": row.get("target_path"),
            "owner_user_id": row.get("owner_user_id"),
            "user_prompt": row.get("user_prompt"),
            "estimated_tokens": row.get("estimated_tokens"),
            "price_cents": row.get("price_cents"),
            "currency": row.get("currency"),
            "stripe_checkout_session_id": row.get("stripe_checkout_session_id"),
            "stripe_payment_status": row.get("stripe_payment_status"),
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO jobs (
                    id, source_filename, target_language, submit_kind, concurrency,
                    created_at, status, progress, error, warning, source_path, target_path,
                    owner_user_id, user_prompt, estimated_tokens, price_cents, currency,
                    stripe_checkout_session_id, stripe_payment_status
                ) VALUES (
                    :id, :source_filename, :target_language, :submit_kind, :concurrency,
                    :created_at, :status, :progress, :error, :warning, :source_path, :target_path,
                    :owner_user_id, :user_prompt, :estimated_tokens, :price_cents, :currency,
                    :stripe_checkout_session_id, :stripe_payment_status
                )
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    progress = excluded.progress,
                    error = excluded.error,
                    warning = excluded.warning,
                    estimated_tokens = excluded.estimated_tokens,
                    price_cents = excluded.price_cents,
                    stripe_payment_status = excluded.stripe_payment_status
                """,
                params,
            )
            self._conn.commit()

    def load_all(self) -> list[dict]:
        with self._lock:
            cursor = self._conn.execute("SELECT * FROM jobs ORDER BY created_at")
            columns = [description[0] for description in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]


@dataclass(frozen=True)
class User:
    id: str
    provider: str
    provider_user_id: str
    email: Optional[str]
    name: Optional[str]
    created_at: str
    last_login_at: str


class UserStore:
    """Persists OAuth-authenticated users to the same SQLite file as JobStore
    (WAL mode supports multiple connections to one file)."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                provider_user_id TEXT NOT NULL,
                email TEXT,
                name TEXT,
                created_at TEXT NOT NULL,
                last_login_at TEXT NOT NULL,
                UNIQUE(provider, provider_user_id)
            )
            """
        )
        self._conn.commit()

    def upsert_from_oauth(
        self, provider: str, provider_user_id: str, email: Optional[str], name: Optional[str]
    ) -> User:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM users WHERE provider = ? AND provider_user_id = ?",
                (provider, provider_user_id),
            ).fetchone()
            if existing is None:
                user_id = uuid.uuid4().hex
                self._conn.execute(
                    """
                    INSERT INTO users (id, provider, provider_user_id, email, name, created_at, last_login_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, provider, provider_user_id, email, name, now, now),
                )
            else:
                user_id = existing[0]
                self._conn.execute(
                    "UPDATE users SET email = ?, name = ?, last_login_at = ? WHERE id = ?",
                    (email, name, now, user_id),
                )
            self._conn.commit()
        return self.get(user_id)

    def get(self, user_id: str) -> Optional[User]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, provider, provider_user_id, email, name, created_at, last_login_at "
                "FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return User(
            id=row[0],
            provider=row[1],
            provider_user_id=row[2],
            email=row[3],
            name=row[4],
            created_at=row[5],
            last_login_at=row[6],
        )


user_store = UserStore(Path(settings.storage_dir) / "jobs.db")
