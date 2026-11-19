"""自适应等待与「树稳定 → 截图 → 操作」严格顺序（执行器链路）。

覆盖：AC-1.1 / AC-1.2 / AC-1.4 / AC-1.5 / AC-1.6 / AC-1.8 / AC-1.10 / AC-2.8 / AC-5.4。
所有断言都打在**可观测行为**上：``dump_tree`` / ``screenshot`` / ``tap_xy`` 的调用
顺序与次数、事件 payload、``StepResult`` 字段。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from mobile_qa_helpers import (  # noqa: E402
    APP, TreeDriver, make_case, make_config, make_executor, page, shifted,
    thin_page,
)
from atp.agents.perception.page_stabilizer import PageStabilizer  # noqa: E402
from atp.common.stepir import CaseIR, Step  # noqa: E402
from atp.common.types import Action, RunCancelled  # noqa: E402

TARGET = (540, 2040)      # page() 里「目标按钮」的中心
THIN_CENTER = (540, 2220)  # 白屏半树里「附件」的中心


def seq_driver(*trees: object) -> TreeDriver:
    """按顺序播放前 n 次抓树结果，之后一直停在最后一屏。"""

    class _SeqDriver(TreeDriver):
        def __init__(self):
            super().__init__(trees[-1])
            self._seq = list(trees)

        def dump_tree(self):
            self.dumps += 1
            self.log.append(("dump",))
            return self._seq[min(self.dumps - 1, len(self._seq) - 1)]

    return _SeqDriver()


class NeverStableDriver(TreeDriver):
    """每次抓树文本都变：永不稳。"""

    def __init__(self):
        super().__init__(page(count=30))
        self._n = 0

    def dump_tree(self):
        self._n += 1
        self.dumps += 1
        self.log.append(("dump",))
        return page(count=30, target_text=f"目标按钮-{self._n}")


class RenderThenStableTest(unittest.TestCase):
    """AC-1.1 / AC-1.2：白屏半树 → 画完 → 稳定 → 单次截图 → 点击。"""

    def setUp(self):
        self.tmp_cfg = make_config()
        self.drv = seq_driver(thin_page(), thin_page(), page())
        self.exe = make_executor(self.drv, self.tmp_cfg)

    def test_ac_1_1_before_evidence_matches_rendered_screen(self):
        case = make_case(1)
        result = self.exe.run(case, mode="replay", run_id="qa-1-1")

        self.assertEqual(len(result.steps), 1)
        sr = result.steps[0]
        self.assertEqual(sr.status, "passed", f"应当命中画完之后的页面: {sr.detail}")
        self.assertEqual(case.steps[0].evidence.tree_hash, page().fingerprint(),
                         "before 证据必须对应 60 节点那一屏")

        self.assertEqual(len(self.drv.taps), 1, "一次点击，且只点一次")
        x, y = self.drv.taps[0]
        self.assertEqual((x, y), TARGET, "坐标必须落在目标 bounds 中心")
        self.assertNotEqual((x, y), THIN_CENTER, "绝不能按白屏那一屏的坐标点击")
        self.assertGreaterEqual(self.drv.dumps, 3, "白屏两拍之后才画完")

    def test_ac_1_2_screenshot_only_after_last_stable_dump(self):
        self.exe.run(make_case(1), mode="replay", run_id="qa-1-2")
        log = self.drv.log
        tap_idx = next(i for i, e in enumerate(log) if e[0] == "tap")
        before = log[:tap_idx]

        shots = [i for i, e in enumerate(before) if e[0] == "shot"]
        self.assertTrue(shots, "稳定后必须取且取一次截图")
        # ① 截图永远紧跟在一次抓树之后（树的唯一用途是给截图定时刻）
        for i in shots:
            self.assertEqual(before[i - 1][0], "dump",
                             f"第 {i} 个调用是截图，但它前面不是抓树：{before[max(0, i - 3):i + 1]}")
        # ② 每次 gate 只截一张：两次截图之间必然夹着抓树（轮询期间绝不截图）
        for a, b in zip(shots, shots[1:]):
            self.assertGreaterEqual(b - a, 2, f"轮询期间出现了连续截图：{before[a:b + 1]}")
        # ③ 最后一张截图之后到点击之前，不再抓树
        self.assertTrue(all(e[0] != "dump" for e in before[shots[-1] + 1:]),
                        "截图必须发生在最后一次稳定抓树之后、点击之前")
        self.assertEqual(before[-1][0], "shot", "点击前最后一个动作必须是截图")
        # ④ 一轮稳定轮询里至少两次抓树（连续两拍指纹一致）
        self.assertGreaterEqual(self.drv.dumps, 4)


class FailClosedTest(unittest.TestCase):
    """AC-1.4 / AC-1.5 / AC-1.6：门禁不可信时绝不点击。"""

    def test_ac_1_4_never_stable_fails_closed(self):
        drv = NeverStableDriver()
        exe = make_executor(drv, make_config())
        result = exe.run(make_case(1), mode="replay", run_id="qa-1-4")

        sr = result.steps[0]
        self.assertEqual(sr.status, "failed")
        self.assertEqual(sr.error, "page_unstable")
        self.assertEqual(drv.taps, [], "超时 fail-closed：一次点击都不允许")
        types = [e.get("type") for e in drv.events]
        self.assertIn("page_ready_timeout", types, "必须发出 page_ready_timeout 事件")
        timeout_ev = next(e for e in drv.events if e.get("type") == "page_ready_timeout")
        self.assertTrue(timeout_ev.get("degraded"))
        self.assertIn("detail", timeout_ev)
        self.assertLessEqual(int(timeout_ev.get("wait_ms", 0)), 3300,
                             "单次门禁耗时必须 ≤ max_wait_ms + 300ms")
        self.assertLessEqual(sr.latency_ms, 3300,
                             f"单步耗时必须 ≤ max_wait_ms + 300ms，实际 {sr.latency_ms}ms")
        self.assertLessEqual(int(timeout_ev.get("tree_polls", 99)), 8,
                             "单次门禁抓树次数上限 8 次")

    def test_ac_1_5_tree_stale_never_clicked(self):
        drv = TreeDriver(page(count=40))
        drv.tree_stale = True
        exe = make_executor(drv, make_config())
        result = exe.run(make_case(1), mode="replay", run_id="qa-1-5")

        sr = result.steps[0]
        self.assertEqual(sr.status, "failed", "缓存树不允许作为点击依据")
        self.assertEqual(drv.taps, [], "绝不拿缓存树点击")
        self.assertIn("page_ready_timeout", [e.get("type") for e in drv.events])

    def test_ac_1_6_not_idle_no_retry_no_full_wait(self):
        drv = TreeDriver(page(count=40))
        drv.not_idle = True
        exe = make_executor(drv, make_config())
        t0 = time.perf_counter()
        result = exe.run(make_case(2), mode="replay", run_id="qa-1-6")
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        self.assertEqual(len(result.steps), 2)
        for sr in result.steps:
            self.assertEqual(sr.status, "failed")
            self.assertEqual(sr.error, "page_unstable")
        self.assertEqual(drv.taps, [])
        self.assertLess(elapsed_ms, 3000, "not_idle 不得重试三遍、不得等满 12s")
        self.assertLessEqual(drv.dumps, 6, "每步最多一次门禁抓树 + 一次兜底 observe")


class NoAdditiveWaitTest(unittest.TestCase):
    """AC-1.8 / AC-2.8：等待不叠加，且由稳定判据而不是配置值退出。"""

    def _launch_case(self) -> CaseIR:
        return CaseIR(case_id="qa-launch", title="冷启动一步", platform="android",
                      app_id=APP,
                      steps=[Step(step_id="s01", intent="打开应用",
                                  action=Action.LAUNCH_APP, args={"app": APP})])

    def _gate_events(self, drv: TreeDriver) -> list[dict]:
        return [e for e in drv.events
                if e.get("type") in ("page_ready", "page_ready_timeout")]

    def test_ac_1_8_single_step_waits_are_merged(self):
        cfg = make_config()
        cfg.cold_start.enabled = True
        cfg.cold_start.settle_ms = 800
        cfg.cold_start.launch_settle_ms = 1800
        drv = TreeDriver(page(count=40))
        exe = make_executor(drv, cfg)
        t0 = time.perf_counter()
        result = exe.run(self._launch_case(), mode="replay", run_id="qa-1-8")
        wall_ms = int((time.perf_counter() - t0) * 1000)

        self.assertEqual(result.steps[0].status, "passed")
        gate = self._gate_events(drv)
        self.assertGreaterEqual(len(gate), 3,
                                f"launch/popup/scroll 多处门禁应当都发生，实际 {len(gate)}")
        total_wait = sum(int(e.get("wait_ms", 0)) for e in gate)
        self.assertLessEqual(total_wait, cfg.page_ready.max_wait_ms,
                             f"稳定等待总时长不得是各 ceiling 之和，实际 {total_wait}ms")
        self.assertLess(wall_ms, cfg.page_ready.max_wait_ms,
                        f"整步墙钟也必须在单次上限内，实际 {wall_ms}ms")
        shots = [i for i, e in enumerate(drv.log) if e[0] == "shot"]
        # 共 3 张：run_start 一帧 + 步骤 before + 步骤 after 证据图
        self.assertEqual(len(shots), 3,
                         f"截图次数异常：{[(i, drv.log[i]) for i in shots]}")
        self.assertLessEqual(len(shots) - 1, 2,
                             "单步内截图不得超过 2 次（before + after）")

    def test_ac_2_8_zero_ceiling_still_respects_min_wait(self):
        cfg = make_config()
        drv = TreeDriver(page(count=40))
        st = PageStabilizer(drv, cfg)
        res = st.wait_stable("step_before", ceiling_ms=0)
        self.assertTrue(res.stable)
        self.assertGreaterEqual(res.wait_ms, cfg.page_ready.min_wait_ms,
                               "ceiling=0 也必须至少等 min_wait_ms")
        self.assertLess(res.wait_ms, cfg.page_ready.max_wait_ms,
                        "并由稳定判据退出，而不是睡满上限")

    def test_ac_2_8_huge_ceiling_exits_on_stability(self):
        cfg = make_config()
        drv = TreeDriver(page(count=40))
        st = PageStabilizer(drv, cfg)
        res = st.wait_stable("step_before", ceiling_ms=5000)
        self.assertTrue(res.stable)
        self.assertLess(res.wait_ms, 1000, "稳定之后不许睡满 5000ms")
        self.assertLessEqual(res.tree_polls, cfg.page_ready.max_tree_polls)

    def test_ac_2_8_launch_settle_zero_keeps_step_min_wait(self):
        cfg = make_config()
        cfg.cold_start.enabled = True
        cfg.cold_start.launch_settle_ms = 0
        cfg.cold_start.settle_ms = 0
        drv = TreeDriver(page(count=40))
        exe = make_executor(drv, cfg)
        exe.run(self._launch_case(), mode="replay", run_id="qa-2-8-zero")

        waits = [int(e.get("wait_ms", 0)) for e in self._gate_events(drv)
                 if e.get("reason") in ("step_before", "run_start")]
        self.assertTrue(waits, "步骤门禁必须仍然发生")
        self.assertGreaterEqual(min(waits), cfg.page_ready.min_wait_ms,
                               "launch_settle_ms=0 时也仍由步骤门禁保证 min_wait_ms")

    def test_ac_2_8_launch_settle_5000_does_not_sleep_full(self):
        cfg = make_config()
        cfg.cold_start.enabled = True
        cfg.cold_start.launch_settle_ms = 5000
        cfg.cold_start.settle_ms = 0
        drv = TreeDriver(page(count=40))
        exe = make_executor(drv, cfg)
        t0 = time.perf_counter()
        result = exe.run(self._launch_case(), mode="replay", run_id="qa-2-8-big")
        wall_ms = int((time.perf_counter() - t0) * 1000)

        self.assertEqual(result.steps[0].status, "passed")
        self.assertLess(wall_ms, 2500, f"稳定后不得睡满 5000ms，实际 {wall_ms}ms")


class SamePageChangeTest(unittest.TestCase):
    """AC-1.10：细粒度指纹变、粗粒度签名不变 → 不判偏离、不 BACK。"""

    def test_ac_1_10_one_pixel_scroll_is_not_deviation(self):
        drv = TreeDriver(page(count=40))
        drv.after_tap = lambda d: setattr(d, "tree", shifted(page(count=40), dy=1))
        exe = make_executor(drv, make_config())
        result = exe.run(make_case(1), mode="replay", run_id="qa-1-10")

        self.assertEqual(result.steps[0].status, "passed")
        self.assertEqual(drv.taps, [(540, 2040)], "同页变化不该引发第二次点击")
        self.assertEqual(drv.back_count(), 0, "同页变化绝不能触发 BACK")
        types = [e.get("type") for e in drv.events]
        self.assertNotIn("deviation_detected", types)
        self.assertNotIn("deviation_back", types)


class CancelFlowTest(unittest.TestCase):
    """AC-5.4：轮询中点取消 → 立刻停，且停止后没有设备动作。"""

    def test_ac_5_4_cancel_in_post_action_gate(self):
        drv = TreeDriver(page(count=40))
        drv.after_tap = lambda d: setattr(d, "cancelled", True)
        exe = make_executor(drv, make_config())
        with self.assertRaises(RunCancelled) as ctx:
            exe.run(make_case(3), mode="replay", run_id="qa-5-4")

        partial = getattr(ctx.exception, "partial", None)
        self.assertIsNotNone(partial, "取消必须留痕（partial RunResult）")
        self.assertTrue(partial.aborted)
        self.assertLessEqual(len(partial.steps), 1, "取消后不得继续推进步骤")
        self.assertEqual(len(drv.taps), 1, "取消之后不得再有点击")
        self.assertEqual(drv.back_count(), 0, "取消之后不得再有 BACK")
        self.assertEqual(drv.swipes, [])


class CarryStableSampleTest(unittest.TestCase):
    """跨门禁复用：上一轮门禁的稳定指纹可充当本轮第 1 拍（真机上省一次 2.5s 抓树）。

    注意：会话级 reuse（默认 500ms 窗口）比这条更激进，所以用例都先跨过 500ms，
    确保测的是「跨门禁复用」而不是「同一瞬间直接复用」。
    """

    def _stabilizer(self, drv, cfg) -> PageStabilizer:
        return PageStabilizer(drv, cfg)

    def test_no_action_means_one_dump_is_enough(self):
        cfg = make_config()
        drv = TreeDriver(page(count=40))
        stab = self._stabilizer(drv, cfg)

        first = stab.wait_stable("step_before")
        self.assertTrue(first.stable)
        self.assertGreaterEqual(first.tree_polls, 2, "首拍没有可复用样本，必须采满两拍")

        time.sleep(0.7)            # 跨过 reuse_window_ms(500)，仍留在 carry_stable_ms(1500) 内
        before = drv.dumps
        second = stab.wait_stable("step_before")
        self.assertTrue(second.stable)
        self.assertEqual(drv.dumps - before, 1, "期间无设备动作时，第二拍只需再抓一次树")
        self.assertEqual(second.tree_polls, 1)
        self.assertFalse(second.reused, "这里走的是跨门禁复用，不是会话级 reuse")

    def test_action_invalidates_the_carried_sample(self):
        cfg = make_config()
        drv = TreeDriver(page(count=40))
        stab = self._stabilizer(drv, cfg)
        stab.wait_stable("step_before")

        stab.invalidate()          # 等价于执行器在 perform() 之后调的那一次
        before = drv.dumps
        result = stab.wait_stable("post_action")

        self.assertTrue(result.stable)
        self.assertGreaterEqual(drv.dumps - before, 2,
                                "有动作之后必须重新采满两拍，不许吃旧样本")
        self.assertGreaterEqual(result.tree_polls, 2)

    def test_switch_off_restores_two_polls(self):
        cfg = make_config()
        cfg.page_ready.carry_stable_ms = 0
        cfg.page_ready.reuse_window_ms = 0
        drv = TreeDriver(page(count=40))
        stab = self._stabilizer(drv, cfg)
        stab.wait_stable("step_before")

        before = drv.dumps
        result = stab.wait_stable("step_before")
        self.assertTrue(result.stable)
        self.assertEqual(drv.dumps - before, 2, "关掉复用就该回到两拍")

    def test_carried_flag_is_visible_in_events(self):
        cfg = make_config()
        drv = TreeDriver(page(count=40))
        stab = PageStabilizer(drv, cfg, on_event=drv.event_sink())
        stab.wait_stable("step_before")
        time.sleep(0.7)
        stab.wait_stable("step_before")

        ready = [e for e in drv.events if e.get("type") == "page_ready"]
        self.assertTrue(ready)
        self.assertIn("carried", ready[-1], "事件必须能看出这一拍是否吃了旧样本")
        self.assertTrue(ready[-1]["carried"])


if __name__ == "__main__":
    unittest.main()
