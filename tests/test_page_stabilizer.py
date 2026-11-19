"""页面稳定门禁的库级用例（AC-1.3 / AC-1.5 / AC-1.6 / AC-1.7 / AC-1.11）。

这一层只测 ``atp/core/page_stabilizer.py`` 自身的判据：稳定/不稳的边界、
薄树不得提前放行、缓存树永不参与决策、非移动端 no-op、not_idle 一拍即停。
执行器链路（截图时机、点击次数、事件 payload）在 ``test_adaptive_wait.py``。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from mobile_qa_helpers import (  # noqa: E402
    H, W, SeqStabilizer, TreeDriver, make_config, page, thin_page,
)
from atp.agents.perception.page_stabilizer import (  # noqa: E402
    PageFrame, PageStack, PageStabilizer, has_loading, jaccard, node_count,
    page_signature, walk_nodes,
)
from atp.common.types import UINode  # noqa: E402


def _loading_page() -> UINode:
    rows = [UINode(cls="TextView", text=f"字段{i}", bounds=(60, 200 + i * 26, 1020, 224 + i * 26))
            for i in range(30)]
    rows.append(UINode(cls="ProgressBar", text="加载中", bounds=(400, 1700, 680, 1760)))
    return UINode(cls="Root", bounds=(0, 0, W, H), children=rows)


class WebDriver(TreeDriver):
    platform = "web"


class SignatureTest(unittest.TestCase):
    """两级签名：粗粒度只看身份，细粒度才看 bounds。"""

    def test_coarse_signature_ignores_bounds(self):
        """AC-1.10 前提：滚动 1 像素让 fine_fp 变、coarse_sig 不变。"""
        base = page()
        moved = page()
        for node in moved.walk():                     # 整棵树下移 1px
            x1, y1, x2, y2 = node.bounds
            node.bounds = (x1, y1 + 1, x2, y2 + 1)
        self.assertNotEqual(base.fingerprint(), moved.fingerprint(),
                            "细粒度指纹必须包含 bounds")
        self.assertEqual(page_signature(base), page_signature(moved),
                         "粗粒度签名绝不能含绝对坐标")
        self.assertEqual(jaccard(page_signature(base), page_signature(moved)), 1.0)

    def test_signature_has_no_absolute_coordinates(self):
        sig = page_signature(page())
        ints = [v for item in sig for v in item if isinstance(v, int)]
        self.assertTrue(ints, "容器骨架里应当有层级字段")
        self.assertTrue(all(0 <= v <= 12 for v in ints),
                        f"签名里的整数只能是层级（≤12），不能是像素坐标: {sorted(set(ints))}")
        for item in sig:
            if item[0] == "n":
                self.assertTrue(all(isinstance(v, str) for v in item[1:]),
                                f"节点签名里出现了非字符串（疑似坐标）: {item}")
            else:
                self.assertEqual(len(item), 4, f"容器骨架格式不对: {item}")

    def test_jaccard_edges(self):
        self.assertEqual(jaccard(set(), set()), 1.0)
        self.assertEqual(jaccard({"a"}, set()), 0.0)
        self.assertEqual(jaccard({"a", "b"}, {"a", "b"}), 1.0)
        self.assertEqual(jaccard({"a"}, {"a", "b"}), 0.5)

    def test_loading_marker_detected(self):
        self.assertTrue(has_loading(_loading_page()))
        self.assertFalse(has_loading(page()))

    def test_walk_and_count(self):
        root = page(count=5)
        pairs = list(walk_nodes(root))
        self.assertEqual(len(pairs), node_count(root))
        self.assertEqual(pairs[0][1], 0, "第一个产出的一定是根节点（深度 0）")
        self.assertEqual(node_count(None), 0)


class PageStackTest(unittest.TestCase):
    def test_push_current_previous(self):
        stack = PageStack()
        self.assertIsNone(stack.current())
        self.assertIsNone(stack.previous())
        stack.push(PageFrame(step_id="s01"))
        stack.push(PageFrame(step_id="s02"))
        self.assertEqual(stack.current().step_id, "s02")
        self.assertEqual(stack.previous().step_id, "s01")
        self.assertEqual(len(stack), 2)
        self.assertEqual(stack[0].frame_id, "frame-1", "帧号自动递增")
        self.assertGreater(stack[0].at, 0.0)
        self.assertEqual(stack.pop().step_id, "s02")
        stack.clear()
        self.assertEqual(len(stack), 0)

    def test_push_type_guard(self):
        with self.assertRaises(TypeError):
            PageStack().push({"step_id": "x"})


class StabilizeCriteriaTest(unittest.TestCase):
    """AC-1.3 / AC-1.5 / AC-1.6 / AC-1.7 / AC-1.11。"""

    def setUp(self):
        self.cfg = make_config()
        self.pr = self.cfg.page_ready

    # ---- AC-1.3 ---------------------------------------------------------
    def test_ac_1_3_stable_when_two_identical_fingerprints(self):
        drv = TreeDriver(page(count=60))
        events: list[dict] = []
        st = PageStabilizer(drv, self.cfg,
                            on_event=lambda name, **kw: events.append({"type": name, **kw}))
        res = st.wait_stable("step_before")

        self.assertTrue(res.stable, "指纹连续一致 + 无 loading + 非缓存树 → 必须判稳定")
        self.assertFalse(res.degraded)
        self.assertGreaterEqual(res.wait_ms, self.pr.min_wait_ms, "不得早于 min_wait_ms")
        self.assertLessEqual(res.wait_ms, self.pr.max_wait_ms, "不得晚于 max_wait_ms")
        self.assertGreaterEqual(res.tree_polls, 2)
        self.assertLessEqual(res.tree_polls, self.pr.max_tree_polls, "tree_polls 必须 ≤8")
        self.assertEqual(drv.shots, 0, "稳定门禁期间绝不允许截图")
        self.assertEqual([e["type"] for e in events], ["page_ready"])
        self.assertIn("fine_fp", events[0])
        self.assertFalse(events[0]["degraded"])

    def test_ac_1_3_node_count_drift_within_10_percent_tolerated(self):
        """同指纹下节点数波动 ≤10% 仍算稳定；超过就必须重新计数。"""
        small = page(count=20)    # 23 个节点
        grown = page(count=22)    # 25 个节点（+2 ≈ 9%）
        st = SeqStabilizer(TreeDriver(small), self.cfg,
                           [(small, "fp-a"), (grown, "fp-a")])
        res = st.wait_stable("step_before")
        self.assertTrue(res.stable, "10% 以内的节点数波动属于渲染抖动，应放行")

    def test_ac_1_3_node_count_jump_rejected(self):
        small = page(count=20)    # 23 个节点
        balloon = page(count=40)  # 43 个节点（+87%）
        cfg = make_config()
        cfg.page_ready.max_tree_polls = 3          # 缩短等待：判据只看「是否放行」
        st = SeqStabilizer(TreeDriver(small), cfg,
                           [(small, "fp-a"), (balloon, "fp-a"),
                            (small, "fp-a"), (balloon, "fp-a")])
        res = st.wait_stable("step_before")
        self.assertFalse(res.stable, "节点数暴涨说明还在渲染，不得放行")
        self.assertTrue(res.degraded)

    # ---- AC-1.11 --------------------------------------------------------
    def test_ac_1_11_thin_tree_never_released_early(self):
        """半渲染白屏（节点 <20）即使 coarse_sig 一致也不得判稳定。"""
        drv = TreeDriver(thin_page())
        st = PageStabilizer(drv, self.cfg)
        self.assertLess(node_count(drv.tree), self.pr.min_nodes)
        res = st.wait_stable("step_before")
        self.assertFalse(res.stable, "薄树不得因粗粒度签名一致而提前放行")
        self.assertTrue(res.degraded)
        self.assertEqual(res.reason, "thin_stable")
        self.assertEqual(drv.shots, 0)
        self.assertLessEqual(res.tree_polls, self.pr.max_tree_polls,
                             "薄树必须有界收手，不能等满上限")

    def test_ac_1_11_thin_then_full_screen_waits_for_real_page(self):
        """白屏半树 → 画完 → 才稳定；证据必须来自画完那一屏。"""
        half, full = thin_page(), page(count=40)
        st = SeqStabilizer(TreeDriver(half), self.cfg,
                           [(half, "fp-thin"), (half, "fp-thin"),
                            (full, "fp-full"), (full, "fp-full")])
        res = st.wait_stable("step_before")
        self.assertTrue(res.stable)
        self.assertEqual(res.fine_fp, "fp-full", "返回的必须是画完之后那一屏")

    # ---- AC-1.5 ---------------------------------------------------------
    def test_ac_1_5_tree_stale_never_stable(self):
        drv = TreeDriver(page(count=60))
        drv.tree_stale = True
        st = PageStabilizer(drv, self.cfg)
        res = st.wait_stable("step_before")
        self.assertFalse(res.stable, "缓存树绝不参与稳定性判定")
        self.assertTrue(res.degraded)
        self.assertNotEqual(res.reason, "stable")
        self.assertEqual(drv.shots, 0, "缓存树也不该顺手截图当证据")

    def test_ac_1_5_loading_page_never_stable(self):
        drv = TreeDriver(_loading_page())
        st = PageStabilizer(drv, self.cfg)
        res = st.wait_stable("step_before")
        self.assertFalse(res.stable, "带明确加载标记的页面不得放行")

    # ---- AC-1.6 ---------------------------------------------------------
    def test_ac_1_6_not_idle_stops_after_single_poll(self):
        drv = TreeDriver(page(count=60))
        drv.not_idle = True
        events: list[dict] = []
        st = PageStabilizer(drv, self.cfg,
                            on_event=lambda name, **kw: events.append({"type": name, **kw}))
        res = st.wait_stable("step_before")
        self.assertFalse(res.stable)
        self.assertEqual(res.reason, "unsupported_page")
        self.assertEqual(res.tree_polls, 1, "not_idle 必须一拍即停，不重试")
        self.assertFalse(res.timed_out)
        self.assertLess(res.wait_ms, 1000, "不能等满 12s 的上限")
        self.assertEqual(drv.dumps, 1, "只允许抓一次树")

    # ---- AC-1.7 ---------------------------------------------------------
    def test_ac_1_7_web_is_noop(self):
        drv = WebDriver(page(count=60))
        st = PageStabilizer(drv, self.cfg)
        res = st.wait_stable("step_before")
        self.assertTrue(res.stable, "非移动端必须 no-op 放行")
        self.assertEqual(res.reason, "non_mobile_noop")
        self.assertEqual(res.wait_ms, 0)
        self.assertEqual(drv.dumps, 0, "no-op 不该抓树")
        self.assertEqual(drv.shots, 0)

    def test_ac_1_7_web_stable_observe_keeps_original_behavior(self):
        drv = WebDriver(page(count=60))
        st = PageStabilizer(drv, self.cfg)
        obs = st.stable_observe("step_before")
        self.assertIsNotNone(obs)
        self.assertEqual(drv.dumps, 1)
        self.assertEqual(drv.shots, 1, "no-op 分支只走原有 observe()，不额外取图")

    # ---- 取消 -----------------------------------------------------------
    def test_cancel_inside_polling_raises_quickly(self):
        """AC-5.4：轮询中取消 → 一个轮询间隔内抛 RunCancelled，且不再截图/点击。"""
        from atp.common.types import RunCancelled

        drv = NeverStableDriver()
        st = PageStabilizer(drv, self.cfg, should_cancel=lambda: drv.dumps >= 2)
        with self.assertRaises(RunCancelled):
            st.wait_stable("step_before")
        self.assertLessEqual(drv.dumps, 3, "取消后最多再拍一次就必须停")
        self.assertEqual(drv.shots, 0)
        self.assertEqual(drv.taps, [], "取消后不得有任何设备动作")


class NeverStableDriver(TreeDriver):
    """每次 dump 都换文本，用来构造「永不稳」。"""

    def __init__(self, **kwargs):
        super().__init__(page(count=30), **kwargs)
        self._n = 0

    def dump_tree(self):
        self._n += 1
        self.dumps += 1
        self.log.append(("dump",))
        return page(count=30, target_text=f"目标按钮-{self._n}")


class UnstableTimeoutTest(unittest.TestCase):
    def test_never_stable_times_out_fail_closed(self):
        cfg = make_config()
        drv = NeverStableDriver()
        events: list[dict] = []
        st = PageStabilizer(drv, cfg,
                            on_event=lambda name, **kw: events.append({"type": name, **kw}))
        res = st.wait_stable("step_before")
        self.assertFalse(res.stable, "永不稳必须 fail-closed")
        self.assertTrue(res.degraded)
        self.assertEqual(res.reason, "timeout_unstable")
        self.assertLessEqual(res.tree_polls, cfg.page_ready.max_tree_polls)
        self.assertLessEqual(res.wait_ms, cfg.page_ready.max_wait_ms + 300)
        self.assertEqual([e["type"] for e in events], ["page_ready_timeout"])
        self.assertIn("detail", events[0])


class CachedDegradeRecoveryTest(unittest.TestCase):
    """回归：已登记「不稳 / 薄」的指纹只能让循环少等，无权否决当下证据。

    历史缺陷：``_unstable_fps`` 一经登记，同指纹永远不再判稳（与「有机会翻案」
    的注释矛盾），会导致同一 run 内后续步骤假失败。
    """

    def test_cached_unstable_never_blocks_a_settled_page(self):
        cfg = make_config()
        drv = TreeDriver(page(count=40))
        st = PageStabilizer(drv, cfg)
        fp = page(count=40).fingerprint()
        st.note_unstable(fp)          # 双登记：历史超时 + 历史薄树
        st.note_thin(fp)

        for i in range(10):
            st.invalidate()           # 每次设备动作后 reuse 失效，必须重新判定
            res = st.wait_stable("step_before")
            self.assertTrue(res.stable, f"第 {i + 1} 次等待被判不稳（{res.reason}）")
            self.assertFalse(res.degraded, f"第 {i + 1} 次被判 degraded（{res.reason}）")
            self.assertEqual(res.reason, "stable")
            self.assertLessEqual(res.tree_polls, cfg.page_ready.max_tree_polls,
                                 "翻案也必须尽快收敛，不能等满上限")

    def test_overturn_after_a_real_timeout(self):
        """先真跑一次超时让指纹进黑名单，再让同一屏稳定下来 → 必须放行。"""
        cfg = make_config()
        cfg.page_ready.max_tree_polls = 3          # 缩短首轮超时
        drv = FlipDriver()
        st = PageStabilizer(drv, cfg)

        first = st.wait_stable("step_before")
        self.assertFalse(first.stable, "首轮确实是永不稳")
        self.assertIn(first.fine_fp, st._unstable_fps, "首轮必须登记该指纹（缺陷前提）")

        drv.flip = True                            # 同一屏此刻开始稳定
        st.invalidate()
        second = st.wait_stable("step_before")
        self.assertTrue(second.stable, f"同屏已稳定却被历史登记否决：{second.reason}")
        self.assertFalse(second.degraded)
        self.assertEqual(second.reason, "stable")

    def test_thin_page_still_never_released_after_fix(self):
        """回归 AC-1.11：修完翻案逻辑后，薄树依然不得提前放行。"""
        cfg = make_config()
        drv = TreeDriver(thin_page())
        st = PageStabilizer(drv, cfg)
        st.note_thin(thin_page().fingerprint())
        res = st.wait_stable("step_before")
        self.assertFalse(res.stable, "薄树绝不能被判稳")
        self.assertEqual(res.reason, "thin_stable")
        self.assertTrue(res.degraded)


class FlipDriver(TreeDriver):
    """先永不稳（每拍换文案），翻面后固定返回「最后一次抓到的」那一屏。"""

    def __init__(self):
        super().__init__(page(count=40))
        self.flip = False
        self._n = 0
        self.last: UINode | None = None

    def dump_tree(self):
        self.dumps += 1
        self.log.append(("dump",))
        if not self.flip:
            self._n += 1
            self.last = page(count=40, target_text=f"目标按钮-{self._n}")
        return self.last if self.last is not None else page(count=40)


if __name__ == "__main__":
    unittest.main()
