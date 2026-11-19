"""指标与回归：零新增模型调用、感知预算、基线可复现（AC-2.5 / AC-6.1 辅助）。

这里的用例回答两个问题：
1. 全命中的 replay 路径有没有偷偷多花模型钱？
2. 门禁 / 记忆 / 偏离三条新路径有没有把感知次数抬上去？
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from mobile_qa_helpers import (  # noqa: E402
    APP, CountingHealer, CountingLLM, TreeDriver, make_case, make_config,
    make_executor, page,
)
from mobile_scenarios import run_baseline  # noqa: E402
from atp.agents.perception.judge import Judge  # noqa: E402
from atp.common.stepir import CaseIR, LocatorStrategy, Postcondition, Step  # noqa: E402
from atp.common.types import Action  # noqa: E402


def dsl_case() -> CaseIR:
    """两步全命中：一步带 dsl 断言，一步纯点击。"""
    steps = [
        Step(step_id="s01", intent="点击目标按钮",
             action=Action.TAP,
             strategies=[LocatorStrategy.make("text", "目标按钮")],
             postcondition=Postcondition(kind="assert_visible", claim="目标按钮")),
        Step(step_id="s02", intent="再点一次目标按钮",
             action=Action.TAP,
             strategies=[LocatorStrategy.make("text", "目标按钮")]),
    ]
    return CaseIR(case_id="qa-metrics", title="指标用例", platform="android",
                  app_id=APP, steps=steps)


class ZeroModelCostTest(unittest.TestCase):
    """AC-2.5：全命中 replay 下模型调用计数不变（= 0）。"""

    def test_ac_2_5_no_llm_calls_on_full_hit_replay(self):
        cfg = make_config()
        drv = TreeDriver(page(count=40))
        llm = CountingLLM()
        healer = CountingHealer()
        exe = make_executor(drv, cfg, judge=Judge(llm=llm), healer=healer)

        result = exe.run(dsl_case(), mode="replay", run_id="qa-2-5")

        self.assertTrue(result.passed)
        self.assertEqual(llm.call_count, 0, "门禁 / 记忆 / 偏离三条新路径都不得调模型")
        self.assertEqual(healer.calls, 0, "全命中时不该触发定位器修复")
        self.assertEqual(result.ai_steps, 0)
        for sr in result.steps:
            self.assertFalse(sr.ai_used)
            self.assertNotIn("llm", sr.judge_method)

    def test_ac_2_5_gate_and_deviation_emit_no_model_events(self):
        cfg = make_config()
        drv = TreeDriver(page(count=40))
        llm = CountingLLM()
        exe = make_executor(drv, cfg, judge=Judge(llm=llm))
        exe.run(dsl_case(), mode="replay", run_id="qa-2-5b")

        types = [e.get("type") for e in drv.events]
        self.assertNotIn("healing", types)
        self.assertNotIn("llm_call", types)
        self.assertEqual(llm.call_count, 0)


class SensingBudgetTest(unittest.TestCase):
    """感知预算：健康页面上门禁不得把抓树 / 截图次数抬上去。"""

    def test_sensing_budget_per_step(self):
        cfg = make_config()
        drv = TreeDriver(page(count=40))
        exe = make_executor(drv, cfg)
        result = exe.run(make_case(3), mode="replay", run_id="qa-budget")

        steps = len(result.steps)
        self.assertEqual(steps, 3)
        self.assertTrue(result.passed)
        self.assertLessEqual(drv.dumps / steps, 5,
                             f"每步抓树次数超标：{drv.dumps}/{steps}")
        self.assertLessEqual(drv.shots / steps, 3,
                             f"每步截图次数超标：{drv.shots}/{steps}")
        self.assertEqual(len(drv.taps), steps, "一步一点，不得重复点击")
        self.assertEqual(drv.swipes, [], "健康页面不该触发滑动查找")
        self.assertEqual(drv.backs.count("back"), 0, "健康页面不该出现 BACK")

    def test_gate_events_are_attributed(self):
        cfg = make_config()
        drv = TreeDriver(page(count=40))
        exe = make_executor(drv, cfg)
        exe.run(make_case(2), mode="replay", run_id="qa-events")

        ready = [e for e in drv.events if e.get("type") == "page_ready"]
        self.assertGreaterEqual(len(ready), 2, "每一步都应留下门禁事件")
        for event in ready:
            self.assertIn("wait_ms", event)
            self.assertIn("tree_polls", event)
            self.assertIn("fine_fp", event)
            self.assertFalse(event.get("degraded"))
        pushes = [e for e in drv.events if e.get("type") == "page_stack_push"]
        self.assertTrue(pushes, "页面栈必须有入栈事件")


class BaselineTest(unittest.TestCase):
    """基线指标可复现（AC-0.1 / AC-2.1 的输入）。"""

    def test_run_baseline_shape(self):
        metrics = run_baseline(3)
        self.assertNotIn("error", metrics, f"基线运行报错：{metrics.get('error')}")
        for key in ("steps", "passed", "dumps", "screenshots", "taps", "backs",
                    "elapsed_ms", "events"):
            self.assertIn(key, metrics)
        self.assertEqual(metrics["steps"], 3)
        self.assertTrue(metrics["passed"])
        self.assertEqual(metrics["taps"], 3)
        self.assertEqual(metrics["backs"], 0)
        self.assertGreater(metrics["elapsed_ms"], 0)
        self.assertLessEqual(metrics["dumps"] / 3, 5)
        self.assertLessEqual(metrics["screenshots"] / 3, 3)

    def test_baseline_is_repeatable(self):
        first = run_baseline(3)
        second = run_baseline(3)
        self.assertEqual(first["taps"], second["taps"], "点击次数必须可复现")
        self.assertEqual(first["backs"], second["backs"])
        self.assertEqual(first["passed"], second["passed"])
        self.assertLessEqual(abs(first["dumps"] - second["dumps"]), 3,
                             f"抓树次数波动过大：{first['dumps']} vs {second['dumps']}")


if __name__ == "__main__":
    unittest.main()
