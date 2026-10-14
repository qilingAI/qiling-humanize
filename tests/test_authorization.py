import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_humanize import client, KEY

DEVICE = "device-secret-for-test-only"
VERIFY = client.BASE_URL + "/account/api-authorize?request=browser-confirmation-id"


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key_path = Path(self.directory.name) / "config/credentials.json"
        self.now = 0
        for target, name, value in (
            (client, "KEY_FILE", self.key_path),
            (client.time, "monotonic", lambda: self.now), (client.time, "sleep", self.sleep),
        ):
            mock = patch.object(target, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.creation = {"device_code": DEVICE, "verification_url": VERIFY, "expires_in": 600, "interval": 3}
        self.success = {"status": "authorized", "api_key": KEY, "app_key": "", "base_url": client.BASE_URL,
                        "models_url": client.BASE_URL + "/api/v1/open/models", "model_docs_url": client.BASE_URL + "/api/v1/open/model-docs"}

    def sleep(self, seconds):
        self.now += seconds

    def test_browser_confirmation_then_poll_then_private_save(self):
        shown = []
        responses = [self.creation, {"status": "pending"}, self.success]
        with patch.object(client, "request", side_effect=responses) as request, patch.object(client.webbrowser, "open", return_value=True) as browser:
            result = client.authorize(on_authorization_url=shown.append)
        browser.assert_called_once_with(VERIFY)
        self.assertEqual(shown, [VERIFY])
        self.assertEqual(request.call_args_list[0].kwargs["payload"], {"client_name": "文章去 AI 味 Skill"})
        self.assertEqual([call.args[1] for call in request.call_args_list], ["/api/v1/auth/api-key/requests", "/api/v1/auth/api-key/token", "/api/v1/auth/api-key/token"])
        for call in request.call_args_list[1:]:
            self.assertEqual(call.kwargs["payload"], {"device_code": DEVICE})
        self.assertEqual(self.now, 6)
        self.assertEqual(client.read_json(self.key_path)["api_key"], KEY)
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn(DEVICE, self.key_path.read_text())
        if os.name == "posix":
            self.assertEqual(self.key_path.stat().st_mode & 0o777, 0o600)

    def test_cli_authorization_is_enabled_and_never_prints_secrets(self):
        out = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(client, "request", side_effect=[self.creation, self.success]), patch.object(client.sys, "argv", ["humanize", "authorize", "--no-browser"]), patch.object(client.sys, "stdout", out):
            self.assertEqual(client.main(), 0)
        messages = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(messages[0]["verification_url"], VERIFY)
        self.assertTrue(messages[1]["configured"])
        self.assertNotIn(KEY, out.getvalue())
        self.assertNotIn(DEVICE, out.getvalue())

    def test_no_browser_shows_only_confirmation_link(self):
        shown = []
        with patch.object(client, "request", side_effect=[self.creation, self.success]), patch.object(client.webbrowser, "open") as browser:
            client.authorize(open_browser=False, on_authorization_url=shown.append)
        browser.assert_not_called()
        self.assertEqual(shown, [VERIFY])

    def test_denied_and_consumed_stop_without_saving(self):
        for status in ("denied", "consumed"):
            with self.subTest(status=status), patch.object(client, "request", side_effect=[self.creation, {"status": status}]) as request:
                with self.assertRaises(client.ClientError):
                    client.authorize(open_browser=False, on_authorization_url=lambda url: None)
                self.assertEqual(request.call_count, 2)
                self.assertFalse(self.key_path.exists())

    def test_lost_token_response_stops_without_retry_or_secret_output(self):
        with patch.object(client, "request", side_effect=[self.creation, client.RemoteError(DEVICE + KEY)]) as request:
            with self.assertRaises(client.ClientError) as error:
                client.authorize(open_browser=False, on_authorization_url=lambda url: None)
        self.assertEqual(request.call_count, 2)
        self.assertNotIn(DEVICE, str(error.exception))
        self.assertNotIn(KEY, str(error.exception))
        self.assertFalse(self.key_path.exists())

    def test_rate_limit_obeys_retry_after(self):
        responses = [self.creation, client.RemoteError("限流", 429, 10), self.success]
        with patch.object(client, "request", side_effect=responses):
            client.authorize(open_browser=False, on_authorization_url=lambda url: None)
        self.assertEqual(self.now, 13)

    def test_expiry_stops_without_more_token_requests(self):
        self.creation["expires_in"] = 6
        with patch.object(client, "request", side_effect=[self.creation, {"status": "pending"}]) as request:
            with self.assertRaisesRegex(client.ClientError, "超时"):
                client.authorize(open_browser=False, on_authorization_url=lambda url: None)
        self.assertEqual(self.now, 6)
        self.assertEqual(request.call_count, 2)

    def test_http_410_is_expired(self):
        with patch.object(client, "request", side_effect=[self.creation, client.RemoteError("过期", 410)]):
            with self.assertRaisesRegex(client.ClientError, "过期"):
                client.authorize(open_browser=False, on_authorization_url=lambda url: None)
        self.assertFalse(self.key_path.exists())

    def test_bad_origin_or_device_in_browser_url_is_rejected_before_opening(self):
        urls = ["https://other.example/account/api-authorize?request=confirm", VERIFY.replace("https:", "http:"),
                VERIFY + "&device_code=" + DEVICE, client.BASE_URL + "/other?request=confirm"]
        for url in urls:
            with self.subTest(url=url), patch.object(client, "request", return_value={**self.creation, "verification_url": url}) as request, patch.object(client.webbrowser, "open") as browser:
                with self.assertRaises(client.ClientError):
                    client.authorize()
                self.assertEqual(request.call_count, 1)
                browser.assert_not_called()

    def test_wrong_server_or_software_result_does_not_save_key(self):
        for field, value in (("base_url", "https://other.example"), ("app_key", "software-app"), ("api_key", "bad-key")):
            with self.subTest(field=field), patch.object(client, "request", side_effect=[self.creation, {**self.success, field: value}]):
                with self.assertRaises(client.ClientError):
                    client.authorize(open_browser=False, on_authorization_url=lambda url: None)
                self.assertFalse(self.key_path.exists())

    def test_cli_reuses_existing_key_without_new_authorization(self):
        client.save_key(KEY)
        out = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(client, "request") as request, patch.object(client.sys, "argv", ["humanize", "authorize"]), patch.object(client.sys, "stdout", out):
            self.assertEqual(client.main(), 0)
        request.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["authorization"], "reused")
        self.assertNotIn(KEY, out.getvalue())

    def test_authorization_http_request_sends_no_api_key_and_masks_device_error(self):
        response = io.BytesIO(json.dumps(self.creation).encode())
        with patch.object(client, "build_opener") as opener:
            opener.return_value.open.return_value = response
            client.request("POST", "/api/v1/auth/api-key/requests", payload={"client_name": "文章去 AI 味 Skill"})
            req = opener.return_value.open.call_args.args[0]
            self.assertIsNone(req.get_header("X-api-key"))
        detail = io.BytesIO(json.dumps({"detail": {"message": DEVICE}}).encode())
        error = client.HTTPError(client.BASE_URL, 403, "Forbidden", {}, detail)
        with patch.object(client, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(client.RemoteError) as caught:
                client.request("POST", "/api/v1/auth/api-key/token", payload={"device_code": DEVICE})
            self.assertNotIn(DEVICE, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
