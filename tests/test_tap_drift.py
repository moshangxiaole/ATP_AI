"""搜索结果页跑偏治理：输入框不能被当成可点击的结果条目。

事故 run-20261117-170154-6dcb：模型要"点击搜索结果第一条『示例综艺节目』"，
节点列表里 TextInput '示例综艺节目' 标着「可点击」排在第 0 位，真正的结果行
ListItem 可点击却空文本——模型连点输入框 5 次，键盘开合把画面签名变了，
重复闸门数不到，一直在联想词页打转。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.agents.planner import _descendant_label, indexed_nodes  # noqa: E402
from atp.agents.runners.guard import GuardMixin  # noqa: E402
from atp.common.types import Observation, UINode  # noqa: E402


def _search_page() -> UINode:
    """复刻示例视频应用联想词页：输入框含关键词，第一条联想词挂在空文本 ListItem 里。"""
    return UINode(cls="Column", id="4", bounds=(0, 0, 1256, 2760), children=[
        UINode(cls="Row", id="8533", bounds=(0, 120, 1256, 286), children=[
            UINode(cls="TextInput", id="8536", text="示例综艺节目",
                   bounds=(175, 147, 1205, 259), clickable=True),
        ]),
        UINode(cls="List", id="9042", bounds=(0, 286, 1256, 1700), children=[
            # 第一条结果行：可点击容器无文案，字挂在子 Text 上
            UINode(cls="ListItem", id="9045", bounds=(52, 280, 1204, 455),
                   clickable=True, children=[
                       UINode(cls="Text", id="9064", text="示例综艺节目",
                              bounds=(60, 300, 300, 360)),
                   ]),
            UINode(cls="ListItem", id="9047", bounds=(52, 455, 1204, 630),
                   clickable=True, children=[
                       UINode(cls="Text", id="9072", text="示例综艺节目 第二季",
                              bounds=(60, 480, 340, 540)),
                   ]),
        ]),
    ])


class DescendantLabelTest(unittest.TestCase):
    def test_label_from_child_text(self):
        item = _search_page().children[1].children[0]
        self.assertEqual(_descendant_label(item), "示例综艺节目")

    def test_no_children_returns_empty(self):
        self.assertEqual(_descendant_label(UINode(cls="Text", id="x")), "")


class IndexedNodesTest(unittest.TestCase):
    def setUp(self):
        self.root = _search_page()
        self.picked, listing = indexed_nodes(self.root, focus=["示例综艺节目"])
        self.listing = listing

    def _line(self, node_id: str) -> str:
        for ln in self.listing.splitlines():
            if f"id={node_id}" in ln:
                return ln
        self.fail(f"节点 {node_id} 不在列表里")

    def test_editable_marked_as_input_not_clickable(self):
        ln = self._line("8536")
        self.assertIn("输入框", ln)
        self.assertNotIn("可点击", ln)

    def test_clickable_empty_row_gets_child_text_as_ctx(self):
        ln = self._line("9045")
        self.assertIn("可点击", ln)
        self.assertIn("ctx='示例综艺节目'", ln)

    def test_plain_static_text_unchanged(self):
        ln = self._line("9064")
        self.assertIn("静态", ln)
        self.assertIn("'示例综艺节目'", ln)


class TapsEditableTest(unittest.TestCase):
    def _obs(self, root: UINode) -> Observation:
        return Observation(platform="harmony", device_id="hm",
                           width=1256, height=2760, root=root)

    def test_rect_on_input_box(self):
        obs = self._obs(_search_page())
        node = UINode(cls="Stack", id="8535", bounds=(175, 147, 1205, 259))
        self.assertTrue(GuardMixin._taps_editable(node, obs))

    def test_rect_on_suggestion_row(self):
        obs = self._obs(_search_page())
        node = UINode(cls="ListItem", id="9045", bounds=(52, 280, 1204, 455),
                      clickable=True)
        self.assertFalse(GuardMixin._taps_editable(node, obs))

    def test_no_bounds_is_false(self):
        obs = self._obs(_search_page())
        self.assertFalse(GuardMixin._taps_editable(None, obs))
        self.assertFalse(GuardMixin._taps_editable(
            UINode(cls="Text", id="x", bounds=(0, 0, 0, 0)), obs))


if __name__ == "__main__":
    unittest.main()
