import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class FrontendSecurityRegressionTests(unittest.TestCase):
    def read(self, relative_path):
        with open(os.path.join(ROOT, relative_path), encoding="utf-8") as handle:
            return handle.read()

    def test_tokens_are_not_persisted_in_browser_storage(self):
        browser_sources = "\n".join(
            [
                self.read("src/templates/index.html"),
                self.read("src/templates/feedback_viewer.html"),
                self.read("src/templates/insights_page.html"),
                self.read("src/static/js/collection-manager.js"),
            ]
        )
        self.assertNotIn("localStorage.getItem('fabricToken')", browser_sources)
        self.assertNotIn("localStorage.setItem('fabricToken'", browser_sources)
        self.assertNotIn("sessionStorage.setItem('fabricToken'", browser_sources)

    def test_fabric_connection_state_is_not_persisted_in_browser_storage(self):
        browser_sources = "\n".join(
            [
                self.read("src/templates/feedback_viewer.html"),
                self.read("src/static/js/modern-filter-system.js"),
            ]
        )
        self.assertNotIn("localStorage.setItem('fabricConnected'", browser_sources)
        self.assertNotIn("localStorage.setItem('stateManagementEnabled'", browser_sources)
        self.assertNotIn("localStorage.setItem('fabricStateData'", browser_sources)
        self.assertNotIn("searchParams.set('fabric_connected'", browser_sources)
        self.assertNotIn("params.set('fabric_connected'", browser_sources)
        self.assertNotIn("get('fabric_connected')", browser_sources)

    def test_local_feedback_edits_do_not_require_fabric(self):
        viewer = self.read("src/templates/feedback_viewer.html")
        self.assertIn("let stateManagementEnabled = true;", viewer)
        self.assertIn("fetch('/api/feedback/notes'", viewer)
        self.assertNotIn("if (!stateManagementEnabled || !fabricConnected)", viewer)

    def test_legacy_unvalidated_token_endpoint_is_not_used(self):
        viewer = self.read("src/templates/feedback_viewer.html")
        self.assertNotIn("fetch('/api/store_session_token'", viewer)
        self.assertIn("fetch('/api/fabric/token/validate'", viewer)

    def test_shared_safe_dom_helpers_are_loaded(self):
        for template in (
            "src/templates/index.html",
            "src/templates/feedback_viewer.html",
            "src/templates/insights_page.html",
        ):
            self.assertIn("safe-dom.js", self.read(template), template)

    def test_export_uses_same_origin_post(self):
        index = self.read("src/templates/index.html")
        self.assertIn("fetch('/api/feedback/export'", index)
        self.assertIn("method: 'POST'", index)
        self.assertNotIn(
            "window.location.href = '/api/feedback/export",
            index,
        )

    def test_terminal_polling_releases_resumed_collection_state(self):
        index = self.read("src/templates/index.html")
        manager = self.read("src/static/js/collection-manager.js")
        self.assertIn("clearCollectionManagerOperation();", index)
        self.assertIn(
            "this.isCollecting || this.activeOperationType",
            manager,
        )

    def test_taxonomy_ui_creates_api_valid_defaults(self):
        index = self.read("src/templates/index.html")
        self.assertIn("audience: 'All'", index)
        self.assertIn("feature_area: 'General'", index)

    def test_insights_handles_no_op_fabric_writes(self):
        insights = self.read("src/templates/insights_page.html")
        self.assertIn(
            "response.ok && result.operation_id",
            insights,
        )
        self.assertIn("Connected - nothing to write", insights)

    def test_source_defaults_prioritize_sql_server_and_azure_sql(self):
        sources = self.read("src/static/js/source-configuration.js")

        self.assertIn("this.configurationVersion = 2;", sources)
        self.assertIn("repo: 'vscode-mssql'", sources)
        self.assertIn("repo: 'SqlClient'", sources)
        self.assertIn("id: 'stackoverflow'", sources)
        self.assertIn("id: 'hackerNews'", sources)
        self.assertIn("id: 'devCommunity'", sources)
        self.assertRegex(
            sources,
            re.compile(r"fabricCommunity:\s*\{\s*enabled:\s*false"),
        )
        self.assertRegex(
            sources,
            re.compile(r"ado:\s*\{\s*enabled:\s*false"),
        )
        self.assertNotIn("parentWorkItem: '1319103'", sources)


if __name__ == "__main__":
    unittest.main()
