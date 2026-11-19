"""弹窗处置与长程记忆。

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

class PopupMixin:
    """共享实现。"""

    def _popup_ctx(self, case: CaseIR) -> PopupContext:
        return PopupContext(
            app_id=str(getattr(case, "app_id", "") or self._case_app_id or ""),
            platform=str(getattr(self.driver, "platform", "") or ""),
            run_id=self._run_id,
            mode=self._mode,
            enabled=bool(self.use_popup_memory),
            app_version=str(getattr(case, "app_version", "") or ""),
        )

    def _confirm_memory(self, sr: StepResult, deviation: bool = False) -> None:
        """步骤收尾：推进「后续 2 步无偏离」确认窗口，必要时升级为 active。"""
        if self._popup_mem is None:
            return
        try:
            committed = self._popup_mem.confirm_window(
                run_id=self._run_id, passed=bool(sr.ok), deviation=bool(deviation))
            for entry in committed:
                self._emit("popup_memory_learn", sig=entry.sig, state=entry.state,
                           hits=entry.hits, confidence=entry.confidence, committed=True)
                if entry.state == "retired":
                    self._emit("popup_memory_retired", sig=entry.sig, reason="miss_limit")
        except Exception:  # noqa: BLE001
            pass

    def _dismiss_popups(self, obs: Observation, idx: int, step: Step,
                        case: CaseIR | None = None,
                        rounds: int = 3) -> Observation:
        """动作之前把挡路的弹窗清掉，返回（可能已变化的）观察结果。

        判断只用现成的观察结果，不额外抓树；真的点了才重新观察一次。
        每处置一个都发 `popup_dismissed` 事件，报告里能看见"这步关了哪几个弹窗"。
        有长程记忆时先试零决策快路径；失败自动登记 miss 并回退全量决策。
        """
        self._popup_changed = False
        if self.popup is None:
            return obs

        def _emit(ev: dict) -> None:
            kind = str(ev.get("type", "popup_dismissed") or "popup_dismissed")
            self._emit(kind, step_id=step.step_id, index=idx,
                       **{k: v for k, v in ev.items() if k != "type"})
            if kind == "popup_memory_miss" and str(ev.get("state", "")) == "retired":
                self._emit("popup_memory_retired", step_id=step.step_id, index=idx,
                           sig=ev.get("sig", ""), reason=ev.get("reason", ""))

        ctx = self._popup_ctx(case) if case is not None else PopupContext(
            platform=str(getattr(self.driver, "platform", "") or ""),
            run_id=self._run_id, mode=self._mode,
            enabled=bool(self.use_popup_memory), app_id=self._case_app_id)
        obs, done = dismiss_popups(self.driver, obs, self.popup,
                                   settle_ms=self._popup_settle, on_event=_emit,
                                   rounds=rounds, memory=self._popup_mem,
                                   stabilizer=self._stabilizer, ctx=ctx)
        self._popup_changed = bool(done)
        if done:
            sources = "、".join(str(d.get("source", "guard")) for d in done[:3])
            self._emit("log", level="info",
                       message=f"{step.step_id} 弹窗处置：{sources}（{len(done)} 个）")
        return obs
