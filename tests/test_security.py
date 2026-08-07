import base64
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import Request

from dashboard.app import (
    _allowed_results_url,
    _client_ip,
    _make_unlock_token,
    _rate_buckets,
    _render_brief_md,
    _valid_host_header,
    app,
)


class SecurityTests(unittest.TestCase):
    def setUp(self):
        _rate_buckets.clear()

    def test_admin_routes_fail_closed_without_credentials(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INDRA_DASHBOARD_USER", None)
            os.environ.pop("INDRA_DASHBOARD_PASSWORD", None)
            response = TestClient(app).get("/")
        self.assertEqual(response.status_code, 503)

    def test_public_health_check_has_no_integration_details(self):
        response = TestClient(app).get("/api/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def test_untrusted_host_is_rejected(self):
        self.assertFalse(_valid_host_header("attacker.test/victim.test"))
        response = TestClient(app).get(
            "/api/healthz",
            headers={"Host": "attacker.test"},
        )
        self.assertEqual(response.status_code, 400)

    def test_admin_routes_require_valid_basic_auth(self):
        env = {
            "INDRA_DASHBOARD_USER": "operator",
            "INDRA_DASHBOARD_PASSWORD": "a-strong-test-password",
        }
        with patch.dict(os.environ, env, clear=False):
            client = TestClient(app)
            self.assertEqual(client.get("/not-a-route").status_code, 401)
            token = base64.b64encode(b"operator:a-strong-test-password").decode()
            response = client.get("/not-a-route", headers={"Authorization": f"Basic {token}"})
        self.assertEqual(response.status_code, 404)

    def test_weak_dashboard_password_fails_closed(self):
        env = {
            "INDRA_DASHBOARD_USER": "operator",
            "INDRA_DASHBOARD_PASSWORD": "too-short",
        }
        with patch.dict(os.environ, env, clear=False):
            response = TestClient(app).get("/")
        self.assertEqual(response.status_code, 503)

    def test_cross_site_admin_post_is_rejected(self):
        env = {
            "INDRA_DASHBOARD_USER": "operator",
            "INDRA_DASHBOARD_PASSWORD": "a-strong-test-password",
        }
        token = base64.b64encode(b"operator:a-strong-test-password").decode()
        with patch.dict(os.environ, env, clear=False):
            response = TestClient(app).post(
                "/api/templates/1/delete",
                headers={
                    "Authorization": f"Basic {token}",
                    "Sec-Fetch-Site": "cross-site",
                },
            )
        self.assertEqual(response.status_code, 403)

    def test_forwarded_ip_is_used_only_for_trusted_proxy(self):
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-forwarded-for", b"203.0.113.9")],
            "client": ("10.0.0.5", 1234),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
            "root_path": "",
            "http_version": "1.1",
        }
        request = Request(scope)
        with patch.dict(os.environ, {"INDRA_TRUSTED_PROXIES": "127.0.0.1/32"}, clear=False):
            self.assertEqual(_client_ip(request), "10.0.0.5")
        with patch.dict(os.environ, {"INDRA_TRUSTED_PROXIES": "10.0.0.0/8"}, clear=False):
            self.assertEqual(_client_ip(request), "203.0.113.9")

    def test_markdown_renderer_removes_scriptable_content(self):
        rendered = _render_brief_md(
            '<script>alert("x")</script> [click](javascript:alert("x")) **safe**'
        )
        self.assertNotIn("<script", rendered.lower())
        self.assertNotIn("javascript:", rendered.lower())
        self.assertIn("<strong>safe</strong>", rendered)

    def test_public_routes_reject_multipart_and_excessive_form_fields(self):
        client = TestClient(app)
        multipart = client.post(
            "/api/email/signup",
            files={"email": (None, "person@example.test")},
        )
        self.assertEqual(multipart.status_code, 415)
        body = "&".join(f"field{i}=x" for i in range(101))
        oversized_fields = client.post(
            "/api/email/signup",
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(oversized_fields.status_code, 413)

    def test_results_url_is_restricted_to_configured_origin(self):
        with patch.dict(os.environ, {"SITE_ORIGIN": "https://example.test"}, clear=False):
            self.assertTrue(_allowed_results_url("https://example.test/labs/result?id=1"))
            self.assertFalse(_allowed_results_url("https://attacker.test/phishing"))
            self.assertFalse(_allowed_results_url("http://example.test/insecure"))

    def test_assessment_token_has_no_default_secret(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ASSESS_SECRET", None)
            with self.assertRaises(RuntimeError):
                _make_unlock_token("person@example.test", "big-five", "run-1")


if __name__ == "__main__":
    unittest.main()
