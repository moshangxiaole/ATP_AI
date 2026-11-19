"""AI 执行熔断回归（run-20261113-154411 教训）。

那场 PC 端执行里模型「换着花样原地转」：31 步还在同一界面，swipe 对着不可
滚动的搜索浮层硬滑，没人喊停。这里钉住四道闸：

1. 页面不可滑动时，模型的 swipe 请求被忽略（不给设备硬滑），连续 3 次被
   忽略直接中止 —— 而不是让驱动抛「不支持 swipe」;
2. 连续 N 步页面签名无变化（动作签名各不相同、防打转计数器认不出）判 stalled；
3. 墙钟超时 ai_max_seconds 自动终止；
4. Windows UIA 的 "Edit" 控件、AutomationId 带 search/edit 的节点必须被当成
   可编辑 —— 否则输入前不清空，关键词拼接直接毁掉整条用例。
"""
from __future__ import annotations

import itertools
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from atp.agents.runner import AiRunner  # noqa: E402
from atp.config import Config  # noqa: E402
from atp.agents.perception.locator import is_editable  # noqa: E402
from atp.common.stepir import NLCase  # noqa: E402
from atp.agents.platforms.base import BaseDriver  # noqa: E402
from atp.common.types import UINode  # noqa: E402
from console_fakes import FakeOaDriver, ScriptedPlanner  # noqa: E402


class _StubClient:
    """假模型：只点亮"模型可用"分支，不发生任何真实调用。"""

    available = True

    class cfg:  # noqa: N801 - 只要属性形状对得上
        model = "stub"
        vision_model = "stub"

    def chat_json(self, messages, model=None, **kwargs):
        return {"passed": True, "confidence": 0.95, "evidence": "stub", "missing": ""}


def _nl(max_steps: int = 40) -> NLCase:
    return NLCase(case_id="breaker-1", title="原地打转回归",
                  nl="打开应用后点击标题", platform="android", max_steps=max_steps)


class BreakerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_breaker_"))
        self.cfg = Config(home=self.tmp)
        self.cfg.artifacts_dir = str(self.tmp / "art")
        self.cfg.db_path = str(self.tmp / "t.db")
        self.cfg.apps_path = str(self.tmp / "apps.json")
        self.cfg.persist_runs = False
        self.cfg.screenshot_on_success = False

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, script, driver=None):
        runner = AiRunner(driver or FakeOaDriver(), self.cfg,
                          planner=ScriptedPlanner(script), client=_StubClient())
        return runner.run(_nl(), out_dir=self.cfg.artifacts, run_id="t1")


class SwipeGuardTest(BreakerTestBase):
    def test_swipe_on_unscrollable_page_is_blocked(self):
        """页面没有可滚动容器时 swipe 必须被拦下：连发 3 次后判 stalled 中止，
        而不是把硬滑发给驱动（假驱动没实现 swipe，漏拦会报"不支持 swipe_xy"）。"""
        script = [{"action": "swipe", "direction": "up", "intent": "翻页找"}] * 4
        case, result = self._run(script)
        errors = [s.error or "" for s in result.steps]
        self.assertTrue(any("页面不可滑动" in e for e in errors),
                        f"应因 swipe 被拦而中止，实际: {errors}")
        self.assertFalse(any("不支持 swipe" in e for e in errors),
                         "滑动必须在发给驱动之前就被拦下")
        self.assertFalse(result.passed)

    def test_swipe_allowed_on_scrollable_page(self):
        """页面真有可滚动容器时滑动照常执行（这里没有滚动容器会驱动报错，
        说明闸门没有误伤——错误来自驱动而非守卫）。"""
        driver = FakeOaDriver()
        # 给树塞一个 scrollable 节点，模拟长列表页
        root = driver.dump_tree()
        root.children.append(UINode(cls="ListView", bounds=(0, 200, 540, 900),
                                    scrollable=True))
        with mock.patch.object(driver, "dump_tree", return_value=root):
            case, result = self._run(
                [{"action": "swipe", "direction": "up", "intent": "翻页找"}],
                driver=driver)
        self.assertTrue(any("不支持 swipe" in (s.error or "") for s in result.steps),
                        "可滚动页面的 swipe 不该被守卫拦截，应走到驱动层")


