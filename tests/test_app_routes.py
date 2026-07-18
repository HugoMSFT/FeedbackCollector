import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from unittest import mock

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import app as app_module
from job_manager import JobManager
from local_store import LocalStore


class ImmediateThread:
    def __init__(self, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()


class EmptyPublicCollector:
    queries = ["SQL Server"]
    days = 30
    tags = ["sqlserver"]

    def configure(self, _settings):
        return None

    def collect(self):
        return []

    def close(self):
        return None


class FailingPublicCollector(EmptyPublicCollector):
    def collect(self):
        raise RuntimeError("upstream unavailable")


class AppRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_store = app_module.local_store
        self.original_job_manager = app_module.job_manager
        app_module.local_store = LocalStore(
            os.path.join(self.temp_dir.name, "routes.db")
        )
        app_module.job_manager = JobManager(
            os.path.join(self.temp_dir.name, "jobs.db")
        )
        app_module.app.config.update(TESTING=True, SECRET_KEY="route-tests")
        with app_module._state_lock:
            app_module.collection_status.clear()
            app_module.collection_status.update(
                {
                    "status": "ready",
                    "message": "Ready",
                    "source_states": {},
                }
            )
            app_module.last_collected_feedback = []
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.local_store = self.original_store
        app_module.job_manager = self.original_job_manager
        self.temp_dir.cleanup()

    def test_local_state_aliases_persist_without_browser_token(self):
        app_module.local_store.upsert_feedback_items(
            [{"Feedback_ID": "item-1", "Title": "Feedback"}]
        )

        first = self.client.post(
            "/api/feedback/state",
            json={"feedback_id": "item-1", "state": "TRIAGED"},
        )
        second = self.client.post(
            "/api/feedback/state/update",
            json={"feedback_id": "item-1", "notes": "Investigating"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        item = app_module.local_store.load_all()[0]
        self.assertEqual(item["State"], "TRIAGED")
        self.assertEqual(item["Feedback_Notes"], "Investigating")

    def test_unknown_local_state_update_returns_not_found(self):
        response = self.client.post(
            "/api/feedback/state",
            json={"feedback_id": "missing", "state": "TRIAGED"},
        )
        self.assertEqual(response.status_code, 404)

    def test_feedback_snapshot_never_falls_back_to_process_memory(self):
        app_module.last_collected_feedback = [
            {"Feedback_ID": "memory-only", "Feedback": "Not persisted"}
        ]

        self.assertEqual(app_module._load_feedback_snapshot(), [])

    def test_state_only_fabric_sync_does_not_freeze_categorization(self):
        app_module.local_store.upsert_feedback_items(
            [
                {
                    "Feedback_ID": "item-1",
                    "Primary_Domain": "Automatic domain",
                }
            ]
        )

        with mock.patch.object(
            app_module,
            "get_server_fabric_token",
            return_value="validated-token",
        ), mock.patch(
            "fabric_sql_writer.FabricSQLWriter"
        ) as writer_type:
            response = self.client.post(
                "/api/feedback/states/sync",
                json={
                    "state_changes": [
                        {"feedback_id": "item-1", "state": "TRIAGED"}
                    ]
                },
            )

        self.assertEqual(response.status_code, 200)
        item = app_module.local_store.load_all()[0]
        self.assertEqual(item["State"], "TRIAGED")
        self.assertFalse(item["User_Modified_Categorization"])
        writer_type.return_value.update_feedback_states.assert_called_once()

    def test_fabric_state_load_preserves_manual_categorization(self):
        app_module.local_store.upsert_feedback_items(
            [
                {
                    "Feedback_ID": "item-1",
                    "Primary_Domain": "Automatic domain",
                }
            ]
        )
        app_module.local_store.update_state(
            "item-1",
            primary_domain="Manual domain",
            mark_user_modified=True,
        )

        with mock.patch.object(
            app_module,
            "get_server_fabric_token",
            return_value="validated-token",
        ), mock.patch(
            "fabric_sql_writer.FabricSQLWriter"
        ) as writer_type:
            writer_type.return_value.load_feedback_states.return_value = {
                "item-1": {
                    "state": "TRIAGED",
                    "domain": "Manual domain",
                    "notes": "Synced note",
                    "user_modified_categorization": False,
                }
            }
            response = self.client.post(
                "/api/feedback/states/load",
                json={"feedback_ids": ["item-1"]},
            )

        app_module.local_store.upsert_feedback_items(
            [
                {
                    "Feedback_ID": "item-1",
                    "Primary_Domain": "New automatic domain",
                }
            ]
        )
        item = app_module.local_store.load_all()[0]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(item["State"], "TRIAGED")
        self.assertEqual(item["Feedback_Notes"], "Synced note")
        self.assertEqual(item["Primary_Domain"], "Manual domain")
        self.assertEqual(item["User_Modified_Categorization"], 1)

    def test_fabric_state_load_rejects_non_array_ids(self):
        response = self.client.post(
            "/api/feedback/states/load",
            json={"feedback_ids": "item-1"},
        )
        self.assertEqual(response.status_code, 400)

    def test_full_fabric_sync_pushes_local_state_before_remote_pull(self):
        app_module.local_store.upsert_feedback_items(
            [{"Feedback_ID": "item-1", "Feedback": "Local feedback"}]
        )
        app_module.local_store.update_state(
            "item-1",
            state="TRIAGED",
            notes="Local note",
            updated_by="local-user",
            mark_user_modified=True,
        )

        events = []
        cursor = mock.Mock()
        cursor.fetchall.side_effect = lambda: (
            events.append("pull")
            or [
                (
                    "item-1",
                    "NEW",
                    "Stale remote note",
                    None,
                    datetime(2020, 1, 1),
                    "remote-user",
                    False,
                )
            ]
        )
        connection = mock.Mock()
        connection.cursor.return_value = cursor
        writer = mock.Mock()
        writer.connect_with_token.return_value = connection
        writer.write_feedback_bulk.return_value = {
            "new_items": 0,
            "existing_items": 1,
            "total_items": 1,
            "id_regenerated": 0,
        }
        writer.update_feedback_states.side_effect = lambda _changes: events.append(
            "push"
        )

        with mock.patch.object(
            app_module,
            "get_server_fabric_token",
            return_value="validated-token",
        ), mock.patch(
            "fabric_sql_writer.FabricSQLWriter",
            return_value=writer,
        ):
            response = self.client.post("/api/fabric/sync", json={})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, ["push", "pull"])
        pushed_change = writer.update_feedback_states.call_args.args[0][0]
        self.assertEqual(pushed_change["state"], "TRIAGED")
        self.assertEqual(pushed_change["notes"], "Local note")
        item = app_module.local_store.load_all()[0]
        self.assertEqual(item["State"], "TRIAGED")
        self.assertEqual(item["Feedback_Notes"], "Local note")

    def test_filtered_feedback_validates_pagination_and_ignores_connection_hint(self):
        app_module.local_store.upsert_feedback_items(
            [
                {
                    "Feedback_ID": "stored",
                    "Feedback": "Stored item",
                    "Created": "2025-01-01",
                },
                {
                    "Feedback_ID": "local",
                    "Feedback": "Local item",
                    "Created": "2025-01-02",
                },
            ]
        )

        invalid = self.client.get(
            "/api/feedback/filtered?page=invalid"
        )
        with mock.patch.object(
            app_module,
            "get_server_fabric_token",
            return_value="validated-token",
        ), mock.patch(
            "fabric_sql_writer.FabricSQLWriter"
        ) as writer_type:
            writer_type.return_value.get_stored_feedback_ids.return_value = [
                "stored"
            ]
            filtered = self.client.get(
                "/api/feedback/filtered"
                "?show_only_stored=true&fabric_connected=true"
            )

        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(filtered.status_code, 200)
        self.assertTrue(filtered.json["fabric_connected"])
        self.assertEqual(filtered.json["total_count"], 1)
        self.assertEqual(
            filtered.json["feedback"][0]["Feedback_ID"],
            "stored",
        )
        writer_type.assert_called_once_with(bearer_token="validated-token")
        writer_type.return_value.get_stored_feedback_ids.assert_called_once_with()

    def test_sync_write_ignores_request_tokens_and_requires_vault_token(self):
        app_module.local_store.upsert_feedback_items(
            [{"Feedback_ID": "item-1", "Matched_Keywords": ["fabric"]}]
        )
        response = self.client.post(
            "/api/write_to_fabric",
            json={"fabric_token": "unvalidated"},
        )
        self.assertEqual(response.status_code, 401)

    def test_legacy_token_storage_endpoint_is_retired(self):
        response = self.client.post(
            "/api/store_session_token",
            json={"token": "unvalidated"},
        )
        self.assertEqual(response.status_code, 410)

    def test_validated_fabric_token_is_server_side_and_can_be_cleared(self):
        connection = mock.Mock()
        with mock.patch(
            "fabric_sql_writer.FabricSQLWriter"
        ) as writer_type:
            writer_type.return_value.connect_with_token.return_value = connection
            validated = self.client.post(
                "/api/fabric/token/validate",
                json={"token": "  validated-token  "},
            )

        status = self.client.get("/api/fabric/token/status")
        cleared = self.client.post("/api/fabric/token/clear")
        status_after_clear = self.client.get("/api/fabric/token/status")

        self.assertEqual(validated.status_code, 200)
        self.assertNotIn("validated-token", validated.get_data(as_text=True))
        writer_type.assert_called_once_with(bearer_token="validated-token")
        connection.close.assert_called_once_with()
        self.assertTrue(status.json["has_token"])
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(status_after_clear.json["has_token"])

    def test_failed_fabric_token_validation_hides_backend_details(self):
        with mock.patch(
            "fabric_sql_writer.FabricSQLWriter",
            side_effect=RuntimeError("sensitive backend details"),
        ):
            response = self.client.post(
                "/api/fabric/token/validate",
                json={"token": "invalid-token"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["message"], "Token validation failed")
        self.assertNotIn("sensitive", response.get_data(as_text=True))
        self.assertFalse(
            self.client.get("/api/fabric/token/status").json["has_token"]
        )

    def test_async_fabric_write_exposes_durable_terminal_result(self):
        app_module.local_store.upsert_feedback_items(
            [{"Feedback_ID": "item-1", "Matched_Keywords": ["fabric"]}]
        )
        connection = mock.Mock()

        with mock.patch(
            "fabric_sql_writer.FabricSQLWriter"
        ) as writer_type:
            writer = writer_type.return_value
            writer.connect_with_token.return_value = connection
            writer.write_feedback_bulk.return_value = {
                "new_items": 1,
                "existing_items": 0,
            }
            self.client.post(
                "/api/fabric/token/validate",
                json={"token": "validated-token"},
            )
            with mock.patch.object(
                app_module.threading,
                "Thread",
                ImmediateThread,
            ):
                started = self.client.post("/api/write_to_fabric_async")

        progress = self.client.get(
            f"/api/fabric_progress/{started.json['operation_id']}"
        )

        self.assertEqual(started.status_code, 202)
        self.assertEqual(progress.status_code, 200)
        self.assertEqual(progress.json["status"], "completed")
        self.assertTrue(progress.json["success"])
        self.assertEqual(progress.json["new_items"], 1)
        writer.write_feedback_bulk.assert_called_once()

    def test_async_fabric_write_failure_is_generic_and_durable(self):
        app_module.local_store.upsert_feedback_items(
            [{"Feedback_ID": "item-1", "Matched_Keywords": ["fabric"]}]
        )
        connection = mock.Mock()

        with mock.patch(
            "fabric_sql_writer.FabricSQLWriter"
        ) as writer_type:
            writer = writer_type.return_value
            writer.connect_with_token.return_value = connection
            writer.write_feedback_bulk.side_effect = RuntimeError(
                "sensitive database details"
            )
            self.client.post(
                "/api/fabric/token/validate",
                json={"token": "validated-token"},
            )
            with mock.patch.object(
                app_module.threading,
                "Thread",
                ImmediateThread,
            ):
                started = self.client.post("/api/write_to_fabric_async")

        progress = self.client.get(
            f"/api/fabric_progress/{started.json['operation_id']}"
        )

        self.assertEqual(started.status_code, 202)
        self.assertEqual(progress.json["status"], "error")
        self.assertFalse(progress.json["success"])
        self.assertNotIn(
            "sensitive",
            progress.get_data(as_text=True),
        )

    def test_fabric_job_cancel_route_persists_request(self):
        operation_id = app_module.job_manager.create("fabric_write", 1)

        accepted = self.client.post(
            f"/api/cancel_fabric_write/{operation_id}"
        )
        snapshot = app_module.job_manager.snapshot(operation_id)
        app_module.job_manager.cancel(operation_id)
        completed = self.client.post(
            f"/api/cancel_fabric_write/{operation_id}"
        )

        self.assertEqual(accepted.status_code, 202)
        self.assertTrue(snapshot["cancel_requested"])
        self.assertEqual(completed.status_code, 409)

    def test_collection_request_is_snapshotted_before_worker_starts(self):
        captured = {}

        def fake_collection_body(**kwargs):
            captured.update(kwargs)
            with app_module._state_lock:
                app_module.collection_status["status"] = "completed"

        request_body = {
            "sources": {"stackoverflow": {"enabled": True}},
            "settings": {"limit": 5},
        }
        with mock.patch.object(
            app_module,
            "_collect_feedback_body",
            side_effect=fake_collection_body,
        ), mock.patch.object(
            app_module.threading,
            "Thread",
            ImmediateThread,
        ):
            response = self.client.post("/api/collect", json=request_body)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["request_config"], request_body)
        self.assertIsInstance(captured["cancel_event"], threading.Event)
        self.assertEqual(captured["operation_id"], response.json["operation_id"])

    def test_collection_rejects_invalid_source_limit(self):
        response = self.client.post(
            "/api/collect",
            json={
                "sources": {
                    "stackoverflow": {
                        "enabled": True,
                        "maxItems": "unbounded",
                    }
                }
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_collection_rejects_invalid_public_source_filters(self):
        invalid_days = self.client.post(
            "/api/collect",
            json={
                "sources": {
                    "hackerNews": {
                        "enabled": True,
                        "queries": ["SQL Server"],
                        "days": 0,
                        "maxItems": 5,
                    }
                }
            },
        )
        invalid_tags = self.client.post(
            "/api/collect",
            json={
                "sources": {
                    "devCommunity": {
                        "enabled": True,
                        "tags": "sqlserver",
                        "maxItems": 5,
                    }
                }
            },
        )

        self.assertEqual(invalid_days.status_code, 400)
        self.assertEqual(invalid_tags.status_code, 400)

    def test_missing_ado_config_is_skipped_while_public_sources_complete(self):
        request_body = {
            "sources": {
                "ado": {
                    "enabled": True,
                    "parentWorkItem": "42",
                    "maxItems": 5,
                },
                "hackerNews": {
                    "enabled": True,
                    "queries": ["SQL Server"],
                    "days": 30,
                    "maxItems": 5,
                },
            },
            "settings": {},
        }
        with mock.patch.multiple(
            app_module.config,
            ADO_PAT=None,
            ADO_ORG_URL=None,
            ADO_PROJECT_NAME=None,
            ADO_PARENT_WORK_ITEM_ID=None,
        ), mock.patch.object(
            app_module,
            "HackerNewsCollector",
            return_value=EmptyPublicCollector(),
        ), mock.patch.object(
            app_module,
            "get_working_ado_items",
        ) as get_ado_items, mock.patch.object(
            app_module.threading,
            "Thread",
            ImmediateThread,
        ):
            response = self.client.post("/api/collect", json=request_body)

        self.assertEqual(response.status_code, 202)
        get_ado_items.assert_not_called()
        with app_module._state_lock:
            status = dict(app_module.collection_status)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["source_states"]["ado"]["state"], "skipped")
        self.assertEqual(
            status["source_states"]["hackerNews"]["state"],
            "success",
        )

    def test_failed_public_source_does_not_discard_other_source_run(self):
        request_body = {
            "sources": {
                "hackerNews": {
                    "enabled": True,
                    "queries": ["SQL Server"],
                    "days": 30,
                    "maxItems": 5,
                },
                "devCommunity": {
                    "enabled": True,
                    "tags": ["sqlserver"],
                    "maxItems": 5,
                },
            },
            "settings": {},
        }
        with mock.patch.object(
            app_module,
            "HackerNewsCollector",
            return_value=FailingPublicCollector(),
        ), mock.patch.object(
            app_module,
            "DevCommunityCollector",
            return_value=EmptyPublicCollector(),
        ), mock.patch.object(
            app_module.threading,
            "Thread",
            ImmediateThread,
        ):
            response = self.client.post("/api/collect", json=request_body)

        self.assertEqual(response.status_code, 202)
        with app_module._state_lock:
            status = dict(app_module.collection_status)
        self.assertEqual(status["status"], "completed")
        self.assertIn("source warning", status["message"])
        self.assertEqual(
            status["source_states"]["hackerNews"]["state"],
            "error",
        )
        self.assertEqual(
            status["source_states"]["devCommunity"]["state"],
            "success",
        )

    def test_collection_rejects_unknown_or_empty_sources(self):
        unknown = self.client.post(
            "/api/collect",
            json={"sources": {"unknown": {"enabled": True}}},
        )
        empty = self.client.post(
            "/api/collect",
            json={"sources": {"reddit": {"enabled": False}}},
        )

        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(empty.status_code, 400)

    def test_recategorization_is_local_and_preserves_manual_overrides(self):
        app_module.local_store.upsert_feedback_items(
            [
                {
                    "Feedback_ID": "automatic",
                    "Feedback": "Automatic feedback",
                    "Category": "Old automatic category",
                },
                {
                    "Feedback_ID": "manual",
                    "Feedback": "Manual feedback",
                    "Category": "Old automatic category",
                },
            ]
        )
        app_module.local_store.update_state(
            "manual",
            category="Manual category",
            enhanced_category="Manual category",
            mark_user_modified=True,
        )
        categorization = {
            "primary_category": "New category",
            "legacy_category": "New legacy category",
            "subcategory": "New subcategory",
            "audience": "Developer",
            "priority": "high",
            "feature_area": "New feature",
            "confidence": 0.9,
            "primary_domain": "New domain",
            "domains": ["New domain"],
            "impact_type": "Bug",
        }

        with mock.patch.object(
            app_module.utils,
            "enhanced_categorize_feedback",
            return_value=categorization,
        ):
            response = self.client.post("/api/categories/recategorize")

        items = {
            item["Feedback_ID"]: item
            for item in app_module.local_store.load_all()
        }
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["recategorized"], 1)
        self.assertEqual(response.json["skipped_user_modified"], 1)
        self.assertEqual(
            items["automatic"]["Category"],
            "New legacy category",
        )
        self.assertEqual(items["automatic"]["Primary_Domain"], "New domain")
        self.assertEqual(items["manual"]["Category"], "Manual category")

    def test_category_save_updates_runtime_categorizer(self):
        original = app_module.copy.deepcopy(
            app_module.config.ENHANCED_FEEDBACK_CATEGORIES
        )
        payload = {
            "CUSTOM": {
                "name": "Custom category",
                "audience": "All",
                "subcategories": {
                    "CUSTOM_SUBCATEGORY": {
                        "name": "Custom subcategory",
                        "keywords": ["unique-taxonomy-keyword"],
                        "priority": "high",
                        "feature_area": "Custom feature",
                    }
                },
            }
        }

        try:
            with mock.patch.object(app_module.config, "save_categories"):
                response = self.client.post("/api/categories", json=payload)

            categorized = app_module.utils.enhanced_categorize_feedback(
                "Contains unique-taxonomy-keyword"
            )
            self.assertEqual(response.status_code, 200)
            self.assertIs(
                app_module.utils.ENHANCED_FEEDBACK_CATEGORIES,
                app_module.config.ENHANCED_FEEDBACK_CATEGORIES,
            )
            self.assertEqual(
                categorized["primary_category"],
                "Custom category",
            )
        finally:
            app_module._replace_runtime_mapping(
                "ENHANCED_FEEDBACK_CATEGORIES",
                original,
            )

    def test_category_save_rejects_incomplete_runtime_shape(self):
        with mock.patch.object(app_module.config, "save_categories") as save:
            response = self.client.post(
                "/api/categories",
                json={
                    "BROKEN": {
                        "name": "Broken",
                        "subcategories": {},
                    }
                },
            )

        self.assertEqual(response.status_code, 400)
        save.assert_not_called()

    def test_collection_cancellation_has_distinct_terminal_state(self):
        cancel_event = threading.Event()
        cancel_event.set()

        app_module._collect_feedback_body(
            request_config={"sources": {}, "settings": {}},
            online_mode=False,
            operation_id="cancelled-operation",
            cancel_event=cancel_event,
        )

        self.assertEqual(app_module.collection_status["status"], "cancelled")
        self.assertTrue(app_module.collection_status["cancel_requested"])

    def test_cancel_endpoint_sets_active_operation_event(self):
        event = threading.Event()
        app_module._collection_cancel_event = event
        with app_module._state_lock:
            app_module.collection_status.update(
                {
                    "status": "running",
                    "operation_id": "active-operation",
                }
            )

        response = self.client.post(
            "/api/collection/active-operation/cancel"
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(event.is_set())

    def test_export_requires_post_and_returns_local_store_snapshot(self):
        app_module.local_store.upsert_feedback_items(
            [{"Feedback_ID": "exported", "Feedback": "Persisted"}]
        )

        get_response = self.client.get("/api/feedback/export")
        with mock.patch.object(app_module, "DATA_DIR", self.temp_dir.name):
            post_response = self.client.post(
                "/api/feedback/export",
                json={"download": True},
            )

        self.assertEqual(get_response.status_code, 405)
        self.assertEqual(post_response.status_code, 200)
        self.assertIn(
            "attachment",
            post_response.headers.get("Content-Disposition", ""),
        )
        self.assertIn("Persisted", post_response.get_data(as_text=True))
        post_response.close()


if __name__ == "__main__":
    unittest.main()
