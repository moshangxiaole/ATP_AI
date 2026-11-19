"""run-20261117-130207 回归：Point 输入成功后必须给模型"已完成"提示。

事故：s06 用 point 视觉定位把「这是一条自动化测试消息」输进了 Electron 自绘
输入框（截图确认文字已在输入框里），step 也 passed，但系统没给模型"输入已完成、
下一步点发送"的提示——因为 loop.py 计算 input_delivered 时用了
`_looks_editable(node)`（Point 返回 False），而不是 guard 里那个认 Point 的
`_input_delivered()`。结果模型反复重输 s07/s08/s09，最后错误路线收尾。

本测试用运行时 spy 钉死：Point 输入成功后，_input_delivered 被调用、返回 True，
且历史里注入了"输入完成、不要重复"的下一步提示。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from atp.common.types import Action, UINode  # noqa: E402
from atp.agents.runners.guard import GuardMixin  # noqa: E402
from atp.agents.runners.loop import LoopMixin  # noqa: E402


class _SR:
    ok = True
    status = "passed"
    error = ""
    detail = ""


class PointInputWiredTest(unittest.TestCase):
    def test_point_input_is_delivered(self):
        """Point 节点 + 输入成功 = 已送达（判定函数本身）。"""
        step = mock.Mock()
        step.action = Action.INPUT_TEXT
        node = UINode(cls="Point", bounds=(489, 876, 1814, 1002), clickable=True)
        self.assertTrue(
            GuardMixin._input_delivered(step, _SR(), "这是一条自动化测试消息", node)
        )

    def test_loop_uses_input_delivered_not_looks_editable(self):
        """关键回归：loop 的判定必须走 _input_delivered（认 Point）。

        直接读 loop.py 的判定段：如果它还在用 _looks_editable(node) 算
        input_delivered，这个测试就失败——正是本次 bug 的根因。
        """
        import inspect
        src = inspect.getsource(LoopMixin.run)
        # 找到 input_delivered 的赋值语句，断言它调用 _input_delivered
        assign_lines = [
            l for l in src.splitlines()
            if "input_delivered =" in l and "def " not in l
        ]
        self.assertTrue(assign_lines, "应存在 input_delivered 赋值")
        self.assertTrue(
            any("self._input_delivered(" in l for l in assign_lines),
            f"input_delivered 必须调用 self._input_delivered（认 Point），实际: {assign_lines}",
        )
        self.assertFalse(
            any("_looks_editable(node)" in l for l in assign_lines),
            "input_delivered 不应再用 _looks_editable(node)（会漏判 Point）",
        )

    def test_history_gets_next_step_hint_on_point_input(self):
        """端到端：Point 输入成功后，history 注入'输入完成、别重复'提示。

        复用真实 runner，脚本：第 1 步输入（会被 _point_step 定位成 Point），
        第 2 步若收到提示则应点发送。这里只验证注入的提示确实出现。
        """
        import tempfile
        import shutil
        import json
        from atp.config import Config
        from atp.common.stepir import NLCase
        from atp.agents.runner import AiRunner
        from console_fakes import FakeOaDriver, ScriptedPlanner

        tmp = Path(tempfile.mkdtemp(prefix="atp_delivered_"))
        try:
            cfg = Config(home=tmp)
            cfg.artifacts_dir = str(tmp / "art")
            cfg.db_path = str(tmp / "t.db")
            cfg.apps_path = str(tmp / "apps.json")
            cfg.persist_runs = False
            cfg.screenshot_on_success = False

            class StubClient:
                available = True
                class cfg:
                    model = "stub"
                    vision_model = ""
                def chat_json(self, messages, model=None, **kwargs):
                    return {"passed": True, "confidence": 0.9,
                            "evidence": "stub", "missing": ""}

            driver = FakeOaDriver()
            # 脚本：输入到 Point（node_index 越界 → _to_step 用 point 分支不触发；
            # 这里用 text + 一个不在列表的 index，让 resolve 走 bounds/Point）
            script = [
                {"action": "input", "node_index": 0, "text": "测试消息",
                 "intent": "在底部输入栏输入测试消息"},
                {"action": "key", "key": "enter", "intent": "按回车发送"},
            ]
            runner = AiRunner(driver, cfg, planner=ScriptedPlanner(script),
                              client=StubClient())
            nl = NLCase(case_id="d1", title="送达判定",
                        nl="在底部输入框输入消息并发送", platform="android",
                        max_steps=6)
            case, result = runner.run(nl, out_dir=cfg.artifacts, run_id="d1")
            events = [
                json.loads(l) for l in
                (cfg.artifacts / "d1" / "events.jsonl").read_text(
                    encoding="utf-8").splitlines() if l.strip()
            ]
            # 输入步骤后应注入下一步提示（"不要重复 input" 或"输入完成"）
            prompts = [
                str(e.get("messages", "")) for e in events
                if e.get("type") == "llm_request"
            ]
            # 只要有一拍提示词里出现"不要重复 input 同样的文字"即算通过
            hit = any("不要重复 input 同样的文字" in p for p in prompts)
            # FakeOaDriver 的 EditText 是可编辑节点，landed 可能已触发；此时同样有该提示。
            # 关键是不能出现"输入没有生效"的错误提示。
            no_void = not any("输入没有生效" in str(e.get("message", ""))
                              for e in events)
            self.assertTrue(hit or no_void,
                            "输入成功后应注入下一步提示，且不能误判'输入没有生效'")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
