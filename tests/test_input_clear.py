"""输入前先清空。

真机上搜索框几乎总残留上一次的关键词：直接 `input_text` 会拼成
「示例演员示例电视剧」，搜索结果一屏空白，还很难看出是哪一步错了。
所以 `INPUT_TEXT` 的语义统一成 **tap → 清空 → 输入**。

清空同时要有边界：只对"看起来是输入框"的节点做。tap 一旦歪到正文区/列表行上，
桌面的 Ctrl+A + Delete 会选中整页并删掉内容，代价比"输入拼在一起"大得多。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.common.types import Action, UINode  # noqa: E402
from atp.agents.platforms.base import BaseDriver, NotSupported  # noqa: E402

W, H = 1080, 1920


class InputDriver(BaseDriver):
    """只记录调用顺序的假驱动。"""

    platform = "android"

    def __init__(self, cls: str = "EditText", node_id: str = "com.demo:id/et_search",
                 desc: str = ""):
        super().__init__("fake-input")
        self.node = UINode(cls=cls, id=node_id, desc=desc, text="旧关键词",
                           bounds=(40, 300, W - 40, 400), clickable=True)
        self.calls: list[str] = []
        self.typed: list[str] = []
        self.clear_error: Exception | None = None

    # ---------- 感知 ----------
    def connect(self) -> None:
        self._connected = True

    def screen_size(self) -> tuple[int, int]:
        return W, H

    def screenshot(self) -> bytes:
        return b""

    def dump_tree(self) -> UINode:
        return UINode(cls="Root", bounds=(0, 0, W, H), children=[self.node])

    # ---------- 输入 ----------
    def tap_xy(self, x: int, y: int) -> None:
        self.calls.append("tap")

    def input_text(self, text: str) -> None:
        self.calls.append("input")
        self.typed.append(text)

    def clear_text(self) -> None:
        self.calls.append("clear")
        if self.clear_error is not None:
            raise self.clear_error


class TestClearBeforeInput(unittest.TestCase):
    def test_edit_box_is_cleared_first(self):
        drv = InputDriver()
        res = drv.perform(Action.INPUT_TEXT, drv.node, {"text": "示例演员"})
        self.assertTrue(res.ok)
        self.assertEqual(drv.calls, ["tap", "clear", "input"])
        self.assertEqual(drv.typed, ["示例演员"])
        self.assertIn("已清空", res.detail)

    def test_clear_even_when_text_empty(self):
        # 只清空不输入：清空本身也是用户在用例里表达的语义
        drv = InputDriver()
        drv.perform(Action.INPUT_TEXT, drv.node, {"text": ""})
        self.assertEqual(drv.calls, ["tap", "clear"])

    def test_plain_text_node_is_not_cleared(self):
        # 正文/列表行上按 Ctrl+A + Delete 会删掉别的内容，宁可不查
        drv = InputDriver(cls="TextView", node_id="com.demo:id/tv_title")
        drv.perform(Action.INPUT_TEXT, drv.node, {"text": "abc"})
        self.assertEqual(drv.calls, ["tap", "input"])

    def test_editable_by_desc(self):
        # 驱动会把"可编辑输入区"写进 desc，cls 未必带 EditText
        drv = InputDriver(cls="Layout", node_id="", desc="可编辑输入区")
        drv.perform(Action.INPUT_TEXT, drv.node, {"text": "abc"})
        self.assertEqual(drv.calls, ["tap", "clear", "input"])

    def test_editable_by_id(self):
        drv = InputDriver(cls="Layout", node_id="com.demo:id/et_nickname")
        drv.perform(Action.INPUT_TEXT, drv.node, {"text": "abc"})
        self.assertEqual(drv.calls, ["tap", "clear", "input"])

    def test_clear_failure_does_not_block_input(self):
        # 清空失败（拿不到焦点、驱动没实现）不能把整步判死
        drv = InputDriver()
        drv.clear_error = NotSupported("该平台不支持 clear_text")
        res = drv.perform(Action.INPUT_TEXT, drv.node, {"text": "abc"})
        self.assertTrue(res.ok)
        self.assertEqual(drv.calls, ["tap", "clear", "input"])
        self.assertEqual(drv.typed, ["abc"])

    def test_switch_off(self):
        drv = InputDriver()
        drv.clear_before_input = False
        drv.perform(Action.INPUT_TEXT, drv.node, {"text": "abc"})
        self.assertEqual(drv.calls, ["tap", "input"])


if __name__ == "__main__":
    unittest.main()
