"""选择弹层自动确认的回归：动画期间也必须补拍【确定】，但不能误点表单底部按钮。

真机事故（run-20261110-183915-0aa3）：点开【请假类别】后弹层还在展开动画里，
第 8 步点【年假】时弹层刚好落位——于是"点前这一屏"和"点后这一屏"的确认栏
坐标差了 150 多像素。当时自动确认的闸是 `pre_bounds == post_bounds` 加
`指纹完全相同`，两条同时不成立，整段补确认被跳过，模型只能反复点【年假】，
最后被判原地打转、整条用例失败（截图里【年假】其实早就变蓝了）。

这里钉三件事：
1. 弹层展开动画导致的位移，不能挡住自动确认；
2. 表单页底部的「取消 | 确定」在同一个滚动容器里，绝不能被当成弹层补点；
3. 弹层选完就自动关闭的（点选项即生效）不需要补确认。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.agents.runner import AiRunner, indexed_nodes  # noqa: E402
from atp.config import Config  # noqa: E402
from atp.agents.perception.sheets import find_confirm_bar, same_bar, shares_scroll_with  # noqa: E402
from atp.common.stepir import NLCase  # noqa: E402
from atp.common.types import UINode  # noqa: E402
from atp.agents.platforms.base import BaseDriver  # noqa: E402

W, H = 1260, 2800
OPTIONS = ["事假", "病假", "年假", "产假"]


def cfg_for(tmp: Path) -> Config:
    cfg = Config(home=tmp)
    cfg.artifacts_dir = str(tmp / "art")
    cfg.cases_dir = str(tmp / "cases")
    cfg.db_path = str(tmp / "t.db")
    cfg.apps_path = str(tmp / "apps.json")
    cfg.persist_runs = False
    cfg.screenshot_on_success = False
    cfg.scroll_find.settle_ms = 0
    cfg.cold_start.enabled = False
    cfg.apps.enabled = False
    return cfg


class ByTextPlanner:
    """剧本式规划器：看到哪个节点就点哪个，用来复现"点完选项又点一次"。"""

    def __init__(self, targets: list[str]):
        self.targets = list(targets)
        self.i = 0

    def next(self, nl, obs, history):
        if self.i >= len(self.targets):
            return {"action": "done", "done_reason": "剧本结束"}
        want = self.targets[self.i]
        nodes, _ = indexed_nodes(obs.root)
        for idx, n in enumerate(nodes):
            if (n.text or "").strip() == want:
                self.i += 1
                return {"action": "tap", "node_index": idx, "intent": f"点击{want}",
                        "_nodes": nodes}
        return {"action": "done", "done_reason": f"界面上没有{want}"}


class AnimatingSheetDriver(BaseDriver):
    """弹层带展开动画：升起的那一屏还在半路，点完选项后才落位。"""

    platform = "android"

    def __init__(self):
        super().__init__("fake-anim")
        self.state = "form"
        self.selected = ""
        self.confirmed = False
        self.taps: list[tuple[int, int]] = []

    def connect(self) -> None:
        self._connected = True

    def screen_size(self) -> tuple[int, int]:
        return W, H

    def screenshot(self) -> bytes:
        return b""

    def _bar_y(self) -> int:
        # 动画中比最终位置低 150px：正是真机上出现的那种位移
        return 2350 if self.state == "opening" else 2500

    def _opt_y(self, i: int) -> int:
        base = 1500 if self.state == "opening" else 1650
        return base + i * 110

    def dump_tree(self) -> UINode:
        if self.state == "form":
            row = UINode(cls="TextView", text=self.selected or "请选择",
                         bounds=(60, 1200, 400, 1270), clickable=True)
            return UINode(cls="Root", bounds=(0, 0, W, H),
                          children=[UINode(cls="WebView", bounds=(0, 100, W, 2650),
                                           scrollable=True, children=[row])])

        opts = [UINode(cls="TextView", text=t,
                       bounds=(0, self._opt_y(i), W, self._opt_y(i) + 120),
                       clickable=True)
                for i, t in enumerate(OPTIONS)]
        y = self._bar_y()
        bar = [UINode(cls="TextView", text="取消", bounds=(0, y, 630, y + 67), clickable=True),
               UINode(cls="TextView", text="确定", bounds=(630, y, 1260, y + 67), clickable=True)]
        # 选项在自己的可滚列表里，确认栏在列表外面 —— 真机上就是这个结构
        listing = UINode(cls="View", bounds=(0, 1100, W, 2450), scrollable=True, children=opts)
        panel = UINode(cls="FrameLayout", bounds=(0, 900, W, 2650),
                       children=[listing] + bar)
        return UINode(cls="Root", bounds=(0, 0, W, H),
                      children=[UINode(cls="WebView", bounds=(0, 100, W, 2650),
                                       scrollable=True,
                                       children=[UINode(cls="TextView", text="请假类别",
                                                        bounds=(66, 900, 283, 960))]),
                                panel])

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))
        if self.state == "form":
            if 1150 <= y <= 1300:
                self.state = "opening"
            return
        if y >= self._bar_y() - 40:
            if x >= 630:
                self.confirmed = True
                self.state = "form"
            else:
                self.selected = ""
                self.state = "form"
            return
        for i, t in enumerate(OPTIONS):
            if self._opt_y(i) - 20 <= y <= self._opt_y(i) + 140:
                self.selected = t          # 只高亮，界面结构不变
                self.state = "open"        # 动画结束，弹层落位
                return

    def swipe_xy(self, x1, y1, x2, y2, ms=300):
        return


class FormFooterDriver(BaseDriver):
    """表单页：字段和底部「取消 | 确定」在**同一个**可滚 WebView 里。

    这种结构绝不能当成选择弹层——补一拍「确定」等于替用户提交表单。
    """

    platform = "android"

    def __init__(self):
        super().__init__("fake-form-footer")
        self.taps: list[tuple[int, int]] = []
        self.confirmed = False

    def connect(self) -> None:
        self._connected = True

    def screen_size(self) -> tuple[int, int]:
        return W, H

    def screenshot(self) -> bytes:
        return b""

    def dump_tree(self) -> UINode:
        fields = [UINode(cls="TextView", text=t, bounds=(60, 300 + i * 260, 1180, 300 + i * 260 + 120),
                         clickable=True)
                  for i, t in enumerate(["请假类别", "时间范围", "共计天数", "事由"])]
        footer = [UINode(cls="TextView", text="取消", bounds=(0, 2500, 630, 2567), clickable=True),
                  UINode(cls="TextView", text="确定", bounds=(630, 2500, 1260, 2567), clickable=True)]
        return UINode(cls="Root", bounds=(0, 0, W, H),
                      children=[UINode(cls="WebView", bounds=(0, 100, W, 2650),
                                       scrollable=True, children=fields + footer)])

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))
        if y >= 2500 and x >= 630:
            self.confirmed = True

    def swipe_xy(self, x1, y1, x2, y2, ms=300):
        return


class AutoClosePickDriver(BaseDriver):
    """点选项即生效、弹层自动关闭的选择器（不需要补确认）。"""

    platform = "android"

    def __init__(self):
        super().__init__("fake-autoclose")
        self.state = "form"
        self.selected = ""
        self.taps: list[tuple[int, int]] = []

    def connect(self) -> None:
        self._connected = True

    def screen_size(self) -> tuple[int, int]:
        return W, H

    def screenshot(self) -> bytes:
        return b""

    def dump_tree(self) -> UINode:
        if self.state == "form":
            row = UINode(cls="TextView", text=self.selected or "请选择",
                         bounds=(60, 1200, 400, 1270), clickable=True)
            return UINode(cls="Root", bounds=(0, 0, W, H),
                          children=[UINode(cls="WebView", bounds=(0, 100, W, 2650),
                                           scrollable=True, children=[row])])
        opts = [UINode(cls="TextView", text=t, bounds=(0, 1650 + i * 110, W, 1770 + i * 110),
                       clickable=True)
                for i, t in enumerate(OPTIONS)]
        y = 2500
        bar = [UINode(cls="TextView", text="取消", bounds=(0, y, 630, y + 67), clickable=True),
               UINode(cls="TextView", text="确定", bounds=(630, y, 1260, y + 67), clickable=True)]
        listing = UINode(cls="View", bounds=(0, 1100, W, 2450), scrollable=True, children=opts)
        return UINode(cls="Root", bounds=(0, 0, W, H),
                      children=[UINode(cls="FrameLayout", bounds=(0, 900, W, 2650),
                                       children=[listing] + bar)])

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))
        if self.state == "form":
            if 1150 <= y <= 1300:
                self.state = "sheet"
            return
        for i, t in enumerate(OPTIONS):
            if 1630 + i * 110 <= y <= 1790 + i * 110:
                self.selected = t
                self.state = "form"        # 点完直接关，没有确认环节
                return

    def swipe_xy(self, x1, y1, x2, y2, ms=300):
        return


class AnimatingSheetConfirmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_sheet_anim_"))
        self.cfg = cfg_for(self.tmp)
        self.drv = AnimatingSheetDriver()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bar_shift_is_still_the_same_bar(self):
        """动画位移 150px（5% 屏高）必须仍判为同一条确认栏。"""
        self.drv.state = "opening"
        a = find_confirm_bar(self.drv.observe())
        self.drv.state = "open"
        b = find_confirm_bar(self.drv.observe())
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertTrue(same_bar(a, b, self.drv.observe()))

    def test_fingerprint_differs(self):
        """这条测试钉住"指纹闸"不能再用：两屏本来就不同。"""
        self.drv.state = "opening"
        a = self.drv.observe()
        self.drv.state = "open"
        b = self.drv.observe()
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_ai_confirms_even_when_sheet_moves(self):
        runner = AiRunner(self.drv, self.cfg, planner=ByTextPlanner(["请选择", "年假"]),
                          client=None)
        nl = NLCase(case_id="sheet-anim", title="选年假",
                    nl="在请假表单里把请假类别选成年假", platform="android")
        case, result = runner.run(nl, out_dir=self.cfg.artifacts, run_id="t-anim")

        self.assertEqual(self.drv.selected, "年假")
        self.assertTrue(self.drv.confirmed, "弹层动画导致位移时，自动确认被跳过了")
        intents = [s.intent for s in case.steps]
        self.assertTrue(any("确认选择" in i for i in intents), intents)
        self.assertTrue(result.passed, [s.error for s in result.steps if s.error])

    def test_sheet_closed_after_confirm(self):
        """补完确认后弹层必须真的关掉——这才是"不再原地踏步"的依据。"""
        runner = AiRunner(self.drv, self.cfg, planner=ByTextPlanner(["请选择", "年假"]),
                          client=None)
        nl = NLCase(case_id="sheet-anim2", title="选年假",
                    nl="在请假表单里把请假类别选成年假", platform="android")
        case, result = runner.run(nl, out_dir=self.cfg.artifacts, run_id="t-anim2")

        self.assertEqual(self.drv.state, "form", "补了确认，弹层却没关")
        self.assertEqual(self.drv.selected, "年假")
        confirms = [s for s in case.steps if "确认选择" in s.intent]
        self.assertEqual(len(confirms), 1, [s.intent for s in case.steps])


class FormFooterSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_sheet_safe_"))
        self.cfg = cfg_for(self.tmp)
        self.drv = FormFooterDriver()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_option_shares_scroll_with_bar(self):
        obs = self.drv.observe()
        bar = find_confirm_bar(obs)
        self.assertIsNotNone(bar)
        field = next(n for n in obs.root.find_by_text("请假类别") if n.visible)
        self.assertTrue(shares_scroll_with(obs.root, field, bar),
                        "表单字段与底部按钮在同一个滚动容器里，必须识别出来")

    def test_never_auto_confirms_form_footer(self):
        runner = AiRunner(self.drv, self.cfg, planner=ByTextPlanner(["请假类别"]), client=None)
        nl = NLCase(case_id="form-footer", title="点表单字段",
                    nl="在请假表单里点一下请假类别", platform="android")
        runner.run(nl, out_dir=self.cfg.artifacts, run_id="t-form")
        self.assertFalse(self.drv.confirmed, "把表单底部的【确定】当成弹层确认点了，等于替用户提交")


class AutoClosePickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_sheet_auto_"))
        self.cfg = cfg_for(self.tmp)
        self.drv = AutoClosePickDriver()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_confirm_when_sheet_closes_itself(self):
        runner = AiRunner(self.drv, self.cfg, planner=ByTextPlanner(["请选择", "年假"]), client=None)
        nl = NLCase(case_id="auto-close", title="选年假",
                    nl="在请假表单里把请假类别选成年假", platform="android")
        case, result = runner.run(nl, out_dir=self.cfg.artifacts, run_id="t-auto")
        self.assertEqual(self.drv.selected, "年假")
        self.assertFalse(any("确认选择" in s.intent for s in case.steps),
                         "弹层自己关了，不该再补确认")


if __name__ == "__main__":
    unittest.main()
