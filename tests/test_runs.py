"""执行产物层回归：目录/文件命名 + 事件日志。

产物命名是对外契约（报告页按相对路径内嵌截图与录像、控制台按文件名找产物），
所以只允许从 `atp.runs.artifacts` 出；事件日志写失败绝不允许影响执行。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.runs.artifacts import (EVENTS_FILE, RECORDING_FILE, REPORT_FILE,  # noqa: E402
                                STEPIR_FILE, new_run_id, run_dir)
from atp.runs.events import EventLog  # noqa: E402


class RunDirTest(unittest.TestCase):
    def test_run_dir_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = run_dir(tmp, "case-1")
            self.assertTrue(path.is_dir())
            self.assertEqual(path.name, "case-1")

    def test_replay_run_id_is_unique(self):
        a = new_run_id("samplechat")
        b = new_run_id("samplechat")
        self.assertNotEqual(a, b)
        self.assertRegex(a, r"^samplechat-\d{8}-\d{6}-[0-9a-f]{4}$")

    def test_ai_run_id_keeps_tag(self):
        rid = new_run_id("samplechat", "ai", unique=False)
        self.assertRegex(rid, r"^samplechat-ai-\d{8}-\d{6}$")

    def test_empty_case_id_falls_back(self):
        self.assertTrue(new_run_id("", unique=False).startswith("run-"))


class EventLogTest(unittest.TestCase):
    def test_writes_jsonl_and_skips_private_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp)
            log = EventLog.open(art)
            log.write("step_started", {"index": 1, "intent": "点搜索", "_nodes": object()})
            log.write("log", {"level": "warn", "message": "目标不可见"})
            log.close()

            rows = [json.loads(line) for line in
                    (art / EVENTS_FILE).read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["type"] for r in rows], ["step_started", "log"])
            self.assertNotIn("_nodes", rows[0])
            self.assertEqual(rows[0]["intent"], "点搜索")
            self.assertIn("ts", rows[0])

    def test_write_after_close_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = EventLog.open(Path(tmp))
            log.close()
            log.write("log", {"message": "晚了"})   # 不允许抛异常
            log.close()                              # 幂等

    def test_unwritable_dir_does_not_raise(self):
        # 目录不存在时退化成"丢日志"，执行本身不受影响
        log = EventLog.open(Path(tempfile.gettempdir()) / "atp-no-such-dir" / "deep")
        self.assertEqual(log.path.name, EVENTS_FILE)
        log.write("log", {"message": "x"})
        log.close()


class ArtifactNamesTest(unittest.TestCase):
    def test_names_are_stable(self):
        self.assertEqual(
            (EVENTS_FILE, REPORT_FILE, RECORDING_FILE, STEPIR_FILE),
            ("events.jsonl", "report.html", "recording.mp4", "stepir.json"),
        )


if __name__ == "__main__":
    unittest.main()