class StallBreakerTest(BreakerTestBase):
    def test_page_unchanged_for_stall_limit_aborts(self):
        """换着动作原地转：交替点两个静态节点，动作签名各不相同，
        但页面签名一步都没变 —— 连续 6 步后必须中止。"""
        script = ([{"action": "tap", "node_index": 0, "intent": "点击阳光OA"},
                   {"action": "tap", "node_index": 1, "intent": "点击用户名"}] * 10)
        case, result = self._run(script)
        errors = [s.error or "" for s in result.steps]
        self.assertTrue(any("页面无任何变化" in e for e in errors),
                        f"应因页面停滞中止，实际: {errors}")
        # 中止后不能再跑满 40 步
        self.assertLess(len(result.steps), 12)


class TimeoutBreakerTest(BreakerTestBase):
    def test_wall_clock_timeout_aborts(self):
        """墙钟超限自动终止：假时钟每读一次 +10s，30s 上限内最多走两三步。"""
        self.cfg.ai_max_seconds = 30

        class _FakeClock:
            def __init__(self):
                self._c = itertools.count()

            def perf_counter(self):
                return next(self._c) * 10.0

            def sleep(self, _s):
                pass

            def time(self):
                return 0.0

        script = ([{"action": "tap", "node_index": 0, "intent": "点击阳光OA"},
                   {"action": "tap", "node_index": 1, "intent": "点击用户名"}] * 10)
        with mock.patch("atp.agents.runner.time", _FakeClock()):
            case, result = self._run(script)
        errors = [s.error or "" for s in result.steps]
        self.assertTrue(any("自动终止" in e for e in errors),
                        f"应因超时中止，实际: {errors}")
        self.assertFalse(result.passed)


class RedundantLaunchTest(BreakerTestBase):
    def test_midrun_launch_without_app_uses_case_app(self):
        """两点一起钉住：
        1. run-20261113-163446：模型中途发的 launch 不带 app，绝不能拿空串去打驱动；
        2. run-20261113-202528：应用已经在跑时**不该**重启（会把会话和已填内容全丢掉），
           所以这次 launch 直接跳过，压根走不到驱动。
        """
        from atp.agents.perception.judge import Judge, JudgeResult
        from atp.common.types import Action

        nl = NLCase(case_id="redo-launch", title="打开应用", nl="打开应用后看看",
                    platform="android", app_id="com.sunshine.oa", max_steps=8)
        driver = FakeOaDriver()
        runner = AiRunner(driver, self.cfg,
                          planner=ScriptedPlanner(
                              [{"action": "launch", "intent": "再启动一次应用"}]),
                          client=_StubClient())
        verdict = JudgeResult(passed=True, method="llm-accept", score=0.9, detail="ok")
        with mock.patch.object(Judge, "accept", lambda self, *a, **kw: verdict):
            case, result = runner.run(nl, out_dir=self.cfg.artifacts, run_id="t2")

        launches = [s for s in case.steps if s.action is Action.LAUNCH_APP]
        self.assertEqual(len(launches), 1,
                         "应用已在运行，模型中途那次重启应当被拦下，只留预启动那一步")
        for st in launches:
            self.assertEqual(st.args.get("app"), "com.sunshine.oa",
                             f"{st.step_id} 的 app 参数不能是空串")

    def test_explicit_restart_still_goes_through(self):
        """真的要求重启时（意图里写明「重启」）必须放行，且同样要带上用例级 app_id。"""
        from atp.agents.perception.judge import Judge, JudgeResult
        from atp.common.types import Action

        nl = NLCase(case_id="restart-app", title="重启应用", nl="重启应用后看看",
                    platform="android", app_id="com.sunshine.oa", max_steps=8)
        runner = AiRunner(FakeOaDriver(), self.cfg,
                          planner=ScriptedPlanner(
                              [{"action": "launch", "intent": "重启应用后重新进入"}]),
                          client=_StubClient())
        verdict = JudgeResult(passed=True, method="llm-accept", score=0.9, detail="ok")
        with mock.patch.object(Judge, "accept", lambda self, *a, **kw: verdict):
            case, result = runner.run(nl, out_dir=self.cfg.artifacts, run_id="t3")

        launches = [s for s in case.steps if s.action is Action.LAUNCH_APP]
        self.assertGreaterEqual(len(launches), 2, "写明重启就该执行")
        for st in launches:
            self.assertEqual(st.args.get("app"), "com.sunshine.oa")


