"""模型调用链路日志回归。

run-20261116-093006 排查时只能看到解析后的 `decision`，看不到模型当时收到的
提示词、也不到它原话返回了什么，更看不到每一步驱动实际做了什么动作。
这里钉住：完整提示词 / 原始返回 / 解析结果 / 异常 / 执行操作都要落盘，
且图片 base64 与 API Key 不能进日志。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from atp.agents.llm.client import LLMClient, LLMError, THROTTLE  # noqa: E402
from atp.agents.runner import AiRunner  # noqa: E402
from atp.config import Config, ModelConfig  # noqa: E402
from atp.common.stepir import NLCase  # noqa: E402
from atp.runs.artifacts import ACTIONS_FILE, EVENTS_FILE, LLM_FILE  # noqa: E402
from atp.runs.events import EventLog  # noqa: E402
from console_fakes import FakeLLMClient, FakeOaDriver, ScriptedPlanner  # noqa: E402


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _reply(content: str, usage: dict | None = None) -> bytes:
    payload = {"choices": [{"message": {"content": content}}]}
    if usage:
        payload["usage"] = usage
    return json.dumps(payload, ensure_ascii=False).encode()


def _ok_opener(content: str, usage: dict | None = None):
    def build(*_args):
        op = mock.Mock()
        op.open.return_value = _Resp(_reply(content, usage))
        return op
    return build


def _client(trace) -> LLMClient:
    cfg = ModelConfig()
    cfg.base_url = "https://api.example.com/v1"
    cfg.api_key = "secret-key-must-not-be-logged"
    cfg.model = "test-model"
    cfg.throttle = False        # 单测里别等限速
    return LLMClient(cfg, timeout=2, trace=trace)


class TraceTest(unittest.TestCase):
    def tearDown(self):
        # 测试里关掉了进程级限速，别把全局状态带给别的用例
        THROTTLE.enabled = True

    def test_full_prompt_and_raw_response_are_logged(self):
        events: list[tuple[str, dict]] = []
        content = '{"action":"input","text":"彭婉","node_index":33}'
        messages = [{"role": "system", "content": "你是 UI 自动化测试执行大脑"},
                    {"role": "user", "content": "当前界面节点：[33] Edit '搜索'"}]
        with mock.patch("atp.agents.llm.client.urllib.request.build_opener",
                        side_effect=_ok_opener(content, {"prompt_tokens": 128})):
            got = _client(lambda t, p: events.append((t, p))).chat_json(messages)

        self.assertEqual(got["text"], "彭婉")
        self.assertEqual([t for t, _ in events],
                         ["llm_request", "llm_response", "llm_parsed"])
        req = events[0][1]
        self.assertEqual(req["messages"], messages)
        self.assertEqual(req["model"], "test-model")
        self.assertTrue(req["json_mode"])
        self.assertNotIn("secret-key-must-not-be-logged", json.dumps(req, ensure_ascii=False))
        resp = events[1][1]
        self.assertEqual(resp["raw"], content)
        self.assertEqual(resp["usage"]["prompt_tokens"], 128)
        self.assertGreaterEqual(resp["elapsed_ms"], 0)
        self.assertEqual(events[2][1]["parsed"], got)
        self.assertEqual(req["call_id"], resp["call_id"])

    def test_image_parts_are_sanitized(self):
        events: list[tuple[str, dict]] = []
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "这个按钮在哪"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + "A" * 5000}},
        ]}]
        with mock.patch("atp.agents.llm.client.urllib.request.build_opener",
                        side_effect=_ok_opener('{"found":true}')):
            _client(lambda t, p: events.append((t, p))).chat_json(messages)

        dumped = json.dumps(events[0][1], ensure_ascii=False)
        self.assertNotIn("AAAA", dumped)
        self.assertIn("<image:", dumped)
        self.assertIn("这个按钮在哪", dumped)

    def test_failure_is_logged_too(self):
        events: list[tuple[str, dict]] = []

        def build(*_args):
            op = mock.Mock()
            op.open.side_effect = OSError("connection refused")
            return op

        with mock.patch("atp.agents.llm.client.urllib.request.build_opener",
                        side_effect=build), \
             mock.patch("atp.agents.llm.client.time.sleep"):
            with self.assertRaises(LLMError):
                _client(lambda t, p: events.append((t, p))).chat_json(
                    [{"role": "user", "content": "hi"}])

        self.assertEqual(events[0][0], "llm_request")
        self.assertEqual([t for t, _ in events if t == "llm_error"],
                         ["llm_error"])   # 重试 N 次仍只留一条汇总错误
        self.assertIn("connection refused", events[-1][1]["error"])

    def test_runner_wires_client_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(home=Path(tmp))
            client = LLMClient(ModelConfig())
            runner = AiRunner(FakeOaDriver(), cfg,
                              planner=ScriptedPlanner([]), client=client)
            self.assertEqual(client.trace, runner._log_event)


class EventLogMirrorTest(unittest.TestCase):
    def test_llm_and_actions_get_their_own_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp)
            log = EventLog.open(art)
            log.write("step_started", {"index": 1})
            log.write("llm_request", {"messages": [{"role": "user", "content": "hi"}]})
            log.write("action_request", {"action": "input_text", "args": {"text": "彭婉"}})
            log.write("action_result", {"ok": True, "detail": "已输入"})
            log.close()

            main = [json.loads(l) for l in (art / EVENTS_FILE).read_text(encoding="utf-8").splitlines()]
            llm = [json.loads(l) for l in (art / LLM_FILE).read_text(encoding="utf-8").splitlines()]
            actions = [json.loads(l) for l in (art / ACTIONS_FILE).read_text(encoding="utf-8").splitlines()]
            self.assertEqual([e["type"] for e in main],
                             ["step_started", "llm_request", "action_request", "action_result"])
            self.assertEqual([e["type"] for e in llm], ["llm_request"])
            self.assertEqual([e["type"] for e in actions],
                             ["action_request", "action_result"])
            self.assertEqual(llm[0]["messages"][0]["content"], "hi")


class RunnerActionLogTest(unittest.TestCase):
    def test_run_writes_action_request_and_result(self):
        """真的跑一次首跑：events/actions 里必须有驱动动作的请求与返回。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(home=Path(tmp))
            cfg.artifacts_dir = str(Path(tmp) / "art")
            cfg.db_path = str(Path(tmp) / "t.db")
            cfg.apps_path = str(Path(tmp) / "apps.json")
            cfg.persist_runs = False
            cfg.screenshot_on_success = False
            runner = AiRunner(
                FakeOaDriver(), cfg,
                planner=ScriptedPlanner([
                    {"action": "input", "node_index": 2, "text": "tester",
                     "intent": "在用户名输入框填写 tester"},
                ]),
                client=FakeLLMClient())
            runner.run(NLCase(case_id="log-1", title="日志", nl="填写用户名",
                              platform="android", max_steps=5),
                       out_dir=cfg.artifacts, run_id="log-1")
            art = Path(cfg.artifacts) / "log-1"
            rows = [json.loads(l) for l in
                    (art / EVENTS_FILE).read_text(encoding="utf-8").splitlines()]
            reqs = [r for r in rows if r["type"] == "action_request"]
            results = [r for r in rows if r["type"] == "action_result"]
            self.assertTrue(reqs, "每一步驱动动作都要有请求日志")
            self.assertTrue(results, "每一步驱动动作都要有返回日志")
            self.assertTrue(any(r["action"] == "input_text" and
                               r["args"].get("text") == "tester" for r in reqs))
            self.assertTrue(any(r.get("node") and r["node"]["text"] for r in reqs))
            self.assertTrue((art / ACTIONS_FILE).exists())
            self.assertTrue((art / LLM_FILE).exists())


if __name__ == "__main__":
    unittest.main()
