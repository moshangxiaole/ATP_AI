"""断言证据（witness）的回归测试。

核心诉求：语义断言在**回放**时必须零模型调用。
手段是首跑把"这条断言靠界面上哪句话成立"抄成一段文本，
回放时先查这段文本在不在；查不到才回落到模型。

不依赖真机、不依赖模型：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.scriptgen.compiler import to_python  # noqa: E402
from atp.agents.perception.judge import Judge  # noqa: E402
from atp.common.stepir import CaseIR, Postcondition, Step  # noqa: E402
from atp.common.types import Action, Observation, UINode  # noqa: E402


def obs(*texts: str, url: str = "https://demo.example.com/Admin/Index/index") -> Observation:
    root = UINode(cls="Root", bounds=(0, 0, 1536, 826))
    root.children = [
        UINode(cls="Text", text=t, bounds=(0, i * 40, 400, i * 40 + 30))
        for i, t in enumerate(texts)
    ]
    return Observation(platform="web", device_id="web", width=1536, height=826,
                       root=root, url=url)


class Cfg:
    vision_model = ""
    model = "fake"


class ExplodingLLM:
    """只要被调用就炸——用来证明"回放零 AI"不是口头承诺。"""

    available = True
    cfg = Cfg()

    def chat_json(self, *a, **kw):  # noqa: D401, ANN002
        raise AssertionError("回放不应该调用模型")


class TestWitnessJudge(unittest.TestCase):
    def test_witness_hit_skips_model(self):
        post = Postcondition(kind="assert_screen", claim="登录成功，进入后台管理首页",
                             method="llm", witness="协同办公云平台")
        j = Judge(llm=ExplodingLLM())  # type: ignore[arg-type]
        r = j.check(post, obs("协同办公云平台", "待办事项", "我的审批"))
        self.assertTrue(r.passed)
        self.assertEqual(r.method, "dsl-witness")
        self.assertEqual(r.score, 1.0)

    def test_witness_miss_falls_back_to_claim_keywords(self):
        # 证据失效，但界面文本刚好覆盖断言关键词 → 仍然零模型通过。
        # 注意关键词是按标点切开的（"登录成功，后台首页" → 两段）。
        post = Postcondition(kind="assert_screen", claim="登录成功，后台首页",
                             method="llm", witness="已经下线的旧文案")
        j = Judge(llm=ExplodingLLM())  # type: ignore[arg-type]
        r = j.check(post, obs("后台首页", "登录成功"))
        self.assertTrue(r.passed)
        self.assertEqual(r.method, "dsl-precheck")

    def test_witness_miss_without_coverage_uses_model(self):
        # 证据和关键词都不成立 → 必须回落语义判定（降本可以，降级不行）
        post = Postcondition(kind="assert_screen", claim="登录成功，进入后台管理首页",
                             method="llm", witness="已经下线的旧文案")
        calls = []

        class FakeLLM:
            available = True
            cfg = Cfg()

            def chat_json(self, *a, **kw):
                calls.append(1)
                return {"passed": True, "confidence": 0.95, "evidence": "首页侧边栏可见"}

        r = Judge(llm=FakeLLM()).check(post, obs("请重新登录", "安全码"))
        self.assertEqual(len(calls), 1)
        self.assertTrue(r.passed)
        self.assertEqual(r.method, "llm")

    def test_plain_assert_text_unaffected(self):
        post = Postcondition(kind="assert_text", claim="协同办公云平台", method="dsl")
        r = Judge().check(post, obs("协同办公云平台"))
        self.assertTrue(r.passed)
        self.assertEqual(r.method, "dsl")


class TestWitnessCompile(unittest.TestCase):
    def _case(self, post: Postcondition) -> CaseIR:
        case = CaseIR(case_id="w1", title="证据回放", platform="web", nl="x")
        case.steps.append(Step(step_id="a01", intent="断言：登录成功",
                               action=Action.WAIT_FOR, postcondition=post, source="ai"))
        return case

    def test_witness_rendered_into_script(self):
        code = to_python(self._case(Postcondition(
            kind="assert_screen", claim="登录成功，进入后台管理首页",
            method="llm", witness="协同办公云平台")))
        self.assertIn("witness='协同办公云平台'", code)
        self.assertIn("assert_screen", code)

    def test_no_witness_keeps_script_clean(self):
        code = to_python(self._case(Postcondition(kind="assert_text", claim="待办", method="dsl")))
        self.assertNotIn("witness=", code)

    def test_witness_survives_roundtrip(self):
        post = Postcondition(kind="assert_screen", claim="登录成功", method="llm",
                             witness="协同办公云平台")
        case = self._case(post)
        back = CaseIR.model_validate_json(case.model_dump_json())
        self.assertEqual(back.steps[0].postcondition.witness, "协同办公云平台")


class TestWitnessCapture(unittest.TestCase):
    """首跑时怎么从界面上挑证据。"""

    def _pick(self, before: Observation, after: Observation) -> str:
        from atp.agents.runner import AiRunner

        class Stub(AiRunner):
            def __init__(self):  # noqa: D401
                self.driver = type("D", (), {
                    "wait_idle": lambda self, ms: True,
                    "observe": lambda self: after,
                })()

        return Stub()._capture_witness(before)

    def test_picks_new_long_text(self):
        before = obs("登录名", "密码", "安全码", "登 录")
        after = obs("协同办公云平台", "我的待办", "退出")
        self.assertEqual(self._pick(before, after), "协同办公云平台")

    def test_ignores_dynamic_numbers(self):
        before = obs("登录名", "登 录")
        after = obs("欢迎 05115", "2026-11-10 09:30", "待办事项")
        self.assertEqual(self._pick(before, after), "待办事项")

    def test_no_new_text_returns_empty(self):
        # 挑不到就老实返回空——宁可回放多问一次模型，也不留假证据
        same = obs("登录名", "密码", "登 录")
        self.assertEqual(self._pick(same, obs("登录名", "密码", "登 录")), "")


class TestWitnessDiscriminative(unittest.TestCase):
    """证据必须区分「操作前 / 操作后」，否则它证明不了任何事。"""

    def test_witness_present_before_is_dropped(self):
        from atp.agents.executor import discriminative_post

        # 真实踩过的坑：拿登录页标题当"登录成功"的证据，
        # 结果点完登录 263ms 就"通过"，页面根本没跳。
        post = Postcondition(kind="assert_screen", claim="登录成功", method="llm",
                             witness="协同办公云平台")
        before = obs("协同办公云平台", "登 录", "忘记密码")
        fixed, dropped = discriminative_post(post, before)
        self.assertEqual(dropped, "协同办公云平台")
        self.assertEqual(fixed.witness, "")
        self.assertEqual(fixed.claim, "登录成功")  # 其它字段不能动，断言本身要保留

    def test_witness_absent_before_is_kept(self):
        from atp.agents.executor import discriminative_post

        post = Postcondition(kind="assert_screen", claim="登录成功", method="llm",
                             witness="我的待办")
        before = obs("协同办公云平台", "登 录")
        fixed, dropped = discriminative_post(post, before)
        self.assertEqual(dropped, "")
        self.assertEqual(fixed.witness, "我的待办")

    def test_no_witness_is_noop(self):
        from atp.agents.executor import discriminative_post

        post = Postcondition(kind="assert_text", claim="待办", method="dsl")
        fixed, dropped = discriminative_post(post, obs("待办"))
        self.assertEqual(dropped, "")
        self.assertIs(fixed, post)


if __name__ == "__main__":
    unittest.main()
