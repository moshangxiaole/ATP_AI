"""桌面驱动 launch_app 的回归测试。

对应的是 run-20261113-145734-f20f 事故：桌面端用例「打开【示例聊天应用-测试版】」首步
launch_app 拿空串去 os.startfile("") 必然失败，应用起不来、后续全崩。这里固定两条：
1. 空 app_id 必须抛清晰错误，而不是静默 os.startfile("")；
2. 台账里登记的「正斜杠路径」要归一化成 Windows 反斜杠再启动（mock 掉 os.startfile，
   绝不在测试机上真把应用拉起来）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import atp.agents.platforms.pc.desktop as desktop  # noqa: E402


class TestDesktopLaunch(unittest.TestCase):
    def test_empty_app_raises(self):
        drv = desktop.DesktopUiaDriver(window="__never_used__")
        with self.assertRaises(RuntimeError):
            drv.launch_app("")

    def test_forward_slash_normalized(self):
        drv = desktop.DesktopUiaDriver(window="__never_used__")
        with mock.patch.object(desktop.os, "startfile") as sf, \
                mock.patch.object(drv, "_pick_window", return_value=None):
            drv.launch_app("C:/Program Files/samplechat-pc/示例聊天应用.exe")
        sf.assert_called_once()
        called = sf.call_args[0][0]
        self.assertEqual(called, r"C:\Program Files\samplechat-pc\示例聊天应用.exe")
        self.assertNotIn("/", called,
                         "Windows 路径必须归一化成反斜杠")


class _FakeWindowPattern:
    def __init__(self, log):
        self.log = log

    def SetWindowVisualState(self, state):
        self.log.append(state)


class _FakeWin:
    def __init__(self, cls, log):
        self.ControlTypeName = cls
        self._log = log

    def GetWindowPattern(self):
        return _FakeWindowPattern(self._log)


class _FakeRoot:
    def __init__(self, children):
        self._children = children

    def GetChildren(self):
        return self._children


class _FakeAuto:
    def __init__(self, children):
        self._children = children

    def GetRootControl(self):
        return _FakeRoot(self._children)


class TestDesktopShowDesktop(unittest.TestCase):
    def test_minimize_all_uses_minimized_state(self):
        """UIA 的 WindowVisualState 是 Normal=0 / Maximized=1 / Minimized=2。
        早先错写成 0，等于把窗口恢复原状——"一键回桌面"从来没生效过
        （run-20261113-164346 截图里 VS Code / 控制台仍在屏上）。"""
        log = []
        drv = desktop.DesktopUiaDriver(window="__never_used__")
        drv._auto = _FakeAuto([
            _FakeWin("WindowControl", log),
            _FakeWin("PaneControl", log),
            _FakeWin("ToolBarControl", log),   # 不可最小化的类型应被跳过
        ])
        drv._minimize_all_windows()
        self.assertEqual(log, [2, 2], "只对窗口/Pane 调最小化，且状态必须是 2(Minimized)")


class _FakeCtrl:
    def __init__(self, cls):
        self.ControlTypeName = cls


class TestDesktopTreeFilter(unittest.TestCase):
    def test_tooltip_is_not_a_tree_node(self):
        """Chromium 的悬停 ToolTip 文案与真实条目一样，点它毫无反应；
        模型会反复选中它（run-20261113-164346 连点 6 次「联系人乙」无效）。
        这类纯展示层不允许进树。"""
        drv = desktop.DesktopUiaDriver(window="__never_used__")
        self.assertIsNone(drv._to_node(_FakeCtrl("ToolTipControl"), 0, [0]))


class _FakeActiveWin:
    ProcessId = 4242
    NativeWindowHandle = 0x1234

    def __init__(self, focused):
        self._focused = focused

    def SetActive(self):
        self._focused.append("SetActive")

    def SetFocus(self):
        self._focused.append("SetFocus")


class _FakeFg:
    def __init__(self, pid):
        self.ProcessId = pid


class _FakeFocusAuto:
    """前台窗口是别的进程（例如网盘浮层）时，必须把应用拉回来。"""

    def __init__(self, fg_pid):
        self._fg_pid = fg_pid

    def GetForegroundControl(self):
        return _FakeFg(self._fg_pid)

    def SetForegroundWindow(self, hwnd):
        raise RuntimeError("should not be needed")


class TestDesktopFocus(unittest.TestCase):
    def test_focus_app_pulls_forward_when_other_process_is_front(self):
        drv = desktop.DesktopUiaDriver(window="__never_used__")
        focused = []
        drv._root_ctrl = _FakeActiveWin(focused)
        drv._auto = _FakeFocusAuto(fg_pid=9999)      # 网盘浮层在前台
        with mock.patch.object(desktop.time, "sleep"):
            drv.focus_app()
        self.assertEqual(focused, ["SetActive"], "必须把被测窗口拉回前台")

    def test_focus_app_noop_when_app_already_front(self):
        drv = desktop.DesktopUiaDriver(window="__never_used__")
        focused = []
        drv._root_ctrl = _FakeActiveWin(focused)
        drv._auto = _FakeFocusAuto(fg_pid=4242)      # 前台就是应用自己
        drv.focus_app()
        self.assertEqual(focused, [], "应用已在前台时不该反复激活")


class _Rect:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class _NodeCtrl:
    """足够 _to_node 使用的假控件。"""

    def __init__(self, cls="PaneControl", name="", children=(), rid=(1, 1),
                 rect=(0, 0, 400, 300)):
        self.ControlTypeName = cls
        self.Name = name
        self.AutomationId = ""
        self.IsKeyboardFocusable = True
        self.IsEnabled = True
        self.HasKeyboardFocus = False
        self.ProcessId = 4242
        self.BoundingRectangle = _Rect(*rect)
        self._rid = rid
        self._children = list(children)

    def GetRuntimeId(self):
        return self._rid

    def GetChildren(self):
        return self._children

    def GetScrollPattern(self):
        return None


class TestDesktopStaleWindow(unittest.TestCase):
    def test_empty_tree_repicks_window(self):
        """窗口句柄失效时（应用切后台/被最小化/重启），整棵树只剩空壳，
        模型会以为"界面异常"然后回桌面重启（run-20261113-202528 的 s08）。
        必须自动重新认窗口再 dump 一次。"""
        drv = desktop.DesktopUiaDriver(window="__never_used__")
        stale = _NodeCtrl("PaneControl", "stale")
        fresh = _NodeCtrl("WindowControl", "示例聊天应用", children=[
            _NodeCtrl("ButtonControl", "搜索", rid=(2, 1), rect=(10, 10, 200, 40)),
        ])
        drv._root_ctrl = stale
        with mock.patch.object(drv, "_pick_window", return_value=fresh), \
                mock.patch.object(desktop.time, "sleep"):
            tree = drv.dump_tree()
        labels = [n.label for n in tree.walk()]
        self.assertIn("搜索", labels, f"重新认窗口后应能 dump 出真实控件，实际: {labels}")


class _FgSameProc:
    ProcessId = 4242


class _ClickAuto:
    """记录坐标点击，前台永远是应用自己（focus_app 直接返回）。"""

    def __init__(self):
        self.clicks = []
        self.control_clicks = 0

    def GetForegroundControl(self):
        return _FgSameProc()

    def Click(self, x, y, **kw):
        self.clicks.append((x, y))

    def DoubleClick(self, x, y, **kw):
        self.clicks.append((x, y))

    def RightClick(self, x, y, **kw):
        self.clicks.append((x, y))

    def DragDrop(self, *a, **kw):
        pass

    def SendKeys(self, *a, **kw):
        pass


class _TapCtrl:
    ControlTypeName = "TextControl"

    def __init__(self):
        self.clicks = 0

    def Click(self, simulateMove=False):
        self.clicks += 1


class TestDesktopTapCoordinate(unittest.TestCase):
    def test_tap_clicks_bounds_center_not_control(self):
        """Electron/Chromium 的 UIA 控件级 Click 经常不触发页面自己的点击处理器
        （run-20261113-205329 s04：点了搜索结果，下拉面板纹丝不动）。
        点击必须走 bounds 中心的屏幕坐标——人手就是这么点的。"""
        from atp.common.types import Action, UINode

        drv = desktop.DesktopUiaDriver(window="__never_used__")
        drv._auto = _ClickAuto()
        ctrl = _TapCtrl()
        node = UINode(cls="Text", id="uia://9", text="彭婉",
                      bounds=(100, 200, 300, 260))
        drv._controls["uia://9"] = ctrl
        drv._root_ctrl = mock.Mock(ProcessId=4242)
        with mock.patch.object(desktop.time, "sleep"):
            drv.perform(Action.TAP, node, {})
        self.assertEqual(drv._auto.clicks, [(200, 230)],
                         "必须点 bounds 中心 (200,230)")
        self.assertEqual(ctrl.clicks, 0, "不允许走控件级 Click")


class _HealCtrl:
    """带 texts 载荷的假顶层窗口，供空树自愈测试使用。"""

    ProcessId = 4242

    def __init__(self, texts, rid=(1,)):
        self.texts = texts
        self._rid = rid

    def GetRuntimeId(self):
        return self._rid


class _FakeHealDriver(desktop.DesktopUiaDriver):
    """_dump_once 按当前 _root_ctrl.texts 造树；grow_after 之前强制吐光杆树，
    模拟「翻页后 a11y 树延迟暴露」。"""

    def __init__(self):
        super().__init__(window="__never_used__")
        self.grow_after = 2
        self.dumps = 0
        self._first_root = None

    def _dump_once(self):
        from atp.common.types import UINode

        self.dumps += 1
        if self._first_root is None:
            self._first_root = self._root_ctrl
        texts = getattr(self._root_ctrl, "texts", [])
        if self._root_ctrl is self._first_root and self.dumps <= self.grow_after:
            texts = []      # 原窗口在 grow_after 拍内一直是光杆
        kids = [UINode(cls="Edit", id=f"uia://{i}", text=t,
                       bounds=(10 * i, 10, 80, 40))
                for i, t in enumerate(texts)]
        win = UINode(cls="Window", id="uia://w", text="示例聊天应用",
                     bounds=(0, 0, 1920, 1200), children=kids)
        return UINode(cls="Desktop", bounds=(0, 0, 1920, 1200), children=[win])


class TestDesktopHollowHeal(unittest.TestCase):
    def test_hollow_tree_waits_for_a11y(self):
        """进会话/翻页后第一次 dump 只有光杆窗口（新 renderer 的无障碍树
        还没暴露，run-20261113-205329：模型看到"节点列表为空"直接 done）。
        必须等待重试让它长出来，长出来了就不该重新认窗口。"""
        drv = _FakeHealDriver()
        drv.grow_after = 2
        drv._root_ctrl = _HealCtrl(["消息输入框", "发送"])
        with mock.patch.object(desktop.time, "sleep"), \
                mock.patch.object(drv, "_pick_window") as pw:
            got = drv.dump_tree()
        pw.assert_not_called()
        self.assertIn("发送", [n.label for n in got.walk()])

    def test_hollow_still_after_wait_repicks_window(self):
        """等到树还不出来（句柄真死了）就必须重新认窗口。"""
        drv = _FakeHealDriver()
        drv.grow_after = 10**9          # 永远不长出来
        drv._root_ctrl = _HealCtrl([])
        with mock.patch.object(desktop.time, "sleep"), \
                mock.patch.object(drv, "_pick_window",
                                  return_value=_HealCtrl(["消息输入框", "发送"],
                                                         rid=(2,))) as pw:
            got = drv.dump_tree()
        pw.assert_called_once()
        self.assertIn("发送", [n.label for n in got.walk()])


class _WheelAuto(_ClickAuto):
    def __init__(self):
        super().__init__()
        self.wheels = []
        self.moves = []

    def MoveTo(self, x, y, **kw):
        self.moves.append((x, y))

    def WheelDown(self, n=1, **kw):
        self.wheels.append(("down", n))

    def WheelUp(self, n=1, **kw):
        self.wheels.append(("up", n))


class TestDesktopSwipeWheel(unittest.TestCase):
    def test_swipe_up_uses_wheel_down_not_drag(self):
        """桌面 swipe = 鼠标滚轮滚动（UIA 对 Electron 容器经常不报
        ScrollPattern，run-20261113-211702 想滚会话列表被"不可滑动"闸拦死
        3 次直接熔断）。DragDrop 会选中文字/拖动文件，绝不能用。"""
        drv = desktop.DesktopUiaDriver(window="__never_used__")
        auto = _WheelAuto()
        drv._auto = auto
        drv._root_ctrl = mock.Mock(ProcessId=4242)
        with mock.patch.object(desktop.time, "sleep"):
            drv.swipe_xy(500, 800, 500, 300)      # 向上挥 500px
        self.assertEqual(auto.wheels, [("down", 5)],
                         "向上挥 = 内容上移 = 滚轮向下，500px ≈ 5 格")
        self.assertEqual(auto.moves, [(500, 300)], "滚轮要落在挥动终点上方")

    def test_swipe_down_uses_wheel_up(self):
        drv = desktop.DesktopUiaDriver(window="__never_used__")
        auto = _WheelAuto()
        drv._auto = auto
        drv._root_ctrl = mock.Mock(ProcessId=4242)
        with mock.patch.object(desktop.time, "sleep"):
            drv.swipe_xy(500, 300, 500, 900)      # 向下挥 600px
        self.assertEqual(auto.wheels, [("up", 6)])


class TestDesktopInputTextClipboard(unittest.TestCase):
    """run-20261116-101353：SendKeys 逐字符发中文被 Electron 自绘输入框静默丢弃
    （驱动报 ok、截图却逐字节相同）。改用剪贴板 SetClipboardText + Ctrl+V 送达；
    老版本 uiautomation 没有 SetClipboardText 时回退到 SendKeys。"""

    def test_input_text_pastes_via_clipboard(self):
        drv = desktop.DesktopUiaDriver(window="__never_used__")
        auto = mock.Mock()
        drv._auto = auto
        with mock.patch.object(drv, "focus_app"):
            drv.input_text("这是一条自动化测试消息")
        auto.SetClipboardText.assert_called_once_with("这是一条自动化测试消息")
        auto.SendKeys.assert_called_once_with("{Ctrl}v", waitTime=0.05)

    def test_input_text_falls_back_to_sendkeys(self):
        drv = desktop.DesktopUiaDriver(window="__never_used__")
        auto = mock.Mock()
        auto.SetClipboardText.side_effect = AttributeError("no clipboard in this build")
        drv._auto = auto
        with mock.patch.object(drv, "focus_app"):
            drv.input_text("hello")
        auto.SendKeys.assert_called_once_with("hello", waitTime=0.02)

    def test_input_text_empty_is_noop(self):
        drv = desktop.DesktopUiaDriver(window="__never_used__")
        auto = mock.Mock()
        drv._auto = auto
        with mock.patch.object(drv, "focus_app"):
            drv.input_text("")
        auto.SendKeys.assert_not_called()
        auto.SetClipboardText.assert_not_called()


if __name__ == "__main__":
    unittest.main()