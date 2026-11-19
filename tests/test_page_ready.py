"""规划前必须等这一屏画完：白屏/半渲染的树不能交给模型。

真机事故：点【请假申请单】后 H5 表单还在白屏，树里只有壳子加一个「附件」，
模型只能挑到「附件」点下去，整条用例跑到上传附件页，之后滑动也回不来了。
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
from atp.common.stepir import NLCase  # noqa: E402
from atp.common.types import UINode  # noqa: E402
from atp.agents.platforms.base import BaseDriver  # noqa: E402

W, H = 1260, 2800


def cfg_for(tmp: Path) -> Config:
    cfg = Config(home=tmp)
    cfg.artifacts_dir = str(tmp / "art")
    cfg.cases_dir = str(tmp / "cases")
    cfg.db_path = str(tmp / "t.db")
    cfg.apps_path = str(tmp / "apps.json")
    cfg.persist_runs = False
    cfg.screenshot_on_success = False
    cfg.cold_start.enabled = False
    cfg.apps.enabled = False
    return cfg


class RenderingDriver(BaseDriver):
    """前几次 dump 只有白屏壳子，之后才画完。"""

    platform = "android"

    def __init__(self, blank_dumps: int = 2, form_nodes: int = 60):
        super().__init__("fake-render")
        self.blank_dumps = blank_dumps
        self.form_nodes = form_nodes
        self.dumps = 0
        self.planned_sizes: list[int] = []
        self.taps: list[tuple[int, int]] = []

    def connect(self) -> None:
        self._connected = True

    def screen_size(self) -> tuple[int, int]:
        return W, H

    def screenshot(self) -> bytes:
        return b""

    def dump_tree(self) -> UINode:
        self.dumps += 1
        if self.dumps <= self.blank_dumps:
            # 白屏：只有 WebView 壳 + 底部一个「附件」
            return UINode(cls="Root", bounds=(0, 0, W, H), children=[
                UINode(cls="WebView", bounds=(0, 100, W, 2650), scrollable=True, children=[
                    UINode(cls="TextView", text="附件", bounds=(0, 2400, W, 2500), clickable=True),
                ]),
            ])
        rows = [UINode(cls="TextView", text=f"字段{i}", bounds=(60, 200 + i * 40, 1180, 240 + i * 40),
                       clickable=True) for i in range(self.form_nodes)]
        rows.append(UINode(cls="TextView", text="请选择", bounds=(66, 1333, 213, 1389),
                           clickable=True))
        return UINode(cls="Root", bounds=(0, 0, W, H), children=[
            UINode(cls="WebView", bounds=(0, 100, W, 2650), scrollable=True, children=rows),
        ])

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def swipe_xy(self, x1, y1, x2, y2, ms=300):
        return


class RecordingPlanner:
    def __init__(self, want: str = "请选择"):
        self.want = want
        self.seen: list[int] = []
        self.done = False

    def next(self, nl, obs, history):
        nodes, _ = indexed_nodes(obs.root)
        self.seen.append(len(nodes))
        if self.done:
            return {"action": "done", "done_reason": "已点到目标"}
        for i, n in enumerate(nodes):
            if (n.text or "").strip() == self.want:
                self.done = True
                return {"action": "tap", "node_index": i, "intent": f"点击{self.want}",
                        "_nodes": nodes}
        return {"action": "done", "done_reason": "界面上没有目标"}


class DuplicateLabelContextTest(unittest.TestCase):
    """三个同文案「请选择」必须在提示词里带上各自所属字段名。"""

    def test_ctx_distinguishes_rows(self):
        rows = []
        for field, y in (("请假类别", 1218), ("假期子类别", 1456), ("时间范围", 1690)):
            rows.append(UINode(cls="TextView", text=field, bounds=(66, y, 283, y + 66)))
            rows.append(UINode(cls="TextView", text="请选择",
                               bounds=(66, y + 115, 213, y + 171), clickable=True))
        root = UINode(cls="Root", bounds=(0, 0, W, H), children=rows)
        _, listing = indexed_nodes(root)
        for field in ("请假类别", "假期子类别", "时间范围"):
            self.assertIn(f"ctx={field!r}", listing, f"提示词里没有 {field} 的行上下文")
        self.assertEqual(listing.count("text='请选择'"), 3)


class PageReadyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_ready_"))
        self.cfg = cfg_for(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_planner_never_sees_white_screen(self):
        drv = RenderingDriver(blank_dumps=2)
        planner = RecordingPlanner("请选择")
        runner = AiRunner(drv, self.cfg, planner=planner, client=None)
        nl = NLCase(case_id="ready-1", title="进表单", nl="打开表单点击请选择", platform="android")
        runner.run(nl, out_dir=self.cfg.artifacts, run_id="t-ready")

        self.assertTrue(planner.seen, "模型一次都没被调用")
        self.assertGreaterEqual(planner.seen[0], 20,
                                f"模型拿到了白屏（只有 {planner.seen[0]} 个节点）")

    def test_stable_thin_page_does_not_hang(self):
        """本来就是空列表页：多等两拍就必须放行，不能一直等。"""
        drv = RenderingDriver(blank_dumps=99)
        planner = RecordingPlanner("请选择")
        runner = AiRunner(drv, self.cfg, planner=planner, client=None)
        nl = NLCase(case_id="ready-2", title="空页面", nl="随便看看", platform="android")
        runner.run(nl, out_dir=self.cfg.artifacts, run_id="t-ready2")
        self.assertTrue(planner.seen, "稳不下来的空页面被一直等着，规划器压根没跑")
        self.assertLessEqual(drv.dumps, 8, f"空等太久，dump 了 {drv.dumps} 次")


if __name__ == "__main__":
    unittest.main()
