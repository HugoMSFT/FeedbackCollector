from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("starting", "in_progress", "cancelling")
_UPDATABLE_COLUMNS = {
    "status",
    "progress",
    "processed_items",
    "completed",
    "success",
    "message",
    "operation",
    "cancel_requested",
    "result_json",
}

_CREATE_JOBS_SQL = """
CREATE TABLE IF NOT EXISTS background_jobs (
    operation_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    total_items INTEGER NOT NULL DEFAULT 0,
    processed_items INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    operation TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    logs_json TEXT NOT NULL DEFAULT '[]',
    result_json TEXT NOT NULL DEFAULT '{}'
);
"""


class JobManager:
    """Thread-safe SQLite storage for background operation state."""

    def __init__(self, db_path: str, retention_hours: int = 24):
        self.db_path = db_path
        self.retention_hours = max(1, retention_hours)
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _initialise(self) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(_CREATE_JOBS_SQL)
            placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
            conn.execute(
                f"""
                UPDATE background_jobs
                SET status = 'interrupted',
                    completed = 1,
                    success = 0,
                    message = 'Operation was interrupted by an application restart.',
                    updated_at = ?
                WHERE status IN ({placeholders})
                """,
                [self._now(), *_ACTIVE_STATUSES],
            )
            conn.commit()
        self.cleanup()

    def create(self, kind: str, total_items: int) -> str:
        operation_id = str(uuid.uuid4())
        now = self._now()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO background_jobs (
                    operation_id, kind, status, progress, total_items,
                    processed_items, started_at, updated_at, operation
                ) VALUES (?, ?, 'starting', 0, ?, 0, ?, ?, 'Initializing')
                """,
                (operation_id, kind, max(0, total_items), now, now),
            )
            conn.commit()
        self.cleanup()
        return operation_id

    def update(
        self,
        operation_id: str,
        *,
        only_if_incomplete: bool = False,
        **changes: Any,
    ) -> bool:
        invalid = set(changes) - _UPDATABLE_COLUMNS
        if invalid:
            raise ValueError(f"Unsupported job fields: {sorted(invalid)}")
        if not changes:
            return self.exists(operation_id)

        if "result_json" in changes and not isinstance(changes["result_json"], str):
            changes["result_json"] = json.dumps(changes["result_json"], default=str)

        changes["updated_at"] = self._now()
        assignments = ", ".join(f"{column} = ?" for column in changes)
        values = [
            int(value) if column in {"completed", "success", "cancel_requested"} else value
            for column, value in changes.items()
        ]
        with self._lock, self._connection() as conn:
            predicate = (
                "operation_id = ? AND completed = 0"
                if only_if_incomplete
                else "operation_id = ?"
            )
            cursor = conn.execute(
                f"UPDATE background_jobs SET {assignments} WHERE {predicate}",
                [*values, operation_id],
            )
            conn.commit()
            return cursor.rowcount > 0

    def append_log(self, operation_id: str, message: str, level: str = "info") -> None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT logs_json FROM background_jobs WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            logs = json.loads(row["logs_json"] or "[]")
            sequence = logs[-1]["sequence"] + 1 if logs else 1
            logs.append(
                {
                    "sequence": sequence,
                    "message": message,
                    "type": level,
                    "timestamp": self._now(),
                }
            )
            logs = logs[-500:]
            conn.execute(
                """
                UPDATE background_jobs
                SET logs_json = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (json.dumps(logs), self._now(), operation_id),
            )
            conn.commit()

    def snapshot(self, operation_id: str, after: int = 0) -> Optional[Dict[str, Any]]:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM background_jobs WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None

        result = dict(row)
        logs = json.loads(result.pop("logs_json") or "[]")
        result_payload = json.loads(result.pop("result_json") or "{}")
        if isinstance(result_payload, dict):
            for key, value in result_payload.items():
                if key not in result:
                    result[key] = value
        result["logs"] = [
            log for log in logs if int(log.get("sequence", 0)) > max(0, after)
        ]
        result["next_log_cursor"] = (
            int(logs[-1].get("sequence", 0)) if logs else max(0, after)
        )
        result["completed"] = bool(result["completed"])
        result["success"] = bool(result["success"])
        result["cancel_requested"] = bool(result["cancel_requested"])
        return result

    def exists(self, operation_id: str) -> bool:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM background_jobs WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return row is not None

    def cancellation_requested(self, operation_id: str) -> bool:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT cancel_requested
                FROM background_jobs
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def request_cancellation(self, operation_id: str) -> bool:
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE background_jobs
                SET cancel_requested = 1,
                    status = 'cancelling',
                    operation = 'Cancelling',
                    updated_at = ?
                WHERE operation_id = ? AND completed = 0
                """,
                (self._now(), operation_id),
            )
            conn.commit()
        if cursor.rowcount == 0:
            return False
        self.append_log(operation_id, "Cancellation requested", "warning")
        return True

    def complete(
        self,
        operation_id: str,
        message: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        snapshot = self.snapshot(operation_id)
        total_items = snapshot["total_items"] if snapshot else 0
        self.update(
            operation_id,
            only_if_incomplete=True,
            status="completed",
            progress=100,
            processed_items=total_items,
            completed=True,
            success=True,
            message=message,
            operation="Completed",
            result_json=result or {},
        )

    def cancel(self, operation_id: str) -> None:
        transitioned = self.update(
            operation_id,
            only_if_incomplete=True,
            status="cancelled",
            completed=True,
            success=False,
            message="Operation cancelled.",
            operation="Cancelled",
        )
        if transitioned:
            self.append_log(operation_id, "Operation cancelled", "warning")

    def fail(self, operation_id: str, message: str) -> None:
        transitioned = self.update(
            operation_id,
            only_if_incomplete=True,
            status="error",
            completed=True,
            success=False,
            message=message,
            operation="Failed",
        )
        if transitioned:
            self.append_log(operation_id, message, "danger")

    def cleanup(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.retention_hours)
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM background_jobs
                WHERE completed = 1 AND updated_at < ?
                """,
                (cutoff.isoformat(),),
            )
            conn.commit()
            return cursor.rowcount