class RepeatedInputTest(BreakerTestBase):
    def test_repeated_same_input_is_skipped_not_fatal_at_step3(self):
        """run-20261113-165946：搜索框已填入「联系人乙」，模型又输一次同样的内容，
        旧规则第 3 次直接判死。现在从第 2 次起就跳过执行并提示模型去点结果，
        绝不能再以"同一动作重复 3 次"这种理由把整场判失败。"""
        script = [{"action": "input", "node_index": 2, "text": "联系人乙",
                   "intent": "在搜索框输入联系人乙"}] * 6
        case, result = self._run(script)
        errors = [s.error or "" for s in result.steps]
        self.assertFalse(any("重复 3 次" in e for e in errors), errors)
        # 至多执行一次真正的输入，其余都应被跳过
        inputs = [s for s in result.steps if s.action == "input_text"]
        self.assertLessEqual(len(inputs), 1, [s.step_id for s in inputs])


class _ChangingTapDriver(FakeOaDriver):
    """每次点击界面都真的变（模拟连按同一个数字键 / 连续翻页）。

    注意变化幅度要够大：判重看的是"有没有实质推进"（画面变化 ≥1%），
    只让一行 toast 文案变个词属于"细微变化"，那是 run-20261113-173646 里
    点了搜索框只亮个焦点框的同类情况，本来就该算重复。
    """

    _WORDS = ["已点赞", "已收藏", "已置顶", "已标记", "已备注", "已归档"]

    def __init__(self):
        super().__init__()
        self.hits = 0

    def tap_xy(self, x, y):
        # 整屏切换（home ↔ todo）：这是一次真正的页面变化
        self.state = "todo" if self.state == "home" else "home"
        self.toast = self._WORDS[min(self.hits, len(self._WORDS) - 1)]
        self.hits += 1


class LegitRepeatTest(BreakerTestBase):
    def test_same_spot_taps_that_change_page_are_not_repeats(self):
        """同一坐标连点但页面每拍都在变（连按数字键、连续翻页）属于正常操作，
        重复计数必须归零，不能被防打转规则误杀。"""
        driver = _ChangingTapDriver()
        script = [{"action": "tap", "node_index": 0, "intent": "连续点击同一个位置"}] * 5
        case, result = self._run(script, driver=driver)
        errors = [s.error or "" for s in result.steps]
        self.assertFalse(any("重复" in e or "原地打转" in e for e in errors), errors)
        self.assertGreaterEqual(len([s for s in result.steps if s.action == "tap"]), 5,
                                "页面在变，5 次点击都应真实执行")


class EditableDetectionTest(unittest.TestCase):
    def test_uia_edit_control_is_editable(self):
        """Windows UIA 输入框的 ControlTypeName 就是 "Edit"，早先没有这条
        提示词，桌面端输入前从不清空，关键词拼接毁掉整条用例。"""
        self.assertTrue(is_editable("Edit"))
        self.assertTrue(is_editable("EditText"))

    def test_automation_id_hints_editable(self):
        """UIA AutomationId / resource-id 里的 searchEdit、inputBox 要认得出来。"""
        drv = FakeOaDriver()
        self.assertTrue(drv._looks_editable(
            UINode(cls="Pane", desc="searchEdit", bounds=(0, 0, 10, 10))))
        self.assertTrue(drv._looks_editable(
            UINode(cls="Pane", desc="usernameInput", bounds=(0, 0, 10, 10))))
        self.assertFalse(drv._looks_editable(
            UINode(cls="Pane", desc="btn_submit", bounds=(0, 0, 10, 10))))


if __name__ == "__main__":
    unittest.main()
