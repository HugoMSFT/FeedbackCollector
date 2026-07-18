"""
Fabric SQL Database Writer for feedback state management
Uses direct SQL operations instead of complex PySpark/Lakehouse operations
Much more reliable and faster than the lakehouse approach
"""

try:
    import pyodbc
except ImportError:
    pyodbc = None

import pandas as pd
import json
import logging
import math
import struct
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from config import FABRIC_SQL_SERVER, FABRIC_SQL_DATABASE

logger = logging.getLogger(__name__)


_FABRIC_ALWAYS_SYNC_COLUMNS = (
    "Title",
    "Content",
    "Source",
    "Source_URL",
    "Author",
    "Created_Date",
    "Sentiment",
    "Sentiment_Score",
    "Sentiment_Confidence",
    "Feedback_Gist",
    "Area",
    "Scenario",
    "Tag",
    "Organization",
    "Status",
    "Created_by",
    "Rawfeedback",
    "Matched_Keywords",
)

_FABRIC_CATEGORY_COLUMNS = (
    "Primary_Category",
    "Enhanced_Category",
    "Audience",
    "Priority",
    "Impacttype",
    "Category",
    "Subcategory",
    "Feature_Area",
    "Categorization_Confidence",
    "Primary_Domain",
    "Domains",
    "Auto_Recategorized_Date",
)


class FabricWriteCancelled(RuntimeError):
    """Raised when a caller cancels a cooperative Fabric write."""


def _require_pyodbc():
    if pyodbc is None:
        raise ImportError(
            "pyodbc is not installed. Fabric SQL features require pyodbc and a compatible SQL Server ODBC driver."
        )


