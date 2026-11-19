"""模型调用频次限制回归（run-20261113-173646：428/429 速率限制打断执行）。

免费额度的模型限制的是每分钟请求数，而 AI 执行每一步都要打一次模型，
十几秒就能撞墙。这里钉住四件事：

1. 开关打开时按"最小间隔"节流，关掉时完全不等待；
2. 每分钟上限按滑动窗口生效，超了要等到最早那次调用滑出窗口；
3. 429 会广播冷却给整个进程（不是只让当前调用自己睡一觉）；
4. 429 的提示要能指导用户怎么办（开限速 / 换模型），而不是干巴巴的 HTTP 429。
"""
from __future__ import annotations

import http.server
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.agents.llm import client as client_mod  # noqa: E402
from atp.agents.llm.client import LLMClient, LLMError, THROTTLE  # noqa: E402
from atp.config import ModelConfig  # noqa: E402


class _Throttle:
    """给每个用例一份干净的闸门，别让用例之间互相影响。"""

    def setUp(self):
        self._orig = (THROTTLE.enabled, THROTTLE.min_interval, THROTTLE.max_per_min)
        THROTTLE.configure(enabled=False, min_interval_ms=0, max_per_min=0)

    def tearDown(self):
        THROTTLE._calls.clear()
        THROTTLE._cooldown_until = 0.0
        THROTTLE.configure(enabled=self._orig[0],
                           min_interval_ms=int(self._orig[1] * 1000),
                           max_per_min=self._orig[2])


class ThrottleWindowTest(_Throttle, unittest.TestCase):
    def test_disabled_does_not_wait(self):
        THROTTLE.configure(enabled=False, min_interval_ms=5000, max_per_min=1)
        t0 = time.monotonic()
        for _ in range(4):
            THROTTLE.before_call()
        self.assertLess(time.monotonic() - t0, 0.2, "关掉限速后不该有任何等待")

    def test_min_interval_is_respected(self):
        THROTTLE.configure(enabled=True, min_interval_ms=200, max_per_min=1000)
        t0 = time.monotonic()
        stamps = []
        for _ in range(3):
            THROTTLE.before_call()
            stamps.append(time.monotonic() - t0)
        self.assertGreaterEqual(stamps[1] - stamps[0], 0.15, stamps)
        self.assertGreaterEqual(stamps[2] - stamps[1], 0.15, stamps)

    def test_per_minute_cap_waits_for_window(self):
        THROTTLE.configure(enabled=True, min_interval_ms=0, max_per_min=2)
        now = time.monotonic()
        # 预置两条 59 秒前的调用：窗口已满，下一次必须等到它们滑出 60 秒窗口
        with THROTTLE._lock:
            THROTTLE._calls.clear()
            THROTTLE._calls.extend([now - 59.0, now - 58.6])
        t0 = time.monotonic()
        THROTTLE.before_call()
        waited = time.monotonic() - t0
        self.assertGreaterEqual(waited, 0.9, f"应等约 1s，实际 {waited:.2f}s")
        self.assertLess(waited, 5.0, f"不该等过头：{waited:.2f}s")

    def test_penalize_blocks_every_caller(self):
        """429 之后是"账号被限"，不是这一个请求慢：冷却要广播给后续调用。"""
        THROTTLE.configure(enabled=True, min_interval_ms=0, max_per_min=1000)
        THROTTLE.penalize(0.4)
        t0 = time.monotonic()
        THROTTLE.before_call()
        self.assertGreaterEqual(time.monotonic() - t0, 0.35)


class RateLimitWaitTest(unittest.TestCase):
    def _err(self, headers=None):
        import email.message

        class _E(Exception):
            pass

        e = _E("429")
        msg = email.message.Message()
        for k, v in (headers or {}).items():
            msg[k] = v
        e.headers = msg
        return e

    def test_retry_after_header_wins(self):
        self.assertEqual(LLMClient._rate_limit_wait(self._err({"Retry-After": "7"}), 0), 7.0)

    def test_backoff_grows_without_header(self):
        waits = [LLMClient._rate_limit_wait(self._err(), i) for i in range(4)]
        self.assertEqual(waits, [5.0, 10.0, 20.0, 40.0], waits)

    def test_backoff_is_capped(self):
        self.assertLessEqual(LLMClient._rate_limit_wait(self._err(), 10), 60.0)


