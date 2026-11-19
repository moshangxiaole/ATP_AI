"""验收与自动确认。
共享实现。方法体与原 ``agents/runner.py`` 一致，只换归属文件。
"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path
from typing import Callable
from atp.common.profiles import PlatformProfile, profile_for
from atp.runs.artifacts import REPORT_FILE, STEPIR_FILE, new_run_id, run_dir
from atp.runs.events import EventLog
from atp.config import Config
from atp.agents.perception.judge import Judge, JudgeResult
from atp.agents.perception.frames import (DEFAULT_MIN_RATIO, PROGRESS_MIN_RATIO,
                                          frame_delta_ratio)
from atp.agents.perception.locator import derive_strategies, resolve
from atp.agents.perception.page_stabilizer import PageStabilizer, page_signature
from atp.memory.popups import PopupGuard, dismiss_popups
from atp.memory.popup_memory import PopupContext, PopupMemory
from atp.common.stepir import (CaseIR, LocatorStrategy, NLCase, Postcondition,
                               RunResult, Step, StepResult)
from atp.common.types import Action, Observation, RunCancelled, UINode
from atp.agents.perception.vision import capture_template, visual_strategy
from atp.agents.platforms.base import BaseDriver
from atp.agents.llm.client import LLMClient
from atp.agents.llm.vision_ground import VisionGrounder
from atp.agents.planner import (LlmHealer, LlmPlanner, Planner,  # noqa: F401
                                focus_keywords, indexed_nodes, rectify_target)
_STRONG_LOCATORS = {"a11y_id", "text", "desc", "xpath"}
_VISUAL_CLASSES = {"canvas", "svg", "img", "image", "map", "video", "webgl"}
_INTERACTION_ACTIONS = {
    Action.TAP, Action.LONG_PRESS, Action.DOUBLE_TAP, Action.INPUT_TEXT,
    Action.CLEAR_TEXT, Action.SWIPE, Action.SCROLL_TO, Action.BACK, Action.HOME,
    Action.PRESS_KEY, Action.OPEN_URL,
}
class AcceptMixin:
    """共享实现。"""

    # 「进入播放页」类场景词 + 详情页上会出现的按钮文案排除表
    _PLAY_PAGE_WORDS = ("播放页面", "播放页", "开始播放", "进入播放")
    _PLAY_BTN_EXCLUDE = ("暂停", "列表", "相关", "缓存", "倍速", "下一",
                         "直播", "预约", "会员")

    def _scene_expects_play(self, nl: NLCase) -> bool:
        text = f"{getattr(nl, 'nl', '') or ''} {getattr(nl, 'title', '') or ''}"
        return any(k in text for k in self._PLAY_PAGE_WORDS)

    def _play_button_on_screen(self, obs: Observation) -> bool:
        """界面上是否还躺着一个没点的【播放】按钮（详情页特征）。

        场景说「进入播放页面」而界面只有【播放】按钮 = 还停在详情页，
        视频没有真的开始放（run-20261117-184531：停在详情页就被判成功）。
        """
        try:
            for n in obs.root.walk():
                if not n.visible:
                    continue
                label = f"{n.text or ''}{n.desc or ''}".strip()
                if not label or "播放" not in label or "暂停" in label:
                    continue
                if any(x in label for x in self._PLAY_BTN_EXCLUDE):
                    continue
                if len(label) <= 8:      # 「播放」/「立即播放」这类短按钮文案
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False

    def _accept(self, nl: NLCase, case: CaseIR, result: RunResult, art: Path,
                history: list[str]) -> None:
        """首跑收尾的统一验收。
        「每一步的 perform 都返回 ok」只能证明手势发出去了：点击命中了某个节点、
        输入框里塞进了字。它证明不了下拉菜单真的展开了、消息真的发出了、
        页面真的跳到了目标页——线上就是这样跑出一条「1 步就成功」的假用例：
        指令要求"点【+】→ 点【钱包】→ 进入【我的钱包】"，实际只点了一次、
        菜单都没打开，却因为零失败被判通过并沉淀，回放次次失败。
        所以这里做两道闸：
        1. 结构闸（零成本）：规划必须正常收尾；整个过程必须至少有一个"实质交互"动作，
           只有启动/杀进程/最大化这种准备动作一律不算；
        2. 语义闸（一次模型调用，只在首跑）：拿最终界面问一次"场景真做完了吗"。
        用户在控制台显式给了断言时以断言为准，不再多花这一次调用。
        """
        t0 = time.perf_counter()
        n = len(result.steps) + 1
        st = StepResult(step_id="v01", intent="验收：整个场景是否真的完成",
                        action="verify", ai_used=True)
        self._emit("step_started", index=n, total=0, step_id=st.step_id,
                   intent=st.intent, action=st.action)
        obs = None
        if self.client.available:
            try:
                obs = self._settle_obs()
            except Exception:  # noqa: BLE001
                obs = None
        if obs is not None and obs.screenshot:
            name = f"{n:02d}_v01_after.png"
            try:
                (art / name).write_bytes(obs.screenshot)
                st.screenshot = name
            except OSError:
                pass
        interactions = [s for s in case.steps if s.action in _INTERACTION_ACTIONS]
        problems: list[str] = []
        if not case.steps:
            problems.append("没有执行任何步骤")
        elif not interactions:
            problems.append("整个过程只做了启动/准备动作，没有任何实际交互")
        if problems:
            st.status = "failed"
            st.error = "；".join(problems)
        elif nl.asserts:
            # 用户写下的验收标准就是标准，断言全过即视为场景完成
            st.status = "passed"
            st.detail = f"{len(nl.asserts)} 条断言全过，视为场景完成"
        elif not self.client.available:
            st.status = "passed"
            st.detail = "未配置模型：只做结构校验（规划正常收尾 + 有实际交互）"
        elif obs is None:
            st.status = "failed"
            st.error = "拿不到最终界面，无法验收场景是否完成"
        else:
            verdict = Judge(self.client).accept(nl.nl or nl.title, history, obs)
            # 语义闸的确定性补刀：模型容易把"到了详情页"合理化成"进入了播放页"，
            # 界面上还躺着【播放】按钮就说明视频没真的开始放。
            if (verdict.passed and self._scene_expects_play(nl)
                    and self._play_button_on_screen(obs)):
                verdict = JudgeResult(
                    passed=False, method="llm-accept", score=verdict.score,
                    detail="场景要求进入播放页面，但界面仍停在详情页（只有【播放】按钮），"
                           "视频没有真正开始播放")
            st.judge_method = verdict.method
            st.judge_score = verdict.score
            st.status = "passed" if verdict.passed else "failed"
            st.detail = verdict.detail
            if not verdict.passed:
                st.error = verdict.detail or "语义验收未通过"
        st.latency_ms = int((time.perf_counter() - t0) * 1000)
        result.steps.append(st)
        self._emit("step_finished", index=n, total=0, result=st.model_dump(), evidence={})
        if st.status != "passed":
            self._emit("log", level="warn", message=f"验收未通过：{st.error}")
        # 验收通过也要沉淀成「可回放的步骤」：早先只进 result.steps，回放里根本
        # 没有这一步 —— AI 首跑 9 步、回放只剩 8 步，最终画面零校验，
        # "回放通过"不代表"场景还在"。只在拿到界面证据（witness）时沉淀：
        # 回放走零成本证据自证，不依赖模型；挑不到证据就不沉淀（保持旧行为），
        # 绝不沉淀一条离线回放必失败的 llm 断言。
        if st.status == "passed" and not nl.asserts and obs is not None:
            witness = self._witness_from(obs)
            if witness:
                case.steps.append(Step(
                    step_id=st.step_id, intent="验收：整个场景是否真的完成",
                    action=Action.WAIT_FOR,
                    postcondition=Postcondition(
                        kind="assert_screen", claim=nl.nl or nl.title,
                        method="dsl", witness=witness,
                    ),
                    source="ai",
                ))
    def _probe_accept(self, nl: NLCase, history: list[str]):
        """规划中途的"完成度抽查"：只问一次模型，不落步骤、不改结果。
        和 `_accept` 的区别：那个是收尾的正式验收（会写一条 v01 步骤进结果），
        这个只用来决定"要不要把模型打回继续做"，所以要便宜、无副作用。
        """
        try:
            obs = self._settle_obs(max_wait=3.0)
        except Exception:  # noqa: BLE001
            return None
        if obs is None:
            return None
        try:
            return Judge(self.client).accept(nl.nl or nl.title, history, obs)
        except Exception as e:  # noqa: BLE001 —— 抽查失败就按"通过"处理，不影响主流程
            self._emit("log", level="warn", message=f"完成度抽查失败，按通过处理：{e}")
            return None
    def _auto_confirm(self, idx: int, node: UINode, pre_obs: Observation,
                      post_obs: Observation, case: CaseIR, result: RunResult,
                      art: Path, history: list[str]) -> Observation | None:
        """选择弹层的第二拍：选项点完了，界面没变，说明还差一个【确定】。
        为什么必须自动补：这类弹层在节点树里的"选中态"什么都不改（H5 只换个颜色），
        所以"点年假"和"什么都点不到"在观测层面完全一样。模型会以为没生效、再点一次，
        重复几次就被判定原地打转，整条用例白跑。
        误判的代价是替用户点了确认/提交，所以条件卡得很死，缺一不可：
        - 点之前**已经**有「取消 | 确定」这一条（说明我们是在弹层里选值，不是刚打开弹层）；
        - 点完之后那一条还在，且位置基本没动（**允许动画位移，不能要求像素级相等**）；
        - 点的是确认栏上方的单行短文案（选项的长相），不是确认/取消本身；
        - 选项与确认栏不在同一个滚动容器里（表单页的字段和底部按钮是同一个容器，那不能点）。
        """
        from atp.agents.perception.sheets import find_confirm_bar, refind_option, same_bar, shares_scroll_with
        bar = find_confirm_bar(post_obs)
        if bar is None or not bar.confirm.enabled:
            return None
        pre_bar = find_confirm_bar(pre_obs)
        if pre_bar is None or not same_bar(pre_bar, bar, post_obs):
            return None
        # 点之前拿到的坐标会因展开动画失效，按文案在**新树**里重新找一遍
        opt = refind_option(post_obs, node, bar)
        if opt is None:
            return None
        if shares_scroll_with(post_obs.root, opt, bar):
            return None
        step = Step(
            step_id=f"s{idx:02d}", intent=f"确认选择：点击【{bar.label}】",
            action=Action.TAP,
            strategies=derive_strategies(bar.confirm, post_obs.root,
                                         (post_obs.width, post_obs.height)),
            args={}, source="ai",
        )
        self._emit("step_started", index=idx, total=0, step_id=step.step_id,
                   intent=step.intent, action=step.action.value)
        sr = self._execute(step, bar.confirm, post_obs, art, idx)
        case.steps.append(step)
        result.steps.append(sr)
        sr.detail = " · ".join(x for x in (sr.detail, f"选择弹层自动确认【{bar.label}】") if x)
        self._emit("step_finished", index=idx, total=0, result=sr.model_dump(),
                   evidence={"before": step.evidence.before, "after": step.evidence.after})
        self._emit("sheet_confirmed", index=idx, label=bar.label)
        self._emit("log", level="info",
                   message=f"选择弹层：选项已点，自动补一拍【{bar.label}】")
        history.append(f"{step.step_id} tap 确认选择【{bar.label}】 -> OK（自动补确认）")
        return self.driver.observe()
