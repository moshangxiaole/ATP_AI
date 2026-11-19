"""定位器的两条真机修正：坐标策略不能点遮挡物，重复文案要按位置消歧。

事故（run / AI 首跑 2026-11-10）：请假表单里【请假类别】【假期子类别】【时间范围】
的值全是「请选择」。滚动后目标旧坐标落到页面底部，那里正好被固定栏的「附件」盖住。
坐标兜底策略用 find_smallest_at 取最上层节点，交回「附件」——于是"点请假类别"
名正言顺地跳进了附件上传页，模型再怎么滑也回不来。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.agents.perception.locator import LocatorStrategy, resolve  # noqa: E402
from atp.common.types import UINode  # noqa: E402

W, H = 1260, 2800


def form_tree(visible_rows: int = 3) -> UINode:
    """三个同文案「请选择」+ 底部固定栏（绘制顺序在最后 = 盖在上面）。"""
    rows = []
    for i, y in enumerate((1333, 1571, 1806)):
        b = (66, y, 213, y + 56) if i < visible_rows else (0, 0, 0, 0)
        rows.append(UINode(cls="TextView", text="请选择", bounds=b, clickable=True))
    footer = UINode(cls="TextView", text="附件", bounds=(66, 2317, 178, 2383), clickable=True)
    return UINode(cls="Root", bounds=(0, 0, W, H), children=[
        UINode(cls="WebView", bounds=(0, 100, W, 2650), scrollable=True, children=rows),
        footer,
    ])


def strategies(bounds_value: list[int] | None = None) -> list[LocatorStrategy]:
    out = [LocatorStrategy.make("text", "请选择")]
    if bounds_value:
        out.append(LocatorStrategy.make("bounds", bounds_value))
    return out


class BoundsMustNotHitCoverTest(unittest.TestCase):
    def test_old_bounds_point_into_footer_is_rejected(self):
        """三个「请选择」都滚出视野，旧坐标落在「附件」上：绝不能算命中。"""
        tree = form_tree(visible_rows=0)   # 目标都不在首屏，只剩固定栏
        loc = resolve(tree, strategies([52, 823, 169, 843]), (W, H),
                      avoid_occluded=True)
        self.assertFalse(loc.ok, "坐标策略把遮挡物『附件』当成目标了")
        self.assertEqual(loc.offscreen, "请选择", "应当说明目标滚出视野，好让执行器去滑动")

    def test_bounds_still_works_when_no_occlusion(self):
        """没有别的策略文字时（纯视觉 point 步骤），坐标兜底照常生效。"""
        tree = UINode(cls="Root", bounds=(0, 0, W, H), children=[
            UINode(cls="Button", text="提交", bounds=(0, 2400, W, 2500), clickable=True),
        ])
        loc = resolve(tree, [LocatorStrategy.make("bounds", [100, 860, 900, 900])],
                      (W, H), avoid_occluded=True)
        self.assertTrue(loc.ok)

    def test_bounds_matching_expected_label_is_allowed(self):
        """落点确实就是目标文字时，不能被新闸误伤。"""
        tree = UINode(cls="Root", bounds=(0, 0, W, H), children=[
            UINode(cls="Button", text="确定", bounds=(630, 2530, 1260, 2597), clickable=True),
        ])
        loc = resolve(tree, [LocatorStrategy.make("text", "确定"),
                             LocatorStrategy.make("bounds", [500, 900, 1000, 930])],
                      (W, H), avoid_occluded=True)
        self.assertTrue(loc.ok)
        self.assertEqual(loc.node.text, "确定")


class DuplicateTextDisambiguationTest(unittest.TestCase):
    def test_prefers_row_nearest_recorded_position(self):
        tree = form_tree()
        # bounds 记下的是第二行「请选择」(y≈1599)，归一化后 y≈571
        loc = resolve(tree, strategies([52, 565, 169, 585]), (W, H))
        self.assertTrue(loc.ok)
        self.assertAlmostEqual(loc.node.center[1], 1599, delta=32,
                               msg="没有按沉淀时的位置挑中第二行")

    def test_old_position_too_far_keeps_dom_order(self):
        """页面滚过之后旧坐标是误导，不能硬套。"""
        tree = form_tree()
        loc = resolve(tree, strategies([52, 20, 169, 40]), (W, H))
        self.assertTrue(loc.ok)
        self.assertAlmostEqual(loc.node.center[1], 1333 + 28, delta=32)


if __name__ == "__main__":
    unittest.main()