class _RateLimitedServer:
    """一个永远返回 429 的假模型服务。"""

    @classmethod
    def start(cls):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                raw = ('{"error":{"message":"您已达到免费用户的 API 速率限制"}}').encode()
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def stop(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()


class RateLimitedErrorTest(_Throttle, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _RateLimitedServer.start()

    @classmethod
    def tearDownClass(cls):
        _RateLimitedServer.stop()

    def test_429_raises_actionable_error_and_broadcasts_cooldown(self):
        cfg = ModelConfig(base_url=f"http://127.0.0.1:{_RateLimitedServer.port}/v1",
                          model="free-model", api_key="k", throttle=False)
        client = LLMClient(cfg)
        with mock.patch.object(client_mod.THROTTLE, "penalize") as pen:
            with self.assertRaises(LLMError) as ctx:
                client.chat([{"role": "user", "content": "ping"}], max_tokens=8)
        msg = str(ctx.exception)
        self.assertIn("429", msg)
        self.assertIn("限速", msg, f"提示要能指导用户开限速，实际: {msg}")
        self.assertTrue(pen.called, "撞到 429 必须把冷却广播给整个进程")
        self.assertGreaterEqual(pen.call_args[0][0], 1.0)

    def test_throttle_defaults_on_for_free_tier(self):
        cfg = ModelConfig()
        self.assertTrue(cfg.throttle, "默认应开启限速：免费额度模型不压频率必然 429")
        self.assertGreater(cfg.min_interval_ms, 0)
        self.assertGreater(cfg.max_per_min, 0)

    def test_configure_throttle_applies_model_config(self):
        cfg = ModelConfig(throttle=True, min_interval_ms=2500, max_per_min=7)
        client_mod.configure_throttle(cfg)
        snap = THROTTLE.snapshot()
        self.assertTrue(snap["enabled"])
        self.assertEqual(snap["min_interval_ms"], 2500)
        self.assertEqual(snap["max_per_min"], 7)


class BusinessRateLimitTest(_Throttle, unittest.TestCase):
    """run-20261116 新形态：Agnes 免费用户限流走的是 HTTP 200 + 错误 body，
    不是 429。老客户端在这里 KeyError→"响应格式异常"，不重试也不冷却。"""

    def test_detect_agnesai_error_body(self):
        body = {"code": "", "message": "您已达到免费用户的 API 速率限制。升级 Token Plan 即可解锁更高限额。",
                "type": "AgnesAI_error"}
        self.assertIn("速率限制", client_mod._rate_limit_message(body))

    def test_success_and_models_body_not_flagged(self):
        self.assertIsNone(client_mod._rate_limit_message(
            {"choices": [{"message": {"content": "ok"}}],
             "message": "rate limit"}))
        self.assertIsNone(client_mod._rate_limit_message({"data": [{"id": "rate-limit-model"}]}))

    def test_business_limit_cools_down_and_retries(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                raw = json.dumps({"code": "", "message": "您已达到免费用户的 API 速率限制",
                                  "type": "AgnesAI_error"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            cfg = ModelConfig(base_url=f"http://127.0.0.1:{httpd.server_address[1]}/v1",
                              model="test-model-flash", api_key="k", throttle=False)
            client = LLMClient(cfg, attempts=2)
            # 业务限流不该被缓存（错误不是结果），清掉避免跨用例污染
            client_mod.RESPONSE_CACHE._data.clear()
            with mock.patch.object(client_mod.time, "sleep"), \
                 mock.patch.object(client_mod.THROTTLE, "penalize") as pen:
                with self.assertRaises(LLMError) as ctx:
                    client.chat([{"role": "user", "content": "ping"}], max_tokens=8)
            msg = str(ctx.exception)
            self.assertIn("限流", msg, f"提示要带限流信息，实际: {msg}")
            self.assertIn("Token Plan", msg, f"提示要能指导用户，实际: {msg}")
            self.assertTrue(pen.called, "业务层限流同样要广播冷却")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_identical_calls_are_deduped(self):
        """同一请求几秒内的重复调用只发一次（视觉 grounding 反复问同一张图）。"""
        cfg = ModelConfig(base_url="http://127.0.0.1:1/v1", model="m", api_key="k", throttle=False)
        client = LLMClient(cfg, attempts=1)
        client_mod.RESPONSE_CACHE._data.clear()
        ok = {"choices": [{"message": {"content": "ANSWER"}}]}
        with mock.patch.object(client, "_open_with_retry", return_value=ok) as op:
            a = client.chat([{"role": "user", "content": "same"}], max_tokens=8)
            b = client.chat([{"role": "user", "content": "same"}], max_tokens=8)
        self.assertEqual(a, "ANSWER")
        self.assertEqual(b, "ANSWER")
        self.assertEqual(op.call_count, 1, "相同 payload 的第二次调用应命中缓存")


if __name__ == "__main__":
    unittest.main()
