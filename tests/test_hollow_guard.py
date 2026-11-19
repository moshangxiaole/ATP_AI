"""空壳节点树的识别 + 零交互 done 打回 回归。

事故 run-20261117-124001/124037：示例聊天应用-测试版 的 Electron 进程 a11y 没暴露，
UIA 树只剩 8 个空 Pane（带 `156544`/`SampleDesktop` 两个框架噪声文字）。
旧的"带文字节点 <= 1"判据被噪声越过 → 空壳没被识别 → 模型看到空列表，
严格遵守提示词"找不到节点就 done"，只做了启动/最大化就收尾，验收判失败，
整条用例报废。三层修复：空壳判据（驱动/perceive/planner）+ 提示词 + 主循环打回。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from atp.common.types import UINode, Observation  # noqa: E402
from atp.agents.planner import _is_hollow_listing, _PLAN_SYSTEM, LlmPlanner  # noqa: E402
from atp.agents.runners.perceive import PerceiveMixin  # noqa: E402
from atp.agents.platforms.pc.desktop import DesktopUiaDriver  # noqa: E402


def _hollow_root():
    """run-20261117-124001 的空壳树：8 个 Pane，含两个框架噪声文字。"""
    root = UINode(cls="Desktop")
    root.children.append(UINode(cls="Pane", text="示例聊天应用"))
    root.children.append(UINode(cls="Document", text="156544"))
    for i in range(6):
        root.children.append(UINode(cls="Pane", id=f"uia://42-{i}"))
    return root


def _real_root():
    root = UINode(cls="Desktop")
    root.children.append(UINode(cls="Group", text="__layout"))
    root.children.append(UINode(cls="Edit", text="搜索"))
    root.children.append(UINode(cls="Group", text="江可欣"))
    return root


class HollowListingTest(unittest.TestCase):
    def test_hollow_tree_is_detected(self):
        root = _hollow_root()
        listing = "\n".join(
            f"[{i}] {n.text}" for i, n in enumerate(list(root.walk()))
        )
        self.assertTrue(_is_hollow_listing(listing))

    def test_real_tree_is_not_hollow(self):
        lines = []
        for i, n in enumerate(list(_real_root().walk())):
            lines.append(f"[{i}] 静态 cls={n.cls} text={n.text!r}")
        self.assertFalse(_is_hollow_listing("\n".join(lines)))

    def test_perceive_hollow_ignores_framework_noise(self):
        obs = Observation(root=_hollow_root(), screenshot=b"x", platform="desktop",
                          device_id="desktop", width=1920, height=1200)
        self.assertTrue(PerceiveMixin._hollow_obs(obs))

    def test_perceive_not_hollow_on_business_nodes(self):
        obs = Observation(root=_real_root(), screenshot=b"x", platform="desktop",
                          device_id="desktop", width=1920, height=1200)
        self.assertFalse(PerceiveMixin._hollow_obs(obs))

    def test_driver_hollow_ignores_noise(self):
        self.assertTrue(DesktopUiaDriver._hollow(_hollow_root()))
        self.assertFalse(DesktopUiaDriver._hollow(_real_root()))


class ZeroInteractionDoneTest(unittest.TestCase):
    """零交互 done 必须被打回继续执行，而不是放行到收尾验收。"""

    def setUp(self):
        import tempfile
        import shutil as _sh
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_hollow_"))
        self.cfg = __import__("atp.config", fromlist=["Config"]).Config(home=self.tmp)
        self.cfg.artifacts_dir = str(self.tmp / "art")
        self.cfg.db_path = str(self.tmp / "t.db")
        self.cfg.apps_path = str(self.tmp / "apps.json")
        self.cfg.persist_runs = False
        self.cfg.screenshot_on_success = False

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_done_without_interaction_is_sent_back(self):
        from unittest import mock
        from atp.agents.runner import AiRunner
        from atp.common.stepir import NLCase
        from console_fakes import FakeOaDriver, ScriptedPlanner

        class StubClient:
            available = True
            class cfg:
                model = "stub"
                vision_model = ""
            def chat_json(self, messages, model=None, **kwargs):
                return {"passed": True, "confidence": 0.9,
                        "evidence": "stub", "missing": ""}

        driver = FakeOaDriver()
        hollow = _hollow_root()
        # 脚本：第 1 步就 done（模拟事故），之后给一次真实交互动作
        script = [
            {"action": "done", "done_reason": "节点列表为空，界面坏了"},
            {"action": "tap", "node_index": 0, "intent": "点顶部搜索框"},
        ]
        sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
        runner = AiRunner(driver, self.cfg, planner=ScriptedPlanner(script),
                          client=StubClient())
        nl = NLCase(case_id="hollow", title="空壳回归",
                    nl="打开应用并点搜索框", platform="android", max_steps=8)
        with mock.patch.object(driver, "dump_tree", return_value=hollow):
            case, result = runner.run(nl, out_dir=self.cfg.artifacts, run_id="hollow1")
        events = []
        import json
        for line in (self.cfg.artifacts / "hollow1" / "events.jsonl").read_text(
                encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except Exception:
                pass
        sent_back = [e for e in events if "零交互" in str(e.get("message", ""))]
        self.assertTrue(sent_back, "零交互 done 必须触发打回日志")
        # 打回生效的证据：脚本的第二步（tap）被消费了——空壳树上 tap 会被守卫
        # 拦下（不是合法目标），但"尝试了 tap"证明循环没有停在第一次 done。
        tried_second = any(
            "跳过非法动作 tap" in str(e.get("message", "")) for e in events
        ) or any(s.action.value == "tap" for s in case.steps)
        self.assertTrue(tried_second, "打回后应继续执行脚本第二步，而不是停在 done")
        self.assertGreaterEqual(len(sent_back), 1)


class PromptTest(unittest.TestCase):
    def test_prompt_does_not_tell_model_to_done_on_missing_node(self):
        self.assertNotIn("找不到合适节点就用 done", _PLAN_SYSTEM)
        self.assertIn("绝不能用 done", _PLAN_SYSTEM)
        self.assertIn("action=point", _PLAN_SYSTEM)

    def test_prompt_embeds_hollow_hint(self):
        """空壳树时用户消息里要注入"界面正常、用 point、别 done"。"""
        class FakeClient:
            def chat_json(self, messages, **kw):
                self.messages = messages
                return {"action": "point", "target": "顶部搜索框"}

        client = FakeClient()
        obs = Observation(root=_hollow_root(), screenshot=b"x", platform="desktop",
                          device_id="desktop", width=1920, height=1200)
        from atp.common.stepir import NLCase
        nl = NLCase(nl="打开应用搜索江可欣", title="搜索用例", case_id="t", platform="desktop")
        LlmPlanner(client).next(nl, obs, [])
        user = client.messages[1]["content"]
        self.assertIn("节点树读不出内容", user)
        self.assertIn("绝不能用 done", user)
        self.assertIn("action=point", user)


if __name__ == "__main__":
    unittest.main()
