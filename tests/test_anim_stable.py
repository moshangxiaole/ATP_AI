"""常驻动画页降级 + 「进入播放页」完成闸。

事故 run-20261117-184735-5c38：示例视频应用搜索页有轮播，细指纹永不稳定，
回放 s04 门禁超时 fail-closed（AI 首跑同页面容忍继续），且失败发生在
证据截图之前——报告引用空文件，控制台缩略图显示破损图。
事故 run-20261117-184531-d291：场景要求「进入播放页面」，AI 停在只有
【播放】按钮的详情页就被判成功——验收器把详情页合理化成了播放页。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mobile_qa_helpers import (  # noqa: E402
    TreeDriver, make_case, make_config, make_executor, page, shifted,
)
from atp.agents.runners.accept import AcceptMixin  # noqa: E402
from atp.common.stepir import NLCase  # noqa: E402
from atp.common.types import Observation, UINode  # noqa: E402


class AnimDriver(TreeDriver):
    """细指纹永不稳定（每次整体位移），但文本集合恒定 = 常驻动画页。"""

    def __init__(self):
        super().__init__(page(count=30))
        self._n = 0

    def dump_tree(self):
        self._n += 1
        self.dumps += 1
        self.log.append(("dump",))
        return shifted(page(count=30), dy=self._n)


class AnimStableTest(unittest.TestCase):
    def test_anim_page_degrades_and_executes(self):
        drv = AnimDriver()
        exe = make_executor(drv, make_config())
        case = make_case(1)
        result = exe.run(case, mode="replay", run_id="anim-1")
        sr = result.steps[0]
        self.assertEqual(sr.status, "passed",
                         "目标在两拍树里位置一致的动画页必须降级执行，不能 fail-closed")
        self.assertEqual(len(drv.taps), 1, "降级后目标要真的点到")
        self.assertIn("降级", sr.detail)
        # 证据必须落盘：门禁失败路径也要有 before 截图
        self.assertTrue(case.steps[0].evidence.before,
                        "门禁失败路径必须留证据截图")

    def test_churning_content_still_fails_closed(self):
        # 文本每拍都变（目标按钮文字在换）：不算动画页，维持 AC-1.4 fail-closed
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_adaptive_wait import NeverStableDriver
        drv = NeverStableDriver()
        exe = make_executor(drv, make_config())
        result = exe.run(make_case(1), mode="replay", run_id="anim-2")
        sr = result.steps[0]
        self.assertEqual(sr.status, "failed")
        self.assertNotIn("anim_stable", sr.detail)
        self.assertEqual(drv.taps, [])


def _nl(text: str) -> NLCase:
    return NLCase(title=text, nl=text)


def _obs(nodes: list[UINode]) -> Observation:
    return Observation(platform="harmony", device_id="hm",
                       width=1256, height=2760,
                       root=UINode(cls="Root", bounds=(0, 0, 1256, 2760),
                                   children=nodes))


class PlayGateTest(unittest.TestCase):
    def setUp(self):
        self.m = AcceptMixin()

    def test_scene_expects_play(self):
        self.assertTrue(self.m._scene_expects_play(
            _nl("打开示例视频应用，搜索乘风破浪的姐姐，进入播放页面")))
        self.assertTrue(self.m._scene_expects_play(_nl("播放页面表示成功")))
        self.assertFalse(self.m._scene_expects_play(_nl("打开设置，进入关于手机")))

    def test_play_button_on_detail_page_detected(self):
        obs = _obs([
            UINode(cls="Text", id="t", text="示例选秀节目", bounds=(0, 0, 400, 80)),
            UINode(cls="Button", id="p", text="播放",
                   bounds=(300, 1350, 428, 1420), clickable=True),
        ])
        self.assertTrue(self.m._play_button_on_screen(obs))

    def test_pausing_player_is_not_play_button(self):
        # 播放器已在放：按钮是「暂停」，不能当成"还没点播放"
        obs = _obs([
            UINode(cls="Button", id="pause", text="暂停",
                   bounds=(300, 1350, 428, 1420), clickable=True),
        ])
        self.assertFalse(self.m._play_button_on_screen(obs))

    def test_play_list_rows_are_not_button(self):
        obs = _obs([
            UINode(cls="Text", id="r1", text="播放列表",
                   bounds=(0, 800, 200, 860), clickable=True),
        ])
        self.assertFalse(self.m._play_button_on_screen(obs))


if __name__ == "__main__":
    unittest.main()