class FabricSQLWriter:
    """Handles writing feedback state changes to Fabric SQL Database"""

    def __init__(self, bearer_token: str = None):
        _require_pyodbc()
        self.bearer_token = bearer_token
        self.server = FABRIC_SQL_SERVER
        self.database = FABRIC_SQL_DATABASE
        self.current_user = None  # Will be set after connection

        # Validate that required configuration is present
        if not self.server or not self.database:
            raise ValueError("FABRIC_SQL_SERVER and FABRIC_SQL_DATABASE must be configured in .env file")

    def connect_interactive(self):
        """Reject interactive authentication in the web application."""
        raise RuntimeError(
            "Interactive Fabric authentication is not supported. "
            "Validate a bearer token through the application first."
        )

    def _connect(self):
        if not self.bearer_token:
            raise ValueError("A validated Fabric bearer token is required")
        return self.connect_with_token(self.bearer_token)

    def connect_with_token(self, bearer_token: str):
        """Connect using bearer token (for production)"""
        if not isinstance(bearer_token, str) or not bearer_token.strip():
            raise ValueError("A Fabric bearer token is required")
        bearer_token = bearer_token.strip()
        if bearer_token.lower().startswith("bearer "):
            bearer_token = bearer_token[7:].strip()
        if not bearer_token:
            raise ValueError("A Fabric bearer token is required")

        # Try multiple driver names in order of preference
        drivers_to_try = [
            "ODBC Driver 18 for SQL Server",
            "ODBC Driver 17 for SQL Server",
            "ODBC Driver 13 for SQL Server",
            "SQL Server Native Client 11.0",
        ]

        for driver_name in drivers_to_try:
            try:
                logger.info(f"Trying to connect with bearer token using driver: {driver_name}")

                connection_string = f"""
                DRIVER={{{driver_name}}};
                SERVER={self.server};
                DATABASE={self.database};
                Encrypt=yes;
                TrustServerCertificate=no;
                """

                # SQL_COPT_SS_ACCESS_TOKEN expects a length-prefixed UTF-16-LE
                # ACCESSTOKEN structure, not the raw token bytes.
                token_bytes = bearer_token.encode("utf-16-le")
                token_struct = struct.pack(
                    f"<I{len(token_bytes)}s",
                    len(token_bytes),
                    token_bytes,
                )

                # Use token for authentication (SQL_COPT_SS_ACCESS_TOKEN = 1256)
                conn = pyodbc.connect(
                    connection_string,
                    attrs_before={1256: token_struct},
                    timeout=30,
                )
                logger.info(
                    f"Successfully connected to Fabric SQL database using bearer token with driver: {driver_name}"
                )
                return conn

            except Exception as e:
                logger.warning(f"Bearer token connection with driver {driver_name} failed: {e}")
                continue

        # If all drivers failed
        raise Exception(
            f"Failed to connect with bearer token using any available driver. Available drivers: {pyodbc.drivers()}"
        )

    def ensure_feedback_state_table(self, conn):
        """Create FeedbackState table if it doesn't exist"""
        cursor = conn.cursor()

        # Check if table exists
        cursor.execute(
            """
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_NAME = 'FeedbackState'
        """
        )

        table_exists = cursor.fetchone()[0] > 0

        if not table_exists:
            logger.info("Creating FeedbackState table...")

            create_table_sql = """
            CREATE TABLE FeedbackState (
                Feedback_ID NVARCHAR(200) PRIMARY KEY,
                State NVARCHAR(20),
                Feedback_Notes NVARCHAR(MAX),
                Primary_Domain NVARCHAR(100),
                Category NVARCHAR(100),
                Subcategory NVARCHAR(200),
                Feature_Area NVARCHAR(200),
                Last_Updated DATETIME2 DEFAULT GETDATE(),
                Updated_By NVARCHAR(100)
            );
            """

            cursor.execute(create_table_sql)
            conn.commit()
            logger.info("FeedbackState table created successfully")
        else:
            logger.info("FeedbackState table already exists - checking for missing columns...")
            self.migrate_feedback_state_table(conn)

    def migrate_feedback_state_table(self, conn):
        """Add missing columns to existing FeedbackState table"""
        cursor = conn.cursor()
        new_columns = ["Category NVARCHAR(100)", "Subcategory NVARCHAR(200)", "Feature_Area NVARCHAR(200)"]

        try:
            for column_def in new_columns:
                column_name = column_def.split()[0]
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_NAME = 'FeedbackState' AND COLUMN_NAME = ?
                """,
                    [column_name],
                )

                column_exists = cursor.fetchone()[0] > 0
                if not column_exists:
                    cursor.execute(f"ALTER TABLE FeedbackState ADD {column_def}")
                    logger.info(
                        "Added missing column to FeedbackState: %s",
                        column_name,
                    )

            self._widen_feedback_id_column(conn, "FeedbackState")
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("Unable to migrate FeedbackState")
            raise

    def get_current_user(self, conn):
        """Get the current authenticated user from SQL connection"""
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT SUSER_NAME()")
            result = cursor.fetchone()
            if result:
                user = result[0]
                self.current_user = user
                logger.info(f"Current SQL user: {user}")
                return user
            else:
                self.current_user = "unknown_user"
                return "unknown_user"
        except Exception as e:
            logger.error(f"Error getting current user: {e}")
            self.current_user = "unknown_user"
            return "unknown_user"

    def ensure_feedback_table(self, conn):
        """Create main Feedback table if it doesn't exist, or migrate existing table"""
        cursor = conn.cursor()

        # Check if table exists
        cursor.execute(
            """
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_NAME = 'Feedback'
        """
        )

        table_exists = cursor.fetchone()[0] > 0

        if not table_exists:
            logger.info("Creating new Feedback table with all columns...")

            create_table_sql = """
            CREATE TABLE Feedback (
                Feedback_ID NVARCHAR(200) PRIMARY KEY,
                Title NVARCHAR(500),
                Content NVARCHAR(MAX),
                Source NVARCHAR(50),
                Source_URL NVARCHAR(1000),
                Author NVARCHAR(100),
                Created_Date DATETIME2,
                Sentiment NVARCHAR(20),
                Sentiment_Score FLOAT,
                Sentiment_Confidence NVARCHAR(20),
                Primary_Category NVARCHAR(100),
                Enhanced_Category NVARCHAR(200),
                Audience NVARCHAR(50),
                Priority NVARCHAR(20),
                -- Additional fields from collectors
                Feedback_Gist NVARCHAR(1000),
                Area NVARCHAR(100),
                Impacttype NVARCHAR(100),
                Scenario NVARCHAR(50),
                Tag NVARCHAR(200),
                Organization NVARCHAR(200),
                Status NVARCHAR(50),
                Created_by NVARCHAR(100),
                Rawfeedback NVARCHAR(MAX),
                Category NVARCHAR(100),
                Subcategory NVARCHAR(200),
                Feature_Area NVARCHAR(200),
                Categorization_Confidence FLOAT,
                Primary_Domain NVARCHAR(100),
                Domains NVARCHAR(MAX), -- Store as JSON string
                Matched_Keywords NVARCHAR(MAX), -- Store matched keywords as JSON array
                User_Modified_Categorization BIT DEFAULT 0, -- Flag to protect user changes from auto-recategorization
                Auto_Recategorized_Date DATETIME2, -- Timestamp of last automatic recategorization
                Collected_Date DATETIME2 DEFAULT GETDATE()
            );
            """

            cursor.execute(create_table_sql)
            conn.commit()
            logger.info("✅ New Feedback table created successfully with all columns")
        else:
            logger.info("Feedback table exists - checking for missing columns...")
            self.migrate_feedback_table(conn)

    def migrate_feedback_table(self, conn):
        """Add missing columns to existing Feedback table"""
        cursor = conn.cursor()

        # List of new columns to add
        new_columns = [
            "Feedback_Gist NVARCHAR(1000)",
            "Area NVARCHAR(100)",
            "Impacttype NVARCHAR(100)",
            "Scenario NVARCHAR(50)",
            "Tag NVARCHAR(200)",
            "Organization NVARCHAR(200)",
            "Status NVARCHAR(50)",
            "Created_by NVARCHAR(100)",
            "Rawfeedback NVARCHAR(MAX)",
            "Category NVARCHAR(100)",
            "Subcategory NVARCHAR(200)",
            "Feature_Area NVARCHAR(200)",
            "Categorization_Confidence FLOAT",
            "Primary_Domain NVARCHAR(100)",
            "Domains NVARCHAR(MAX)",
            "Matched_Keywords NVARCHAR(MAX)",
            "Sentiment_Score FLOAT",
            "Sentiment_Confidence NVARCHAR(20)",
            "User_Modified_Categorization BIT DEFAULT 0",
            "Auto_Recategorized_Date DATETIME2",
        ]

        try:
            for column_def in new_columns:
                column_name = column_def.split()[0]
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_NAME = 'Feedback' AND COLUMN_NAME = ?
                """,
                    [column_name],
                )

                column_exists = cursor.fetchone()[0] > 0
                if not column_exists:
                    cursor.execute(f"ALTER TABLE Feedback ADD {column_def}")
                    logger.info("Added missing Feedback column: %s", column_name)

            self._widen_feedback_id_column(conn, "Feedback")

            cursor.execute("UPDATE Feedback SET Audience = 'Developer' WHERE Audience IN ('ISV', 'Platform')")
            updated_rows = cursor.rowcount
            conn.commit()
            if updated_rows > 0:
                logger.info("Updated %s legacy audience values", updated_rows)
        except Exception:
            conn.rollback()
            logger.exception("Unable to migrate Feedback")
            raise

        logger.info("Feedback table migration completed")

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        return "[" + identifier.replace("]", "]]") + "]"

    def _widen_feedback_id_column(self, conn, table_name: str) -> None:
        """Widen an indexed Feedback_ID without losing its key constraints."""
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT CHARACTER_MAXIMUM_LENGTH
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = 'dbo'
              AND TABLE_NAME = ?
              AND COLUMN_NAME = 'Feedback_ID'
            """,
            [table_name],
        )
        id_column = cursor.fetchone()
        if (
            not id_column
            or id_column[0] is None
            or int(id_column[0]) <= 0
            or id_column[0] >= 200
        ):
            return

        cursor.execute(
            """
            SELECT
                kc.name,
                kc.type,
                i.type_desc,
                COUNT(ic.key_ordinal) AS key_column_count
            FROM sys.key_constraints kc
            INNER JOIN sys.tables t
                ON t.object_id = kc.parent_object_id
            INNER JOIN sys.schemas s
                ON s.schema_id = t.schema_id
            INNER JOIN sys.indexes i
                ON i.object_id = kc.parent_object_id
               AND i.index_id = kc.unique_index_id
            INNER JOIN sys.index_columns ic
                ON ic.object_id = i.object_id
               AND ic.index_id = i.index_id
               AND ic.key_ordinal > 0
            INNER JOIN sys.columns c
                ON c.object_id = ic.object_id
               AND c.column_id = ic.column_id
            WHERE s.name = 'dbo'
              AND t.name = ?
            GROUP BY kc.name, kc.type, i.type_desc
            HAVING SUM(
                CASE WHEN c.name = 'Feedback_ID' THEN 1 ELSE 0 END
            ) > 0
            ORDER BY kc.name
            """,
            [table_name],
        )
        constraints = cursor.fetchall()
        if any(int(row[3]) != 1 for row in constraints):
            raise RuntimeError(
                f"Cannot automatically widen composite key constraints on "
                f"dbo.{table_name}.Feedback_ID"
            )

        cursor.execute(
            """
            SELECT DISTINCT i.name
            FROM sys.indexes i
            INNER JOIN sys.tables t
                ON t.object_id = i.object_id
            INNER JOIN sys.schemas s
                ON s.schema_id = t.schema_id
            INNER JOIN sys.index_columns ic
                ON ic.object_id = i.object_id
               AND ic.index_id = i.index_id
            INNER JOIN sys.columns c
                ON c.object_id = ic.object_id
               AND c.column_id = ic.column_id
            WHERE s.name = 'dbo'
              AND t.name = ?
              AND c.name = 'Feedback_ID'
              AND i.is_primary_key = 0
              AND i.is_unique_constraint = 0
              AND i.name IS NOT NULL
            """,
            [table_name],
        )
        dependent_indexes = [str(row[0]) for row in cursor.fetchall()]
        if dependent_indexes:
            raise RuntimeError(
                f"Cannot automatically widen dbo.{table_name}.Feedback_ID "
                "while custom indexes depend on it: "
                + ", ".join(dependent_indexes)
            )

        qualified_table = (
            f"{self._quote_identifier('dbo')}."
            f"{self._quote_identifier(table_name)}"
        )
        for constraint_name, _kind, _index_type, _count in constraints:
            cursor.execute(
                f"ALTER TABLE {qualified_table} DROP CONSTRAINT "
                f"{self._quote_identifier(str(constraint_name))}"
            )

        cursor.execute(
            f"ALTER TABLE {qualified_table} ALTER COLUMN "
            f"{self._quote_identifier('Feedback_ID')} NVARCHAR(200) NOT NULL"
        )

        for constraint_name, kind, index_type, _count in constraints:
            constraint_type = "PRIMARY KEY" if kind == "PK" else "UNIQUE"
            storage = (
                "CLUSTERED"
                if str(index_type).upper() == "CLUSTERED"
                else "NONCLUSTERED"
            )
            cursor.execute(
                f"ALTER TABLE {qualified_table} ADD CONSTRAINT "
                f"{self._quote_identifier(str(constraint_name))} "
                f"{constraint_type} {storage} "
                f"({self._quote_identifier('Feedback_ID')})"
            )

    def load_feedback_states(self):
        """Load state data from FeedbackState table for server-side filtering"""
        conn = None
        try:
            conn = self._connect()

            if not conn:
                raise ConnectionError("Could not connect to Fabric SQL")

            cursor = conn.cursor()

            # Return only explicit state overrides. Falling back to the Feedback
            # table here would turn automatic categorization into a stale override.
            query = """
                SELECT
                    fs.Feedback_ID,
                    fs.State,
                    fs.Primary_Domain,
                    fs.Feedback_Notes,
                    fs.Last_Updated,
                    fs.Updated_By,
                    CASE
                        WHEN fs.Primary_Domain IS NOT NULL THEN 1
                        ELSE COALESCE(f.User_Modified_Categorization, 0)
                    END AS User_Modified_Categorization
                FROM FeedbackState fs
                LEFT JOIN Feedback f ON fs.Feedback_ID = f.Feedback_ID
                ORDER BY fs.Last_Updated DESC
            """

            cursor.execute(query)
            rows = cursor.fetchall()

            # Convert to dictionary for easy lookup
            state_data = {}
            for row in rows:
                feedback_id = row[0]
                state_data[feedback_id] = {
                    "state": row[1],
                    "domain": row[2],
                    "notes": row[3],
                    "last_updated": row[4].isoformat() if row[4] else None,
                    "updated_by": row[5],
                    "user_modified_categorization": bool(row[6]),
                }

            cursor.close()

            logger.info(f"📊 Loaded {len(state_data)} state records from FeedbackState table")
            return state_data

        except Exception:
            logger.exception("Error loading feedback states")
            raise
        finally:
            if conn:
                conn.close()

    def get_stored_feedback_ids(self) -> List[str]:
        """Return the non-empty feedback IDs stored in Fabric."""
        conn = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT DISTINCT Feedback_ID
                FROM Feedback
                WHERE Feedback_ID IS NOT NULL AND Feedback_ID != ''
                """
            )
            return [str(row[0]) for row in cursor.fetchall() if row[0]]
        except Exception:
            logger.exception("Error loading stored feedback IDs")
            raise
        finally:
            if conn:
                conn.close()

    @staticmethod
    def _text_value(value: Any, max_length: Optional[int] = None) -> str:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return ""
        if isinstance(value, (dict, list, tuple)):
            text = json.dumps(value, default=str, ensure_ascii=False)
        else:
            text = str(value)
        return text[:max_length] if max_length is not None else text

    @classmethod
    def _bounded_text_value(
        cls,
        value: Any,
        max_length: int,
        field_name: str,
    ) -> str:
        text = cls._text_value(value)
        if len(text) > max_length:
            raise ValueError(
                f"{field_name} exceeds the Fabric limit of "
                f"{max_length} characters"
            )
        return text

    @staticmethod
    def _date_value(value: Any) -> Optional[datetime]:
        if not value or (isinstance(value, float) and pd.isna(value)):
            return None
        if isinstance(value, datetime):
            parsed = value
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        elif not isinstance(value, datetime):
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    @classmethod
    def _prepare_feedback_row(
        cls,
        feedback: Dict[str, Any],
    ) -> Dict[str, Any]:
        matched_keywords = feedback.get("Matched_Keywords", [])
        if isinstance(matched_keywords, str):
            try:
                json.loads(matched_keywords)
                matched_keywords_json = matched_keywords
            except (TypeError, ValueError):
                matched_keywords_json = json.dumps([matched_keywords])
        elif isinstance(matched_keywords, (list, tuple)):
            matched_keywords_json = json.dumps(
                list(matched_keywords),
                default=str,
                ensure_ascii=False,
            )
        else:
            matched_keywords_json = "[]"

        domains = feedback.get("Domains", [])
        if isinstance(domains, str):
            try:
                json.loads(domains)
                domains_json = domains
            except (TypeError, ValueError):
                domains_json = json.dumps([domains])
        else:
            domains_json = json.dumps(
                domains or [],
                default=str,
                ensure_ascii=False,
            )

        audience = cls._text_value(feedback.get("Audience"))
        if audience in {"ISV", "Platform"}:
            audience = "Developer"
        elif audience not in {"Developer", "Customer"}:
            audience = "Customer"

        confidence = feedback.get("Categorization_Confidence")
        try:
            confidence = float(confidence) if confidence not in (None, "") else None
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None and not math.isfinite(confidence):
            confidence = None

        sentiment_score = feedback.get("Sentiment_Score")
        try:
            sentiment_score = (
                float(sentiment_score)
                if sentiment_score not in (None, "")
                else None
            )
        except (TypeError, ValueError):
            sentiment_score = None
        if sentiment_score is not None and not math.isfinite(sentiment_score):
            sentiment_score = None

        return {
            "Title": cls._text_value(
                feedback.get("Title")
                or feedback.get("Feedback_Gist")
                or feedback.get("Feedback"),
                500,
            ),
            "Content": cls._text_value(
                feedback.get("Content") or feedback.get("Feedback")
            ),
            "Source": cls._text_value(
                feedback.get("Source") or feedback.get("Sources"),
                50,
            ),
            "Source_URL": cls._text_value(
                feedback.get("Source_URL") or feedback.get("Url"),
                1000,
            ),
            "Author": cls._text_value(
                feedback.get("Author") or feedback.get("Customer"),
                100,
            ),
            "Created_Date": cls._date_value(
                feedback.get("Created_Date") or feedback.get("Created")
            ),
            "Sentiment": cls._text_value(feedback.get("Sentiment"), 20),
            "Sentiment_Score": sentiment_score,
            "Sentiment_Confidence": cls._text_value(
                feedback.get("Sentiment_Confidence"),
                20,
            ),
            "Primary_Category": cls._bounded_text_value(
                feedback.get("Primary_Category") or feedback.get("Category"),
                100,
                "Primary_Category",
            ),
            "Enhanced_Category": cls._bounded_text_value(
                feedback.get("Enhanced_Category"),
                200,
                "Enhanced_Category",
            ),
            "Audience": cls._bounded_text_value(
                audience,
                50,
                "Audience",
            ),
            "Priority": cls._bounded_text_value(
                feedback.get("Priority"),
                20,
                "Priority",
            ),
            "Feedback_Gist": cls._text_value(
                feedback.get("Feedback_Gist"),
                1000,
            ),
            "Area": cls._text_value(feedback.get("Area"), 100),
            "Impacttype": cls._bounded_text_value(
                feedback.get("Impacttype"),
                100,
                "Impacttype",
            ),
            "Scenario": cls._text_value(feedback.get("Scenario"), 50),
            "Tag": cls._text_value(feedback.get("Tag"), 200),
            "Organization": cls._text_value(
                feedback.get("Organization"),
                200,
            ),
            "Status": cls._text_value(feedback.get("Status"), 50),
            "Created_by": cls._text_value(feedback.get("Created_by"), 100),
            "Rawfeedback": cls._text_value(feedback.get("Rawfeedback")),
            "Category": cls._bounded_text_value(
                feedback.get("Category"),
                100,
                "Category",
            ),
            "Subcategory": cls._bounded_text_value(
                feedback.get("Subcategory"),
                200,
                "Subcategory",
            ),
            "Feature_Area": cls._bounded_text_value(
                feedback.get("Feature_Area"),
                200,
                "Feature_Area",
            ),
            "Categorization_Confidence": confidence,
            "Primary_Domain": cls._bounded_text_value(
                feedback.get("Primary_Domain"),
                100,
                "Primary_Domain",
            ),
            "Domains": domains_json,
            "Matched_Keywords": matched_keywords_json,
            "Auto_Recategorized_Date": cls._date_value(
                feedback.get("Auto_Recategorized_Date")
            ),
            "User_Modified_Categorization": int(
                bool(feedback.get("User_Modified_Categorization"))
            ),
        }

    def write_feedback_bulk(
        self,
        feedback_data: List[Dict[str, Any]],
        use_token: bool = True,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancellation_requested: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, int]:
        """Synchronize SQLite feedback rows into Fabric by canonical ID."""
        if not feedback_data:
            logger.info("No feedback data to write")
            return {
                "new_items": 0,
                "existing_items": 0,
                "total_items": 0,
                "id_regenerated": 0,
                "id_generated": 0,
            }
        if not use_token:
            raise ValueError("Interactive Fabric authentication is not supported")

        conn = None
        try:
            from id_generator import FeedbackIDGenerator

            logger.info("Synchronizing %s feedback items to Fabric", len(feedback_data))
            conn = self._connect()
            self.get_current_user(conn)
            self.ensure_feedback_table(conn)
            self.ensure_feedback_state_table(conn)
            cursor = conn.cursor()
            cursor.execute("SELECT Feedback_ID FROM Feedback")
            existing_ids = {str(row[0]) for row in cursor.fetchall() if row[0]}

            new_items = 0
            existing_items = 0
            id_generated = 0
            insert_columns = (
                "Feedback_ID",
                *_FABRIC_ALWAYS_SYNC_COLUMNS,
                *_FABRIC_CATEGORY_COLUMNS,
                "User_Modified_Categorization",
            )
            new_items_params: List[List[Any]] = []
            total_items = len(feedback_data)

            for index, feedback in enumerate(feedback_data, start=1):
                if cancellation_requested and cancellation_requested():
                    raise FabricWriteCancelled("Fabric write cancelled")
                try:
                    feedback_id = feedback.get("Feedback_ID") or feedback.get("id")
                    if feedback_id:
                        feedback_id = str(feedback_id).strip()
                    else:
                        feedback_id = (
                            FeedbackIDGenerator.generate_id_from_feedback_dict(
                                feedback
                            )
                        )
                        id_generated += 1
                    if not feedback_id or len(feedback_id) > 200:
                        raise ValueError(
                            "Feedback_ID must contain between 1 and 200 characters"
                        )

                    prepared = self._prepare_feedback_row(feedback)
                    incoming_user_modified = prepared[
                        "User_Modified_Categorization"
                    ]

                    if feedback_id in existing_ids:
                        existing_items += 1
                        assignments = [
                            f"{column} = ?"
                            for column in _FABRIC_ALWAYS_SYNC_COLUMNS
                        ]
                        params = [
                            prepared[column]
                            for column in _FABRIC_ALWAYS_SYNC_COLUMNS
                        ]
                        for column in _FABRIC_CATEGORY_COLUMNS:
                            assignments.append(
                                f"{column} = CASE "
                                "WHEN ? = 1 OR "
                                "COALESCE(User_Modified_Categorization, 0) = 0 "
                                f"THEN ? ELSE {column} END"
                            )
                            params.extend(
                                [
                                    incoming_user_modified,
                                    prepared[column],
                                ]
                            )
                        assignments.extend(
                            [
                                "User_Modified_Categorization = CASE "
                                "WHEN ? = 1 THEN 1 "
                                "ELSE COALESCE(User_Modified_Categorization, 0) END",
                                "Collected_Date = GETDATE()",
                            ]
                        )
                        params.extend([incoming_user_modified, feedback_id])
                        cursor.execute(
                            "UPDATE Feedback SET "
                            + ", ".join(assignments)
                            + " WHERE Feedback_ID = ?",
                            params,
                        )
                    else:
                        new_items_params.append(
                            [
                                feedback_id,
                                *[
                                    prepared[column]
                                    for column in _FABRIC_ALWAYS_SYNC_COLUMNS
                                ],
                                *[
                                    prepared[column]
                                    for column in _FABRIC_CATEGORY_COLUMNS
                                ],
                                incoming_user_modified,
                            ]
                        )
                        existing_ids.add(feedback_id)
                        new_items += 1
                except Exception:
                    logger.exception("Error processing feedback item %s", index)
                    raise
                finally:
                    if progress_callback:
                        progress_callback(index, total_items)

            # Execute bulk insert if there are new items
            if cancellation_requested and cancellation_requested():
                raise FabricWriteCancelled("Fabric write cancelled")
            if new_items_params:
                placeholders = ", ".join("?" for _ in insert_columns)
                cursor.executemany(
                    f"INSERT INTO Feedback ({', '.join(insert_columns)}) "
                    f"VALUES ({placeholders})",
                    new_items_params,
                )

            if cancellation_requested and cancellation_requested():
                raise FabricWriteCancelled("Fabric write cancelled")

            conn.commit()

            result = {
                "new_items": new_items,
                "existing_items": existing_items,
                "total_items": total_items,
                "id_regenerated": id_generated,
                "id_generated": id_generated,
            }
            logger.info(
                "Fabric sync complete: %s new, %s updated, %s IDs generated",
                new_items,
                existing_items,
                id_generated,
            )
            return result

        except FabricWriteCancelled:
            if conn:
                conn.rollback()
            raise
        except Exception:
            if conn:
                conn.rollback()
            logger.exception("Error in Fabric feedback sync")
            raise
        finally:
            if conn:
                conn.close()

    def sync_domains_from_state_to_feedback(self, conn):
        """
        Sync domain updates from FeedbackState table to Feedback table
        This ensures that when the Feedback table is recreated, domain updates are not lost
        """
        try:
            cursor = conn.cursor()

            # Update Feedback table with domain values from FeedbackState where they exist
            update_query = """
            UPDATE f
            SET f.Primary_Domain = fs.Primary_Domain,
                f.User_Modified_Categorization = 1
            FROM Feedback f
            INNER JOIN FeedbackState fs ON f.Feedback_ID = fs.Feedback_ID
            WHERE fs.Primary_Domain IS NOT NULL
            AND fs.Primary_Domain != ''
            AND (
                f.Primary_Domain IS NULL
                OR f.Primary_Domain != fs.Primary_Domain
                OR COALESCE(f.User_Modified_Categorization, 0) = 0
            )
            """

            cursor.execute(update_query)
            updated_rows = cursor.rowcount

            if updated_rows > 0:
                logger.info(f"✅ Synced {updated_rows} domain updates from FeedbackState to Feedback table")
            else:
                logger.debug("No domain updates to sync from FeedbackState to Feedback table")

            cursor.close()
            return updated_rows

        except Exception:
            logger.exception("Error syncing domains from state to feedback")
            raise

    def sync_domains_from_state(self, use_token: bool = True) -> int:
        """
        Manually sync domain updates from FeedbackState to Feedback table

        Args:
            use_token: Must be True. Interactive authentication is unsupported.

        Returns:
            int: Number of records updated
        """
        if not use_token:
            raise ValueError("Interactive Fabric authentication is not supported")

        conn = None
        try:
            conn = self._connect()
            if not conn:
                raise ConnectionError("Could not connect to Fabric SQL")

            # Ensure both tables exist
            self.ensure_feedback_table(conn)
            self.ensure_feedback_state_table(conn)

            # Sync domains
            updated_count = self.sync_domains_from_state_to_feedback(conn)

            if updated_count > 0:
                conn.commit()
                logger.info(f"✅ Domain sync complete: {updated_count} records updated")

            return updated_count

        except Exception:
            logger.exception("Error syncing Fabric domains")
            raise
        finally:
            if conn:
                conn.close()

    def update_feedback_states(self, state_changes: List[Dict[str, Any]]) -> bool:
        """
        Update feedback states in Fabric SQL database

        Args:
            state_changes: List of state change dictionaries
        Returns:
            bool: True when all changes commit successfully
        """
        if not state_changes:
            logger.info("No state changes to update")
            return True

        if not self.bearer_token:
            raise ValueError("A Fabric bearer token is required")

        normalized_changes = []
        for index, change in enumerate(state_changes):
            if not isinstance(change, dict):
                raise ValueError(f"State change {index} must be an object")
            feedback_id = change.get("feedback_id")
            if (
                not isinstance(feedback_id, str)
                or not feedback_id.strip()
                or len(feedback_id) > 200
            ):
                raise ValueError(
                    f"State change {index} has an invalid feedback_id"
                )
            normalized_changes.append(
                {**change, "feedback_id": feedback_id.strip()}
            )
        state_changes = normalized_changes

        conn = None
        try:
            logger.info(f"Updating {len(state_changes)} feedback states in Fabric SQL database")
            conn = self.connect_with_token(self.bearer_token)

            # Ensure table exists
            self.ensure_feedback_state_table(conn)

            cursor = conn.cursor()

            # Process each state change
            updated_count = 0
            for change in state_changes:
                feedback_id = change["feedback_id"]

                logger.info(f"Processing state change for feedback_id: {feedback_id}")

                # Check if record exists
                cursor.execute("SELECT COUNT(*) FROM FeedbackState WHERE Feedback_ID = ?", [feedback_id])
                exists = cursor.fetchone()[0] > 0

                if exists:
                    # Update existing record
                    update_sql = """
                    UPDATE FeedbackState
                    SET State = COALESCE(?, State),
                        Feedback_Notes = COALESCE(?, Feedback_Notes),
                        Primary_Domain = COALESCE(?, Primary_Domain),
                        Updated_By = COALESCE(?, Updated_By),
                        Last_Updated = GETDATE()
                    WHERE Feedback_ID = ?
                    """

                    cursor.execute(
                        update_sql,
                        [
                            change.get("state"),
                            change.get("notes"),
                            change.get("domain"),
                            self.current_user or change.get("updated_by") or "unknown_user",
                            feedback_id,
                        ],
                    )

                    logger.info(f"Updated existing record for feedback_id: {feedback_id}")

                else:
                    # Insert new record
                    insert_sql = """
                    INSERT INTO FeedbackState (Feedback_ID, State, Feedback_Notes, Primary_Domain, Updated_By, Last_Updated)
                    VALUES (?, ?, ?, ?, ?, GETDATE())
                    """

                    cursor.execute(
                        insert_sql,
                        [
                            feedback_id,
                            change.get("state", "NEW"),
                            change.get("notes"),
                            change.get("domain"),
                            self.current_user or change.get("updated_by") or "unknown_user",
                        ],
                    )

                    logger.info(f"Inserted new record for feedback_id: {feedback_id}")

                # Keep the denormalized domain in the Feedback table synchronized.
                if "domain" in change and change.get("domain") is not None:
                    cursor.execute(
                        """
                        UPDATE Feedback
                        SET Primary_Domain = ?,
                            User_Modified_Categorization = 1
                        WHERE Feedback_ID = ?
                        """,
                        [change.get("domain"), feedback_id],
                    )
                    logger.info(f"Synced Primary_Domain to Feedback table for feedback_id: {feedback_id}")

                updated_count += 1

            # Commit all changes
            conn.commit()

            logger.info(f"Successfully updated {updated_count} feedback states in Fabric SQL database")
            return True

        except Exception:
            if conn:
                conn.rollback()
            logger.exception("Error updating feedback states in Fabric SQL database")
            raise
        finally:
            if conn:
                conn.close()

    def get_feedback_state(self, feedback_id: str, use_token: bool = True) -> Dict[str, Any]:
        """Get current state of a feedback item from SQL database"""
        if not use_token:
            raise ValueError("Interactive Fabric authentication is not supported")
        conn = None
        try:
            conn = self._connect()

            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT Feedback_ID, State, Feedback_Notes, Primary_Domain, Last_Updated, Updated_By
                FROM FeedbackState
                WHERE Feedback_ID = ?
            """,
                [feedback_id],
            )

            row = cursor.fetchone()

            if row:
                return {
                    "feedback_id": row[0],
                    "state": row[1],
                    "notes": row[2],
                    "domain": row[3],
                    "last_updated": row[4],
                    "updated_by": row[5],
                }
            else:
                return None

        except Exception:
            logger.exception("Error getting feedback state from SQL database")
            raise
        finally:
            if conn:
                conn.close()

    def recategorize_all_feedback(self, use_token: bool = True) -> Dict[str, int]:
        raise RuntimeError(
            "Direct Fabric recategorization is disabled. Re-categorize the "
            "authoritative local store, then synchronize it to Fabric."
        )


def update_feedback_states_in_fabric_sql(bearer_token: str, state_changes: List[Dict[str, Any]]) -> bool:
    """
    Convenience function to update feedback states in Fabric SQL database
    This replaces the problematic lakehouse/PySpark approach

    Args:
        bearer_token: Fabric bearer token for authentication
        state_changes: List of state changes to apply

    Returns:
        bool: True if successful, False otherwise
    """
    if not bearer_token:
        raise ValueError("A Fabric bearer token is required")
    writer = FabricSQLWriter(bearer_token=bearer_token)
    return writer.update_feedback_states(state_changes)
