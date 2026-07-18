import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import runtime_paths


class RuntimePathTests(unittest.TestCase):
    def test_windows_packaged_data_uses_local_app_data(self):
        with mock.patch.object(runtime_paths.sys, "platform", "win32"), \
             mock.patch.dict(
                 runtime_paths.os.environ,
                 {"LOCALAPPDATA": r"C:\Users\test\AppData\Local"},
                 clear=False,
             ):
            path = runtime_paths.get_user_data_dir()

        self.assertEqual(
            path,
            os.path.abspath(
                r"C:\Users\test\AppData\Local\FeedbackCollector"
            ),
        )

    def test_linux_packaged_data_honors_xdg_data_home(self):
        with mock.patch.object(runtime_paths.sys, "platform", "linux"), \
             mock.patch.dict(
                 runtime_paths.os.environ,
                 {"XDG_DATA_HOME": "/tmp/user-data"},
                 clear=False,
             ):
            path = runtime_paths.get_user_data_dir()

        self.assertEqual(
            path,
            os.path.abspath("/tmp/user-data/FeedbackCollector"),
        )

    def test_packaged_startup_migrates_legacy_database_and_taxonomy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = os.path.join(temp_dir, "package")
            legacy_dir = os.path.join(project_root, "data")
            user_dir = os.path.join(temp_dir, "user-data")
            os.makedirs(legacy_dir)
            legacy_db = os.path.join(legacy_dir, "feedback_store.db")
            connection = sqlite3.connect(legacy_db)
            try:
                connection.execute(
                    "CREATE TABLE feedback (Feedback_ID TEXT PRIMARY KEY)"
                )
                connection.execute(
                    "INSERT INTO feedback(Feedback_ID) VALUES ('legacy')"
                )
                connection.commit()
            finally:
                connection.close()
            with open(
                os.path.join(legacy_dir, "categories.json"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write('{"legacy": {}}')

            with mock.patch.object(
                runtime_paths.sys,
                "frozen",
                True,
                create=True,
            ), mock.patch.object(
                runtime_paths,
                "PROJECT_ROOT",
                project_root,
            ), mock.patch.object(
                runtime_paths,
                "DATA_DIR",
                user_dir,
            ), mock.patch.object(
                runtime_paths,
                "LOCAL_DB_PATH",
                os.path.join(user_dir, "feedback_store.db"),
            ):
                migrated = runtime_paths.migrate_legacy_packaged_data()

            migrated_db = os.path.join(user_dir, "feedback_store.db")
            connection = sqlite3.connect(migrated_db)
            try:
                row = connection.execute(
                    "SELECT Feedback_ID FROM feedback"
                ).fetchone()
            finally:
                connection.close()

            self.assertEqual(row[0], "legacy")
            self.assertIn("feedback_store.db", migrated)
            self.assertIn("categories.json", migrated)
            self.assertTrue(
                os.path.isfile(os.path.join(user_dir, "categories.json"))
            )


if __name__ == "__main__":
    unittest.main()
