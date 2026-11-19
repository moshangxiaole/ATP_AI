"""回放内核：状态、事件、取消、落库。

共享实现。方法体与原 ``agents/executor.py`` 一致，只换归属文件。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Protocol

from atp.common.profiles import PlatformProfile, profile_for
from atp.runs.artifacts import REPORT_FILE, STEPIR_FILE, new_run_id, run_dir
from atp.config import Config
from atp.agents.platforms.base import BaseDriver
from atp.db.store import CaseDB
from atp.memory.apps import looks_like_package
from atp.agents.perception.deviation import BackController, DeviationDetector
from atp.agents.perception.judge import Judge, JudgeResult, text_corpus
from atp.agents.perception.locator import LocateResult, derive_strategies, describe, occluded_by, resolve
from atp.agents.perception.page_stabilizer import PageFrame, PageStack, PageStabilizer, page_signature
from atp.memory.popups import PopupGuard, dismiss_popups
from atp.memory.popup_memory import PopupContext, PopupMemory
from atp.agents.perception.scroller import find_by_scrolling, trail_text
from atp.common.stepir import (CaseIR, LocatorStrategy, Postcondition, RunResult,
                               ScreenProfile, Step, StepResult)
from atp.common.types import Action, Observation, RunCancelled
from atp.agents.perception.vision import VisualLocator, denorm_rect, frame_diff_ratio, make_point_node

def discriminative_post(post: Postcondition, before: Observation) -> tuple[Postcondition, str]:
    """把"证明不了任何事"的断言证据剔掉。

    证据的全部价值在于「它只在成功之后才出现」。如果这段文本在动作之前就已经在
    界面上，它就区分不了前后状态——线上真实踩过：拿登录页的标题"协同办公云平台"
    当"登录成功"的证据，结果点完登录 263ms 就"通过"了，页面根本没跳。

    宁可回落到语义判定多花一次模型钱，也不能让假阳性通过。
    """
    if not post.witness:
        return post, ""
    if post.witness not in text_corpus(before.root):
        return post, ""
    return post.model_copy(update={"witness": ""}), post.witness

class Healer(Protocol):
    def heal(self, step: Step, obs: Observation, case: CaseIR) -> list[LocatorStrategy] | None: ...

EventSink = Callable[[dict], None]

class ExecutorBase:
    """共享实现。"""

    def __init__(
        self,
        driver: BaseDriver,
        config: Config | None = None,
        judge: Judge | None = None,
        healer: Healer | None = None,
        run_root: Path | None = None,
        on_event: EventSink | None = None,
        should_cancel: Callable[[], bool] | None = None,
        use_popup_memory: bool = True,
    ):
        self.driver = driver
        self.config = config or Config()
        self.judge = judge or Judge()
        self.healer = healer
        self.run_root = run_root or self.config.artifacts
        self.ai_budget = self.config.ai_step_budget
        # 观测钩子：Web 控制台靠它把每一步实时推给前端。
        # 回调抛异常视为"请求中止执行"（用于取消），其余异常一律吞掉，不能影响跑用例。
        self.on_event = on_event
        # 协作式取消。光靠事件回调抛异常是不够的：一步动作可能耗十几秒
        # （等界面稳定、滑动查找、断言轮询），这期间一个事件都不会发，
        # 所以取消标志必须由执行循环自己轮询，才谈得上"点了就停"。
        self.should_cancel = should_cancel
        # 视觉兜底：只在树定位全 miss 时才会被真正调用，不命中不花钱
        self.visual = VisualLocator(self.config.cases, self.config.vision) \
            if getattr(self.config, "vision", None) and self.config.vision.enabled else None
        # 弹窗自动处置：真机跑 App 绕不过去的一层
        pc = getattr(self.config, "popup", None)
        self.popup = PopupGuard(
            extra_close_words=list(getattr(pc, "extra_close_words", []) or []),
            limit=getattr(pc, "max_per_step", 3) or 3,
        ) if (pc is None or pc.enabled) else None
        self._popup_settle = getattr(pc, "settle_ms", 600) if pc else 600
        self._screen = ScreenProfile()
        self._db: CaseDB | None = None
        # 应用台账：名字 → 包名，第一次查完就落到 apps.json
        self._apps = None

        # ---- 移动端执行优化：页面门禁 / 弹窗记忆 / 页面栈 / 偏离回退 ----
        self.use_popup_memory = bool(use_popup_memory)
        # 协作式取消：新增的门禁 / 回退循环每轮都查 ``_cancel_seen``，
        # 但不重复调用用户的 should_cancel（避免把「第 N 次检查才取消」的语义挪位）。
        self._cancel_seen = False
        self._stabilizer = PageStabilizer(
            self.driver, self.config,
            should_cancel=self._gate_cancel, on_event=self._gate_event,
        )
        try:
            self._popup_mem = PopupMemory(self.config.popup_memory_file, self.config)
        except Exception:  # noqa: BLE001
            self._popup_mem = None
        self._stack = PageStack()
        detector = DeviationDetector(self.config, self._stack, self._stabilizer)
        self._detector = detector
        self._back = BackController(
            self.driver, detector, self._stack, self._stabilizer, self.config,
            on_event=self._gate_event, should_cancel=self._gate_cancel,
        )
        self._run_id = ""
        self._mode = "replay"
        self._retry_requested = False
        self._retry_detail = ""
        self._retry_mode = False
        self._popup_changed = False
        self._case_app_id = ""

    @property
    def profile(self) -> PlatformProfile:
        """当前端的执行策略。平台差异只从这里读。"""
        return profile_for(getattr(self.driver, "platform", ""))

    def _registry(self):
        if self._apps is None:
            from atp.memory.apps import AppRegistry

            self._apps = AppRegistry.from_config(self.config)
        return self._apps

    def _emit(self, type_: str, **payload) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event({"type": type_, **payload})
        except RunCancelled:
            raise
        except Exception:
            pass

    def _check_cancel(self) -> None:
        """每一步、每次等待都问一次"用户是不是点了取消"。"""
        if self.should_cancel is None:
            return
        try:
            hit = bool(self.should_cancel())
        except Exception:  # noqa: BLE001
            return
        self._cancel_seen = hit
        if hit:
            raise RunCancelled("用户取消执行")

    def _gate_cancel(self) -> bool:
        """给新增循环用的取消探针：只读主循环已确认的标志，不重复调用用户回调。"""
        return bool(self._cancel_seen)

    def _gate_event(self, name: str, **payload) -> None:
        """把 PageStabilizer / BackController 的事件转成统一的事件流。"""
        self._emit(name, **payload)

    @property

    def screen(self) -> ScreenProfile:
        """本次执行所用的画布档案（含相对基准的缩放比）。"""
        return self._screen

    def _record(self, result: RunResult, art: Path, case: CaseIR) -> None:
        """执行记录落库。库不可用只是少一份统计，绝不能让用例判失败。"""
        if not getattr(self.config, "persist_runs", True):
            return
        try:
            if self._db is None:
                self._db = CaseDB(self.config.db_file)
            # 手工写的脚本 / 直接跑的 StepIR 也要保证库里能查到：
            # 只在"库里没有"时补录，避免每次回放都把版本号顶上去
            if self._db.get_case(case.case_id) is None:
                self._db.upsert_case(case, source=case.created_by or "script")
            self._db.record_run(result, screen=self._screen,
                                artifacts_dir=str(art), report_path=result.report_path)
        except Exception:  # noqa: BLE001
            pass
