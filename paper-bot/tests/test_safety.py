from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import main
from notify import QmsgError, prepare_chunks, send_chunk
from safety import SafeError, safe_error, sanitize_for_qmsg


def _http_error() -> requests.HTTPError:
    response = requests.Response()
    response.status_code = 401
    response.url = "https://qmsg.zendee.cn/v3/send/FAKE_KEY_123456"
    response.reason = "Unauthorized"
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        return exc
    raise AssertionError("expected HTTPError")


class SanitizeTests(unittest.TestCase):
    def test_normal_digest_text_unchanged(self) -> None:
        text = "【沙箱内存·每日 3 篇】11-21 周五\n今日候选 125 → 相关 3\n评分 4.5/5"
        self.assertEqual(sanitize_for_qmsg(text), text)

    def test_url_ipv4_and_long_digits_removed(self) -> None:
        text = "见 https://qmsg.zendee.cn/v3/send/SECRET 和 192.168.1.10，编号 123456789"
        cleaned = sanitize_for_qmsg(text)
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("SECRET", cleaned)
        self.assertNotIn("192.168", cleaned)
        self.assertNotIn("123456789", cleaned)
        self.assertIn("[链接]", cleaned)
        self.assertIn("[IP]", cleaned)
        self.assertIn("[数字]", cleaned)

    def test_domain_without_scheme_removed(self) -> None:
        self.assertNotIn("arxiv.org", sanitize_for_qmsg("见 arxiv.org/abs/2411.00001"))


class SafeErrorTests(unittest.TestCase):
    def test_http_error_redacts_key_and_url(self) -> None:
        detail = safe_error(_http_error())
        self.assertNotIn("FAKE_KEY_123456", detail)
        self.assertNotIn("https://", detail)
        self.assertIn("HTTP 401", detail)
        self.assertIn("qmsg.zendee.cn", detail)

    def test_safe_error_passthrough(self) -> None:
        self.assertEqual(safe_error(SafeError("缺少 QMSG_KEY")), "缺少 QMSG_KEY")


class SendChunkTests(unittest.TestCase):
    def _response(self, payload: object) -> MagicMock:
        response = MagicMock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return response

    @patch("notify.time.sleep", return_value=None)
    @patch("notify.requests.post")
    def test_sanitizes_body_and_returns_msg_id(self, post: MagicMock, _sleep: MagicMock) -> None:
        post.return_value = self._response({"success": True, "data": 42})
        msg_id = send_chunk(
            "链接 https://evil.test/x，IP 10.0.0.1，ID 123456789",
            "KEY",
            "https://qmsg.zendee.cn/v3/send/{key}",
            1,
            1,
        )
        body = post.call_args.kwargs["data"]["msg"]
        self.assertEqual(msg_id, 42)
        self.assertNotIn("https://", body)
        self.assertNotIn("10.0.0.1", body)
        self.assertNotIn("123456789", body)

    @patch("notify.time.sleep", return_value=None)
    @patch("notify.requests.post")
    def test_missing_success_is_failure(self, post: MagicMock, _sleep: MagicMock) -> None:
        post.return_value = self._response({"code": 0, "message": "操作成功"})
        with self.assertRaises(QmsgError):
            send_chunk("hello", "KEY", "https://qmsg.zendee.cn/v3/send/{key}", 1, 1)

    @patch("notify.time.sleep", return_value=None)
    @patch("notify.requests.post")
    def test_http_error_does_not_leak_key(self, post: MagicMock, _sleep: MagicMock) -> None:
        response = MagicMock()
        response.raise_for_status.side_effect = _http_error()
        post.return_value = response
        with self.assertRaises(QmsgError) as ctx:
            send_chunk("hello", "FAKE_KEY_123456", "https://qmsg.zendee.cn/v3/send/{key}", 1, 1)
        self.assertNotIn("FAKE_KEY_123456", str(ctx.exception))
        self.assertNotIn("https://", str(ctx.exception))


class ChunkTests(unittest.TestCase):
    def test_prefix_and_limit(self) -> None:
        text = "\n\n".join(["x" * 120] * 4)
        chunks = prepare_chunks(text, 300)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 300)

    def test_url_is_removed_before_splitting(self) -> None:
        text = "a" * 280 + " https://evil.test/very-long-path " + "b" * 80
        chunks = prepare_chunks(text, 100)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertNotIn("https://", chunk)
            self.assertNotIn("evil.test", chunk)


class FailureNotificationTests(unittest.TestCase):
    @patch("main._record_failure")
    @patch("main.run_daily", side_effect=_http_error())
    @patch("main.notify.send_qmsg")
    def test_top_level_error_message_is_redacted(
        self, send_qmsg: MagicMock, _run: MagicMock, _record: MagicMock
    ) -> None:
        with (
            patch.dict(os.environ, {"QMSG_KEY": "FAKE_KEY_123456"}, clear=False),
            patch.object(sys, "argv", ["main.py"]),
        ):
            code = main.main()

        self.assertEqual(code, 1)
        message = send_qmsg.call_args.args[0]
        self.assertNotIn("https://", message)
        self.assertNotIn("FAKE_KEY_123456", message)
        self.assertIn("HTTP 401", message)

    def test_failure_journal_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"general": {"timezone": "Asia/Shanghai", "data_dir": tmp}}
            main._record_failure(cfg, _http_error())
            state = json.loads(
                (Path(tmp) / "last_run.json").read_text(encoding="utf-8")
            )

        text = json.dumps(state, ensure_ascii=False)
        self.assertEqual(state["status"], "failed")
        self.assertNotIn("https://", text)
        self.assertNotIn("FAKE_KEY_123456", text)
        self.assertIn("HTTP 401", text)


if __name__ == "__main__":
    unittest.main()
