"""首跑的"成功"必须是真成功。

线上出过一条极其典型的假用例：指令是「打开【示例聊天应用】→ 点【+】→ 点【钱包】→ 进入【我的钱包】则成功」，
执行记录里只有 1 步（还是在下拉菜单没展开时点了一次），没有任何判定，却状态 passed 并沉淀进库。
原因很简单：`RunResult.passed` 等价于"每一步的 perform 都返回了 ok"——手势发出去就算通过。

这个文件钉住三层修正：
1. 规划器异常（模型不可用）不许当成 done；
2. 规划没正常收尾（中止/卡死/步数耗尽）必须留一条失败步骤；
3. 收尾必须有整体验收：结构校验（有实际交互）+ 语义验收（最终界面能不能证明场景完成）。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from atp.agents.runner import AiRunner, LlmPlanner  # noqa: E402
from atp.agents.llm.client import LLMError  # noqa: E402
from atp.config import Config  # noqa: E402
from atp.agents.perception.judge import Judge, JudgeResult  # noqa: E402
from atp.memory.sediment import sediment_blocker  # noqa: E402
from atp.common.stepir import CaseIR, NLCase, RunResult, Step  # noqa: E402
from atp.common.types import Action  # noqa: E402
from console_fakes import FakeOaDriver, ScriptedPlanner  # noqa: E402


class _StubClient:
    """假模型：只用来点亮"模型可用"这条分支，真正的调用都被替换掉。"""

    available = True

    class cfg:  # noqa: N801 - 只要属性形状对得上
        model = "stub"
        vision_model = "stub"


class PlannerErrorTest(unittest.TestCase):
    def test_llm_error_aborts_instead_of_done(self):
        client = mock.Mock()
        client.chat_json.side_effect = LLMError("连接超时")
        obs = FakeOaDriver().observe()
        nl = NLCase(title="随便", nl="点一下登录")
        decision = LlmPlanner(client).next(nl, obs, [])
        self.assertEqual(decision["action"], "abort")
        self.assertIn("模型不可用", decision["abort_reason"])


class AcceptanceGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_accept_"))
        self.cfg = Config(home=self.tmp)
        self.cfg.artifacts_dir = str(self.tmp / "art")
        self.cfg.cases_dir = str(self.tmp / "cases")
        self.cfg.db_path = str(self.tmp / "data" / "t.db")
        self.nl = NLCase(case_id="accept-1", title="点登录",
                         nl="打开应用，点击【登录】进入工作台", platform="android")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, script: list[dict], verdict: JudgeResult):
        runner = AiRunner(FakeOaDriver(), self.cfg, planner=ScriptedPlanner(script),
                          client=_StubClient())
        with mock.patch.object(Judge, "accept", lambda self, *a, **kw: verdict):
            return runner.run(self.nl, out_dir=self.cfg.artifacts, run_id="t1")

    def test_failed_acceptance_blocks_sediment(self):
        """手势都成功了，但界面证明不了场景完成 → 整条不算通过。"""
        verdict = JudgeResult(passed=False, method="llm-accept", score=0.2,
                              detail="未完成：当前仍停留在登录页")
        case, result = self._run(
            [{"action": "tap", "node_index": 2, "intent": "点击登录"}], verdict)

        self.assertTrue(case.steps, "剧本至少执行了一步，否则测的不是验收")
        verify = [s for s in result.steps if s.step_id == "v01"]
        self.assertEqual(len(verify), 1, "首跑收尾必须留下一条验收步骤")
        self.assertEqual(verify[0].status, "failed")
        self.assertFalse(result.passed)
        self.assertIn("未通过", sediment_blocker(result))

    def test_passed_acceptance_allows_sediment(self):
        verdict = JudgeResult(passed=True, method="llm-accept", score=0.92,
                              detail="工作台已展示今日待办")
        case, result = self._run(
            [{"action": "tap", "node_index": 2, "intent": "点击登录"}], verdict)
        self.assertTrue(result.passed)
        self.assertEqual(sediment_blocker(result), "")

    def test_accepted_verify_is_sedimented_for_replay(self):
        """真机事故：验收步骤只进 result.steps 不进 case.steps，回放里根本
        没有验收 —— AI 首跑 9 步、回放只剩 8 步，最终画面零校验。"""
        verdict = JudgeResult(passed=True, method="llm-accept", score=0.92,
                              detail="工作台已展示今日待办")
        case, result = self._run(
            [{"action": "tap", "node_index": 2, "intent": "点击登录"}], verdict)

        sedimented = [s for s in case.steps if s.step_id == "v01"]
        self.assertEqual(len(sedimented), 1, "验收步骤必须沉淀进用例")
        st = sedimented[0]
        self.assertEqual(st.action, Action.WAIT_FOR)
        self.assertEqual(st.postcondition.kind, "assert_screen")
        self.assertTrue(st.postcondition.witness or st.postcondition.method == "llm",
                        "有界面证据用证据，没有才回放时问模型")

    def test_nothing_but_done_is_not_success(self):
        """一步没做就说完成：不该被当成通过。"""
        verdict = JudgeResult(passed=True, method="llm-accept", detail="ok")
        case, result = self._run([], verdict)
        verify = [s for s in result.steps if s.step_id == "v01"][0]
        self.assertEqual(verify.status, "failed")
        self.assertIn("没有执行任何步骤", verify.error)
        self.assertFalse(result.passed)
        self.assertTrue(sediment_blocker(result))

    def test_setup_only_case_is_not_success(self):
        """只有启动/杀进程这类准备动作时，结构闸直接否掉，连模型都不问。"""
        case = CaseIR(case_id="c1", title="只启动")
        case.steps = [
            Step(step_id="s01", intent="结束进程", action=Action.TERMINATE_APP),
            Step(step_id="s02", intent="启动应用", action=Action.LAUNCH_APP),
        ]
        result = RunResult(run_id="r1", case_id="c1", case_title="只启动",
                           platform="android", device_id="fake", mode="ai")
        result.steps = []
        runner = AiRunner(FakeOaDriver(), self.cfg, planner=ScriptedPlanner([]),
                          client=_StubClient())
        asked = []
        with mock.patch.object(Judge, "accept",
                               lambda self, *a, **kw: asked.append(1) or JudgeResult(passed=True)):
            runner._accept(self.nl, case, result, self.tmp / "art", [])
        self.assertFalse(asked, "结构闸没过就不该再花钱问模型")
        self.assertEqual(result.steps[0].status, "failed")
        self.assertIn("只做了启动/准备动作", result.steps[0].error)


class WebOpenUrlMaximizeTest(unittest.TestCase):
    """WEB 用例的「打开 URL」步骤：打开即立刻最大化，画布基准按最大化后重采。

    用户要求：把「打开URL后最大化窗口」写进第一步骤——最大化是 open_url 的
    固有语义，不单列一步；画布档案若停留在小窗口尺寸，沉淀的坐标分母就是错的。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_weburl_"))
        self.cfg = Config(home=self.tmp)
        self.cfg.artifacts_dir = str(self.tmp / "art")
        self.cfg.cases_dir = str(self.tmp / "cases")
        self.cfg.db_path = str(self.tmp / "data" / "t.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _driver(self):
        from atp.common.types import UINode
        from atp.agents.platforms.base import BaseDriver

        class FakeWeb(BaseDriver):
            platform = "web"

            def __init__(self):
                super().__init__("fake-web")
                self.maximized = False
                self.opened: list[str] = []

            def connect(self):
                self._connected = True

            def screen_size(self):
                return (1920, 950) if self.maximized else (1200, 700)

            def screenshot(self):
                return b""

            def dump_tree(self):
                label = "流程管理" if self.maximized else "管理中心"
                return UINode(cls="Root", bounds=(0, 0, *self.screen_size()),
                              children=[UINode(cls="Text", text=label,
                                               bounds=(10, 10, 200, 50))])

            def wait_idle(self, timeout_ms=1500):
                pass

            def open_url(self, url):
                self.opened.append(url)

            def maximize(self):
                self.maximized = True
                return self.screen_size()

        return FakeWeb()

    def test_open_url_maximizes_inline_and_reprofiles(self):
        from atp.common.stepir import NLCase

        nl = NLCase(case_id="weburl", title="打开管理后台",
                    nl="打开 http://x/Admin 登录后进入流程管理", platform="web")
        runner = AiRunner(self._driver(), self.cfg,
                          planner=ScriptedPlanner(
                              [{"action": "open_url", "url": "http://x/Admin",
                                "intent": "打开登录页面"}]),
                          client=_StubClient())
        with mock.patch.object(Judge, "accept",
                               lambda self, *a, **kw:
                               JudgeResult(passed=True, method="llm-accept")):
            case, result = runner.run(nl, out_dir=self.cfg.artifacts, run_id="w1")

        drv = runner.driver
        self.assertEqual(drv.opened, ["http://x/Admin"])
        self.assertTrue(drv.maximized, "打开 URL 后必须立刻最大化")
        # 画布基准 = 最大化后的尺寸；不能停留在打开时的小窗口
        self.assertEqual((case.screen.width, case.screen.height), (1920, 950))
        # 最大化不单列步骤（写进 open_url 这一步）
        self.assertFalse([s for s in case.steps if s.action == Action.MAXIMIZE],
                         "最大化不该是独立步骤")
        open_steps = [s for s in case.steps if s.action == Action.OPEN_URL]
        self.assertTrue(open_steps)
        self.assertIn("已最大化窗口", " ".join(
            r.detail for r in result.steps if r.step_id == open_steps[0].step_id))

    def test_nl_url_maximizes_inline(self):
        """URL 由参数传入时（nl.url 路径）：同样并入第一步、立即最大化。"""
        from atp.common.stepir import NLCase

        nl = NLCase(case_id="weburl2", title="打开管理后台",
                    nl="登录后进入流程管理", platform="web",
                    url="http://x/Admin")
        runner = AiRunner(self._driver(), self.cfg,
                          planner=ScriptedPlanner([]), client=_StubClient())
        with mock.patch.object(Judge, "accept",
                               lambda self, *a, **kw:
                               JudgeResult(passed=True, method="llm-accept")):
            case, result = runner.run(nl, out_dir=self.cfg.artifacts, run_id="w2")

        drv = runner.driver
        self.assertTrue(drv.maximized)
        self.assertEqual((case.screen.width, case.screen.height), (1920, 950))
        self.assertFalse([s for s in case.steps if s.action == Action.MAXIMIZE])
        s01 = next(r for r in result.steps if r.step_id == "s01")
        self.assertIn("已最大化窗口", s01.detail)


if __name__ == "__main__":
    unittest.main()
