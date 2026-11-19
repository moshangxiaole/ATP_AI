"""两条真机踩出来的硬要求。

1. **屏幕外的节点不算找到**。uiautomator 对滚出视口的节点不会省略，只是把 bounds 报成
   (0,0,0,0)；早先这些节点会被当成"已命中"，于是执行器对着 (0,0) 点了一下，
   界面毫无反应，还不触发滑动——线上表现就是"一直在打转"。
   现在的铁律：**当前页找不到，就一定滑动去找**。

2. **选择弹层要点确定**。点【请假类别】升起选项列表，点【年假】只是高亮，
   不点【确定】就不生效；而选中态在节点树里什么都不改，模型看不出"生效了"，
   于是重复点、被判定打转。执行器补一拍确认，并记成真实步骤（回放同样会点）。
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
from atp.agents.executor import Executor  # noqa: E402
from atp.agents.perception.judge import Judge  # noqa: E402
from atp.agents.perception.locator import LocatorStrategy, resolve  # noqa: E402
from atp.agents.perception.sheets import find_confirm_bar, is_option  # noqa: E402
from atp.common.stepir import CaseIR, NLCase, Step  # noqa: E402
from atp.common.types import Action, Observation, UINode  # noqa: E402
from atp.agents.platforms.base import BaseDriver  # noqa: E402

W, H = 1080, 2400


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


class OffscreenListDriver(BaseDriver):
    """像 uiautomator 那样：滚出视口的行仍然在树里，只是 bounds 全 0。"""

    platform = "android"

    def __init__(self, items: list[str], page: int = 4):
        super().__init__("fake-h5")
        self.items = list(items)
        self.page = page
        self.offset = 0
        self.swipes: list[tuple[int, int, int, int]] = []
        self.taps: list[tuple[int, int]] = []

    def connect(self) -> None:
        self._connected = True

    def screen_size(self) -> tuple[int, int]:
        return W, H

    def screenshot(self) -> bytes:
        return b""

    def dump_tree(self) -> UINode:
        step = (H - 400) // max(1, self.page)
        rows: list[UINode] = []
        for i, text in enumerate(self.items):
            if self.offset <= i < self.offset + self.page:
                y1 = 300 + (i - self.offset) * step
                rows.append(UINode(cls="TextView", text=text, bounds=(0, y1, W, y1 + step - 20),
                                   clickable=True))
            else:
                # 关键：不在视口里，但依然在树上
                rows.append(UINode(cls="TextView", text=text, bounds=(0, 0, 0, 0),
                                   clickable=True))
        container = UINode(cls="WebView", bounds=(0, 200, W, H - 100), scrollable=True,
                           children=rows)
        return UINode(cls="Root", bounds=(0, 0, W, H), children=[container])

    def swipe_xy(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.swipes.append((x1, y1, x2, y2))
        limit = max(0, len(self.items) - self.page)
        if y2 < y1:
            self.offset = min(limit, self.offset + 1)
        elif y2 > y1:
            self.offset = max(0, self.offset - 1)

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class OffscreenIsNotAHitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_offscreen_"))
        self.cfg = cfg_for(self.tmp)
        self.items = [f"字段{i}" for i in range(12)]
        self.items[9] = "事由"
        self.drv = OffscreenListDriver(self.items, page=4)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_zero_bounds_node_is_not_a_hit(self):
        obs = self.drv.observe()
        loc = resolve(obs.root, [LocatorStrategy.make("text", "事由")], (W, H))
        self.assertFalse(loc.ok, "屏幕外节点被当成了命中")
        self.assertEqual(loc.offscreen, "事由")
        self.assertIn("不在可视区域", loc.reason)

    def test_visible_node_still_hits(self):
        obs = self.drv.observe()
        loc = resolve(obs.root, [LocatorStrategy.make("text", "字段1")], (W, H))
        self.assertTrue(loc.ok)

    def test_executor_scrolls_to_find_it(self):
        """执行器必须真的滑，而不是对着 (0,0) 空点。"""
        case = CaseIR(case_id="c1", title="找事由", platform="android")
        case.steps = [Step(step_id="s01", intent="点击事由",
                           action=Action.TAP,
                           strategies=[LocatorStrategy.make("text", "事由")])]
        result = Executor(self.drv, self.cfg, Judge(), None).run(case, mode="replay")

        self.assertTrue(result.passed, [s.error for s in result.steps])
        self.assertGreater(len(self.drv.swipes), 0, "目标在屏幕外却没有滑动")
        self.assertTrue(self.drv.taps, "滑动后应该点到目标")
        self.assertNotIn((0, 0), self.drv.taps, "点了 (0,0)：坐标全 0 的节点被当成可点")

    def test_planner_never_sees_offscreen_nodes(self):
        """模型看不到屏幕外节点，就不会照着它去点；只能按提示词滑动。"""
        obs = self.drv.observe()
        nodes, listing = indexed_nodes(obs.root)
        self.assertNotIn("事由", listing)
        self.assertTrue(all(n.visible for n in nodes))


class FooterCoverDriver(BaseDriver):
    """表单底部有固定栏：滑到它下面的字段坐标是对的，点下去却落在固定栏上。

    真机实测的原型：点「请假类别」的「请选择」，直接跳进了「附件」页——
    因为那一段正好被底部的「编辑附言 / 附件 / 提交」盖住。
    """

    platform = "android"

    def __init__(self):
        super().__init__("fake-footer")
        self.offset = 0
        self.swipes: list[tuple[int, int, int, int]] = []
        self.taps: list[tuple[int, int]] = []

    def connect(self) -> None:
        self._connected = True

    def screen_size(self) -> tuple[int, int]:
        return 1260, 2800

    def screenshot(self) -> bytes:
        return b""

    def dump_tree(self) -> UINode:
        rows = []
        for i in range(20):
            y = 300 + i * 220 - self.offset
            inside = 100 <= y and y + 200 <= 2700
            rows.append(UINode(cls="TextView", text=f"字段{i}",
                               bounds=(60, y, 900, y + 200) if inside else (0, 0, 0, 0),
                               clickable=True))
        footer = UINode(cls="LinearLayout", bounds=(0, 2100, 1260, 2800), children=[
            UINode(cls="TextView", text="提交", bounds=(60, 2200, 400, 2300), clickable=True),
        ])
        return UINode(cls="Root", bounds=(0, 0, 1260, 2800), children=[
            UINode(cls="WebView", bounds=(0, 100, 1260, 2800), scrollable=True, children=rows),
            footer,
        ])

    def swipe_xy(self, x1, y1, x2, y2, ms=300) -> None:
        self.swipes.append((x1, y1, x2, y2))
        if y2 < y1:
            self.offset = min(20 * 220, self.offset + 220)
        else:
            self.offset = max(0, self.offset - 220)

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class OccludedTargetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_occl_"))
        self.cfg = cfg_for(self.tmp)
        self.drv = FooterCoverDriver()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_occlusion_detected(self):
        from atp.agents.perception.locator import describe, occluded_by

        obs = self.drv.observe()
        node = next(n for n in obs.root.find_by_text("字段8"))
        blocker = occluded_by(obs.root, node)
        self.assertIsNotNone(blocker, "被固定栏盖住却没识别出来")
        self.assertEqual(describe(blocker), "提交")

    def test_occluded_is_not_a_hit_when_avoiding(self):
        from atp.agents.perception.locator import resolve as _resolve

        obs = self.drv.observe()
        strategies = [LocatorStrategy.make("text", "字段8")]
        loose = _resolve(obs.root, strategies, (1260, 2800))
        self.assertTrue(loose.ok)
        strict = _resolve(obs.root, strategies, (1260, 2800), avoid_occluded=True)
        self.assertFalse(strict.ok, "被遮挡的目标不该算命中")

    def test_executor_scrolls_it_out_from_under_the_footer(self):
        case = CaseIR(case_id="c2", title="点字段8", platform="android")
        case.steps = [Step(step_id="s01", intent="点击字段8", action=Action.TAP,
                           strategies=[LocatorStrategy.make("text", "字段8")])]
        result = Executor(self.drv, self.cfg, Judge(), None).run(case, mode="replay")

        self.assertTrue(result.passed, [s.error for s in result.steps])
        self.assertGreater(len(self.drv.swipes), 0, "被遮挡却没滑动")
        # 点到的必须是"从固定栏下面露出来之后"的位置
        self.assertTrue(self.drv.taps)
        self.assertLess(self.drv.taps[0][1], 2100, f"点在固定栏上了: {self.drv.taps[0]}")


class SheetDriver(BaseDriver):
    """带选择弹层的假页面：点【请选择】升起选项，点【确定】才落值。"""

    platform = "android"

    def __init__(self):
        super().__init__("fake-sheet")
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

    def dump_tree(self) -> UINode:
        if self.state == "form":
            row = UINode(cls="TextView", text="请选择" if not self.selected else self.selected,
                         bounds=(60, 1200, 500, 1280), clickable=True)
            return UINode(cls="Root", bounds=(0, 0, W, H),
                          children=[UINode(cls="WebView", bounds=(0, 100, W, H),
                                           children=[row])])
        options = [UINode(cls="TextView", text=t, bounds=(300, 700 + i * 110, 780, 780 + i * 110),
                          clickable=True)
                   for i, t in enumerate(["事假", "病假", "年假", "产假"])]
        bar = [UINode(cls="Button", text="取消", bounds=(60, 1900, 520, 2020), clickable=True),
               UINode(cls="Button", text="确定", bounds=(560, 1900, 1020, 2020), clickable=True)]
        return UINode(cls="Root", bounds=(0, 0, W, H),
                      children=[UINode(cls="FrameLayout", bounds=(0, 600, W, 2100),
                                       children=options + bar)])

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))
        if self.state == "form" and 1100 <= y <= 1300:
            self.state = "sheet"
            return
        if self.state != "sheet":
            return
        if y >= 1900:
            if x >= 560:                     # 确定
                self.confirmed = True
                self.state = "form"
            else:                            # 取消
                self.selected = ""
                self.state = "form"
            return
        for i, t in enumerate(["事假", "病假", "年假", "产假"]):
            if 700 + i * 110 <= y <= 780 + i * 110:
                # 选中只高亮，界面结构不变（这正是 AI 看不出"生效"的原因）
                self.selected = t
                return

    def swipe_xy(self, x1, y1, x2, y2, ms=300):
        return


class ByTextPlanner:
    """按文案点下一步，用来模拟"模型看得到哪个节点就点哪个"。"""

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


class OffscreenPlanner:
    """故意让模型挑一个 bounds 全 0 的节点：验证首跑也会滑动去找。"""

    def __init__(self, label: str):
        self.label = label
        self.done = False

    def next(self, nl, obs, history):
        if self.done:
            return {"action": "done", "done_reason": "已点到目标"}
        nodes = [n for n in obs.root.walk() if (n.text or "").strip() == self.label]
        self.done = True
        return {"action": "tap", "node_index": 0, "intent": f"点击{self.label}",
                "_nodes": nodes}


class AiFirstRunMustScrollTest(unittest.TestCase):
    """回放会滑动，AI 首跑也必须会滑动——不能只报一句"不在可视区域"就收工。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_ai_scroll_"))
        self.cfg = cfg_for(self.tmp)
        items = [f"字段{i}" for i in range(20)]
        items[15] = "事由"
        self.drv = OffscreenListDriver(items, page=6)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_run_scrolls_to_offscreen_target(self):
        runner = AiRunner(self.drv, self.cfg, planner=OffscreenPlanner("事由"), client=None)
        nl = NLCase(case_id="ai-scroll", title="点事由", nl="在长列表里点事由", platform="android")
        case, result = runner.run(nl, out_dir=self.cfg.artifacts, run_id="t-ai-scroll")

        self.assertGreater(len(self.drv.swipes), 0, "首跑面对屏幕外目标却没有滑动")
        self.assertTrue(any("滑动" in (s.detail or "") for s in result.steps),
                        [s.detail for s in result.steps])
        self.assertTrue(self.drv.taps, "滑动找到了却没点")


class SheetAutoConfirmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_sheet_"))
        self.cfg = cfg_for(self.tmp)
        self.drv = SheetDriver()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_find_confirm_bar(self):
        self.drv.state = "sheet"
        bar = find_confirm_bar(self.drv.observe())
        self.assertIsNotNone(bar)
        self.assertEqual(bar.label, "确定")
        opt = next(n for n in self.drv.observe().root.find_by_text("年假"))
        self.assertTrue(is_option(opt, bar, self.drv.observe()))
        cancel = next(n for n in self.drv.observe().root.find_by_text("取消"))
        self.assertFalse(is_option(cancel, bar, self.drv.observe()))

    def test_ai_first_run_clicks_confirm(self):
        runner = AiRunner(self.drv, self.cfg, planner=ByTextPlanner(["请选择", "年假"]),
                          client=None)
        nl = NLCase(case_id="sheet-1", title="选年假",
                    nl="打开表单，请假类别选择年假", platform="android")
        case, result = runner.run(nl, out_dir=self.cfg.artifacts, run_id="t-sheet")

        self.assertEqual(self.drv.selected, "年假")
        self.assertTrue(self.drv.confirmed, "点完选项没有自动点【确定】")
        intents = [s.intent for s in result.steps]
        self.assertTrue(any("确认选择" in i for i in intents), intents)
        # 确认这一拍必须进 case.steps，否则首跑过了、回放却卡住
        self.assertTrue(any("确认选择" in s.intent for s in case.steps), [s.intent for s in case.steps])
        self.assertTrue(result.passed, [s.error for s in result.steps if s.error])


if __name__ == "__main__":
    unittest.main()
