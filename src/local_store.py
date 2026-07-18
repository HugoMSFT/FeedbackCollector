"""
Local SQLite-backed persistence for feedback items.

Why this exists:
    - Timestamped CSV snapshots are write-once and never reflect user edits
      (state / notes / domain / category / audience changes). On process
      restart, those edits were lost unless Fabric SQL was connected.
    - This module gives us a durable, offline-capable, deduplicated store
      keyed by the deterministic ``Feedback_ID`` produced by
      :class:`id_generator.FeedbackIDGenerator`.

Design:
    - Two tables, mirroring the Fabric SQL schema in ``fabric_sql_writer.py``
      so the same merge rules apply on either backend:

        * ``feedback`` - one row per ``Feedback_ID`` holding the collected
          content (title, body, source URL, sentiment, raw category guesses,
          etc.). Re-collection upserts these "content" columns.
        * ``feedback_state`` - one row per ``Feedback_ID`` holding the user's
          edits (State, Notes, Primary_Domain, Category overrides, Audience,
          Last_Updated, Updated_By). Collection runs NEVER overwrite these.

    - A ``meta`` table tracks the schema version for forward-safe migrations.

    - Duplicate handling is centred on ``Feedback_ID``. For collection runs we
      use ``INSERT ... ON CONFLICT(Feedback_ID) DO UPDATE`` and skip columns
      that the user has touched (gated by ``User_Modified_Categorization``).

CSV import / export are intentionally implemented here too so the round-trip
stays consistent with the merge rules.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


# Columns that describe user edits and must NEVER be clobbered by a
# collection run. They live in the feedback_state table.
USER_EDIT_COLUMNS = (
    "State",
    "Feedback_Notes",
    "Last_Updated",
    "Updated_By",
)

# Columns that may have been hand-edited by a user via the UI. When
# User_Modified_Categorization is 1 we keep the user's value during a
# collection upsert; otherwise we let fresh categorisation overwrite.
USER_MODIFIABLE_CATEGORY_COLUMNS = (
    "Primary_Domain",
    "Enhanced_Category",
    "Category",
    "Subcategory",
    "Feature_Area",
    "Audience",
    "Priority",
)

# Columns stored in the feedback_state table (used for joins on read).
STATE_COLUMNS = USER_EDIT_COLUMNS + USER_MODIFIABLE_CATEGORY_COLUMNS + (
    "User_Modified_Categorization",
)

# Columns we serialise as JSON because they may be lists.
JSON_LIST_COLUMNS = ("Matched_Keywords", "Domains")


_SCHEMA_VERSION = 3


_CREATE_FEEDBACK_SQL = """
CREATE TABLE IF NOT EXISTS feedback (
    Feedback_ID TEXT PRIMARY KEY,
    Feedback_Gist TEXT,
    Feedback TEXT,
    Title TEXT,
    Content TEXT,
    Area TEXT,
    Sources TEXT,
    Source TEXT,
    Source_URL TEXT,
    Impacttype TEXT,
    Scenario TEXT,
    Categorization_Confidence REAL,
    Domains TEXT,           -- JSON array
    Matched_Keywords TEXT,  -- JSON array
    Tag TEXT,
    Customer TEXT,
    Author TEXT,
    Created TEXT,
    Created_Date TEXT,
    Organization TEXT,
    Status TEXT,
    Created_by TEXT,
    Sentiment TEXT,
    Sentiment_Score REAL,
    Sentiment_Confidence TEXT,
    Url TEXT,
    Rawfeedback TEXT,
    Score INTEGER,
    View_Count INTEGER,
    Collected_Date TEXT DEFAULT CURRENT_TIMESTAMP,
    Auto_Recategorized_Date TEXT,
    Extra_Json TEXT          -- catch-all for any column the schema doesn't know
);
"""

_CREATE_STATE_SQL = """
CREATE TABLE IF NOT EXISTS feedback_state (
    Feedback_ID TEXT PRIMARY KEY,
    State TEXT,
    Feedback_Notes TEXT,
    Primary_Domain TEXT,
    Enhanced_Category TEXT,
    Category TEXT,
    Subcategory TEXT,
    Feature_Area TEXT,
    Audience TEXT,
    Priority TEXT,
    User_Modified_Categorization INTEGER DEFAULT 0,
    Last_Updated TEXT,
    Updated_By TEXT,
    FOREIGN KEY (Feedback_ID) REFERENCES feedback(Feedback_ID) ON DELETE CASCADE
);
"""

_CREATE_META_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _serialise_value(column: str, value: Any) -> Any:
    """Convert in-memory representation to something SQLite can store."""
    if value is None:
        return None
    if column in JSON_LIST_COLUMNS:
        if isinstance(value, str):
            # Already serialised (e.g. came from a CSV roundtrip). Validate.
            try:
                json.loads(value)
                return value
            except (ValueError, TypeError):
                # Treat the string as a single-element list so we don't lose it.
                return json.dumps([value])
        try:
            return json.dumps(list(value))
        except TypeError:
            return json.dumps([str(value)])
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and pd.isna(value):
        return None
    return value


def _deserialise_value(column: str, value: Any) -> Any:
    if value is None:
        return None
    if column in JSON_LIST_COLUMNS:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (ValueError, TypeError):
                return [value] if value else []
        return value
    return value


def _neutralise_spreadsheet_formula(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    trimmed = value.lstrip()
    if trimmed.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class LocalStore:
    """Thread-safe SQLite-backed feedback store."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        # SQLite supports concurrent readers but a single writer at a time.
        # A lock around write paths keeps Flask threads tidy.
        self._write_lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._initialise()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _txn(self):
        """Yield a connection inside a transaction. Commits on success."""
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def _initialise(self) -> None:
        with self._txn() as conn:
            conn.execute(_CREATE_META_SQL)
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                ("schema_version",),
            ).fetchone()
            try:
                current_version = int(row[0]) if row else 0
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Local database has an invalid schema version") from exc

            if current_version > _SCHEMA_VERSION:
                raise RuntimeError(
                    f"Local database schema {current_version} is newer than "
                    f"this application supports ({_SCHEMA_VERSION})"
                )

            for version in range(current_version + 1, _SCHEMA_VERSION + 1):
                self._apply_migration(conn, version)
                conn.execute(
                    """
                    INSERT INTO meta(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    ("schema_version", str(version)),
                )
        logger.info(f"LocalStore ready at {self.db_path}")

    def _apply_migration(self, conn: sqlite3.Connection, version: int) -> None:
        if version == 1:
            conn.execute(_CREATE_FEEDBACK_SQL)
            conn.execute(_CREATE_STATE_SQL)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_feedback_source ON feedback(Sources)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_feedback_state_state ON feedback_state(State)"
            )
            return

        if version == 2:
            # Older development databases may predate fields now required by
            # state-preserving upserts. ALTER only the columns that are absent.
            self._add_column_if_missing(
                conn,
                "feedback",
                "Extra_Json",
                "TEXT",
            )
            self._add_column_if_missing(
                conn,
                "feedback",
                "Auto_Recategorized_Date",
                "TEXT",
            )
            self._add_column_if_missing(
                conn,
                "feedback_state",
                "User_Modified_Categorization",
                "INTEGER DEFAULT 0",
            )
            self._add_column_if_missing(
                conn,
                "feedback_state",
                "Last_Updated",
                "TEXT",
            )
            self._add_column_if_missing(
                conn,
                "feedback_state",
                "Updated_By",
                "TEXT",
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(Created)"
            )
            return

        if version == 3:
            # Earlier builds copied automatic categorization into the state
            # table. Preserve those values in Extra_Json before clearing the
            # non-user overrides so upgrades cannot discard categorization.
            rows = conn.execute(
                """
                SELECT
                    f.Feedback_ID,
                    f.Extra_Json,
                    s.Primary_Domain,
                    s.Enhanced_Category,
                    s.Category,
                    s.Subcategory,
                    s.Feature_Area,
                    s.Audience,
                    s.Priority
                FROM feedback f
                INNER JOIN feedback_state s
                    ON s.Feedback_ID = f.Feedback_ID
                WHERE COALESCE(s.User_Modified_Categorization, 0) = 0
                """
            ).fetchall()
            category_columns = USER_MODIFIABLE_CATEGORY_COLUMNS
            for row in rows:
                try:
                    extras = json.loads(row["Extra_Json"] or "{}")
                except (TypeError, ValueError):
                    extras = {}
                if not isinstance(extras, dict):
                    extras = {}

                changed = False
                for column in category_columns:
                    value = row[column]
                    if value is not None and column not in extras:
                        extras[column] = value
                        changed = True
                if changed:
                    conn.execute(
                        "UPDATE feedback SET Extra_Json = ? WHERE Feedback_ID = ?",
                        (
                            json.dumps(extras, default=str),
                            row["Feedback_ID"],
                        ),
                    )

            conn.execute(
                """
                UPDATE feedback_state
                SET Primary_Domain = NULL,
                    Enhanced_Category = NULL,
                    Category = NULL,
                    Subcategory = NULL,
                    Feature_Area = NULL,
                    Audience = NULL,
                    Priority = NULL
                WHERE COALESCE(User_Modified_Categorization, 0) = 0
                """
            )
            return

        raise RuntimeError(f"No migration is defined for schema version {version}")

    def _add_column_if_missing(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        if column not in self._table_columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _table_columns(self, conn: sqlite3.Connection, table: str) -> List[str]:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return [row[1] for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def count(self) -> int:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT COUNT(*) FROM feedback")
            return int(cur.fetchone()[0])
        finally:
            conn.close()

    def get_all_feedback_ids(self) -> List[str]:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT Feedback_ID FROM feedback")
            return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    def get_user_modified_ids(self) -> List[str]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT Feedback_ID FROM feedback_state WHERE User_Modified_Categorization = 1"
            )
            return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    def load_all(self) -> List[Dict[str, Any]]:
        """Return every feedback row joined with its state, as plain dicts.

        The returned dicts use the same key shape as the in-memory
        ``last_collected_feedback`` list so callers don't need to translate.
        """
        conn = self._connect()
        try:
            feedback_cols = self._table_columns(conn, "feedback")
            state_cols = self._table_columns(conn, "feedback_state")

            cur = conn.execute(
                """
                SELECT f.*,
                       s.State AS s_State,
                       s.Feedback_Notes AS s_Feedback_Notes,
                       s.Primary_Domain AS s_Primary_Domain,
                       s.Enhanced_Category AS s_Enhanced_Category,
                       s.Category AS s_Category,
                       s.Subcategory AS s_Subcategory,
                       s.Feature_Area AS s_Feature_Area,
                       s.Audience AS s_Audience,
                       s.Priority AS s_Priority,
                       s.User_Modified_Categorization AS s_User_Modified_Categorization,
                       s.Last_Updated AS s_Last_Updated,
                       s.Updated_By AS s_Updated_By
                FROM feedback f
                LEFT JOIN feedback_state s ON s.Feedback_ID = f.Feedback_ID
                """
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        items: List[Dict[str, Any]] = []
        for row in rows:
            item: Dict[str, Any] = {}
            for col in feedback_cols:
                item[col] = _deserialise_value(col, row[col])

            # Overlay state columns (only if non-NULL so we don't blank fields
            # that the feedback table happened to carry).
            state_overlay: List[Tuple[str, str]] = [
                ("State", "s_State"),
                ("Feedback_Notes", "s_Feedback_Notes"),
                ("Primary_Domain", "s_Primary_Domain"),
                ("Enhanced_Category", "s_Enhanced_Category"),
                ("Category", "s_Category"),
                ("Subcategory", "s_Subcategory"),
                ("Feature_Area", "s_Feature_Area"),
                ("Audience", "s_Audience"),
                ("Priority", "s_Priority"),
                ("Last_Updated", "s_Last_Updated"),
                ("Updated_By", "s_Updated_By"),
            ]
            for target, alias in state_overlay:
                v = row[alias]
                if v is not None:
                    item[target] = v

            user_modified = row["s_User_Modified_Categorization"]
            item["User_Modified_Categorization"] = bool(user_modified) if user_modified is not None else False

            # Restore Extra_Json into the dict so collectors that wrote
            # extra fields still expose them to the UI.
            extra_json = item.pop("Extra_Json", None)
            if extra_json:
                try:
                    extras = json.loads(extra_json)
                    if isinstance(extras, dict):
                        for k, v in extras.items():
                            item.setdefault(k, v)
                except (ValueError, TypeError):
                    logger.warning(
                        "Ignoring invalid Extra_Json for feedback ID %s",
                        item.get("Feedback_ID"),
                    )

            items.append(item)
        return items

    # ------------------------------------------------------------------
    # Upsert from collection
    # ------------------------------------------------------------------

    def upsert_feedback_items(
        self,
        items: Iterable[Dict[str, Any]],
    ) -> Dict[str, int]:
        """Insert/update feedback rows from a collection run.

        Duplicate handling:
            * Keyed on ``Feedback_ID`` (deterministic, content-derived).
            * For pre-existing rows we refresh content columns but keep:
              - all of ``feedback_state`` (state, notes, audit fields)
              - user-modified categorisation columns when the
                ``User_Modified_Categorization`` flag is set in
                ``feedback_state``
            * Items missing ``Feedback_ID`` are skipped (with a warning).

        Returns a small summary dict for logging.
        """
        items = list(items)
        if not items:
            return {"inserted": 0, "updated": 0, "skipped": 0}

        inserted = 0
        updated = 0
        skipped = 0

        with self._txn() as conn:
            feedback_cols = set(self._table_columns(conn, "feedback"))
            user_modified_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT Feedback_ID FROM feedback_state WHERE User_Modified_Categorization = 1"
                ).fetchall()
            }
            existing_ids = {
                row[0]
                for row in conn.execute("SELECT Feedback_ID FROM feedback").fetchall()
            }
            existing_extras: Dict[str, Dict[str, Any]] = {}
            for existing_row in conn.execute(
                """
                SELECT Feedback_ID, Extra_Json
                FROM feedback
                WHERE Extra_Json IS NOT NULL
                """
            ).fetchall():
                try:
                    parsed = json.loads(existing_row[1])
                except (TypeError, ValueError):
                    logger.warning(
                        "Ignoring invalid stored Extra_Json for feedback ID %s",
                        existing_row[0],
                    )
                    continue
                if isinstance(parsed, dict):
                    existing_extras[existing_row[0]] = parsed

            for item in items:
                fid = item.get("Feedback_ID")
                if not fid:
                    skipped += 1
                    continue
                if not isinstance(fid, str) or len(fid) > 200:
                    logger.warning("Skipping feedback item with an invalid ID")
                    skipped += 1
                    continue

                row, extras = self._split_known_columns(item, feedback_cols)
                row["Feedback_ID"] = fid

                is_existing = fid in existing_ids
                if is_existing and fid in user_modified_ids:
                    # Don't overwrite categorisation columns the user
                    # explicitly changed.
                    previous_extras = existing_extras.get(fid, {})
                    for col in USER_MODIFIABLE_CATEGORY_COLUMNS:
                        row.pop(col, None)
                        extras.pop(col, None)
                        if col in previous_extras:
                            extras[col] = previous_extras[col]
                if extras:
                    row["Extra_Json"] = json.dumps(extras, default=str)

                cols = list(row.keys())
                placeholders = ",".join("?" for _ in cols)
                values = [_serialise_value(c, row[c]) for c in cols]

                if is_existing:
                    update_cols = [c for c in cols if c != "Feedback_ID"]
                    if not update_cols:
                        updated += 1
                        continue
                    set_clause = ",".join(
                        f"{c}=excluded.{c}" for c in update_cols
                    )
                    sql = (
                        f"INSERT INTO feedback ({','.join(cols)}) VALUES ({placeholders}) "
                        f"ON CONFLICT(Feedback_ID) DO UPDATE SET {set_clause}"
                    )
                    conn.execute(sql, values)
                    updated += 1
                else:
                    sql = (
                        f"INSERT INTO feedback ({','.join(cols)}) VALUES ({placeholders}) "
                        f"ON CONFLICT(Feedback_ID) DO NOTHING"
                    )
                    conn.execute(sql, values)
                    existing_ids.add(fid)
                    inserted += 1

                # Make sure a feedback_state row exists, but never reset it.
                user_modified = int(
                    bool(item.get("User_Modified_Categorization"))
                )
                state_seed = {
                    "Feedback_ID": fid,
                    "State": item.get("State") or "NEW",
                    "Feedback_Notes": item.get("Feedback_Notes") or "",
                    "Primary_Domain": (
                        item.get("Primary_Domain") if user_modified else None
                    ),
                    "Enhanced_Category": (
                        item.get("Enhanced_Category") if user_modified else None
                    ),
                    "Category": (
                        item.get("Category") if user_modified else None
                    ),
                    "Subcategory": (
                        item.get("Subcategory") if user_modified else None
                    ),
                    "Feature_Area": (
                        item.get("Feature_Area") if user_modified else None
                    ),
                    "Audience": (
                        item.get("Audience") if user_modified else None
                    ),
                    "Priority": (
                        item.get("Priority") if user_modified else None
                    ),
                    "User_Modified_Categorization": user_modified,
                    "Last_Updated": item.get("Last_Updated") or datetime.now(timezone.utc).isoformat(),
                    "Updated_By": item.get("Updated_By") or "System",
                }
                cols2 = list(state_seed.keys())
                placeholders2 = ",".join("?" for _ in cols2)
                conn.execute(
                    f"INSERT INTO feedback_state ({','.join(cols2)}) VALUES ({placeholders2}) "
                    f"ON CONFLICT(Feedback_ID) DO NOTHING",
                    [state_seed[c] for c in cols2],
                )

        logger.info(
            f"LocalStore upsert: inserted={inserted}, updated={updated}, skipped={skipped}"
        )
        return {"inserted": inserted, "updated": updated, "skipped": skipped}

    @staticmethod
    def _split_known_columns(
        item: Dict[str, Any],
        known: Iterable[str],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Partition a feedback dict into (known feedback cols, extras)."""
        known_set = set(known)
        # Workflow state and audit fields belong only on feedback_state.
        # Automatic category values remain in Extra_Json; user overrides are
        # separately overlaid from feedback_state when the row is loaded.
        ignore = set(USER_EDIT_COLUMNS) | {"User_Modified_Categorization"}
        row: Dict[str, Any] = {}
        extras: Dict[str, Any] = {}
        for k, v in item.items():
            if k in ignore:
                continue
            if k in known_set:
                row[k] = v
            else:
                extras[k] = v
        return row, extras

    # ------------------------------------------------------------------
    # State updates (called by edit endpoints)
    # ------------------------------------------------------------------

    def update_state(
        self,
        feedback_id: str,
        *,
        state: Optional[str] = None,
        notes: Optional[str] = None,
        primary_domain: Optional[str] = None,
        enhanced_category: Optional[str] = None,
        category: Optional[str] = None,
        subcategory: Optional[str] = None,
        feature_area: Optional[str] = None,
        audience: Optional[str] = None,
        priority: Optional[str] = None,
        updated_by: Optional[str] = None,
        mark_user_modified: bool = False,
    ) -> bool:
        """Persist a partial state update for an existing feedback item."""
        if (
            not isinstance(feedback_id, str)
            or not feedback_id
            or len(feedback_id) > 200
        ):
            return False

        updates: Dict[str, Any] = {}
        if state is not None:
            updates["State"] = state
        if notes is not None:
            updates["Feedback_Notes"] = notes
        if primary_domain is not None:
            updates["Primary_Domain"] = primary_domain
        if enhanced_category is not None:
            updates["Enhanced_Category"] = enhanced_category
        if category is not None:
            updates["Category"] = category
        if subcategory is not None:
            updates["Subcategory"] = subcategory
        if feature_area is not None:
            updates["Feature_Area"] = feature_area
        if audience is not None:
            updates["Audience"] = audience
        if priority is not None:
            updates["Priority"] = priority
        if mark_user_modified:
            updates["User_Modified_Categorization"] = 1

        updates["Last_Updated"] = datetime.now(timezone.utc).isoformat()
        updates["Updated_By"] = updated_by or "user"

        with self._txn() as conn:
            feedback_exists = conn.execute(
                "SELECT 1 FROM feedback WHERE Feedback_ID = ?",
                (feedback_id,),
            ).fetchone() is not None
            if not feedback_exists:
                return False

            cur = conn.execute(
                "SELECT 1 FROM feedback_state WHERE Feedback_ID = ?",
                (feedback_id,),
            )
            exists = cur.fetchone() is not None

            if exists:
                set_clause = ", ".join(f"{c}=?" for c in updates.keys())
                sql = f"UPDATE feedback_state SET {set_clause} WHERE Feedback_ID = ?"
                conn.execute(sql, [*updates.values(), feedback_id])
            else:
                cols = ["Feedback_ID", *updates.keys()]
                placeholders = ",".join("?" for _ in cols)
                values = [feedback_id, *updates.values()]
                conn.execute(
                    f"INSERT INTO feedback_state ({','.join(cols)}) VALUES ({placeholders})",
                    values,
                )
        return True

    # ------------------------------------------------------------------
    # Bulk state upsert (used when syncing data pulled from Fabric)
    # ------------------------------------------------------------------

    def bulk_upsert_states(self, state_rows: Iterable[Dict[str, Any]]) -> int:
        """Merge Fabric state rows into existing local feedback items."""
        aliases = {
            "State": ("State", "state"),
            "Feedback_Notes": ("Feedback_Notes", "notes"),
            "Primary_Domain": ("Primary_Domain", "domain"),
            "Enhanced_Category": ("Enhanced_Category", "enhanced_category"),
            "Category": ("Category", "category"),
            "Subcategory": ("Subcategory", "subcategory"),
            "Feature_Area": ("Feature_Area", "feature_area"),
            "Audience": ("Audience", "audience"),
            "Priority": ("Priority", "priority"),
            "User_Modified_Categorization": (
                "User_Modified_Categorization",
                "user_modified_categorization",
            ),
            "Last_Updated": ("Last_Updated", "last_updated"),
            "Updated_By": ("Updated_By", "updated_by"),
        }
        count = 0
        with self._txn() as conn:
            for row in state_rows:
                fid = row.get("Feedback_ID") or row.get("feedback_id")
                if (
                    not isinstance(fid, str)
                    or not fid
                    or len(fid) > 200
                ):
                    continue

                feedback_exists = conn.execute(
                    "SELECT 1 FROM feedback WHERE Feedback_ID = ?",
                    (fid,),
                ).fetchone()
                if feedback_exists is None:
                    logger.warning(
                        "Skipping Fabric state for unknown feedback ID %s",
                        fid,
                    )
                    continue

                updates: Dict[str, Any] = {}
                for column, candidate_keys in aliases.items():
                    for key in candidate_keys:
                        if key in row:
                            updates[column] = row[key]
                            break

                if not updates.get("Last_Updated"):
                    updates["Last_Updated"] = datetime.now(timezone.utc).isoformat()
                if not updates.get("Updated_By"):
                    updates["Updated_By"] = "fabric_sync"
                if "User_Modified_Categorization" in updates:
                    updates["User_Modified_Categorization"] = int(
                        bool(updates["User_Modified_Categorization"])
                    )

                existing_state = conn.execute(
                    """
                    SELECT
                        Last_Updated,
                        Updated_By,
                        User_Modified_Categorization
                    FROM feedback_state
                    WHERE Feedback_ID = ?
                    """,
                    (fid,),
                ).fetchone()
                if existing_state is not None:
                    existing_user_modified = bool(existing_state[2])
                    incoming_user_modified = bool(
                        updates.get("User_Modified_Categorization")
                    )
                    if (
                        existing_user_modified
                        and not incoming_user_modified
                    ):
                        for column in USER_MODIFIABLE_CATEGORY_COLUMNS:
                            updates.pop(column, None)

                    incoming_timestamp = _parse_timestamp(
                        updates.get("Last_Updated")
                    )
                    existing_timestamp = _parse_timestamp(
                        existing_state["Last_Updated"]
                    )
                    existing_actor = str(
                        existing_state["Updated_By"] or ""
                    ).casefold()
                    if (
                        incoming_timestamp is not None
                        and existing_timestamp is not None
                        and existing_actor != "system"
                        and incoming_timestamp < existing_timestamp
                    ):
                        logger.info(
                            "Skipping older Fabric state for feedback ID %s",
                            fid,
                        )
                        continue

                if existing_state is None:
                    payload = {"Feedback_ID": fid, **updates}
                    cols = list(payload.keys())
                    placeholders = ",".join("?" for _ in cols)
                    conn.execute(
                        f"INSERT INTO feedback_state ({','.join(cols)}) "
                        f"VALUES ({placeholders})",
                        [_serialise_value(c, payload[c]) for c in cols],
                    )
                else:
                    assignments = []
                    values = []
                    for column, value in updates.items():
                        if column == "User_Modified_Categorization":
                            assignments.append(
                                "User_Modified_Categorization = "
                                "CASE WHEN User_Modified_Categorization = 1 OR ? = 1 "
                                "THEN 1 ELSE 0 END"
                            )
                        else:
                            assignments.append(f"{column} = ?")
                        values.append(_serialise_value(column, value))
                    conn.execute(
                        f"UPDATE feedback_state SET {','.join(assignments)} "
                        "WHERE Feedback_ID = ?",
                        [*values, fid],
                    )
                count += 1
        return count

    # ------------------------------------------------------------------
    # CSV export / import
    # ------------------------------------------------------------------

    def export_to_csv(
        self,
        target_dir: str,
        columns: Optional[List[str]] = None,
        prefix: str = "feedback",
        rows: Optional[Iterable[Dict[str, Any]]] = None,
    ) -> str:
        """Write the joined view to a timestamped CSV. Returns the file path."""
        os.makedirs(target_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{prefix}_{timestamp}.csv"
        filepath = os.path.join(target_dir, filename)

        rows = self.load_all() if rows is None else list(rows)
        if not rows:
            # Still produce an empty file with the headers so downstream
            # tooling doesn't error.
            df = pd.DataFrame(columns=columns or [])
        else:
            df = pd.DataFrame(rows)
            if columns:
                for col in columns:
                    if col not in df.columns:
                        df[col] = None
                df = df.reindex(columns=columns + [c for c in df.columns if c not in columns])

            for col in JSON_LIST_COLUMNS:
                if col in df.columns:
                    df[col] = df[col].apply(
                        lambda v: json.dumps(v) if isinstance(v, (list, dict)) else v
                    )

            # Spreadsheet applications treat leading =, +, -, and @ as
            # formulas. Prefix user-controlled text so exports remain data.
            for col in df.columns:
                if df[col].dtype == object:
                    df[col] = df[col].apply(_neutralise_spreadsheet_formula)

        df.to_csv(filepath, index=False, encoding="utf-8-sig")
        logger.info(f"Exported {len(df)} feedback rows to {filepath}")
        return filepath

    def import_from_csv(
        self,
        filepath: str,
        mode: str = "merge",
    ) -> Dict[str, int]:
        """Import a CSV file produced by ``export_to_csv``.

        Args:
            filepath: Path to a CSV on disk.
            mode: One of:
                - ``"skip_existing"``: leave existing Feedback_IDs untouched.
                - ``"overwrite"``: replace both content and state columns.
                - ``"merge"`` (default): refresh content columns, but keep
                  the existing state row when one is already present.

        Returns:
            ``{"new": int, "updated": int, "skipped": int, "total": int}``.
        """
        if mode not in {"skip_existing", "overwrite", "merge"}:
            raise ValueError(f"Unknown import mode: {mode}")

        if not os.path.exists(filepath):
            raise FileNotFoundError(filepath)

        df = pd.read_csv(filepath, encoding="utf-8-sig", dtype=str, keep_default_na=False)
        # Restore NaN semantics for empty cells then convert to None.
        df = df.where(df != "", None)

        records = df.to_dict("records")

        # Make sure each record has a Feedback_ID; if not, derive one.
        from id_generator import FeedbackIDGenerator  # local to avoid cycles

        new_count = 0
        updated_count = 0
        skipped_count = 0

        with self._txn() as conn:
            existing_feedback = {
                row[0]
                for row in conn.execute("SELECT Feedback_ID FROM feedback").fetchall()
            }
            existing_state = {
                row[0]
                for row in conn.execute("SELECT Feedback_ID FROM feedback_state").fetchall()
            }
            feedback_cols = set(self._table_columns(conn, "feedback"))

            for record in records:
                # Decode JSON list columns coming back from CSV.
                for col in JSON_LIST_COLUMNS:
                    if col in record and isinstance(record[col], str):
                        try:
                            record[col] = json.loads(record[col])
                        except (ValueError, TypeError):
                            pass

                fid = record.get("Feedback_ID")
                if not fid:
                    fid = FeedbackIDGenerator.generate_id_from_feedback_dict(record)
                    record["Feedback_ID"] = fid
                elif len(fid) > 200:
                    raise ValueError(
                        "CSV Feedback_ID values must not exceed 200 characters"
                    )

                already_exists = fid in existing_feedback

                if already_exists and mode == "skip_existing":
                    skipped_count += 1
                    continue

                row, extras = self._split_known_columns(record, feedback_cols)
                row["Feedback_ID"] = fid
                if extras:
                    row["Extra_Json"] = json.dumps(extras, default=str)

                cols = list(row.keys())
                placeholders = ",".join("?" for _ in cols)
                values = [_serialise_value(c, row[c]) for c in cols]

                if already_exists:
                    update_cols = [c for c in cols if c != "Feedback_ID"]
                    if update_cols:
                        set_clause = ",".join(
                            f"{c}=excluded.{c}" for c in update_cols
                        )
                        conn.execute(
                            f"INSERT INTO feedback ({','.join(cols)}) VALUES ({placeholders}) "
                            f"ON CONFLICT(Feedback_ID) DO UPDATE SET {set_clause}",
                            values,
                        )
                    updated_count += 1
                else:
                    conn.execute(
                        f"INSERT INTO feedback ({','.join(cols)}) VALUES ({placeholders})",
                        values,
                    )
                    existing_feedback.add(fid)
                    new_count += 1

                # State write rules per mode.
                state_already = fid in existing_state
                should_write_state = (
                    not state_already
                    or mode == "overwrite"
                )
                if should_write_state:
                    user_modified = int(
                        str(
                            record.get(
                                "User_Modified_Categorization",
                                "0",
                            )
                        )
                        in {"1", "True", "true"}
                    )
                    state_payload = {
                        "Feedback_ID": fid,
                        "State": record.get("State") or "NEW",
                        "Feedback_Notes": record.get("Feedback_Notes") or "",
                        "Primary_Domain": (
                            record.get("Primary_Domain")
                            if user_modified
                            else None
                        ),
                        "Enhanced_Category": (
                            record.get("Enhanced_Category")
                            if user_modified
                            else None
                        ),
                        "Category": (
                            record.get("Category") if user_modified else None
                        ),
                        "Subcategory": (
                            record.get("Subcategory") if user_modified else None
                        ),
                        "Feature_Area": (
                            record.get("Feature_Area") if user_modified else None
                        ),
                        "Audience": (
                            record.get("Audience") if user_modified else None
                        ),
                        "Priority": (
                            record.get("Priority") if user_modified else None
                        ),
                        "User_Modified_Categorization": user_modified,
                        "Last_Updated": record.get("Last_Updated")
                        or datetime.now(timezone.utc).isoformat(),
                        "Updated_By": record.get("Updated_By") or "csv_import",
                    }
                    cols2 = list(state_payload.keys())
                    placeholders2 = ",".join("?" for _ in cols2)
                    set_clause2 = ",".join(f"{c}=excluded.{c}" for c in cols2 if c != "Feedback_ID")
                    conn.execute(
                        f"INSERT INTO feedback_state ({','.join(cols2)}) VALUES ({placeholders2}) "
                        f"ON CONFLICT(Feedback_ID) DO UPDATE SET {set_clause2}",
                        [state_payload[c] for c in cols2],
                    )
                    existing_state.add(fid)

        logger.info(
            f"CSV import ({mode}) from {filepath}: new={new_count}, "
            f"updated={updated_count}, skipped={skipped_count}, total={len(records)}"
        )
        return {
            "new": new_count,
            "updated": updated_count,
            "skipped": skipped_count,
            "total": len(records),
        }

    # ------------------------------------------------------------------
    # First-run migration
    # ------------------------------------------------------------------

    def import_legacy_csv_if_empty(self, data_dir: str) -> Optional[Dict[str, int]]:
        """Seed the DB from the most recent ``feedback_*.csv`` if empty."""
        if self.count() > 0:
            return None
        try:
            candidates = [
                f for f in os.listdir(data_dir)
                if f.startswith("feedback_") and f.endswith(".csv")
            ]
        except FileNotFoundError:
            return None
        if not candidates:
            return None
        latest = sorted(candidates)[-1]
        filepath = os.path.join(data_dir, latest)
        logger.info(f"LocalStore is empty - seeding from {filepath}")
        try:
            return self.import_from_csv(filepath, mode="merge")
        except Exception as e:
            logger.error(f"Failed to seed local store from {filepath}: {e}")
            return None
