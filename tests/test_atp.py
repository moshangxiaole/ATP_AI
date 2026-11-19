"""核心链路的回归测试。

不依赖真机、不依赖模型：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from atp.agents.runner import indexed_nodes  # noqa: E402
from atp.config import Config  # noqa: E402
from atp.scriptgen.compiler import to_python  # noqa: E402
from atp.agents.executor import Executor  # noqa: E402
from atp.agents.perception.judge import Judge  # noqa: E402
from atp.agents.perception.locator import derive_strategies, is_stable_id, is_stable_text, resolve  # noqa: E402
from atp.common.stepir import CaseIR, LocatorStrategy, Postcondition, Step  # noqa: E402
from atp.common.types import Action, UINode  # noqa: E402
from atp.agents.devices.manager import DeviceManager  # noqa: E402
from console_fakes import FakeOaDriver, ScriptedPlanner  # noqa: E402


def tree() -> UINode:
    root = UINode(cls="Root", bounds=(0, 0, 540, 960))
    root.children = [
        UINode(cls="Text", id="pkg:id/tv_title", text="工作台", bounds=(0, 0, 540, 100)),
        UINode(cls="EditText", id="pkg:id/et_comment", text="同意", bounds=(40, 500, 500, 570), clickable=True),
        UINode(cls="Button", id="pkg:id/btn_agree", text="同意", bounds=(40, 640, 260, 710), clickable=True),
    ]
    return root


class TestLocator(unittest.TestCase):
    def test_stable_filters(self):
        self.assertTrue(is_stable_text("审批已提交"))
        self.assertFalse(is_stable_text("2026-11-09"))
        self.assertFalse(is_stable_text("1,234.00"))
        self.assertFalse(is_stable_text("这条文案特别特别长超过了二十四个字符的限制所以不能用"))
        # 长 resource-id 必须保留，否则定位精度会掉一大截
        self.assertTrue(is_stable_id("com.sunshine.oa:id/et_username"))
        self.assertFalse(is_stable_id("pkg:id/item_20261109"))

    def test_derive_prefers_id(self):
        t = tree()
        node = t.children[2]
        sts = derive_strategies(node, t, (540, 960))
        self.assertEqual(sts[0].type, "a11y_id")
        self.assertGreater(sts[0].score, 0.9)
        self.assertIn("text", [s.type for s in sts])
        self.assertIn("xpath", [s.type for s in sts])
        self.assertIn("bounds", [s.type for s in sts])

    def test_text_ambiguity_resolved_by_class(self):
        t = tree()
        # 不加类型限定时，会先撞到上面的输入框（经典的"同意"歧义）
        naive = resolve(t, [LocatorStrategy.make("text", "同意")])
        self.assertEqual(naive.node.cls, "EditText")
        # 加上控件类型限定后，正确落到按钮上
        precise = resolve(t, [LocatorStrategy.make("text", "同意", "Button")])
        self.assertEqual(precise.node.cls, "Button")
        self.assertEqual(precise.node.id, "pkg:id/btn_agree")

    def test_bounds_normalized_roundtrip(self):
        t = tree()
        node = t.children[1]
        sts = [s for s in derive_strategies(node, t, (540, 960)) if s.type == "bounds"]
        hit = resolve(t, sts, (540, 960)).node
        self.assertIsNotNone(hit)
        self.assertEqual(hit.center, node.center)

    def test_bounds_synthesizes_when_no_tree(self):
        empty = UINode(cls="Screen", bounds=(0, 0, 540, 960))
        hit = resolve(empty, [LocatorStrategy.make("bounds", [100, 100, 300, 300])], (540, 960)).node
        self.assertIsNotNone(hit)
        self.assertEqual(hit.cls, "Point")

    def test_weight_learning(self):
        s = LocatorStrategy.make("text", "x")
        before = s.score
        s.bump(True)
        self.assertGreater(s.score, before)
        for _ in range(20):
            s.bump(False)
        self.assertLess(s.score, before)

    def test_all_miss(self):
        r = resolve(tree(), [LocatorStrategy.make("a11y_id", "pkg:id/nope")])
        self.assertFalse(r.ok)
        self.assertTrue(r.attempts)


class TestJudge(unittest.TestCase):
    def setUp(self):
        self.obs = FakeOaDriver().observe()

    def test_visible_and_absent(self):
        j = Judge()
        self.assertTrue(j.check(Postcondition(kind="assert_visible", claim="用户名"), self.obs).passed)
        self.assertFalse(j.check(Postcondition(kind="assert_visible", claim="不存在的元素"), self.obs).passed)
        self.assertTrue(j.check(Postcondition(kind="assert_absent", claim="订单已删除"), self.obs).passed)

    def test_text_and_extract(self):
        j = Judge()
        self.assertTrue(j.check(Postcondition(kind="assert_text", claim="阳光OA"), self.obs).passed)
        self.assertTrue(j.check(Postcondition(kind="extract", claim=r"阳.{1}OA"), self.obs).passed)


class TestStepIR(unittest.TestCase):
    def test_roundtrip(self, tmp=Path("build_tmp")):
        case = CaseIR(case_id="t1", title="往返", platform="android")
        case.steps.append(
            Step(step_id="s01", intent="点击", action=Action.TAP,
                 strategies=[LocatorStrategy.make("text", "登录", "Button")])
        )
        p = tmp / "c.json"
        case.save(p)
        back = CaseIR.load(p)
        self.assertEqual(back.steps[0].strategies[0].node_cls, "Button")
        self.assertEqual(back.steps[0].action, Action.TAP)

    def test_stale_detection(self):
        case = CaseIR(case_id="t2", title="t", ui_fingerprint="aaa")
        self.assertTrue(case.is_stale("bbb"))
        self.assertFalse(case.is_stale("aaa"))


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(home=ROOT)
        self.cfg.artifacts_dir = "build_tmp/art"
        self.cfg.db_path = "build_tmp/unit.db"
        self.cfg.apps_path = "build_tmp/apps.json"
        self.cfg.screenshot_on_success = False

    def _case(self) -> CaseIR:
        case = CaseIR(case_id="mini", title="登录", platform="android", app_id="com.sunshine.oa")
        case.steps = [
            Step(step_id="s01", intent="启动", action=Action.LAUNCH_APP, args={"app": "com.sunshine.oa"}),
            Step(step_id="s02", intent="填用户名", action=Action.INPUT_TEXT,
                 strategies=[LocatorStrategy.make("text", "请输入用户名", "EditText")],
                 args={"text": "tester"}),
            Step(step_id="s03", intent="填密码", action=Action.INPUT_TEXT,
                 strategies=[LocatorStrategy.make("a11y_id", "com.sunshine.oa:id/et_password")],
                 args={"text": "123456"}),
            Step(step_id="s04", intent="登录", action=Action.TAP,
                 strategies=[LocatorStrategy.make("a11y_id", "com.sunshine.oa:id/btn_login")]),
        ]
        return case

    def test_replay_passes_without_ai(self):
        d = FakeOaDriver()
        d.connect()
        r = Executor(d, self.cfg, Judge(), None).run(self._case(), mode="replay")
        self.assertTrue(r.passed, [s.error for s in r.steps if s.error])
        self.assertEqual(r.ai_steps, 0)
        self.assertEqual(d.state, "home")

    def test_broken_locator_fails_then_healer_recovers(self):
        case = self._case()
        case.steps[1].strategies = [LocatorStrategy.make("text", "永远不存在的文案")]

        d = FakeOaDriver()
        d.connect()
        r = Executor(d, self.cfg, Judge(), None).run(case, mode="replay")
        self.assertFalse(r.passed)

        class FakeHealer:
            def heal(self, step, obs, c):
                return [LocatorStrategy.make("text", "请输入用户名", "EditText")]

        d2 = FakeOaDriver()
        d2.connect()
        r2 = Executor(d2, self.cfg, Judge(), FakeHealer()).run(case, mode="replay")
        self.assertTrue(r2.passed, [s.error for s in r2.steps if s.error])
        self.assertGreaterEqual(r2.ai_steps, 1)
        self.assertTrue(any("AI 修复" in s.detail for s in r2.steps))


class TestCompiler(unittest.TestCase):
    def test_generated_code(self):
        case = CaseIR(case_id="c", title="编译", platform="android", app_id="pkg")
        case.steps = [
            Step(step_id="s01", intent="启动", action=Action.LAUNCH_APP, args={"app": "pkg"}),
            Step(step_id="s02", intent="点击登录", action=Action.TAP,
                 strategies=[LocatorStrategy.make("text", "登录", "Button")]),
            Step(step_id="s03", intent="断言", action=Action.WAIT_FOR,
                 postcondition=Postcondition(kind="assert_text", claim="工作台")),
        ]
        code = to_python(case, "c.stepir.json")
        self.assertIn("S.launch('pkg')", code)
        self.assertIn("node_cls='Button'", code)
        self.assertIn("S.check('assert_text', '工作台'", code)
        self.assertIn("sys.path.insert", code)
        compile(code, "generated.py", "exec")  # 语法必须合法


class TestAiRunner(unittest.TestCase):
    def test_scripted_first_run_produces_stepir(self):
        from atp.agents.runner import AiRunner
        from atp.common.stepir import NLCase

        nl = NLCase(case_id="mini_ai", title="登录流程", platform="android",
                    app_id="com.sunshine.oa", nl="输入账号密码登录", asserts=["工作台"])
        plan = [
            {"action": "input", "node_index": 2, "text": "tester", "intent": "用户名"},
            {"action": "input", "node_index": 4, "text": "123456", "intent": "密码"},
            {"action": "tap", "node_index": 5, "intent": "登录"},
            {"action": "done"},
        ]
        cfg = Config(home=ROOT)
        cfg.artifacts_dir = "build_tmp/art"
        cfg.db_path = "build_tmp/unit.db"
        cfg.apps_path = "build_tmp/apps.json"   # 别把测试的包名写进工程的 apps.json
        d = FakeOaDriver()
        d.connect()
        case, r = AiRunner(d, cfg, planner=ScriptedPlanner(plan)).run(nl)
        self.assertTrue(r.passed, [s.error for s in r.steps if s.error])
        self.assertGreater(len(case.steps), 4)
        self.assertTrue(all(s.strategies for s in case.steps if s.action.needs_target))

    def test_indexed_nodes_matches_prompt(self):
        nodes, listing = indexed_nodes(FakeOaDriver().observe().root)
        self.assertEqual(len(nodes), len(listing.splitlines()))


class TestDeviceManager(unittest.TestCase):
    def test_discover_and_lease(self):
        cfg = Config(home=ROOT)
        mgr = DeviceManager(cfg)
        names = {d.platform for d in mgr.discover()}
        self.assertTrue({"desktop", "web"} <= names)
        self.assertNotIn("mock", names, "Mock 终端已经下线，不该再出现在设备列表里")
        info = mgr.resolve("desktop")
        self.assertEqual(info.platform, "desktop")
        with self.assertRaises(Exception):
            mgr._build(type("I", (), {"platform": "mock", "device_id": "x"})())


if __name__ == "__main__":
    unittest.main(verbosity=2)
