import os
import sys
import unittest
from unittest import mock

from flask import Flask, jsonify

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import app_security


def make_app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app_security.configure_app_security(app)

    @app.route("/api/value", methods=["GET", "POST"])
    def value():
        return jsonify({"ok": True})

    return app


class AppSecurityTests(unittest.TestCase):
    def test_local_requests_receive_security_headers(self):
        with mock.patch.dict(
            os.environ,
            {"ALLOW_REMOTE_ACCESS": "0", "ENABLE_DEBUG_ENDPOINTS": "0"},
        ):
            app = make_app()

        response = app.test_client().get("/api/value")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn(
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
            response.headers["Content-Security-Policy"],
        )

    def test_cross_site_mutation_is_blocked(self):
        with mock.patch.dict(os.environ, {"ALLOW_REMOTE_ACCESS": "0"}):
            app = make_app()

        response = app.test_client().post(
            "/api/value",
            headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_remote_access_is_denied_by_default(self):
        with mock.patch.dict(os.environ, {"ALLOW_REMOTE_ACCESS": "0"}):
            app = make_app()

        response = app.test_client().get(
            "/api/value",
            environ_base={"REMOTE_ADDR": "203.0.113.10"},
        )
        self.assertEqual(response.status_code, 403)

    def test_local_mode_rejects_dns_rebinding_host(self):
        with mock.patch.dict(os.environ, {"ALLOW_REMOTE_ACCESS": "0"}):
            app = make_app()

        response = app.test_client().get(
            "/api/value",
            base_url="http://attacker.example",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_missing_remote_address_fails_closed(self):
        with mock.patch.dict(os.environ, {"ALLOW_REMOTE_ACCESS": "0"}):
            app = make_app()

        response = app.test_client().get(
            "/api/value",
            environ_base={"REMOTE_ADDR": ""},
        )
        self.assertEqual(response.status_code, 403)

    def test_remote_access_requires_configured_token(self):
        with mock.patch.dict(
            os.environ,
            {
                "ALLOW_REMOTE_ACCESS": "1",
                "APP_API_TOKEN": "expected-token",
                "SESSION_COOKIE_SECURE": "1",
            },
        ):
            app = make_app()

        client = app.test_client()
        denied = client.get(
            "/api/value",
            environ_base={"REMOTE_ADDR": "203.0.113.10"},
        )
        allowed = client.get(
            "/api/value",
            headers={"X-FeedbackCollector-Token": "expected-token"},
            environ_base={"REMOTE_ADDR": "203.0.113.10"},
            base_url="https://feedback.example",
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        self.assertIn("Strict-Transport-Security", allowed.headers)

    def test_remote_mode_authenticates_loopback_and_rejects_http(self):
        with mock.patch.dict(
            os.environ,
            {
                "ALLOW_REMOTE_ACCESS": "1",
                "APP_API_TOKEN": "expected-token",
                "SESSION_COOKIE_SECURE": "1",
                "TRUST_PROXY_HEADERS": "0",
            },
        ):
            app = make_app()

        client = app.test_client()
        unauthenticated = client.get("/api/value", base_url="https://localhost")
        insecure = client.get(
            "/api/value",
            headers={"X-FeedbackCollector-Token": "expected-token"},
        )

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(insecure.status_code, 400)

    def test_trusted_proxy_proto_enables_https_detection(self):
        with mock.patch.dict(
            os.environ,
            {
                "ALLOW_REMOTE_ACCESS": "1",
                "APP_API_TOKEN": "expected-token",
                "SESSION_COOKIE_SECURE": "1",
                "TRUST_PROXY_HEADERS": "1",
            },
        ):
            app = make_app()

        response = app.test_client().get(
            "/api/value",
            headers={
                "X-FeedbackCollector-Token": "expected-token",
                "X-Forwarded-Proto": "https",
            },
            environ_base={"REMOTE_ADDR": "192.0.2.10"},
        )
        self.assertEqual(response.status_code, 200)

    def test_remote_access_requires_secure_session_cookie(self):
        with mock.patch.dict(
            os.environ,
            {
                "ALLOW_REMOTE_ACCESS": "1",
                "APP_API_TOKEN": "expected-token",
                "SESSION_COOKIE_SECURE": "0",
            },
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "SESSION_COOKIE_SECURE",
            ):
                make_app()

    def test_token_vault_expires_values(self):
        app = Flask(__name__)
        app.secret_key = "test-secret"
        vault = app_security.FabricTokenVault(ttl_seconds=1)
        with app.test_request_context("/"):
            with mock.patch.object(
                app_security.time,
                "monotonic",
                side_effect=[10.0, 10.0, 10.5, 11.1],
            ):
                vault.put("secret")
                self.assertEqual(vault.get(), "secret")
                self.assertIsNone(vault.get())


if __name__ == "__main__":
    unittest.main()
