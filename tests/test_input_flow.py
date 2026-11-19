"""run-20261117-113350 回归：输入前清空、自绘输入框判定、容器不误命中。

问题：
1. 点输入框（Group 容器）后卡住不推进 → 容器应合成 Point 按坐标落点
2. 输入完成未判定已执行、反复重输 → Point 节点应算"已送达"
3. 所有输入都应先清空再输入 → force 清空
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.agents.perception.locator import resolve, LocatorStrategy  # noqa: E402
from atp.agents.runners.guard import GuardMixin  # noqa: E402
from atp.agents.platforms.base import BaseDriver  # noqa: E402
from atp.common.stepir import Step  # noqa: E402
from atp.common.types import Action, UINode, Observation  # noqa: E402
from atp.config import Config  # noqa: E402


class _Result:
    ok = True
    error = ""
    detail = ""


class ClearBeforeInputTest(unittest.TestCase):
    """所有 INPUT_TEXT 都先清空再输入（含自绘 Group/Point 目标）。"""

    def _driver(self):
        class D(BaseDriver):
            platform = "desktop"

            def connect(self): ...
            def screen_size(self): return (1920, 1080)
            def screenshot(self): return b""
            def dump_tree(self): return UINode(cls="Desktop")

            def input_text(self, text): self.typed = text
            def clear_text(self): self.cleared = True
            def tap_xy(self, x, y): ...
            def _looks_editable(self, node): return False  # 模拟自绘 Group

        d = D("fake", Config())
        d.cleared = False
        d.typed = ""
        return d

    def test_group_target_is_cleared_before_input(self):
        d = self._driver()
        node = UINode(cls="Group", id="grp", text="", bounds=(100, 100, 500, 140))
        d.perform(Action.INPUT_TEXT, node, {"text": "测试消息"})
        self.assertTrue(d.cleared, "自绘输入框（Group）输入前也必须清空")
        self.assertEqual(d.typed, "测试消息")

    def test_non_input_action_does_not_clear(self):
        d = self._driver()
        node = UINode(cls="Group", id="grp", bounds=(100, 100, 500, 140))
        d.perform(Action.TAP, node, {})
        self.assertFalse(d.cleared, "非输入动作不能清空，避免误删正文")


class PointDeliveredTest(unittest.TestCase):
    """视觉定位 Point 节点输入后算"已送达"，不再判失败反复重输。"""

    def test_point_counts_as_delivered(self):
        step = Step(step_id="s1", intent="输入", action=Action.INPUT_TEXT,
                    args={"text": "测试消息"})
        node = UINode(cls="Point", bounds=(100, 100, 500, 140), clickable=True)
        self.assertTrue(GuardMixin._input_delivered(step, _Result(), "测试消息", node))

    def test_group_still_not_delivered(self):
        step = Step(step_id="s1", intent="输入", action=Action.INPUT_TEXT,
                    args={"text": "测试消息"})
        node = UINode(cls="Group", bounds=(100, 100, 500, 140))
        self.assertFalse(GuardMixin._input_delivered(step, _Result(), "测试消息", node))

    def test_failed_result_never_delivered(self):
        step = Step(step_id="s1", intent="输入", action=Action.INPUT_TEXT,
                    args={"text": "测试消息"})
        node = UINode(cls="Point", bounds=(100, 100, 500, 140))
        bad = mock.Mock(ok=False)
        self.assertFalse(GuardMixin._input_delivered(step, bad, "测试消息", node))


class ContainerBoundsTest(unittest.TestCase):
    """bounds 命中大容器时合成 Point，不把输入打进 Group。"""

    def test_big_group_becomes_point(self):
        # 视觉落点 [250,700,950,880]（归一化）→ 像素 (480,756)-(1824,950)
        root = UINode(cls="Desktop", bounds=(0, 0, 1920, 1080))
        group = UINode(cls="Group", bounds=(450, 720, 1920, 1080))
        root.children.append(group)
        strategies = [LocatorStrategy.make("bounds", [250, 700, 950, 880])]
        result = resolve(root, strategies, (1920, 1080))
        self.assertEqual(result.node.cls, "Point",
                         f"大 Group 应合成 Point，实际 {result.node}")

    def test_small_static_node_not_converted(self):
        root = UINode(cls="Desktop", bounds=(0, 0, 1920, 1080))
        text = UINode(cls="Text", text="你好", bounds=(480, 756, 560, 790))
        root.children.append(text)
        strategies = [LocatorStrategy.make("bounds", [250, 700, 292, 731])]
        result = resolve(root, strategies, (1920, 1080))
        self.assertEqual(result.node.cls, "Text")


if __name__ == "__main__":
    unittest.main()
