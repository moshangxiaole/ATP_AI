"""步骤完成校验 / 重复动作识别回归（run-20261113-173646 教训）。

那场 PC 端执行一直"定位并点击顶部搜索栏"，连点三次、每步都记 passed，
最后撞上模型 429 才结束。三个真实缺陷：

1. **判重只看坐标**：视觉定位每拍给出的框都会抖几像素（三次点击的 bounds
   各不相同），动作签名每拍都"新"，重复计数器永远数不到 → 无限重复；
2. **不校验这一步有没有效果**：点了没反应也记 passed，模型看不到"没效果"这个
   事实，只能一遍遍重试；
3. **input 没有节点就被静默丢弃**：模型想"在搜索栏输入"，树上没有对应节点，
   `_to_step` 直接返回 None，模型只收到"跳过非法动作 input"，于是回头接着点。

这里逐条钉住。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from atp.agents.runner import AiRunner  # noqa: E402
from atp.config import Config  # noqa: E402
from atp.common.stepir import NLCase, Step  # noqa: E402
from atp.common.types import UINode  # noqa: E402
from console_fakes import FakeOaDriver, ScriptedPlanner  # noqa: E402


class _GroundingClient:
    """假多模态定位器：每次给出的框都抖几像素——真实模型就是这样。"""

    available = True
    vision_model = "stub-vision"

    class cfg:  # noqa: N801
        model = "stub"
        vision_model = "stub-vision"
        vision = True

    def __init__(self, rect=(0.20, 0.05, 0.30, 0.09), jitter=0.004):
        # 默认框落在顶部标题栏上：假界面里点击标题栏不会改变任何状态，
        # 正好模拟"点了等于没点"（真实事故里的搜索栏点击就是这种）。
        self.rect = list(rect)
        self.jitter = jitter
        self.calls = 0

    def chat_json(self, messages, model=None, **kwargs):
        self.calls += 1
        dx = self.jitter * self.calls
        x1, y1, x2, y2 = self.rect
        # 同时满足两种调用方：视觉定位要 found/rect，判定层要 passed/confidence
        return {"found": True, "rect": [x1 + dx, y1, x2 + dx, y2],
                "confidence": 0.9, "reason": "stub",
                "passed": True, "evidence": "stub", "missing": ""}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_effect_"))
        self.cfg = Config(home=self.tmp)
        self.cfg.artifacts_dir = str(self.tmp / "art")
        self.cfg.db_path = str(self.tmp / "t.db")
        self.cfg.apps_path = str(self.tmp / "apps.json")
        self.cfg.persist_runs = False
        self.cfg.screenshot_on_success = False

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, script, driver=None, client=None, nl=None, events=None):
        runner = AiRunner(driver or FakeOaDriver(), self.cfg,
                          planner=ScriptedPlanner(script), client=client or _GroundingClient())
        if events is not None:
            real_emit = runner._emit

            def _emit(kind, **payload):
                events.append({"type": kind, **payload})
                return real_emit(kind, **payload)

            runner._emit = _emit
        return runner.run(nl or NLCase(case_id="eff-1", title="点搜索栏",
                                       nl="点击顶部搜索栏", platform="desktop",
                                       max_steps=20),
                          out_dir=self.cfg.artifacts, run_id="t1")


class JitteredPointRepeatTest(_Base):
    def test_same_target_with_jittered_rects_is_treated_as_repeat(self):
        """坐标抖动的同一个目标必须算重复：否则每次都是"新动作"，永远停不下来。

        第一次执行后界面毫无变化，于是第 2 次起就应该**跳过执行**，
        跳过 3 次即判原地打转中止——而不是一路点到模型限流。
        """
        script = [{"action": "point", "target": "顶部搜索栏",
                   "intent": "点击顶部搜索栏以聚焦输入"}] * 6
        events: list[dict] = []
        case, result = self._run(script, events=events)

        taps = [s for s in result.steps if s.action == "tap"]
        self.assertLessEqual(len(taps), 2,
                             f"抖动坐标不该被当成新动作反复执行: {len(taps)} 次")
        errors = " ".join(s.error or "" for s in result.steps)
        self.assertIn("原地打转", errors, errors)
        self.assertTrue(any("已跳过" in str(e.get("message", "")) for e in events),
                        "跳过重复动作时必须有日志，便于排查")


class StepEffectTest(_Base):
    def test_history_tells_model_when_nothing_changed(self):
        """点了但界面没变化，回给模型的历史必须说清楚"没有产生效果"。

        只说 OK 的话，模型以为点中了，会一直重复同一个动作。
        """
        lines: list[str] = []

        class _Recorder(ScriptedPlanner):
            def next(self, nl, obs, history):
                lines.extend(history)
                return super().next(nl, obs, history)

        script = [{"action": "point", "target": "空白区域", "intent": "点击空白处"}]
        runner = AiRunner(FakeOaDriver(), self.cfg,
                          planner=_Recorder(script), client=_GroundingClient())
        runner.run(NLCase(case_id="eff-2", title="点空白", nl="点一下空白处",
                          platform="desktop", max_steps=6),
                   out_dir=self.cfg.artifacts, run_id="t2")
        joined = "\n".join(lines)
        self.assertIn("界面没有任何变化", joined,
                      f"历史里必须写明这一步没有效果，实际: {joined[:400]}")

    def test_result_detail_marks_no_change(self):
        """报告里也要能看出来这步"点了但没反应"，否则人工排查只能靠猜。"""
        case, result = self._run(
            [{"action": "point", "target": "空白区域", "intent": "点击空白处"}])
        self.assertTrue(result.steps)
        self.assertIn("界面无变化", result.steps[0].detail,
                      result.steps[0].detail)


class PointInputTest(_Base):
    def test_input_without_node_uses_vision_instead_of_being_dropped(self):
        """模型发 action=input 但树上没有节点时，必须走视觉定位去输入，
        不能被 `_to_step` 静默丢掉（模型会以为输入没做，回头继续点搜索栏）。"""
        case, result = self._run(
            [{"action": "input", "target": "顶部搜索栏", "text": "联系人乙",
              "intent": "在顶部搜索栏输入联系人乙"}])
        actions = [s.action for s in result.steps]
        self.assertIn("input_text", actions,
                      f"input 被丢掉了，实际执行: {actions}")
        dropped = [s for s in result.steps
                   if "跳过非法动作" in (s.intent or "") + (s.error or "")]
        self.assertFalse(dropped, [s.model_dump() for s in dropped])


class FocusedEditableTest(_Base):
    def test_tap_on_already_focused_input_is_skipped(self):
        """点一个已经聚焦的输入框不会有任何变化，说明这步早就完成了：
        直接跳过并提示去 input，而不是让模型一遍遍点。"""
        driver = FakeOaDriver()
        root = driver.dump_tree()
        root.children.append(UINode(cls="Edit", id="uia://edit-1", text="搜索",
                                    bounds=(40, 900, 500, 950), focused=True))
        idx = len(root.children) - 1
        script = [{"action": "tap", "node_index": idx,
                   "intent": "点击顶部搜索栏以聚焦输入"}] * 4
        with mock.patch.object(driver, "dump_tree", return_value=root):
            case, result = self._run(script, driver=driver)

        taps = [s for s in result.steps if s.action == "tap"]
        self.assertEqual(taps, [], f"已聚焦的输入框不该再点: {[s.step_id for s in taps]}")
        errors = " ".join(s.error or "" for s in result.steps)
        self.assertIn("反复点击同一位置", errors, errors)


class SettleShotTest(_Base):
    def test_waits_for_stable_frame_before_capture(self):
        """拍"这一步之后"的图要等画面稳下来：固定 sleep 150ms 会拍到动画中间帧，
        拿它判"有没有效果"必然判错（run-20261113-180724 的 s10 差 8.2%）。"""
        import io

        from PIL import Image, ImageDraw

        def frame(block: bool, offset: int = 0) -> bytes:
            img = Image.new("RGB", (400, 300), "#F5F7FA")
            if block:
                ImageDraw.Draw(img).rectangle([20 + offset, 20, 380, 280], fill="#333333")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

        anim1, anim2, still = frame(True, 0), frame(True, 40), frame(False)

        class _SettleDriver(FakeOaDriver):
            """前两拍还在动（动画），之后稳定。"""

            def __init__(self):
                super().__init__()
                self.shots = [anim1, anim2, still, still, still]
                self.n = 0

            def screenshot(self):
                i = min(self.n, len(self.shots) - 1)
                self.n += 1
                return self.shots[i]

        runner = AiRunner(_SettleDriver(), self.cfg,
                          planner=ScriptedPlanner([]), client=_GroundingClient())
        t0 = time.monotonic()
        shot = runner._settle_shot(max_wait_ms=800, quiet_ms=10)
        elapsed = time.monotonic() - t0
        self.assertEqual(shot, still, "必须拿到稳定后的那一帧，而不是第一帧动画中间态")
        self.assertLess(elapsed, 0.8, f"稳定后应立即返回，实际 {elapsed:.2f}s")


class SameTargetTest(unittest.TestCase):
    """同一个输入框被两种方式框住时，不能当成两个目标。"""

    class _Obs:
        width, height = 1920, 1200

    def test_whole_row_and_inner_box_are_same_target(self):
        # run-20261113-180724 的 s07/s08：整行框住 vs 只框住文本框内部
        row = (250 / 1920 * 1000, 690 / 1200 * 1000, 870 / 1920 * 1000, 750 / 1200 * 1000)
        inner = (247 / 1920 * 1000, 694 / 1200 * 1000,
                 1000 / 1920 * 1000, 705 / 1200 * 1000)
        self.assertTrue(AiRunner._same_target(row, inner, self._Obs))

    def test_jittered_points_are_same_target(self):
        a = (350 / 1920 * 1000, 30 / 1200 * 1000, 580 / 1920 * 1000, 60 / 1200 * 1000)
        b = (351 / 1920 * 1000, 10 / 1200 * 1000, 596 / 1920 * 1000, 40 / 1200 * 1000)
        self.assertTrue(AiRunner._same_target(a, b, self._Obs))

    def test_different_rows_are_not_same_target(self):
        a = (250 / 1920 * 1000, 690 / 1200 * 1000, 870 / 1920 * 1000, 750 / 1200 * 1000)
        b = (250 / 1920 * 1000, 800 / 1200 * 1000, 870 / 1920 * 1000, 860 / 1200 * 1000)
        self.assertFalse(AiRunner._same_target(a, b, self._Obs))


class NullTextTest(_Base):
    def test_input_with_null_text_degrades_to_tap(self):
        """模型给 `"text": null` 时，`str(None)` 会把字面量 "None" 打进输入框
        （run-20261113-180724 的 s04 真的输入了 None）。必须退化成点击。"""
        script = [{"action": "input", "node_index": 2, "text": None,
                   "intent": "点击搜索结果进入会话"}]
        case, result = self._run(script)
        self.assertTrue(case.steps)
        step = case.steps[0]
        self.assertEqual(step.action.value, "tap", f"不该执行输入: {step.action}")
        self.assertNotIn("None", str(step.args), "绝不能把 None 当文本输入")


class CommitDuplicateTest(_Base):
    def test_second_send_with_same_intent_is_skipped(self):
        """连点两次【发送】：第一次已经成功、界面也变了，第二次是纯重复
        （run-20261113-180724 的 s10/s11），必须跳过。"""
        driver = FakeOaDriver()
        driver.state = "home"
        script = [{"action": "tap", "node_index": 2,
                   "intent": "点击【发送】按钮，把已输入的消息发出去"}] * 3
        case, result = self._run(script, driver=driver)
        taps = [s for s in result.steps if s.action == "tap"]
        self.assertEqual(len(taps), 1, f"提交类按钮不该点第二遍: {len(taps)} 次")
        self.assertTrue(result.passed, "跳过重复不应把整场判失败")

    def test_non_commit_repeat_is_still_allowed(self):
        """非提交类动作（翻页/连续操作）重复执行必须放行，
        否则会把正常的多页操作误杀。"""
        script = [{"action": "tap", "node_index": 2, "intent": "点击下一页查看下一页"},
                  {"action": "tap", "node_index": 2, "intent": "点击下一页查看第三页"},
                  {"action": "tap", "node_index": 2, "intent": "点击下一页查看第四页"}]
        driver = FakeOaDriver()
        driver.state = "home"
        case, result = self._run(script, driver=driver)
        taps = [s for s in result.steps if s.action == "tap"]
        self.assertEqual(len(taps), 3, f"翻页类重复不该被拦: {len(taps)} 次")


class InputLandedTest(_Base):
    def test_typed_text_visible_in_tree_counts_as_done(self):
        """输入两三个字只改几十个像素，按像素判会误判成"没效果"；
        文字真的出现在界面树上才是完成的硬证据。"""
        script = [{"action": "input", "node_index": 2, "text": "测试员",
                   "intent": "在用户名输入框输入测试员"}]
        case, result = self._run(script)
        self.assertTrue(result.steps)
        self.assertIn("已确认文字上屏", result.steps[0].detail,
                      result.steps[0].detail)


class SearchRegressionTest(_Base):
    """run-20261113-182603：已经进入会话，却因为搜索框被应用清空而回头再搜。"""

    def test_tapping_search_box_again_after_search_is_blocked(self):
        driver = FakeOaDriver()
        script = [
            # 1) 搜索框输入关键词（假界面里 et_username 是唯一可输入框）
            {"action": "input", "node_index": 2, "text": "彭婉",
             "intent": "在顶部搜索框输入【彭婉】"},
            # 2) 点了结果、进了会话，应用把搜索框清空了 → 模型以为没搜，回头又点
            {"action": "tap", "node_index": 2,
             "intent": "点击顶部搜索框准备输入【彭婉】"},
        ]
        events: list[dict] = []
        case, result = self._run(script, driver=driver, events=events)
        taps = [s for s in result.steps if s.action == "tap"]
        self.assertEqual(taps, [], f"搜索完成后不该再点搜索框: {[s.step_id for s in taps]}")
        msgs = [str(e.get("message", "")) for e in events]
        self.assertTrue(any("已搜索过" in m for m in msgs), msgs)
        self.assertTrue(any("清空是正常的" in m for m in msgs),
                        "必须说明搜索框被清空是正常现象，否则模型还会再试一次")

    def test_new_keyword_is_still_allowed(self):
        """换一个关键词搜索是正常操作，不能被"已搜索"闸门拦住。"""
        driver = FakeOaDriver()
        script = [
            {"action": "input", "node_index": 2, "text": "彭婉",
             "intent": "在顶部搜索框输入【彭婉】"},
            {"action": "input", "node_index": 2, "text": "联系人乙",
             "intent": "在顶部搜索框输入【联系人乙】"},
        ]
        case, result = self._run(script, driver=driver)
        inputs = [s for s in result.steps if s.action == "input_text"]
        self.assertGreaterEqual(len(inputs), 2, f"新关键词应允许搜索: {len(inputs)}")


class ProgressRecapTest(_Base):
    def test_recap_is_given_when_a_step_has_no_effect(self):
        """没效果的步骤之后要附上"已完成清单"，模型才不至于忘了进度往回到前面。"""
        lines: list[str] = []

        class _Recorder(ScriptedPlanner):
            def next(self, nl, obs, history):
                lines.extend(history)
                return super().next(nl, obs, history)

        script = [
            {"action": "input", "node_index": 2, "text": "彭婉",
             "intent": "在顶部搜索框输入【彭婉】"},
            {"action": "point", "target": "标题栏空白处", "intent": "点击标题栏"},
        ]
        runner = AiRunner(FakeOaDriver(), self.cfg,
                          planner=_Recorder(script), client=_GroundingClient())
        runner.run(NLCase(case_id="recap-1", title="回顾", nl="先输入再点空白",
                          platform="desktop", max_steps=6),
                   out_dir=self.cfg.artifacts, run_id="t3")
        joined = "\n".join(lines)
        self.assertIn("【进度回顾】", joined, joined[-400:])
        self.assertIn("在顶部搜索框输入【彭婉】", joined, "回顾里要点出已经完成的那一步")


class EventLogTest(_Base):
    def test_events_jsonl_records_every_decision(self):
        """被跳过的动作不产生步骤记录，只能靠执行日志还原"为什么又回到上一步"。"""
        import json

        script = [{"action": "input", "node_index": 2, "text": "彭婉",
                   "intent": "在顶部搜索框输入【彭婉】"}]
        self._run(script)
        path = Path(self.cfg.artifacts) / "t1" / "events.jsonl"
        self.assertTrue(path.exists(), "每次执行都必须留下 events.jsonl")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        types = [r["type"] for r in rows]
        self.assertIn("decision", types, "模型决策必须落盘")
        self.assertIn("step_started", types)
        self.assertIn("step_finished", types)
        dec = next(r for r in rows if r["type"] == "decision")
        self.assertEqual(dec.get("text"), "彭婉")
        self.assertIn("intent", dec)


class PrematureDoneTest(_Base):
    def test_done_is_pushed_back_when_acceptance_fails(self):
        """模型声称完成但验收不通过时，要把它打回继续做，而不是直接收场
        （run-20261113-182603：消息没发就 done，最终验收失败）。"""
        from atp.agents.perception.judge import Judge, JudgeResult

        driver = FakeOaDriver()
        driver.state = "home"
        script = [
            {"action": "tap", "node_index": 2, "intent": "打开待办列表"},
            {"action": "done", "done_reason": "已经做完了"},
            {"action": "tap", "node_index": 3, "intent": "打开消息列表"},
        ]
        bad = JudgeResult(passed=False, method="llm-accept", score=0.2,
                          detail="搜索栏没有输入关键词")
        good = JudgeResult(passed=True, method="llm-accept", score=0.9, detail="ok")
        events: list[dict] = []
        with mock.patch.object(Judge, "accept", side_effect=[bad, good, good]):
            case, result = self._run(script, driver=driver, events=events)

        taps = [s for s in result.steps if s.action == "tap"]
        self.assertEqual(len(taps), 2,
                         f"被打回后应继续执行后续动作，实际: {[s.step_id for s in taps]}")
        self.assertTrue(any("验收不通过" in str(e.get("message", "")) for e in events),
                        "打回时必须说明原因")
        self.assertTrue(result.passed, [s.error for s in result.steps if s.error])


class ContainerInputTest(_Base):
    def test_input_into_whole_window_container_is_refused(self):
        """run-20261113-202528 的 s06：模型选中一块覆盖大半个窗口的容器，
        往里输入消息——按键落到别处，界面一动不动，后面全乱。
        这种"往容器里打字"必须在执行前拦住。"""
        driver = FakeOaDriver()
        root = UINode(cls="Root", bounds=(0, 0, 540, 960), children=[
            UINode(cls="Pane", id="uia://big", text="",
                   bounds=(20, 30, 520, 940)),          # 覆盖 90% 面积的容器
        ])
        events: list[dict] = []
        with mock.patch.object(driver, "dump_tree", return_value=root):
            case, result = self._run(
                [{"action": "input", "node_index": 0, "text": "这是一条自动化测试消息",
                  "intent": "在会话底部可编辑输入区输入消息"}],
                driver=driver, events=events)
        self.assertEqual([s for s in result.steps if s.action == "input_text"], [],
                         "不该往整块容器里输入文本")
        msgs = [str(e.get("message", "")) for e in events]
        self.assertTrue(any("拒绝向其输入文本" in m for m in msgs), msgs)


class PanicHomeTest(_Base):
    def test_home_used_as_restart_button_is_blocked(self):
        """模型一慌就回桌面重来（s08/s09），会把已进入的页面和已填内容全丢掉。"""
        events: list[dict] = []
        case, result = self._run(
            [{"action": "home", "intent": "当前界面异常，无法定位到示例聊天应用应用，返回桌面重来"}],
            events=events)
        self.assertEqual([s for s in result.steps if s.action == "home"], [],
                         "不该把 home 当重启按钮用")
        msgs = [str(e.get("message", "")) for e in events]
        self.assertTrue(any("回桌面重来" in m for m in msgs), msgs)


class InputVoidTest(_Base):
    def test_input_that_takes_effect_nowhere_is_marked_failed(self):
        """输入后界面既没有文字也没有任何变化 = 打空炮，必须判失败并说清原因，
        而不是记 passed 让模型以为已经输好了。"""
        case, result = self._run(
            [{"action": "input", "target": "消息会话底部的输入框",
              "text": "这是一条自动化测试消息", "intent": "在底部输入栏输入消息"}])
        self.assertTrue(result.steps)
        sr = result.steps[0]
        self.assertEqual(sr.status, "failed", sr.model_dump())
        self.assertIn("输入没有生效", sr.error)
        self.assertIn("输入未生效", sr.detail)


class HistoryLineUnitTest(unittest.TestCase):
    def test_repeat_hint_targets_input_when_text_like(self):
        step = Step(step_id="s01", intent="点击顶部搜索栏以聚焦输入",
                    action="tap", args={"text": ""})
        hint = AiRunner._repeat_hint(step, "tap")
        self.assertIn("action=input", hint,
                      "对输入类目标必须直接告诉模型去输入，而不是泛泛地说换一种做法")

    def test_repeat_hint_generic_for_non_input(self):
        step = Step(step_id="s01", intent="点击右上角的头像", action="tap", args={})
        hint = AiRunner._repeat_hint(step, "tap")
        self.assertIn("跳过重复执行", hint)


class NavigationHistoryTest(_Base):
    def test_progressed_navigation_tells_model_page_switched(self):
        """导航类动作（进入/打开/点结果）成功后，历史必须写明"页面已切换"。

        run-20261113-211702：s05 已点进彭婉会话，历史只有一行 OK，模型不知道
        页面切了，还在找"搜索结果"直到被熔断。
        """
        lines: list[str] = []

        class _Recorder(ScriptedPlanner):
            def next(self, nl, obs, history):
                lines.extend(history)
                return super().next(nl, obs, history)

        class _PageSwitchDriver(FakeOaDriver):
            """点结果行后整屏内容都换（模拟真的切到了新页面）。"""
            _WORDS = [
                "search result page with contact list and filter bar shown",
                "chat conversation page with message bubbles and input box",
                "profile card page with avatar nickname and action buttons",
            ]

            def __init__(self):
                super().__init__()
                self.hits = 0

            def _screen(self):
                from console_fakes import node
                word = self._WORDS[min(self.hits, len(self._WORDS) - 1)]
                # 蓝底大色块 ↔ 白底大色块：整块区域都变，帧差远超"实质推进"阈值
                cls = "Button" if self.hits % 2 == 0 else "Panel"
                return [node("Item", "item_result", "彭婉", (20, 20, 300, 80),
                             clickable=True),
                        node(cls, "tv_page", word, (0, 100, 540, 700),
                             clickable=True),
                        node("Text", "tv_foot", "bottom fixed text", (0, 720, 540, 780)),
                        node("Text", "tv_foot2", "second bottom line", (0, 800, 540, 860))]

            def tap_xy(self, x, y):
                t = self.dump_tree().find_smallest_at(x, y)
                if t is not None and t.id.endswith("item_result"):
                    self.hits += 1
                    return
                super().tap_xy(x, y)

        runner = AiRunner(_PageSwitchDriver(), self.cfg,
                          planner=_Recorder([{"action": "tap", "node_index": 0,
                                              "intent": "点击搜索结果进入彭婉会话"}]),
                          client=_GroundingClient())
        runner.run(NLCase(case_id="nav", title="进会话", nl="点击搜索结果进入会话",
                          platform="desktop", max_steps=4),
                   out_dir=self.cfg.artifacts, run_id="nav")
        self.assertTrue(any("界面已切换" in l for l in lines),
                        f"历史里应写明页面已切换，实际: {lines}")


class ReInputAfterClearTest(_Base):
    def test_reinput_allowed_when_text_cleared_by_navigation(self):
        """进入会话后搜索框被自动清空，此时重新输入同一关键词是合法操作，
        不能按"输过同样的词"拦下（run-20261113-211702 里模型想重搜被跳过）。"""
        from console_fakes import node as mknode

        class _AutoClearDriver(FakeOaDriver):
            def __init__(self):
                super().__init__()
                self.word = ""

            def _screen(self):
                return [mknode("Edit", "et_search", self.word, (20, 20, 300, 60),
                               clickable=True),
                        mknode("Button", "btn_enter", "进入会话",
                               (20, 140, 200, 180), clickable=True)]

            def input_text(self, text):
                self.word = text

            def tap_xy(self, x, y):
                t = self.dump_tree().find_smallest_at(x, y)
                if t is not None and t.id.endswith("btn_enter"):
                    self.word = ""      # 切页后搜索框被自动清空
                    return
                super().tap_xy(x, y)

        script = [
            {"action": "input", "text": "彭婉", "node_index": 0,
             "intent": "在搜索框输入彭婉"},
            {"action": "tap", "node_index": 1, "intent": "进入会话"},
            {"action": "input", "text": "彭婉", "node_index": 0,
             "intent": "重新搜索彭婉"},
        ]
        case, result = self._run(
            script, driver=_AutoClearDriver(),
            nl=NLCase(case_id="rein", title="清空重输", nl="搜索后进入再重搜",
                      platform="desktop", max_steps=8))
        inputs = [s for s in result.steps if s.action == "input_text"]
        self.assertEqual(len(inputs), 2,
                         f"清空后的重新输入必须执行，实际步骤: "
                         f"{[(s.action, s.status, s.error) for s in result.steps]}")


class DesktopSwipeGateTest(_Base):
    def test_desktop_allows_swipe_without_scroll_pattern(self):
        """桌面 UIA 树对 Electron 容器经常不报 scrollable；桌面 swipe 已实现为
        鼠标滚轮、无副作用，必须放行（run-20261113-211702 被闸拦死 3 次熔断），
        滚没滚交给"步骤效果校验"判断。"""
        drv = FakeOaDriver()
        drv.platform = "desktop"
        runner = AiRunner(drv, self.cfg, planner=ScriptedPlanner([]),
                          client=_GroundingClient())
        self.assertTrue(runner._page_scrollable(drv.observe()),
                        "桌面端即使树里没有 scrollable 容器也必须放行 swipe")


if __name__ == "__main__":
    unittest.main()
