"""AI 用例取消：模型调用期间的取消探针。

run 的执行线程常卡在一次模型调用里（规划/验收/视觉定位，单次最长 60s×重试）。
只靠每步之间检查，用户点取消后要干等模型返回，看起来像没生效。
LLMClient.cancel_probe 在发请求前、退避等待、响应读完时检查，立刻中断。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.agents.llm.client import LLMClient  # noqa: E402
from atp.common.types import RunCancelled  # noqa: E402
from atp.config import Config, ModelConfig  # noqa: E402


class CancelProbeTest(unittest.TestCase):
    def test_probe_before_request_aborts(self):
        """发请求前探针为 True：直接中断，不打网络请求。"""
        flags = {"cancelled": True}
        # 用内存假配置让 client.available=True：测试不能依赖本机 atp.config.json
        # 里是否恰好填了模型名（干净检出时配置是空的）。
        fake = ModelConfig(base_url="http://localhost:1/v1", api_key="test-key",
                           model="test-model")
        client = LLMClient(fake, cancel_probe=lambda: flags["cancelled"])
        with mock.patch.object(client, "_opener") as opener:
            with self.assertRaises(RunCancelled):
                client.chat([{"role": "user", "content": "hi"}])
            opener.assert_not_called()

    def test_probe_during_backoff_aborts(self):
        """退避等待期间取消：不等完整 backoff，最多 0.2s 内中断。"""
        flags = {"cancelled": False}
        client = LLMClient(ModelConfig(), cancel_probe=lambda: flags["cancelled"])

        # 50ms 后翻转取消旗子，模拟用户在模型退避等待时点了取消
        def flip(_arg):
            flags["cancelled"] = True

        timer = mock.Mock(side_effect=flip)
        with mock.patch.object(time, "sleep", timer):
            t0 = time.monotonic()
            with self.assertRaises(RunCancelled):
                client._interruptible_sleep(5.0)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 1.0, f"应在 ~0.2s 内中断，实际 {elapsed:.2f}s")

    def test_no_probe_keeps_normal_behavior(self):
        """未注入探针时，_check_cancel 是 no-op，不影响普通调用。"""
        client = LLMClient(ModelConfig())
        client._check_cancel()  # 不应抛
        client._interruptible_sleep(0.01)

    def test_set_cancel_probe(self):
        client = LLMClient(ModelConfig())
        flags = {"c": False}
        client.set_cancel_probe(lambda: flags["c"])
        client._check_cancel()  # False，不抛
        flags["c"] = True
        with self.assertRaises(RunCancelled):
            client._check_cancel()
        client.set_cancel_probe(None)
        client._check_cancel()  # 卸载后不再抛


if __name__ == "__main__":
    unittest.main()
