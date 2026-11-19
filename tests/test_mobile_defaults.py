"""移动端两条默认行为的回归测试。

1. **冷启动**：`launch` 之前必须先结束进程——否则 App 还活在后台时只是把上次
   的页面拉回前台，用例起点不确定；
2. **滑动查找**：目标不在首屏时先上下滑动找，找不到才轮到 AI 修复定位器。

这两条都是"执行器保证"的行为，所以测试也打在执行器上，而不是某个脚本里。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from atp.config import Config  # noqa: E402
from atp.agents.executor import Executor  # noqa: E402
from atp.agents.perception.judge import Judge  # noqa: E402
from atp.agents.perception.locator import LocatorStrategy  # noqa: E402
from atp.agents.perception.scroller import find_by_scrolling, has_scrollable, trail_text  # noqa: E402
from atp.common.stepir import CaseIR, Step  # noqa: E402
from atp.common.types import Action, UINode  # noqa: E402
from atp.agents.platforms.base import BaseDriver  # noqa: E402

W, H = 1080, 1920


class ListDriver(BaseDriver):
    """一个会滚的假设备：首屏只显示 `page` 条，滑到底就滑不动（界面指纹不变）。

    用真设备验证之前，先把"滑动 → 重新抓树 → 再定位"这条链路固定下来：
    这一段最容易写成"滑了但没重新看"，结果永远定位不到。
    """

    platform = "android"

    def __init__(self, items: list[str], page: int = 4, scrollable: bool = True):
        super().__init__("fake-list")
        self.items = list(items)
        self.page = page
        self.scrollable = scrollable
        self.offset = 0
        self.swipes: list[tuple[int, int, int, int]] = []
        self.swipe_ms: list[int] = []
        self.taps: list[tuple[int, int]] = []
        self.calls: list[tuple[str, str]] = []

    # ---------- 感知 ----------
    def connect(self) -> None:
        self._connected = True

    def screen_size(self) -> tuple[int, int]:
        return W, H

    def screenshot(self) -> bytes:
        return b""

    def dump_tree(self) -> UINode:
        rows: list[UINode] = []
        top, step = 300, (H - 400) // max(1, self.page)
        for i, text in enumerate(self.items[self.offset:self.offset + self.page]):
            y1 = top + i * step
            rows.append(UINode(cls="TextView", text=text, bounds=(0, y1, W, y1 + step - 20)))
        container = UINode(cls="RecyclerView" if self.scrollable else "Column",
                           bounds=(0, 200, W, H - 100), scrollable=self.scrollable,
                           children=rows)
        return UINode(cls="Root", bounds=(0, 0, W, H), children=[container])

    # ---------- 输入 ----------
    def swipe_xy(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.swipes.append((x1, y1, x2, y2))
        self.swipe_ms.append(ms)
        limit = max(0, len(self.items) - self.page)
        if y2 < y1:            # 手指上滑 = 看更下面的内容
            self.offset = min(limit, self.offset + 1)
        elif y2 > y1:
            self.offset = max(0, self.offset - 1)

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def launch_app(self, app_id: str) -> None:
        self.calls.append(("launch", app_id))

    def terminate_app(self, app_id: str) -> None:
        self.calls.append(("terminate", app_id))


class DeskDriver(ListDriver):
    """假装是桌面端：用来验证"冷启动与滑动查找只对移动端生效"。"""

    platform = "desktop"


def cfg_for(tmp: Path) -> Config:
    cfg = Config(home=ROOT)
    cfg.artifacts_dir = str(tmp / "art")
    cfg.db_path = str(tmp / "t.db")
    # 应用台账也要隔离：跑单测不能往工程的 apps.json 里写条目
    cfg.apps_path = str(tmp / "apps.json")
    cfg.persist_runs = False
    cfg.screenshot_on_success = False
    cfg.scroll_find.settle_ms = 0
    cfg.cold_start.settle_ms = 0
    cfg.cold_start.launch_settle_ms = 0
    return cfg


class TestColdStart(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_cold_"))
        self.cfg = cfg_for(self.tmp)

    def _case(self, *steps: Step) -> CaseIR:
        case = CaseIR(case_id="cold", title="冷启动", platform="android",
                      app_id="com.demo.app")
        case.steps = list(steps)
        return case

    def test_launch_terminates_first(self):
        drv = ListDriver(["首页"])
        case = self._case(Step(step_id="s01", intent="打开应用", action=Action.LAUNCH_APP,
                               args={"app": "com.demo.app"}))
        r = Executor(drv, self.cfg, Judge(), None).run(case, mode="replay")

        self.assertTrue(r.passed, [s.error for s in r.steps if s.error])
        self.assertEqual(drv.calls, [("terminate", "com.demo.app"),
                                    ("launch", "com.demo.app")])
        self.assertIn("冷启动", r.steps[0].detail)

    def test_explicit_terminate_step_is_not_repeated(self):
        drv = ListDriver(["首页"])
        case = self._case(
            Step(step_id="s01", intent="结束进程", action=Action.TERMINATE_APP,
                 args={"app": "com.demo.app"}),
            Step(step_id="s02", intent="打开应用", action=Action.LAUNCH_APP,
                 args={"app": "com.demo.app"}),
        )
        r = Executor(drv, self.cfg, Judge(), None).run(case, mode="replay")

        self.assertTrue(r.passed, [s.error for s in r.steps if s.error])
        # 只杀一次：用例自己写了就不用执行器补刀
        self.assertEqual(drv.calls, [("terminate", "com.demo.app"),
                                    ("launch", "com.demo.app")])
        self.assertIn("上一步已结束进程", r.steps[1].detail)

    def test_non_mobile_platform_skips_terminate(self):
        drv = DeskDriver(["首页"])
        case = self._case(Step(step_id="s01", intent="打开应用", action=Action.LAUNCH_APP,
                               args={"app": "com.demo.app"}))
        Executor(drv, self.cfg, Judge(), None).run(case, mode="replay")
        self.assertEqual(drv.calls, [("launch", "com.demo.app")])

    def test_disabled_in_config(self):
        self.cfg.cold_start.enabled = False
        drv = ListDriver(["首页"])
        case = self._case(Step(step_id="s01", intent="打开应用", action=Action.LAUNCH_APP,
                               args={"app": "com.demo.app"}))
        Executor(drv, self.cfg, Judge(), None).run(case, mode="replay")
        self.assertEqual(drv.calls, [("launch", "com.demo.app")])


class TestSwipeStepSize(unittest.TestCase):
    """步幅要小于视口高度。

    真机反馈："滑得太快、区域太大，目标被滑过头了"——一次翻一屏多，目标行在两次
    抓树之间就被推出视野上方，于是越滑越找不到。现在的口径是：
    一步只翻容器可见高度的 35%，上一屏底部的内容仍留在视野里。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_step_"))
        self.cfg = cfg_for(self.tmp)

    def _run(self, ratio: float | None = None, ms: int | None = None):
        if ratio is not None:
            self.cfg.scroll_find.swipe_ratio = ratio
        if ms is not None:
            self.cfg.scroll_find.swipe_ms = ms
        items = [f"项{i}" for i in range(24)]
        items[7] = "阿妈说"
        drv = ListDriver(items, page=4)
        out = find_by_scrolling(
            drv, [LocatorStrategy.make("text", "阿妈说")], (W, H), 12, None,
            self.cfg.scroll_find,
        )
        self.assertTrue(out.ok, out.reason)
        return drv

    # ListDriver 的滚动容器是 (0,200,W,H-100)，可见高度 1620
    CONTAINER_H = H - 100 - 200

    def test_step_is_below_one_viewport(self):
        drv = self._run()
        self.assertGreater(len(drv.swipes), 0)
        for x1, y1, x2, y2 in drv.swipes:
            dist = abs(y2 - y1)
            # 远小于一屏：不会把上一屏整块推出去
            self.assertLess(dist, self.CONTAINER_H * 0.6)
            self.assertLessEqual(dist, self.CONTAINER_H * 0.36)   # 默认 35%
            self.assertLessEqual(dist, H * 0.45)

    def test_slow_drag_not_fling(self):
        drv = self._run()
        # 260ms 拖一屏会被系统当"甩"，一次跨两三屏
        self.assertTrue(all(ms >= 500 for ms in drv.swipe_ms), drv.swipe_ms)

    def test_gesture_inside_screen(self):
        drv = self._run()
        for x1, y1, x2, y2 in drv.swipes:
            for y in (y1, y2):
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(y, H)
            # 不从屏幕最上沿起手：那里下拉是通知栏
            self.assertGreater(min(y1, y2), H * 0.05)
            self.assertEqual(x1, x2)

    def test_ratio_is_configurable(self):
        small = self._run(ratio=0.2)
        big = self._run(ratio=0.5)
        d_small = max(abs(b - a) for _, a, _, b in small.swipes)
        d_big = max(abs(b - a) for _, a, _, b in big.swipes)
        self.assertLess(d_small, d_big)


