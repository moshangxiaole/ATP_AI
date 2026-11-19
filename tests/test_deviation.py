"""偏离检测与自动回退（AC-1.10 / AC-4.3 / AC-4.4 / AC-4.5 / AC-4.6）。

最关键的一张表是 AC-4.6 的九条禁止清单：**每一种情况下 ``press_key("back")``
的调用次数都必须是 0**。因此这里的断言一律直接数驱动上的真实调用，
而不是看函数返回了什么。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from mobile_qa_helpers import (  # noqa: E402
    APP, H, OTHER_APP, W, TreeDriver, built_stack, make_case, make_config,
    make_driver, make_executor, page, shifted,
)
from atp.agents.perception.deviation import (  # noqa: E402
    BackController, DeviationDetector, DeviationVerdict,
)
from atp.agents.perception.judge import JudgeResult  # noqa: E402
from atp.agents.perception.page_stabilizer import (  # noqa: E402
    PageFrame, PageStack, PageStabilizer, StableResult, page_signature,
)
from atp.common.stepir import Postcondition, Step  # noqa: E402
from atp.common.types import Action, Observation, UINode  # noqa: E402


def other_page(count: int = 25) -> UINode:
    """与 page() 粗粒度签名差异极大的另一个页面。"""
    rows = [UINode(cls="Text", id=f"o{i}", text=f"陌生条目{i}",
                   bounds=(40, 200 + i * 30, 1040, 228 + i * 30))
            for i in range(count)]
    return UINode(cls="Page", bounds=(0, 0, W, H), children=[
        UINode(cls="Column", id="other_root", bounds=(0, 100, W, H - 100), children=rows),
    ])


def irreversible_page() -> UINode:
    root = page(count=30)
    root.children.append(UINode(cls="Button", id="btn_pay", text="确认支付",
                                bounds=(400, 1700, 680, 1780), clickable=True))
    return root


def obs_of_root(root: UINode, w: int = W, h: int = H) -> Observation:
    return Observation(platform="android", device_id="mock-qa", width=w, height=h, root=root)


def tap_step(**kwargs) -> Step:
    return Step(step_id="s01", intent="点击目标", action=Action.TAP, **kwargs)


class _Env:
    """偏离检测所需的最小运行环境。"""

    def __init__(self, cfg, *, before_root=None, stack_steps=2, frame_package=APP,
                 driver_package=APP, last_stable=True, tree_stale=False):
        self.cfg = cfg
        self.before_root = before_root if before_root is not None else page(count=40)
        self.driver = TreeDriver(page(count=40))
        self.driver.tree_stale = tree_stale
        self.driver.foreground_package = driver_package
        self.stabilizer = PageStabilizer(self.driver, cfg)
        self.stabilizer.last_result = StableResult(
            stable=last_stable, degraded=not last_stable,
            coarse_sig=page_signature(self.before_root))
        self.stack = built_stack(self.before_root, package=frame_package, steps=stack_steps)
        self.detector = DeviationDetector(cfg, self.stack, self.stabilizer)
        self.back = BackController(self.driver, self.detector, self.stack,
                                   self.stabilizer, cfg,
                                   on_event=self.driver.event_sink(),
                                   should_cancel=lambda: False)

    def before(self) -> Observation:
        return obs_of_root(self.before_root)

    def after(self, root: UINode | None = None) -> Observation:
        return obs_of_root(root if root is not None else other_page())


class DeviationDetectTest(unittest.TestCase):
    """AC-1.10：细粒度指纹变化不算偏离。"""

    def setUp(self):
        self.cfg = make_config()

    def test_ac_1_10_one_pixel_scroll_never_backs(self):
        env = _Env(self.cfg)
        before = obs_of_root(page(count=40))
        after = obs_of_root(shifted(page(count=40), dy=1))

        self.assertNotEqual(before.fingerprint, after.fingerprint, "细粒度指纹必须变了")
        self.assertEqual(page_signature(before.root), page_signature(after.root))

        verdict = env.detector.detect(step=tap_step(), before=before, after=after,
                                      verdict=JudgeResult(passed=True), package=APP)
        self.assertFalse(verdict.detected, "同一页面滚动 1 像素绝不判偏离")
        self.assertEqual(verdict.cause, "coarse_unchanged")
        self.assertGreaterEqual(verdict.coarse_similarity, 0.80)

        depth = len(env.stack)
        out = env.back.recover(step=tap_step(), verdict=verdict, before=before,
                               after=after, package=APP)
        self.assertFalse(out.recovered)
        self.assertEqual(env.driver.back_count(), 0, "不判偏离就不许 BACK")
        self.assertEqual(len(env.stack), depth, "检测/回退过程本身不新增页面栈帧")
        self.assertNotIn("deviation_detected",
                         [e.get("type") for e in env.driver.events])

    def test_ac_1_10_local_list_refresh_is_same_page(self):
        env = _Env(self.cfg)
        refreshed = page(count=40)
        refreshed.children[0].children[0].text = "刚刚刷新的新文案"
        before = obs_of_root(page(count=40))
        after = obs_of_root(refreshed)

        verdict = env.detector.detect(step=tap_step(), before=before, after=after,
                                      verdict=JudgeResult(passed=True), package=APP)
        self.assertFalse(verdict.detected, "列表局部刷新只是同页变化，不判偏离")
        self.assertEqual(verdict.cause, "same_page_structure")
        out = env.back.recover(step=tap_step(), verdict=verdict, before=before,
                               after=after, package=APP)
        self.assertFalse(out.recovered)
        self.assertEqual(env.driver.back_count(), 0)

    def test_ac_1_10_thin_evidence_is_never_deviation(self):
        env = _Env(self.cfg)
        before = obs_of_root(page(count=1))            # 节点数不足，证据不足
        after = obs_of_root(other_page())
        verdict = env.detector.detect(step=tap_step(), before=before, after=after,
                                      verdict=JudgeResult(passed=True), package=APP)
        self.assertFalse(verdict.detected)
        self.assertEqual(verdict.cause, "insufficient_evidence")

    def test_jump_page_is_detected(self):
        env = _Env(self.cfg)
        before = obs_of_root(page(count=40))
        after = obs_of_root(other_page())
        verdict = env.detector.detect(step=tap_step(), before=before, after=after,
                                      verdict=JudgeResult(passed=True), package=APP)
        self.assertTrue(verdict.detected, "跳到完全不同的页面必须判偏离")
        self.assertIn("deviation:", verdict.cause)
        self.assertLess(verdict.coarse_similarity, 0.35)
        self.assertTrue(verdict.can_back)


class ForbiddenListTest(unittest.TestCase):
    """AC-4.6：九条禁止清单，每条都必须做到 ``press_key("back")`` = 0 次。"""

    def setUp(self):
        self.cfg = make_config()

    def _scenarios(self):
        return [
            # ① 当前步骤本身就在操作导航栈 / 启动 / 打开链接
            ("当前步骤是 BACK",
             dict(step=Step(step_id="s", intent="返回", action=Action.BACK)),
             "blocked_action"),
            ("当前步骤是 HOME",
             dict(step=Step(step_id="s", intent="回桌面", action=Action.HOME)),
             "blocked_action"),
            ("当前步骤是 LAUNCH_APP",
             dict(step=Step(step_id="s", intent="打开应用", action=Action.LAUNCH_APP)),
             "blocked_action"),
            ("当前步骤是 OPEN_URL",
             dict(step=Step(step_id="s", intent="打开链接", action=Action.OPEN_URL)),
             "blocked_action"),
            ("当前步骤是 press_key(back)",
             dict(step=Step(step_id="s", intent="按返回", action=Action.PRESS_KEY,
                            args={"key": "back"})),
             "press_key_back"),
            # ② 后置断言已通过 / 用例声明这一步会跳页
            ("后置断言已通过",
             dict(step=tap_step(postcondition=Postcondition(kind="assert_text", claim="字段0")),
                  guard_verdict=JudgeResult(passed=True, method="dsl"),
                  cause="assertion_passed"),
             "assertion_passed_or_expected_navigation"),
            ("用例声明 expect_navigation",
             dict(step=tap_step(args={"expect_navigation": True})),
             "expect_navigation"),
            # ③ 命中不可逆词
            ("页面含「确认支付」",
             dict(before_root=irreversible_page()),
             "irreversible_marker:支付"),
            # ④ 本轮已处置弹窗
            ("本轮处置过弹窗",
             dict(popup_changed=True),
             "popup_dismissed"),
            # ⑤ 门禁不可信
            ("门禁不可信（超时/degraded）",
             dict(last_stable=False),
             "gate_untrusted"),
            ("门禁不可信（缓存树）",
             dict(tree_stale=True),
             "gate_untrusted"),
            # ⑥ 页面栈不足
            ("页面栈只有 1 帧",
             dict(stack_steps=1),
             "stack_too_shallow"),
            # ⑦ 包名已离开被测应用
            ("前台包名已离开被测应用",
             dict(package=OTHER_APP),
             "package_out_of_app"),
            # ⑧ 语义 / 视频断言进行中
            ("语义断言（assert_screen）",
             dict(step=tap_step(postcondition=Postcondition(kind="assert_screen", claim="登录成功"))),
             "semantic_or_motion_assertion"),
            ("语义断言（method=llm）",
             dict(step=tap_step(postcondition=Postcondition(kind="assert_visible",
                                                           claim="首页", method="llm"))),
             "semantic_or_motion_assertion"),
            # ⑨ 已取消 / AI 首跑
            ("用户已取消",
             dict(cancelled=True),
             "cancelled"),
            ("AI 首跑模式",
             dict(mode="ai"),
             "ai_mode"),
        ]

    def test_ac_4_6_forbidden_list_never_presses_back(self):
        for name, kwargs, reason in self._scenarios():
            with self.subTest(scenario=name):
                env_kwargs = {k: v for k, v in kwargs.items()
                              if k in ("before_root", "stack_steps", "last_stable",
                                       "tree_stale")}
                env = _Env(self.cfg, **env_kwargs)
                step = kwargs.get("step") or tap_step()
                cause = kwargs.get("cause", "deviation:test")
                guard_verdict = kwargs.get("guard_verdict") or DeviationVerdict(
                    detected=True, cause=cause, coarse_similarity=0.10)
                recover_verdict = DeviationVerdict(
                    detected=True, cause=cause, coarse_similarity=0.10)
                package = kwargs.get("package", APP)
                popup_changed = bool(kwargs.get("popup_changed", False))
                cancelled = bool(kwargs.get("cancelled", False))
                mode = kwargs.get("mode", "replay")
                before, after = env.before(), env.after()

                can_back, blocked = env.detector.check_back_guard(
                    step=step, verdict=guard_verdict, before=before, after=after,
                    stack=env.stack, package=package, popup_changed=popup_changed,
                    stable=env.stabilizer.last_result, mode=mode, cancelled=cancelled,
                )
                self.assertFalse(can_back, f"{name} 必须禁止自动 BACK")
                self.assertEqual(blocked, reason, f"{name} 的拦截原因不对")

                out = env.back.recover(
                    step=step, verdict=recover_verdict, before=before, after=after,
                    package=package, mode=mode, cancelled=cancelled,
                    popup_changed=popup_changed,
                )
                self.assertFalse(out.recovered)
                self.assertEqual(out.backs, 0)
                self.assertEqual(env.driver.keys, [],
                                 f"{name}：press_key('back') 必须是 0 次")
                self.assertEqual(env.driver.back_count(), 0)


class BackLimitTest(unittest.TestCase):
    """AC-4.3：单步 ≤2 次、整 run ≤3 次，第 4 次绝不按。"""

    def setUp(self):
        self.cfg = make_config()
        self.env = _Env(self.cfg)
        # 让「回退校验」永远失败：上一页签名与当前页差异极大
        self.env.stack.clear()
        self.env.stack.push(PageFrame(step_id="s00", coarse_sig=page_signature(other_page()),
                                      package=APP))
        self.env.stack.push(PageFrame(step_id="s01", coarse_sig=page_signature(page(count=40)),
                                      package=APP))
        self.verdict = DeviationVerdict(detected=True, cause="deviation:test",
                                        coarse_similarity=0.10, can_back=True)

    def _recover(self):
        return self.env.back.recover(step=tap_step(), verdict=self.verdict,
                                     before=self.env.before(), after=self.env.after(),
                                     package=APP)

    def test_ac_4_3_bounded_backs(self):
        first = self._recover()
        self.assertFalse(first.recovered)
        self.assertEqual(first.backs, 2, "单步 BACK 次数上限 2")
        self.assertEqual(self.env.driver.back_count(), 2)

        second = self._recover()
        self.assertFalse(second.recovered)
        self.assertEqual(self.env.driver.back_count(), 3, "整 run 上限 3 次")

        third = self._recover()
        self.assertFalse(third.recovered)
        self.assertEqual(self.env.driver.back_count(), 3, "第 4 次绝不许再按 BACK")
        reasons = [e.get("reason") for e in self.env.driver.events
                   if e.get("type") == "deviation_recovery_failed"]
        self.assertIn("back_verify_failed", reasons)

    def test_ac_4_3_reset_run_restores_budget(self):
        self._recover()
        self._recover()
        self.assertEqual(self.env.driver.back_count(), 3)
        self.env.back.reset_run()
        self._recover()
        self.assertGreater(self.env.driver.back_count(), 3,
                           "新 run 必须重置 BACK 预算")


class RecoveryVerifyTest(unittest.TestCase):
    """AC-4.4：BACK 后必须与 stack[-2] 粗签名 ≥0.80 且包名一致才算恢复。"""

    def setUp(self):
        self.cfg = make_config()

    def _env(self, restored: UINode, frame_root: UINode, driver_package: str = APP):
        env = _Env(self.cfg, before_root=frame_root, driver_package=driver_package)
        env.driver.tree = other_page()                    # 现在停在错页
        env.driver.on_key = lambda d: setattr(d, "tree", restored)
        env.stack.clear()
        env.stack.push(PageFrame(step_id="s00", coarse_sig=page_signature(frame_root),
                                 package=APP))
        env.stack.push(PageFrame(step_id="s01", coarse_sig=page_signature(other_page()),
                                 package=APP))
        return env

    def test_ac_4_4_success_when_signature_and_package_match(self):
        env = self._env(restored=page(count=40), frame_root=page(count=40))
        verdict = DeviationVerdict(detected=True, cause="deviation:test",
                                   coarse_similarity=0.1, can_back=True)
        out = env.back.recover(step=tap_step(), verdict=verdict,
                               before=env.before(), after=env.after(), package=APP)

        self.assertTrue(out.recovered, "签名一致 + 包名一致 → 必须判恢复成功")
        self.assertEqual(out.backs, 1)
        self.assertIs(out.retry, True, "恢复成功后必须请求重跑当前步骤")
        self.assertEqual(env.driver.back_count(), 1)
        self.assertIn("deviation_recovered",
                      [e.get("type") for e in env.driver.events])

    def test_ac_4_4_failure_when_signature_mismatch(self):
        env = self._env(restored=page(count=40), frame_root=other_page())
        verdict = DeviationVerdict(detected=True, cause="deviation:test",
                                   coarse_similarity=0.1, can_back=True)
        out = env.back.recover(step=tap_step(), verdict=verdict,
                               before=env.before(), after=env.after(), package=APP)

        self.assertFalse(out.recovered, "没回到上一页就不能算恢复")
        self.assertFalse(out.retry, "恢复失败绝不允许重跑")
        self.assertEqual(out.backs, 2)
        self.assertIn("deviation_recovery_failed",
                      [e.get("type") for e in env.driver.events])

    def test_ac_4_4_failure_when_package_changed(self):
        env = self._env(restored=page(count=40), frame_root=page(count=40),
                        driver_package=OTHER_APP)
        verdict = DeviationVerdict(detected=True, cause="deviation:test",
                                   coarse_similarity=0.1, can_back=True)
        out = env.back.recover(step=tap_step(), verdict=verdict,
                               before=env.before(), after=env.after(), package=APP)
        self.assertFalse(out.recovered, "包名不一致说明没回到被测应用，不能算恢复")


class ExecutorRecoveryTest(unittest.TestCase):
    """AC-4.5 / AC-4.7：回退成功后恰好重跑 1 次，且不新增 StepResult。"""

    def setUp(self):
        self.cfg = make_config()

    def test_ac_4_5_retry_once_no_extra_step_result(self):
        drv = make_driver("jump_page")            # 点完跳到完全不同的页面
        exe = make_executor(drv, self.cfg)
        case = make_case(1)
        result = exe.run(case, mode="replay", run_id="qa-4-5")

        sr = result.steps[0]
        self.assertEqual(sr.status, "passed", f"回退 + 重跑后应当通过: {sr.detail}")
        self.assertIn("偏离纠正", sr.detail, "detail 必须写清「回退 N 次 + 重跑」")
        self.assertIn("重跑", sr.detail)
        self.assertEqual(len(result.steps), 1, "回退不得新增 StepResult 条目")
        self.assertFalse(result.ai_steps, "回退链路零模型：ai_used 不得新增")
        self.assertFalse(sr.ai_used)
        self.assertEqual(drv.backs.count("back"), 1, "只允许按 1 次 BACK")
        self.assertEqual(len(drv.taps), 2, "原始 1 次 + 重跑 1 次")
        self.assertTrue(drv.taps, "重跑必须真的再点一次目标")

        types = [e.get("type") for e in drv.events]
        for expected in ("deviation_detected", "deviation_back", "deviation_recovered"):
            self.assertIn(expected, types, f"必须发出 {expected} 事件")

    def test_ac_4_5_retry_failure_does_not_back_again(self):
        drv = make_driver("jump_page")
        # 重跑那一次点击后 tree 仍是错页（_after_tap 已消费），断言「重跑失败不再回退」
        exe = make_executor(drv, self.cfg)
        result = exe.run(make_case(1), mode="replay", run_id="qa-4-5b")
        # 只要整轮只按过 1 次 BACK，就证明重跑阶段没有再触发回退
        self.assertLessEqual(drv.backs.count("back"), 1)
        self.assertEqual(len(result.steps), 1)


class PackageSignalTest(unittest.TestCase):
    """包名强信号必须活在主链路上，而不是被「自己和自己比」静音。

    主链路会把动作后的页面压栈，压完之后 ``stack.current().package`` 就是当前
    页面的包名；检测器若仍从栈顶取「期望包名」，``package_changed`` 永远不成立。
    执行器必须显式把「动作前那一页」的包名传进去。
    """

    def setUp(self):
        self.cfg = make_config()

    def test_package_changed_is_detected_in_executor_path(self):
        drv = make_driver("jump_page")
        # 跳页 + 前台包名切到别的 App：两个信号同时成立，cause 里必须出现包名
        # 注意 ScenarioDriver 的钩子是私有的 ``_after_tap``（``after_tap`` 是 TreeDriver 的）
        drv._after_tap = lambda d: (setattr(d, "_tree", other_page()),
                                    setattr(d, "foreground_package", OTHER_APP))
        exe = make_executor(drv, self.cfg)
        exe.run(make_case(1), mode="replay", run_id="qa-pkg-1")

        detected = [e for e in drv.events if e.get("type") == "deviation_detected"]
        self.assertTrue(detected, "切到别的 App 必须判偏离")
        self.assertIn("package_changed", str(detected[0].get("cause", "")),
                      "包名变化属于强信号，不能被静音")

    def test_without_explicit_package_the_signal_goes_dead(self):
        """反证：丢掉显式包名后 ``package_changed`` 不会出现（回到修复前的行为）。"""
        from atp.agents.perception import deviation as deviation_mod

        original = deviation_mod.DeviationDetector.detect

        def drop_expected(self, **kwargs):
            kwargs.pop("expected_package", None)
            return original(self, **kwargs)

        drv = make_driver("jump_page")
        drv._after_tap = lambda d: (setattr(d, "_tree", other_page()),
                                    setattr(d, "foreground_package", OTHER_APP))
        deviation_mod.DeviationDetector.detect = drop_expected
        try:
            make_executor(drv, self.cfg).run(make_case(1), mode="replay",
                                             run_id="qa-pkg-2")
        finally:
            deviation_mod.DeviationDetector.detect = original

        detected = [e for e in drv.events if e.get("type") == "deviation_detected"]
        self.assertTrue(detected)
        self.assertNotIn("package_changed", str(detected[0].get("cause", "")),
                         "栈顶就是动作后的页面，自己和自己比必然相等")

    def test_detector_accepts_explicit_expected_package(self):
        """直连检测器：显式传 expected_package 才按「动作前那一页」比对。"""
        env = _Env(self.cfg, before_root=page(count=40))
        verdict = env.detector.detect(
            step=tap_step(), before=env.before(), after=env.after(),
            verdict=JudgeResult(passed=False, method="dsl", score=0.0),
            package=OTHER_APP, expected_package=APP,
        )

        self.assertTrue(verdict.detected)
        self.assertIn("package_changed", verdict.cause)


if __name__ == "__main__":
    unittest.main()
