from __future__ import annotations

import datetime as dt
import itertools
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import main
import sources

SUMMARY = {
    "problem": "p",
    "core_idea": "c",
    "evidence": "e",
    "relevance": "r",
    "limitation": "l",
    "read_level": "泛读",
    "terms": ["x"],
    "confidence": "high",
}


class DeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fixture = Path(main.BASE_DIR) / "tests" / "fixtures" / "arxiv_sample.xml"
        self.raw = sources.parse_feed(fixture.read_bytes())
        self.scores = {paper["id"]: (4.5, "ok") for paper in self.raw}
        self.cfg = {
            "general": {
                "timezone": "Asia/Shanghai",
                "data_dir": self.tmp.name,
                "top_n": 5,
                "lookback_hours": 72,
                "min_final_score": 4.0,
                "backfill_days": 14,
            },
            "arxiv": {
                "categories": ["cs.OS"],
                "max_results": 400,
                "user_agent": "test",
            },
            "venue": {"top": ["ASPLOS"], "boost": 1.5, "journal_boost": 1.0},
            "llm": {
                "base_url": "https://api.example.test/v1",
                "model": "test",
                "temperature": 0.0,
                "timeout": 5,
                "json_mode": True,
            },
            "qmsg": {
                "endpoint": "https://qmsg.zendee.cn/v3/send/{key}",
                "max_chars": 300,
                "include_url": False,
            },
        }
        self.today = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        self.data_dir = Path(self.tmp.name)
        self.last_run = self.data_dir / "last_run.json"
        self.digest = self.data_dir / "digests" / f"{self.today}.md"

        env = patch.dict(
            os.environ, {"QMSG_KEY": "KEY", "LLM_API_KEY": "sk-test"}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)
        sleep = patch("main.time.sleep", return_value=None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def _run(self, fetch: MagicMock, send: MagicMock) -> int:
        with (
            patch("main.sources.fetch_arxiv", fetch),
            patch("main.llm.score_papers", return_value=self.scores),
            patch("main.llm.summarize_paper", return_value=SUMMARY),
            patch("main.notify.send_chunk", send),
        ):
            return main.run_daily(self.cfg)

    def test_second_run_same_day_is_skipped(self) -> None:
        fetch = MagicMock(return_value=[dict(paper) for paper in self.raw])
        send = MagicMock(side_effect=itertools.count(1))

        self.assertEqual(self._run(fetch, send), 0)
        first_calls = send.call_count
        first_digest = self.digest.read_text(encoding="utf-8")
        self.assertGreater(first_calls, 0)

        self.assertEqual(self._run(fetch, send), 0)
        self.assertEqual(send.call_count, first_calls)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.digest.read_text(encoding="utf-8"), first_digest)

        state = json.loads(self.last_run.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "sent")
        self.assertEqual(state["sent"], 4)

    def test_failure_resumes_only_missing_chunks(self) -> None:
        fetch = MagicMock(return_value=[dict(paper) for paper in self.raw])
        calls = {"n": 0}

        def flaky(*_args: object) -> int:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return calls["n"]

        send = MagicMock(side_effect=flaky)
        with self.assertRaises(RuntimeError):
            self._run(fetch, send)

        journal = json.loads(self.last_run.read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "failed")
        self.assertTrue(journal["chunks"][0]["sent"])
        self.assertFalse(journal["chunks"][1]["sent"])
        self.assertEqual(journal["error"], "RuntimeError")

        total_chunks = len(journal["chunks"])
        before = send.call_count
        send.side_effect = lambda *_args: 99
        self.assertEqual(self._run(fetch, send), 0)
        self.assertEqual(send.call_count, before + total_chunks - 1)

        state = json.loads(self.last_run.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "sent")
        self.assertEqual(state["sent"], 4)
        self.assertTrue((self.data_dir / "papers.jsonl").exists())

    def test_failed_journal_from_previous_day_is_abandoned(self) -> None:
        fetch = MagicMock(return_value=[dict(paper) for paper in self.raw])
        calls = {"n": 0}

        def flaky(*_args: object) -> int:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return calls["n"]

        send = MagicMock(side_effect=flaky)
        with self.assertRaises(RuntimeError):
            self._run(fetch, send)

        journal = json.loads(self.last_run.read_text(encoding="utf-8"))
        journal["date"] = "2020-01-01"
        self.last_run.write_text(json.dumps(journal), encoding="utf-8")
        send.side_effect = itertools.count(100)

        self.assertEqual(self._run(fetch, send), 0)
        state = json.loads(self.last_run.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "sent")
        # 今天仍应正常生成并发送新日报
        self.assertTrue(self.digest.exists())


if __name__ == "__main__":
    unittest.main()
