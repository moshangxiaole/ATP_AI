"""首跑内核：状态、事件、日志、取消、动作快照。
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
from atp.agents.perception.judge import Judge
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
class RunnerBase:
    """共享实现。"""
    def __init__(self, driver: BaseDriver, config: Config,
                 planner: Planner | None = None, client: LLMClient | None = None,
                 on_event: Callable[[dict], None] | None = None,
                 should_cancel: Callable[[], bool] | None = None):
        self.driver = driver
        self.config = config
        self.client = client or LLMClient(config.model)
        self.planner = planner or LlmPlanner(self.client)
        # 把执行日志挂到共用的模型客户端上：规划 / 修复定位器 / 语义断言 /
        # 视觉定位都走这一个 client，因此每一次调用的完整提示词与原始返回
        # 都会落到 artifacts/<run>/llm.jsonl（events.jsonl 里同步留一份）。
        if hasattr(self.client, "set_trace"):
            self.client.set_trace(self._log_event)
        self.on_event = on_event
        # 协作式取消：每一步、每次规划前后都问一次。只靠事件回调抛异常不够——
        # 一个动作可能跑十几秒、一次规划可能等几十秒，中间根本没有事件可发。
        self.should_cancel = should_cancel
        # 首跑期间临时状态：当前用例 ID + 采集到的视觉模板清单
        self._case_id = ""
        self._assets: list[dict] = []
        # 上一屏的节点规模：用来识别"页面还在白屏渲染"的半棵树
        self._ready_nodes = 0
        self._thin_ok: set[str] = set()   # 确认过"就是空页面"的指纹，别再等
        # 首跑模式：只写弹窗记忆不读、绝不自动 BACK（偏离回退只在回放端启用）
        self.mode = "ai"
        self.auto_back = False
        self._cancel_seen = False
        self._stab: PageStabilizer | None = None
        self._popup_mem: PopupMemory | None = None
        self._events: EventLog | None = None
    @property
    def profile(self) -> PlatformProfile:
        """当前端的执行策略。平台差异只从这里读，执行代码里不再出现平台名。"""
        return profile_for(getattr(self.driver, "platform", ""))
    def _gate_cancel(self) -> bool:
        """给门禁循环用的取消探针：只读主循环已确认的标志，不重复调用用户回调。"""
        return bool(self._cancel_seen)
    def _check_cancel(self) -> None:
        if self.should_cancel is None:
            return
        try:
            hit = bool(self.should_cancel())
        except Exception:  # noqa: BLE001
            return
        self._cancel_seen = hit
        if hit:
            raise RunCancelled("用户取消执行")
    def _emit(self, type_: str, **payload) -> None:
        self._log_event(type_, payload)
        if self.on_event is None:
            return
        try:
            self.on_event({"type": type_, **payload})
        except RunCancelled:
            raise
        except Exception:
            pass
    def _log_event(self, type_: str, payload: dict) -> None:
        """把执行过程写进 ``artifacts/<run>/events.jsonl``（实现见 runs.events）。
        「为什么这一步没做 / 为什么又回到上一步」这类问题，光看步骤列表答不了：
        被跳过的动作根本不产生步骤记录。有了这份日志，模型每一次决策（动作、
        意图、目标、文本）和每一条跳过原因都能事后回放。
        """
        if self._events is not None:
            self._events.write(type_, payload)
    def _gate_event(self, name: str, **payload) -> None:
        self._emit(name, **payload)
    def _popup_memory(self) -> PopupMemory | None:
        if getattr(self.config, "popup_memory", None) is None:
            return None
        if self._popup_mem is None:
            try:
                self._popup_mem = PopupMemory(self.config.popup_memory_file, self.config)
            except Exception:  # noqa: BLE001
                return None
        return self._popup_mem
    def _popup_ctx(self) -> PopupContext:
        return PopupContext(
            app_id=getattr(self, "_case_app_id", "") or "",
            platform=str(getattr(self.driver, "platform", "") or ""),
            run_id=str(getattr(self, "_case_id", "") or ""),
            mode="ai",
            enabled=True,
        )
    def _popup_event(self, ev: dict) -> None:
        kind = str(ev.get("type", "popup_dismissed") or "popup_dismissed")
        self._emit(kind, **{k: v for k, v in ev.items() if k != "type"})
    @staticmethod
    def _observation_snapshot(obs: Observation) -> dict:
        """模型的"眼睛"当时看到了什么：节点数、输入框的值、焦点、文字样本。
        尤其要记输入框 value：桌面 Electron 的 a11y 树经常只暴露占位文案，
        模型看不到自己刚输入的关键词，就会一直重复 input。这条日志能一眼确认
        到底是"驱动没读到值"还是"值确实没进去"。
        """
        try:
            nodes = list(obs.root.walk())
        except Exception:  # noqa: BLE001
            nodes = []
        values = [{"label": n.label, "value": n.value}
                  for n in nodes if getattr(n, "value", "")]
        texts = [n.text for n in nodes if (n.text or "").strip()]
        return {
            "platform": obs.platform, "width": obs.width, "height": obs.height,
            "node_count": len(nodes), "text_count": len(texts),
            "input_values": values[:12],
            "focused": [n.label for n in nodes if n.focused][:5],
            "texts": texts[:40],
        }
    @staticmethod
    def _node_snapshot(node: UINode | None) -> dict | None:
        """把"这一步到底对谁下手"记下来：报告里只有 label，排障要完整节点。"""
        if node is None:
            return None
        return {
            "cls": node.cls, "id": node.id, "text": node.text,
            "value": node.value, "desc": node.desc,
            "bounds": list(node.bounds), "clickable": node.clickable,
            "focused": node.focused,
        }
    def _action_request(self, step: Step, node: UINode | None,
                        strategy_used: str = "") -> None:
        self._log_event("action_request", {
            "step_id": step.step_id, "action": step.action.value,
            "intent": step.intent, "args": dict(step.args),
            "platform": str(getattr(self.driver, "platform", "") or ""),
            "node": self._node_snapshot(node),
            "strategies": [{"type": s.type, "value": s.value}
                           for s in step.strategies],
            "strategy_used": strategy_used,
        })
    def _action_result(self, step: Step, *, ok: bool, detail: str = "",
                       error: str = "", latency_ms: int = 0,
                       judge: str = "", score: float = 0.0) -> None:
        self._log_event("action_result", {
            "step_id": step.step_id, "action": step.action.value,
            "ok": ok, "detail": detail, "error": error,
            "latency_ms": latency_ms, "judge_method": judge, "judge_score": score,
            "evidence": {"before": step.evidence.before,
                         "after": step.evidence.after},
        })
