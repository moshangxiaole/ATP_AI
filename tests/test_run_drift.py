"""run-20261117-110950 跑偏回归：key 回车、UIA id 唯一、发送后不重输。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from atp.agents.runners.steps import _normalize_key  # noqa: E402
from atp.agents.platforms.pc.desktop import DesktopUiaDriver  # noqa: E402
from atp.config import Config  # noqa: E402
from atp.agents.runner import AiRunner  # noqa: E402
from console_fakes import FakeOaDriver, ScriptedPlanner  # noqa: E402


class KeyNormalizeTest(unittest.TestCase):
    def test_enter_variants(self):
        for raw in ("Enter", "enter", "回车", "按回车键", "确认", "发送", "搜索", "{Enter}", ""):
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_key(raw), "enter")

    def test_back_variants(self):
        for raw in ("back", "Back", "返回", "后退"):
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_key(raw), "back")

    def test_other_keys(self):
        self.assertEqual(_normalize_key("Tab"), "tab")
        self.assertEqual(_normalize_key("esc"), "esc")
        self.assertEqual(_normalize_key("删除"), "delete")

    def test_to_step_reads_text_as_key(self):
        """模型把 Enter 填在 text 里（schema 没有 key 字段），必须识别成 enter。"""
        runner = AiRunner(FakeOaDriver(), Config(), client=None)
        obs = FakeOaDriver().observe()
        step = runner._to_step(1, "key", None,
                               {"action": "key", "text": "Enter", "intent": "按回车触发搜索"}, obs)
        self.assertIsNotNone(step)
        self.assertEqual(step.args.get("key"), "enter")


class UiaNodeIdTest(unittest.TestCase):
    def _ctrl(self, rid, name=""):
        c = mock.Mock()
        c.ControlTypeName = "ButtonControl"
        c.Name = name
        c.AutomationId = "btn-x"
        c.HasKeyboardFocus = False
        c.IsEnabled = True
        c.IsKeyboardFocusable = True
        c.BoundingRectangle = mock.Mock(left=10, top=20, right=60, bottom=40)
        c.GetRuntimeId.return_value = rid
        c.GetScrollPattern.side_effect = Exception("no scroll")
        c.GetChildren.return_value = []
        c.GetValuePattern.side_effect = Exception("no value")
        return c

    def test_full_runtime_id_is_unique(self):
        """RuntimeId 是 [进程号, 控件号]，只取 [0] 会让所有控件共用 uia://42。"""
        drv = DesktopUiaDriver()
        a = drv._to_node(self._ctrl([42, 1001], "发送"), 0, [0])
        b = drv._to_node(self._ctrl([42, 1002], "附件"), 0, [1])
        self.assertEqual(a.id, "uia://42-1001")
        self.assertEqual(b.id, "uia://42-1002")
        self.assertNotEqual(a.id, b.id)

    def test_single_element_runtime_id_kept(self):
        """单元素 id（老 uiautomation / 测试 mock）保持 uia://9，兼容现有点击路径。"""
        drv = DesktopUiaDriver()
        n = drv._to_node(self._ctrl([9], "按钮"), 0, [0])
        self.assertEqual(n.id, "uia://9")

    def test_empty_name_falls_back_to_legacy(self):
        drv = DesktopUiaDriver()
        c = self._ctrl([42, 1003], "")
        c.GetLegacyIAccessiblePattern.return_value = mock.Mock(
            CurrentName="发送", CurrentDescription="")
        n = drv._to_node(c, 0, [0])
        self.assertEqual(n.text, "发送")


if __name__ == "__main__":
    unittest.main()
