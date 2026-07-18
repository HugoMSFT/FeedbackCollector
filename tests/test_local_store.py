import csv
import os
import sqlite3
import sys
import tempfile
import unittest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from local_store import LocalStore


class LocalStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "feedback.db")
        self.store = LocalStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_duplicate_accounting_and_state_preservation(self):
        first = {
            "Feedback_ID": "same-id",
            "Title": "First title",
            "Primary_Domain": "Original",
        }
        duplicate = {
            "Feedback_ID": "same-id",
            "Title": "Updated title",
            "Primary_Domain": "Fresh categorization",
        }

        summary = self.store.upsert_feedback_items([first, duplicate])
        self.assertEqual(summary, {"inserted": 1, "updated": 1, "skipped": 0})
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(
            self.store.load_all()[0]["Primary_Domain"],
            "Fresh categorization",
        )

        self.assertTrue(
            self.store.update_state(
                "same-id",
                primary_domain="User choice",
                mark_user_modified=True,
            )
        )
        self.store.upsert_feedback_items(
            [
                {
                    "Feedback_ID": "same-id",
                    "Title": "Newest title",
                    "Primary_Domain": "Should not win",
                }
            ]
        )

        item = self.store.load_all()[0]
        self.assertEqual(item["Title"], "Newest title")
        self.assertEqual(item["Primary_Domain"], "User choice")

    def test_manual_categorization_preserves_all_category_fields(self):
        self.store.upsert_feedback_items(
            [
                {
                    "Feedback_ID": "manual",
                    "Primary_Domain": "Original domain",
                    "Category": "Original category",
                    "Audience": "Developer",
                }
            ]
        )
        self.store.update_state(
            "manual",
            audience="Customer",
            mark_user_modified=True,
        )

        self.store.upsert_feedback_items(
            [
                {
                    "Feedback_ID": "manual",
                    "Primary_Domain": "Automatic domain",
                    "Category": "Automatic category",
                    "Audience": "Developer",
                }
            ]
        )

        item = self.store.load_all()[0]
        self.assertEqual(item["Primary_Domain"], "Original domain")
        self.assertEqual(item["Category"], "Original category")
        self.assertEqual(item["Audience"], "Customer")

    def test_unknown_feedback_edit_is_rejected(self):
        self.assertFalse(self.store.update_state("missing", state="TRIAGED"))

    def test_fabric_state_merge_preserves_manual_categorization(self):
        self.store.upsert_feedback_items(
            [
                {
                    "Feedback_ID": "known",
                    "Primary_Domain": "Original domain",
                    "Category": "Original category",
                }
            ]
        )
        self.store.update_state(
            "known",
            primary_domain="Manual domain",
            category="Manual category",
            mark_user_modified=True,
        )

        touched = self.store.bulk_upsert_states(
            [
                {
                    "Feedback_ID": "known",
                    "State": "TRIAGED",
                    "Feedback_Notes": "Synced note",
                    "User_Modified_Categorization": False,
                },
                {
                    "Feedback_ID": "unknown",
                    "State": "TRIAGED",
                },
            ]
        )
        self.store.upsert_feedback_items(
            [
                {
                    "Feedback_ID": "known",
                    "Primary_Domain": "New automatic domain",
                    "Category": "New automatic category",
                }
            ]
        )

        self.assertEqual(touched, 1)
        self.assertEqual(self.store.count(), 1)
        item = self.store.load_all()[0]
        self.assertEqual(item["State"], "TRIAGED")
        self.assertEqual(item["Feedback_Notes"], "Synced note")
        self.assertEqual(item["Primary_Domain"], "Manual domain")
        self.assertEqual(item["Category"], "Manual category")
        self.assertEqual(item["User_Modified_Categorization"], 1)

    def test_fabric_state_merge_rejects_older_remote_state(self):
        self.store.upsert_feedback_items(
            [{"Feedback_ID": "item-1", "Feedback": "Feedback"}]
        )
        self.store.bulk_upsert_states(
            [
                {
                    "Feedback_ID": "item-1",
                    "State": "TRIAGED",
                    "Feedback_Notes": "Newer",
                    "Last_Updated": "2030-01-01T00:00:00+00:00",
                    "Updated_By": "fabric-user",
                }
            ]
        )
        self.store.bulk_upsert_states(
            [
                {
                    "Feedback_ID": "item-1",
                    "State": "NEW",
                    "Feedback_Notes": "Older",
                    "Last_Updated": "2020-01-01T00:00:00+00:00",
                    "Updated_By": "older-user",
                }
            ]
        )

        item = self.store.load_all()[0]
        self.assertEqual(item["State"], "TRIAGED")
        self.assertEqual(item["Feedback_Notes"], "Newer")
        self.assertEqual(item["Updated_By"], "fabric-user")

    def test_non_manual_fabric_state_cannot_erase_manual_domain(self):
        self.store.upsert_feedback_items(
            [
                {
                    "Feedback_ID": "manual",
                    "Primary_Domain": "Automatic domain",
                }
            ]
        )
        self.store.update_state(
            "manual",
            primary_domain="Local manual domain",
            updated_by="local-user",
            mark_user_modified=True,
        )

        self.store.bulk_upsert_states(
            [
                {
                    "Feedback_ID": "manual",
                    "State": "TRIAGED",
                    "Primary_Domain": None,
                    "Feedback_Notes": "New remote note",
                    "User_Modified_Categorization": False,
                    "Last_Updated": "2030-01-01T00:00:00+00:00",
                    "Updated_By": "remote-user",
                }
            ]
        )

        item = self.store.load_all()[0]
        self.assertEqual(item["State"], "TRIAGED")
        self.assertEqual(item["Feedback_Notes"], "New remote note")
        self.assertEqual(item["Primary_Domain"], "Local manual domain")
        self.assertTrue(item["User_Modified_Categorization"])

    def test_csv_export_neutralises_spreadsheet_formulas(self):
        self.store.upsert_feedback_items(
            [
                {
                    "Feedback_ID": "formula",
                    "Title": "=HYPERLINK(\"https://example.test\")",
                    "Feedback": "+SUM(1,2)",
                    "Author": "@command",
                }
            ]
        )

        path = self.store.export_to_csv(self.temp_dir.name)
        with open(path, newline="", encoding="utf-8-sig") as handle:
            row = next(csv.DictReader(handle))

        self.assertTrue(row["Title"].startswith("'="))
        self.assertTrue(row["Feedback"].startswith("'+"))
        self.assertTrue(row["Author"].startswith("'@"))

    def test_csv_roundtrip_does_not_freeze_automatic_categorization(self):
        self.store.upsert_feedback_items(
            [
                {
                    "Feedback_ID": "automatic",
                    "Primary_Domain": "Original domain",
                }
            ]
        )
        export_path = self.store.export_to_csv(self.temp_dir.name)
        imported = LocalStore(os.path.join(self.temp_dir.name, "imported.db"))
        imported.import_from_csv(export_path)

        imported.upsert_feedback_items(
            [
                {
                    "Feedback_ID": "automatic",
                    "Primary_Domain": "Updated domain",
                }
            ]
        )

        item = imported.load_all()[0]
        self.assertEqual(item["Primary_Domain"], "Updated domain")
        self.assertFalse(item["User_Modified_Categorization"])

    def test_newer_schema_is_rejected(self):
        future_path = os.path.join(self.temp_dir.name, "future.db")
        conn = sqlite3.connect(future_path)
        try:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', '999')"
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaisesRegex(RuntimeError, "newer than"):
            LocalStore(future_path)

    def test_migration_clears_automatic_state_overrides(self):
        self.store.upsert_feedback_items(
            [
                {
                    "Feedback_ID": "migrated",
                    "Primary_Domain": "Current automatic domain",
                }
            ]
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE feedback_state
                SET Primary_Domain = 'Stale state domain',
                    User_Modified_Categorization = 0
                WHERE Feedback_ID = 'migrated'
                """
            )
            conn.execute(
                "UPDATE meta SET value = '2' WHERE key = 'schema_version'"
            )
            conn.commit()
        finally:
            conn.close()

        migrated = LocalStore(self.db_path)
        item = migrated.load_all()[0]
        self.assertEqual(item["Primary_Domain"], "Current automatic domain")

    def test_migration_backfills_legacy_automatic_categorization(self):
        self.store.upsert_feedback_items(
            [{"Feedback_ID": "legacy", "Feedback": "Legacy feedback"}]
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE feedback SET Extra_Json = NULL "
                "WHERE Feedback_ID = 'legacy'"
            )
            conn.execute(
                """
                UPDATE feedback_state
                SET Primary_Domain = 'Legacy domain',
                    Category = 'Legacy category',
                    Audience = 'Developer',
                    Priority = 'high',
                    User_Modified_Categorization = 0
                WHERE Feedback_ID = 'legacy'
                """
            )
            conn.execute(
                "UPDATE meta SET value = '2' WHERE key = 'schema_version'"
            )
            conn.commit()
        finally:
            conn.close()

        migrated = LocalStore(self.db_path)
        item = migrated.load_all()[0]

        self.assertEqual(item["Primary_Domain"], "Legacy domain")
        self.assertEqual(item["Category"], "Legacy category")
        self.assertEqual(item["Audience"], "Developer")
        self.assertEqual(item["Priority"], "high")
        self.assertFalse(item["User_Modified_Categorization"])


if __name__ == "__main__":
    unittest.main()
