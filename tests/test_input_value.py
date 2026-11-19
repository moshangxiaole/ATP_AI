"""输入框当前值（value）回归：run-20261116-093006 卡在搜索栏的根因。

那次执行的截图里搜索框明明已经是「彭婉」、搜索结果也弹出来了，但界面树里
只有占位文案「搜索」——桌面 Electron 把用户输入放在 UIA ValuePattern，
驱动只读了控件 Name。于是：

    _text_landed() 恒为 False → 每步都判「输入未生效」→ 模型被教着重新定位输入框
    → 同一个 input 连发 4 次 → 判重逻辑因"树里读不到文字"把计数清零 → 熔断。

这里钉住三件事：树能读到 value、模型能在节点列表里看到 value、
树确实读不到 value 时也不允许无限重输（并明确把模型推向结果/下一步）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from atp.agents.planner import indexed_nodes  # noqa: E402
from atp.agents.platforms.pc.desktop import DesktopUiaDriver  # noqa: E402
from atp.agents.runner import AiRunner  # noqa: E402
from atp.common.stepir import LocatorStrategy, NLCase, Step, StepResult  # noqa: E402
from atp.common.types import Action, UINode  # noqa: E402
from atp.config import Config  # noqa: E402
from console_fakes import FakeLLMClient, FakeOaDriver, ScriptedPlanner  # noqa: E402

WORD = "彭婉"


def _tree(value: str = "") -> UINode:
    return UINode(cls="Root", bounds=(0, 0, 1920, 1200), children=[
        UINode(cls="Edit", id="uia://search", text="搜索", value=value,
               bounds=(363, 11, 577, 34), clickable=True, focused=True),
        UINode(cls="Button", id="uia://result", text="结果区",
               bounds=(300, 100, 700, 160), clickable=True),
    ])


class ValueVisibilityTest(unittest.TestCase):
    def test_text_landed_accepts_value(self):
        self.assertTrue(AiRunner._text_landed(_tree(WORD), WORD))
        self.assertFalse(AiRunner._text_landed(_tree(), WORD))

    def test_node_listing_shows_value(self):
        nodes, listing = indexed_nodes(_tree(WORD), focus=[WORD])
        self.assertIn(f"value={WORD!r}", listing)
        self.assertIs(nodes[0].value, WORD)

    def test_to_prompt_shows_value(self):
        self.assertIn(f"value={WORD!r}", _tree(WORD).to_prompt())

    def test_find_by_text_sees_value(self):
        self.assertEqual(len(_tree(WORD).find_by_text(WORD)), 1)


class VisualPointInputTest(unittest.TestCase):
    """run-20261116-101353 的教训：驱动报 ok + 视觉定位 ≠ 文字真的上屏。

    那次 s07 的截图前后逐字节相同——文字根本没进输入框。所以这层判定必须
    严格：point 步骤外层没有 node、也没有确认点在可编辑控件上时，绝不能只凭
    "用了 visual/bounds 策略 + sr.ok"就当作送达（否则真实失败被记成 passed，
    test_step_effect 的失败语义被破坏）。真正的送达可靠性在驱动层用剪贴板
    粘贴解决（desktop.input_text），这里只钉住客观、保守的判定。
    """

    def _step(self, *strategy_types):
        return Step(step_id="s07", intent="输入消息",
                    action=Action.INPUT_TEXT,
                    strategies=[LocatorStrategy.make(t, [240, 700, 940, 730]
                                                     if t == "bounds" else {})
                                for t in strategy_types],
                    args={"text": "这是一条自动化测试消息"})

    def _sr(self, status="passed"):
        return StepResult(step_id="s07", intent="输入消息", action="input_text",
                          status=status)

    def test_visual_point_without_editable_node_is_not_delivered(self):
        # 关键回归：视觉定位 + sr.ok，但没有可编辑 node → 不算送达。
        # 旧实现用"有 visual/bounds 策略"就放行，掩盖了 s07 那次真实失败。
        step = self._step("bounds", "visual")
        self.assertFalse(AiRunner._input_delivered(step, self._sr(),
                                                   "这是一条自动化测试消息", None))

    def test_editable_node_with_ok_is_delivered(self):
        # 只有确认点在可编辑控件上时才认送达（不依赖策略类型）
        step = self._step("bounds")
        node = UINode(cls="Edit", text="输入框", bounds=(240, 700, 940, 730),
                      desc="可编辑输入区")
        self.assertTrue(AiRunner._input_delivered(step, self._sr(),
                                                  "这是一条自动化测试消息", node))

    def test_failed_step_is_never_delivered(self):
        step = self._step("bounds", "visual")
        node = UINode(cls="Edit", text="输入框", bounds=(0, 0, 10, 10))
        self.assertFalse(AiRunner._input_delivered(step, self._sr("failed"),
                                                   "这是一条自动化测试消息", node))

    def test_empty_text_is_never_delivered(self):
        step = self._step("visual")
        node = UINode(cls="Edit", text="输入框", bounds=(0, 0, 10, 10))
        self.assertFalse(AiRunner._input_delivered(step, self._sr(), "", node))

    def test_visual_target_node_counts_as_editable(self):
        node = UINode(cls="VisualTarget", text="底部输入栏", bounds=(240, 700, 940, 730),
                      desc="图像识别命中")
        self.assertTrue(AiRunner._looks_editable(node))


class DesktopValuePatternTest(unittest.TestCase):
    def _ctrl(self):
        ctrl = mock.Mock()
        ctrl.ControlTypeName = "EditControl"
        ctrl.Name = "搜索"
        ctrl.AutomationId = "searchEdit"
        ctrl.IsKeyboardFocusable = True
        ctrl.IsEnabled = True
        ctrl.HasKeyboardFocus = True
        ctrl.BoundingRectangle = mock.Mock(left=363, top=11, right=577, bottom=34)
        ctrl.GetRuntimeId.return_value = (42,)
        ctrl.GetChildren.return_value = []
        ctrl.GetScrollPattern.side_effect = Exception("no scroll pattern")
        return ctrl

    def test_value_pattern_is_read_into_node(self):
        ctrl = self._ctrl()
        ctrl.GetValuePattern.return_value = mock.Mock(CurrentValue=WORD)
        node = DesktopUiaDriver()._to_node(ctrl, 0, [0])
        self.assertEqual(node.text, "搜索")
        self.assertEqual(node.value, WORD)

    def test_falls_back_to_legacy_pattern(self):
        ctrl = self._ctrl()
        ctrl.GetValuePattern.side_effect = AttributeError("not supported")
        ctrl.GetLegacyIAccessiblePattern.return_value = mock.Mock(CurrentValue=WORD)
        node = DesktopUiaDriver()._to_node(ctrl, 0, [0])
        self.assertEqual(node.value, WORD)


class _TreeBlindDriver(FakeOaDriver):
    """界面树永远只有占位文案「搜索」：模拟拿不到 ValuePattern 的桌面应用。"""

    platform = "desktop"

    def __init__(self):
        super().__init__()
        self.word = ""

    def _screen(self):
        return list(_tree(self.word if self.show_value else "").children)

    show_value = False

    def input_text(self, text):
        self.word = text

    def clear_text(self):
        self.word = ""


class _ValueVisibleDriver(_TreeBlindDriver):
    show_value = True


class RepeatInputTest(unittest.TestCase):
    def _run(self, driver, repeats=6):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(home=Path(tmp))
            cfg.artifacts_dir = str(Path(tmp) / "art")
            cfg.db_path = str(Path(tmp) / "t.db")
            cfg.apps_path = str(Path(tmp) / "apps.json")
            cfg.persist_runs = False
            cfg.screenshot_on_success = False
            events: list[dict] = []
            script = [{"action": "input", "node_index": 0, "text": WORD,
                       "intent": "在顶部搜索栏输入【彭婉】"}] * repeats
            with mock.patch.dict(os.environ, {"ATP_RECORD": "0"}):
                runner = AiRunner(driver, cfg, planner=ScriptedPlanner(script),
                                  client=FakeLLMClient(), on_event=lambda e: events.append(e))
                case, result = runner.run(
                    NLCase(case_id="rein-1", title="搜索彭婉",
                           nl=f"顶部搜索栏输入【{WORD}】，点击搜索结果",
                           platform="desktop", max_steps=repeats + 2),
                    out_dir=cfg.artifacts, run_id="rein-1")
            return case, result, events

    def test_value_visible_input_runs_once_and_moves_on(self):
        case, result, events = self._run(_ValueVisibleDriver())
        inputs = [s for s in result.steps if s.action == "input_text"]
        self.assertEqual(len(inputs), 1, "输入框里已经有关键词，不该重复输入")
        logs = " ".join(str(e.get("message", "")) for e in events)
        self.assertNotIn("输入未生效", logs)

    def test_tree_blind_input_is_not_repeated_forever(self):
        case, result, events = self._run(_TreeBlindDriver())
        inputs = [s for s in result.steps if s.action == "input_text"]
        self.assertLessEqual(len(inputs), 2,
                             f"树读不到值也不能无限重输，实际执行了 {len(inputs)} 次")
        logs = " ".join(str(e.get("message", "")) for e in events)
        self.assertNotIn("输入未生效", logs)
        self.assertTrue(any(k in logs for k in ("已搜索过", "已输入过同样的文字")), logs)

    def test_tree_blind_run_produces_full_diagnostics(self):
        """断言新日志能直接回答"模型看了什么、返回什么、驱动做了什么"。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(home=Path(tmp))
            cfg.artifacts_dir = str(Path(tmp) / "art")
            cfg.persist_runs = False
            cfg.screenshot_on_success = False
            with mock.patch.dict(os.environ, {"ATP_RECORD": "0"}):
                runner = AiRunner(_TreeBlindDriver(), cfg,
                                  planner=ScriptedPlanner([
                                      {"action": "input", "node_index": 0,
                                       "text": WORD, "intent": "在顶部搜索栏输入【彭婉】"},
                                  ]),
                                  client=FakeLLMClient())
                runner.run(NLCase(case_id="diag-1", title="搜索",
                                  nl="顶部搜索栏输入【彭婉】", platform="desktop",
                                  max_steps=4),
                           out_dir=cfg.artifacts, run_id="diag-1")
            art = Path(cfg.artifacts) / "diag-1"
            rows = [json.loads(l) for l in
                    (art / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            kinds = {r["type"] for r in rows}
            self.assertIn("decision", kinds)
            self.assertIn("observation", kinds)
            self.assertIn("action_request", kinds)
            self.assertIn("action_result", kinds)
            req = next(r for r in rows if r["type"] == "action_request")
            self.assertEqual(req["args"]["text"], WORD)
            self.assertEqual(req["node"]["text"], "搜索")


if __name__ == "__main__":
    unittest.main()
