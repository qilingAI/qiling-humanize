import importlib.util
from http.client import IncompleteRead
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("humanize", Path(__file__).parents[1] / "scripts/humanize.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)
KEY = "qk_" + "a" * 40


class HumanizeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output = self.root / "finished.md"
        self.state = self.root / "finished.md.qiling-state.json"

    def test_split_keeps_all_content_and_stays_inside_api_limit(self):
        text = "\n\t" + "读" * 1200 + "。\n\n" + "书" * 1500 + "！" + "🦊" * 1100 + "\n\n"
        parts = client.split_text(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(0 < len(part) <= 2000 for part in parts))
        self.assertTrue(parts[0].endswith("\n\n"))
        with self.assertRaises(client.ClientError):
            client.split_text("\n  ")

    def test_post_receipt_is_saved_before_query(self):
        calls = []

        def service(method, path, key, payload=None):
            calls.append(method)
            if method == "POST":
                self.assertEqual(payload, {"text": "原文"})
                return {"task_id": "task-1", "charged": "0.00", "balance": "0.00"}
            saved = client.read_json(self.state)
            self.assertEqual(saved["parts"][0]["task_id"], "task-1")
            return {"status": "completed", "text": "改文"}

        with patch.object(client, "request", side_effect=service):
            result = client.rewrite("原文", self.output, self.state, KEY)
        self.assertEqual(calls, ["POST", "GET"])
        self.assertEqual(self.output.read_text(), "改文")
        self.assertEqual(result["receipts"][0]["charged"], "0.00")

    def test_unknown_submission_cannot_be_automatically_resubmitted(self):
        with patch.object(client, "request", side_effect=client.RemoteError("超时")) as request:
            with self.assertRaises(client.ClientError):
                client.rewrite("原文", self.output, self.state, KEY)
            with self.assertRaises(client.ClientError):
                client.rewrite("原文", self.output, self.state, KEY)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(client.read_json(self.state)["parts"][0]["status"], "submitting")
        self.assertFalse(self.output.exists())

    def test_missing_task_id_also_requires_review(self):
        with patch.object(client, "request", return_value={"status": "queued"}) as request:
            for _ in range(2):
                with self.assertRaises(client.ClientError):
                    client.rewrite("原文", self.output, self.state, KEY)
        self.assertEqual(request.call_count, 1)

    def test_restart_reuses_completed_and_pending_parts(self):
        text = "甲" * 2000 + "乙"
        posts = []

        def interrupted(method, path, key, payload=None):
            if method == "POST":
                posts.append(payload["text"])
                return {"task_id": "task-" + str(len(posts))}
            if path.endswith("task-1"):
                return {"status": "completed", "text": "第一段改文"}
            raise client.ClientError("模拟等待中断")

        with patch.object(client, "request", side_effect=interrupted):
            with self.assertRaises(client.ClientError):
                client.rewrite(text, self.output, self.state, KEY)
        self.assertFalse(self.output.exists())
        with patch.object(client, "request", return_value={"status": "completed", "text": "第二段改文"}) as request:
            client.rewrite(text, self.output, self.state, KEY)
        self.assertEqual(len(posts), 2)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], "GET")
        self.assertEqual(self.output.read_text(), "第一段改文\n\n第二段改文")

    def test_failed_task_does_not_trigger_another_paid_post(self):
        with patch.object(client, "request", side_effect=[{"task_id": "failed-task"}, {"status": "failed", "error_code": "UPSTREAM_ERROR", "message": "失败"}]):
            with self.assertRaises(client.ClientError):
                client.rewrite("原文", self.output, self.state, KEY)
        with patch.object(client, "request") as request:
            with self.assertRaises(client.ClientError):
                client.rewrite("原文", self.output, self.state, KEY)
            request.assert_not_called()

    def test_failed_response_cannot_copy_key_to_state_or_error(self):
        with patch.object(client, "request", side_effect=[{"task_id": "task-1"}, {"status": "failed", "message": "错误 " + KEY}]):
            with self.assertRaises(client.ClientError) as error:
                client.rewrite("原文", self.output, self.state, KEY)
        self.assertNotIn(KEY, str(error.exception))
        self.assertNotIn(KEY, self.state.read_text())

    def test_review_can_attach_original_task(self):
        with patch.object(client, "request", side_effect=client.RemoteError("超时")):
            with self.assertRaises(client.ClientError):
                client.rewrite("原文", self.output, self.state, KEY)
        with self.assertRaises(client.ClientError):
            client.repair_part(self.state, 1)
        client.repair_part(self.state, 1, task_id="original-task")
        with patch.object(client, "request", return_value={"status": "completed", "text": "改文"}) as request:
            client.rewrite("原文", self.output, self.state, KEY)
        self.assertEqual(request.call_args.args[0], "GET")

    def test_new_input_cannot_reuse_old_state(self):
        with patch.object(client, "request", side_effect=client.RemoteError("超时")):
            with self.assertRaises(client.ClientError):
                client.rewrite("原文", self.output, self.state, KEY)
        with patch.object(client, "request") as request:
            with self.assertRaises(client.ClientError):
                client.rewrite("不同的原文", self.output, self.state, KEY)
            request.assert_not_called()

    def test_concurrent_command_cannot_submit_same_state(self):
        with client.task_lock(self.state), patch.object(client, "request") as request:
            with self.assertRaises(client.ClientError):
                client.rewrite("原文", self.output, self.state, KEY)
            request.assert_not_called()
        with patch.object(client, "request", side_effect=[{"task_id": "task-1"}, {"status": "completed", "text": "改文"}]):
            client.rewrite("原文", self.output, self.state, KEY)

    def test_restart_after_completion_does_not_make_new_requests(self):
        with patch.object(client, "request", side_effect=[{"task_id": "task-1"}, {"status": "completed", "text": "改文"}]):
            client.rewrite("原文", self.output, self.state, KEY)
        with patch.object(client, "request") as request:
            client.rewrite("原文", self.output, self.state, KEY)
            request.assert_not_called()

    def test_existing_output_is_preserved(self):
        self.output.write_text("用户已有文件", encoding="utf-8")
        with patch.object(client, "request") as request:
            with self.assertRaises(client.ClientError):
                client.rewrite("原文", self.output, self.state, KEY)
            request.assert_not_called()
        self.assertEqual(self.output.read_text(), "用户已有文件")

    def test_credentials_are_private_and_status_does_not_print_key(self):
        key_path = self.root / "config/credentials.json"
        with patch.object(client, "KEY_FILE", key_path), patch.dict(os.environ, {}, clear=True):
            client.save_key(KEY)
            self.assertEqual(client.load_key(), (KEY, "local"))
            if os.name == "posix":
                self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
            output = io.StringIO()
            with patch.object(client.sys, "argv", ["humanize", "key-status"]), patch.object(client.sys, "stdout", output):
                self.assertEqual(client.main(), 0)
            self.assertNotIn(KEY, output.getvalue())
            self.assertTrue(json.loads(output.getvalue())["configured"])
            if os.name == "posix":
                key_path.chmod(0o644)
                with self.assertRaises(client.ClientError):
                    client.load_key()

    def test_request_uses_published_endpoint_and_unicode_json(self):
        response = io.BytesIO(b'{"task_id":"a"}')
        with patch.object(client, "build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(client.request("POST", "/api/v1/open/remove-ai", KEY, {"text": "中文"}), {"task_id": "a"})
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url, "https://toolsoul.daniu7.cn/api/v1/open/remove-ai")
            self.assertEqual(req.get_header("X-api-key"), KEY)
            self.assertEqual(json.loads(req.data.decode("utf-8")), {"text": "中文"})
        with self.assertRaises(client.RemoteError):
            client.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")

    def test_rate_limited_query_waits_and_never_posts_again(self):
        responses = [{"task_id": "slow-task"}, client.RemoteError("限流", 429, 9), {"status": "completed", "text": "改文"}]
        with patch.object(client, "request", side_effect=responses) as request, patch.object(client.time, "sleep") as sleep:
            client.rewrite("原文", self.output, self.state, KEY)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["POST", "GET", "GET"])
        self.assertEqual(sleep.call_args.args[0], 9)

    def test_incomplete_http_response_is_treated_as_unknown_submission(self):
        with patch.object(client, "build_opener") as opener:
            opener.return_value.open.side_effect = IncompleteRead(b"partial")
            with self.assertRaises(client.RemoteError):
                client.request("POST", "/api/v1/open/remove-ai", KEY, {"text": "原文"})

    def test_split_keeps_crlf_together_at_character_limit(self):
        text = "甲" * 1999 + "\r\n" + "乙" * 1999 + "\r\n"
        parts = client.split_text(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(0 < len(part) <= 2000 for part in parts))
        self.assertTrue(all(not part.endswith("\r") for part in parts))
        self.assertEqual(sum(client.line_break_count(part) for part in parts), 2)

    def test_cli_preserves_newlines_from_file_or_stdin_through_json_and_output(self):
        for source in ("file", "stdin"):
            for newline in ("\n", "\r\n", "\r"):
                with self.subTest(source=source, newline=repr(newline)):
                    text = newline + "  第一段第一行" + newline + "第二行" + newline * 3 + "\t第二段，保留字面量 \\n 和 🦊。" + newline * 2
                    rewritten = newline * 2 + "  服务返回第一段" + newline * 3 + "第二段。" + newline * 2
                    original = self.root / (source + str(len(newline)) + "-" + str(ord(newline[-1])) + ".txt")
                    original.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
                    output = original.with_suffix(".out.md")
                    sent = []

                    def service(req, **kwargs):
                        if req.method == "POST":
                            sent.append(json.loads(req.data.decode("utf-8"))["text"])
                            return io.BytesIO(b'{"task_id":"newline-task"}')
                        return io.BytesIO(json.dumps({"status": "completed", "text": rewritten}, ensure_ascii=False).encode("utf-8"))

                    stdin = io.TextIOWrapper(io.BytesIO(original.read_bytes()), encoding="utf-8", newline=None)
                    args = ["humanize", "rewrite", "--input", str(original) if source == "file" else "-", "--output", str(output)]
                    with patch.dict(os.environ, {"QILING_API_KEY": KEY}, clear=True), patch.object(client.sys, "argv", args), patch.object(client.sys, "stdin", stdin), patch.object(client.sys, "stdout", io.StringIO()), patch.object(client, "build_opener") as opener:
                        opener.return_value.open.side_effect = service
                        self.assertEqual(client.main(), 0)
                    stdin.close()
                    self.assertEqual(sent, [text])
                    self.assertEqual(output.read_bytes(), rewritten.encode("utf-8"))

    def test_plan_counts_untrimmed_characters_and_real_newlines(self):
        text = "\r\n第一段\r\n\r\n第二段\r\n"
        stdout = io.StringIO()
        with patch.object(client.sys, "argv", ["humanize", "plan", "--input", "-"]), patch.object(client.sys, "stdin", io.StringIO(text)), patch.object(client.sys, "stdout", stdout), patch.object(client, "request") as request:
            self.assertEqual(client.main(), 0)
            request.assert_not_called()
        plan = json.loads(stdout.getvalue())
        self.assertEqual(plan["characters"], len(text))
        self.assertEqual(plan["line_breaks"], 4)
        self.assertEqual(plan["part_line_breaks"], [4])

    def test_long_article_posts_preserve_every_original_character(self):
        text = "\n\n" + "甲" * 1200 + "\r\n\r\n" + "乙" * 1200 + "\n\n" + "丙" * 1100 + "\n\n"
        submitted, returned = [], []

        def service(method, path, key, payload=None):
            if method == "POST":
                submitted.append(payload["text"])
                return {"task_id": str(len(submitted))}
            result = "\r\n改文" + path.rsplit("/", 1)[-1] + "\r\n\r\n"
            returned.append(result)
            return {"status": "completed", "text": result}

        with patch.object(client, "request", side_effect=service):
            client.rewrite(text, self.output, self.state, KEY)
        self.assertEqual("".join(submitted), text)
        self.assertTrue(all(len(part) <= 2000 for part in submitted))
        self.assertEqual(self.output.read_bytes().decode("utf-8"), "\n\n".join(returned))

    def test_upgrade_resumes_legacy_chunk_boundaries_without_resubmitting(self):
        text = "\n" + "甲" * 2000 + "乙\n"
        client.write_json(self.state, {"base_url": client.BASE_URL, "input_sha256": client.hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                     "output": str(self.output.resolve()), "parts": [{"status": "completed", "text": "\n第一段改文\n"}, {"status": "ready"}]})
        with patch.object(client, "request", side_effect=[{"task_id": "last-task"}, {"status": "completed", "text": "第二段改文"}]) as request:
            client.rewrite(text, self.output, self.state, KEY)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["POST", "GET"])
        self.assertEqual(request.call_args_list[0].args[3], {"text": "乙"})
        self.assertEqual(self.output.read_text(), "第一段改文\n\n第二段改文\n")

    def test_whitespace_only_fragment_is_rejected_before_paid_submission(self):
        with patch.object(client, "request") as request:
            with self.assertRaises(client.ClientError):
                client.rewrite("正文" + " " * 2200, self.output, self.state, KEY)
            request.assert_not_called()

    def test_upgrade_can_resume_legacy_file_with_normalized_windows_newlines(self):
        text = "\r\n第一段\r\n\r\n第二段\r\n"
        previously_read = text.replace("\r\n", "\n")
        client.write_json(self.state, {"base_url": client.BASE_URL, "input_sha256": client.hashlib.sha256(previously_read.encode("utf-8")).hexdigest(),
                                     "output": str(self.output.resolve()), "parts": [{"status": "queued", "task_id": "legacy-task"}]})
        with patch.object(client, "request", return_value={"status": "completed", "text": "完成"}) as request:
            client.rewrite(text, self.output, self.state, KEY)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], "GET")

    def test_trailing_newline_at_limit_stays_with_nonempty_text(self):
        for newline in ("\n", "\r\n", "\r"):
            with self.subTest(newline=repr(newline)):
                text = "甲" * 2000 + newline
                parts = client.split_text(text)
                self.assertEqual("".join(parts), text)
                self.assertTrue(all(part.strip() and len(part) <= 2000 for part in parts))


if __name__ == "__main__":
    unittest.main()
