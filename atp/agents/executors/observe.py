"""观察与判定：页面栈、分辨率、视觉兜底、稳定等待、截图落盘。

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

class ObserveMixin:
    """共享实现。"""

    def _current_package(self, case: CaseIR | None = None) -> str:
        """尽力取前台包名；拿不到时退回用例里的 app_id（不作强信号）。"""
        for key in ("foreground_package", "current_package", "package_name"):
            value = getattr(self.driver, key, "")
            if callable(value):
                try:
                    value = value()
                except Exception:  # noqa: BLE001
                    value = ""
            if value:
                return str(value)
        app = str(getattr(case, "app_id", "") or self._case_app_id or "")
        if app:
            return app.split("/")[0]
        return str(getattr(self.driver, "app_id", "") or "")

    def _push_frame(self, obs: Observation, step_id: str, shot: str = "") -> bool:
        """把一帧稳定后的页面压栈，供 BACK 校验与偏离检测取基线。

        同页操作（输入文本、开关、滚动、列表刷新）**不新增帧**，只刷新栈顶：
        AC-4.2 的「帧数 = 跳页数 + 1」靠这一条保证。否则每一步都往栈里塞一帧，
        ``stack[-2]`` 就永远是「刚刚操作完的同一页」，BACK 校验的目标本身就是错的。

        Returns:
            True 表示真的入栈（发生了跳页），False 表示只更新了栈顶。
        """
        try:
            coarse = page_signature(
                obs.root, getattr(self.config.page_ready, "dynamic_markers", ()))
        except Exception:  # noqa: BLE001
            coarse = frozenset()
        frame = PageFrame(
            step_id=step_id,
            fine_fp=str(getattr(obs, "fingerprint", "") or ""),
            coarse_sig=coarse,
            package=self._current_package(),
            before_shot=shot,
            at=time.time(),
        )
        dc = self.config.deviation
        dedupe = bool(getattr(dc, "frame_dedupe", True))
        threshold = float(getattr(dc, "recover_match_threshold", 0.80) or 0.80)
        if dedupe and self._stack.same_page(frame, threshold=threshold):
            merged = self._stack.update_current(
                fine_fp=frame.fine_fp, coarse_sig=frame.coarse_sig or None,
                package=frame.package, before_shot=shot,
            )
            self._emit("page_stack_touch", step_id=step_id,
                       frame_id=getattr(merged, "frame_id", ""),
                       depth=len(self._stack), fine_fp=frame.fine_fp)
            return False
        self._stack.push(frame)
        self._emit("page_stack_push", step_id=step_id, frame_id=frame.frame_id,
                   depth=len(self._stack), fine_fp=frame.fine_fp,
                   coarse_sig=frame.sig_summary(), package=frame.package)
        return True

    def _persist_stack(self, art: Path) -> None:
        """按需把页面栈落成 ``artifacts/<run_id>/page_stack.json``。

        默认关（设计 Q11：页面文本可能敏感）。写失败只降级，绝不影响用例结论。
        """
        if not bool(getattr(self.config.deviation, "persist_stack", False)):
            return
        try:
            self._stack.set_run_id(self._run_id)
            self._stack.persist(art / "page_stack.json")
        except Exception:  # noqa: BLE001
            pass

    def _carry_gate(self, obs: Observation) -> None:
        """弹窗处置可能换掉观察对象，把门禁标记原样带过去（供偏离守卫使用）。"""
        if obs is None:
            return
        res = self._stabilizer.last_result
        if res is None:
            return
        try:
            if getattr(obs, "page_ready", None) is None:
                setattr(obs, "page_ready",
                        bool(res.stable or res.reason == "thin_stable"))
            setattr(obs, "page_gate_degraded", bool(res.degraded))
            setattr(obs, "page_ready_reason", res.reason)
        except Exception:  # noqa: BLE001
            pass

    def _profile(self, obs: Observation) -> ScreenProfile:
        getter = getattr(self.driver, "screen_profile", None)
        if callable(getter):
            try:
                prof = getter()
                if isinstance(prof, ScreenProfile) and prof.ok:
                    return prof
            except Exception:  # noqa: BLE001
                pass
        return ScreenProfile(width=obs.width, height=obs.height,
                             platform=obs.platform,
                             label=f"{obs.platform}-{obs.width}x{obs.height}")

    def _visual_matcher(self, obs: Observation):
        """给定位器注入的视觉兜底：模板匹配 → 合成可点击节点（坐标已映射到当前画布）。"""
        if self.visual is None:
            return None

        def matcher(strategy: LocatorStrategy):
            value = strategy.value
            if not isinstance(value, dict):
                return None, 0.0, 1.0
            hit = self.visual.locate(value, obs)
            if hit is None:
                return None, 0.0, 1.0
            # 关键的一步：归一化矩形 × 当前画布尺寸 = 本机像素坐标
            px = denorm_rect(hit.rect_norm, (obs.width, obs.height))
            return make_point_node(px, str(value.get("label", ""))), hit.score, hit.scale

        return matcher

    def _settle(self, deadline: float, max_wait: float = 8.0) -> Observation:
        """等界面稳定：连续两次观测指纹一致才算加载完。

        只给语义断言用——它一次要花几十秒，采样时刻错了就是白花钱 + 误判。
        等待统一合并到单一 ``stable_observe``（树-only 轮询 + 单次截图），
        不再串联固定 sleep / wait_idle / 多次 observe。
        """
        self._check_cancel()
        budget = min(max_wait, max(0.1, deadline - time.time()))
        try:
            return self._stabilizer.stable_observe(
                "assert_settle", ceiling_ms=int(budget * 1000))
        except RunCancelled:
            raise
        except Exception:  # noqa: BLE001
            return self.driver.observe()

    def _motion_verdict(self, post: Postcondition, deadline: float) -> JudgeResult:
        """判断画面是否在动——"真的在播"的唯一硬证据。

        节点树看不出这个：播放器、黑屏、加载转圈在树里长得一样。
        取两帧相隔 0.6s 的截图比变化面积，正片通常 >8%，静止页面接近 0。
        """
        min_ratio = post.min_ratio if post.min_ratio > 0 else 0.02
        best = 0.0
        while True:
            self._check_cancel()
            try:
                a = self.driver.screenshot()
                time.sleep(0.6)
                b = self.driver.screenshot()
            except Exception as e:  # noqa: BLE001
                return JudgeResult(passed=False, method="dsl-motion",
                                   detail=f"截图失败: {e}")
            best = max(best, frame_diff_ratio(a, b))
            if best >= min_ratio:
                return JudgeResult(passed=True, method="dsl-motion",
                                   score=round(best, 4),
                                   detail=f"画面变化 {best:.1%}")
            if time.time() > deadline:
                return JudgeResult(passed=False, method="dsl-motion",
                                   score=round(best, 4),
                                   detail=f"画面几乎没变化（最大 {best:.1%}，要求 ≥{min_ratio:.1%}）")

    def _dump(self, art: Path, name: str, data: bytes) -> None:
        if not data:
            return
        (art / name).write_bytes(data)
        # 证据截图同步喂给录屏器：它是兜底帧源（Playwright 系驱动无法开
        # 后台线程），也是线程帧源的画面补充（步骤边界一定有清晰帧）。
        rec = getattr(self, "_recorder", None)
        if rec is not None:
            try:
                rec.ingest(data)
            except Exception:  # noqa: BLE001
                pass