class TestScrollFind(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_scroll_"))
        self.cfg = cfg_for(self.tmp)

    # ---------- 算法层 ----------
    def test_finds_item_below_the_fold(self):
        items = ["群聊A", "群聊B", "群聊C", "群聊D", "群聊E", "群聊F", "阿妈说", "群聊H"]
        drv = ListDriver(items, page=4)          # 首屏看不到「阿妈说」
        out = find_by_scrolling(
            drv, [LocatorStrategy.make("text", "阿妈说")], (W, H), 12, None,
            self.cfg.scroll_find,
        )
        self.assertTrue(out.ok, out.reason)
        self.assertEqual(out.loc.node.text, "阿妈说")
        self.assertGreater(out.swipes, 0)
        self.assertEqual(len(drv.swipes), out.swipes)
        self.assertEqual(out.trail_text, trail_text(out.trail))

    def test_static_page_is_never_swiped(self):
        drv = ListDriver(["群聊A"], page=1, scrollable=False)
        out = find_by_scrolling(
            drv, [LocatorStrategy.make("text", "不存在")], (W, H), 12, None,
            self.cfg.scroll_find,
        )
        self.assertFalse(out.ok)
        self.assertEqual(drv.swipes, [])          # 不可滚就不滑，避免误触
        self.assertIn("可滚动", out.reason)

    def test_bounded_when_target_absent(self):
        drv = ListDriver([f"项{i}" for i in range(20)], page=4)
        out = find_by_scrolling(
            drv, [LocatorStrategy.make("text", "永远没有")], (W, H), 12, None,
            self.cfg.scroll_find,
        )
        self.assertFalse(out.ok)
        # 有上限：单方向 max_swipes，回扫时补回已翻屏数，再多滑 max_swipes 就收手
        self.assertLessEqual(len(drv.swipes), 2 * self.cfg.scroll_find.max_swipes
                             + self.cfg.scroll_find.edge_stop)
        self.assertTrue(out.reason)

    def test_scrollable_detection(self):
        drv = ListDriver(["a"], page=1)
        self.assertTrue(has_scrollable(drv.dump_tree()))
        # 鸿蒙 dumpLayout 不一定带 scrollable，靠控件名兜底
        tree = UINode(cls="Root", children=[UINode(cls="Scroll", bounds=(0, 0, W, H))])
        self.assertTrue(has_scrollable(tree))
        self.assertFalse(has_scrollable(UINode(cls="Root", children=[
            UINode(cls="Text", text="静态页", bounds=(0, 0, W, H))])))

    # ---------- 执行器层 ----------
    def _case(self, target: str) -> CaseIR:
        case = CaseIR(case_id="scroll", title="滑动查找", platform="android",
                      app_id="com.demo.im")
        case.steps = [
            Step(step_id="s01", intent="打开应用", action=Action.LAUNCH_APP,
                 args={"app": "com.demo.im"}),
            Step(step_id="s02", intent=f"打开会话 {target}", action=Action.TAP,
                 strategies=[LocatorStrategy.make("text", target)]),
        ]
        return case

    def test_executor_opens_chat_after_scrolling(self):
        items = ["同事群", "家人群", "同学群", "外卖群", "快递群", "项目群", "阿妈说"]
        drv = ListDriver(items, page=4)
        r = Executor(drv, self.cfg, Judge(), None).run(self._case("阿妈说"), mode="replay")

        self.assertTrue(r.passed, [s.error for s in r.steps if s.error])
        self.assertIn("滑动", r.steps[1].detail)
        self.assertEqual(len(drv.taps), 1)
        self.assertEqual(r.ai_steps, 0)          # 滑动是零 AI 的

    def test_executor_reports_miss_without_lying(self):
        drv = ListDriver(["同事群", "家人群"], page=4)
        r = Executor(drv, self.cfg, Judge(), None).run(self._case("阿妈说"), mode="replay")

        self.assertFalse(r.passed)
        self.assertTrue(r.steps[1].error)
        self.assertIn("已滑动", r.steps[1].detail)   # 搜过了，不是没搜
        self.assertGreater(len(drv.swipes), 0)

    def test_desktop_never_scrolls(self):
        drv = DeskDriver(["同事群", "家人群"], page=4)
        r = Executor(drv, self.cfg, Judge(), None).run(self._case("阿妈说"), mode="replay")
        self.assertFalse(r.passed)
        self.assertEqual(drv.swipes, [])


class TestAiFirstRun(unittest.TestCase):
    """首跑（AI 接管）也得先杀进程再启动。

    否则录下来的第一步就落在 App 上次停留的页面上，沉淀进用例库的定位器全是
    "碰巧对上"的坐标——回放时换台机器就挂，而且再也修不好。
    """

    def test_ai_run_terminates_before_launch(self):
        from atp.agents.runner import AiRunner
        from console_fakes import ScriptedPlanner
        from atp.common.stepir import NLCase

        tmp = Path(tempfile.mkdtemp(prefix="atp_ai_cold_"))
        cfg = cfg_for(tmp)
        drv = ListDriver(["阿妈说", "同事群"], page=4)
        nl = NLCase(case_id="ai_cold", title="打开会话", platform="android",
                    app_id="com.demo.im")
        plan = [{"action": "tap", "node_index": 0, "intent": "打开阿妈说"},
                {"action": "done"}]
        case, _ = AiRunner(drv, cfg, planner=ScriptedPlanner(plan)).run(nl, out_dir=tmp)

        self.assertEqual(drv.calls[:2], [("terminate", "com.demo.im"),
                                        ("launch", "com.demo.im")])
        self.assertEqual(case.steps[0].action, Action.TERMINATE_APP)


class TestConfigBlocks(unittest.TestCase):
    def test_both_blocks_load_from_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "atp.config.json"
            p.write_text(json.dumps({
                "cold_start": {"enabled": False, "settle_ms": 1200},
                "scroll_find": {"max_swipes": 3, "directions": ["down"],
                                "horizontal": True},
            }), encoding="utf-8")
            cfg = Config.load(home=d)
            self.assertFalse(cfg.cold_start.enabled)
            self.assertEqual(cfg.cold_start.settle_ms, 1200)
            self.assertEqual(cfg.scroll_find.max_swipes, 3)
            self.assertEqual(cfg.scroll_find.directions, ["down"])
            self.assertTrue(cfg.scroll_find.horizontal)
            # 没写在文件里的字段保留默认值
            self.assertEqual(cfg.scroll_find.edge_stop, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
