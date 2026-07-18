import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from job_manager import JobManager


class JobManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "jobs.db")
        self.manager = JobManager(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_logs_use_incremental_cursor_and_cancellation_is_durable(self):
        operation_id = self.manager.create("fabric_write", 3)
        self.manager.append_log(operation_id, "first")
        first = self.manager.snapshot(operation_id)
        self.assertEqual([log["message"] for log in first["logs"]], ["first"])

        self.manager.append_log(operation_id, "second")
        second = self.manager.snapshot(
            operation_id,
            after=first["next_log_cursor"],
        )
        self.assertEqual([log["message"] for log in second["logs"]], ["second"])

        self.assertTrue(self.manager.request_cancellation(operation_id))
        self.assertTrue(self.manager.cancellation_requested(operation_id))
        self.manager.cancel(operation_id)
        cancelled = self.manager.snapshot(operation_id)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["completed"])
        self.assertFalse(cancelled["success"])

    def test_restart_marks_active_jobs_interrupted(self):
        operation_id = self.manager.create("fabric_write", 2)
        self.manager.update(operation_id, status="in_progress")

        restarted = JobManager(self.db_path)
        snapshot = restarted.snapshot(operation_id)

        self.assertEqual(snapshot["status"], "interrupted")
        self.assertTrue(snapshot["completed"])
        self.assertFalse(snapshot["success"])

    def test_completed_job_cannot_be_reopened_by_cancellation(self):
        operation_id = self.manager.create("fabric_write", 1)
        self.manager.complete(operation_id, "done")

        self.assertFalse(self.manager.request_cancellation(operation_id))
        self.manager.cancel(operation_id)
        self.manager.fail(operation_id, "late failure")
        snapshot = self.manager.snapshot(operation_id)
        self.assertEqual(snapshot["status"], "completed")
        self.assertFalse(snapshot["cancel_requested"])

    def test_result_payload_cannot_override_job_state(self):
        operation_id = self.manager.create("fabric_write", 1)
        self.manager.complete(
            operation_id,
            "done",
            {"status": "forged", "new_items": 1},
        )

        snapshot = self.manager.snapshot(operation_id)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["new_items"], 1)

    def test_cleanup_removes_expired_completed_jobs(self):
        operation_id = self.manager.create("fabric_write", 0)
        self.manager.complete(operation_id, "done")
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE background_jobs SET updated_at = ? WHERE operation_id = ?",
                (old, operation_id),
            )
            conn.commit()
        finally:
            conn.close()

        manager = JobManager(self.db_path, retention_hours=1)
        self.assertIsNone(manager.snapshot(operation_id))


if __name__ == "__main__":
    unittest.main()
