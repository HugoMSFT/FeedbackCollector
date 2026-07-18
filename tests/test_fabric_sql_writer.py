import json
import os
import struct
import sys
import unittest
from unittest import mock


SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import fabric_sql_writer as writer_module


class FabricSQLWriterTests(unittest.TestCase):
    def _writer(self):
        writer = writer_module.FabricSQLWriter.__new__(
            writer_module.FabricSQLWriter
        )
        writer.bearer_token = "validated-token"
        writer.server = "server.example"
        writer.database = "feedback"
        writer.current_user = None
        return writer

    def test_connect_packs_access_token_for_odbc(self):
        fake_pyodbc = mock.Mock()
        connection = mock.Mock()
        fake_pyodbc.connect.return_value = connection
        writer = self._writer()

        with mock.patch.object(writer_module, "pyodbc", fake_pyodbc):
            result = writer.connect_with_token("Bearer abc")

        self.assertIs(result, connection)
        kwargs = fake_pyodbc.connect.call_args.kwargs
        token_struct = kwargs["attrs_before"][1256]
        encoded = "abc".encode("utf-16-le")
        token_length = struct.unpack("<I", token_struct[:4])[0]
        self.assertEqual(token_length, len(encoded))
        self.assertEqual(token_struct[4:], encoded)
        self.assertEqual(kwargs["timeout"], 30)

    def test_id_migration_recreates_single_column_constraints(self):
        writer = self._writer()
        connection = mock.Mock()
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = (100,)
        cursor.fetchall.side_effect = [
            [
                ("PK_Feedback", "PK", "CLUSTERED", 1),
                ("UK_Feedback_ID", "UQ", "NONCLUSTERED", 1),
            ],
            [],
        ]

        writer._widen_feedback_id_column(connection, "Feedback")

        statements = [
            call.args[0]
            for call in cursor.execute.call_args_list
        ]
        self.assertTrue(
            any(
                "DROP CONSTRAINT [PK_Feedback]" in statement
                for statement in statements
            )
        )
        self.assertTrue(
            any(
                "ALTER COLUMN [Feedback_ID] NVARCHAR(200) NOT NULL"
                in statement
                for statement in statements
            )
        )
        self.assertTrue(
            any(
                "PRIMARY KEY CLUSTERED ([Feedback_ID])" in statement
                for statement in statements
            )
        )
        self.assertTrue(
            any(
                "UNIQUE NONCLUSTERED ([Feedback_ID])" in statement
                for statement in statements
            )
        )

    def test_bulk_sync_updates_existing_canonical_id(self):
        writer = self._writer()
        connection = mock.Mock()
        cursor = connection.cursor.return_value
        cursor.fetchall.return_value = [("canonical-id",)]

        with mock.patch.object(writer, "_connect", return_value=connection), \
             mock.patch.object(writer, "get_current_user"), \
             mock.patch.object(writer, "ensure_feedback_table"), \
             mock.patch.object(writer, "ensure_feedback_state_table"), \
             mock.patch.object(
                 writer,
                 "sync_domains_from_state_to_feedback",
                 return_value=0,
             ):
            feedback = {
                "Feedback_ID": "canonical-id",
                "Title": "Updated title",
                "Content": "Updated content",
                "Category": "Automatic",
                "Matched_Keywords": ["fabric"],
            }
            result = writer.write_feedback_bulk([feedback])

        self.assertEqual(result["new_items"], 0)
        self.assertEqual(result["existing_items"], 1)
        self.assertEqual(result["id_generated"], 0)
        self.assertEqual(feedback["Feedback_ID"], "canonical-id")
        update_call = cursor.execute.call_args_list[-1]
        self.assertIn("UPDATE Feedback SET", update_call.args[0])
        self.assertIn(
            "COALESCE(User_Modified_Categorization, 0)",
            update_call.args[0],
        )
        self.assertEqual(update_call.args[1][-1], "canonical-id")
        connection.commit.assert_called_once_with()

    def test_bulk_sync_keeps_distinct_ids_and_serializes_json(self):
        writer = self._writer()
        connection = mock.Mock()
        cursor = connection.cursor.return_value
        cursor.fetchall.return_value = []

        with mock.patch.object(writer, "_connect", return_value=connection), \
             mock.patch.object(writer, "get_current_user"), \
             mock.patch.object(writer, "ensure_feedback_table"), \
             mock.patch.object(writer, "ensure_feedback_state_table"), \
             mock.patch.object(
                 writer,
                 "sync_domains_from_state_to_feedback",
                 return_value=0,
             ):
            result = writer.write_feedback_bulk(
                [
                    {
                        "Feedback_ID": "source-a",
                        "Title": "Same title",
                        "Content": "Same content",
                        "Rawfeedback": {"number": 1},
                        "Domains": ["Administration"],
                        "Matched_Keywords": ["fabric"],
                    },
                    {
                        "Feedback_ID": "source-b",
                        "Title": "Same title",
                        "Content": "Same content",
                        "Rawfeedback": {"number": 2},
                        "Domains": ["Security"],
                        "Matched_Keywords": ["sql"],
                    },
                ]
            )

        self.assertEqual(result["new_items"], 2)
        insert_sql, rows = cursor.executemany.call_args.args
        columns_text = insert_sql.split("(", 1)[1].split(")", 1)[0]
        columns = [column.strip() for column in columns_text.split(",")]
        self.assertEqual(rows[0][columns.index("Feedback_ID")], "source-a")
        self.assertEqual(rows[1][columns.index("Feedback_ID")], "source-b")
        self.assertEqual(
            json.loads(rows[0][columns.index("Rawfeedback")]),
            {"number": 1},
        )
        self.assertEqual(
            json.loads(rows[1][columns.index("Domains")]),
            ["Security"],
        )

    def test_prepare_feedback_rejects_oversized_category_fields(self):
        with self.assertRaisesRegex(ValueError, "Category exceeds"):
            self._writer()._prepare_feedback_row(
                {
                    "Feedback_ID": "item",
                    "Category": "x" * 101,
                }
            )

    def test_state_sync_rejects_invalid_ids_without_logging_payload(self):
        writer = self._writer()
        connection = mock.Mock()
        with mock.patch.object(
            writer,
            "connect_with_token",
            return_value=connection,
        ) as connect, mock.patch.object(
            writer,
            "ensure_feedback_state_table",
        ):
            with self.assertRaisesRegex(ValueError, "invalid feedback_id"):
                writer.update_feedback_states(
                    [{"feedback_id": "", "notes": "private note"}]
                )
            connect.assert_not_called()

        connection.rollback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
