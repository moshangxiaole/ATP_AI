"""移动端回放入口。

只放该端专属的执行策略；共享引擎见 ``executors/core.py``。
统一入口 ``agents/executor.py`` 按 driver.platform 分发到这里。
"""

from __future__ import annotations

import time
from pathlib import Path

from atp.common.profiles import MOBILE, PlatformProfile
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
from atp.agents.executors.core import ReplayCore

class MobileExecutor(ReplayCore):
    """移动端回放：冷启动、滑动查找、偏离回退。"""

    @property
    def profile(self) -> PlatformProfile:
        return MOBILE

    def _verify_deviation(self, step: Step, case: CaseIR, before: Observation,
                          after_obs: Observation, verdict: JudgeResult,
                          sr: StepResult) -> None:
        """动作后取一次树-only 稳定采样，做偏离检测并按需回退 / 请求重跑。"""
        cfg = self.config.deviation
        if not getattr(cfg, "enabled", True) or self._mode == "ai":
            return
        # Web / Desktop / Electron 完全 no-op：偏离回退只对移动端生效
        if not self.profile.deviation_recovery:
            return
        # 取消探针由 wait_stable 每轮读取（_gate_cancel），这里不重复调用用户回调
        after = after_obs
        gate = None
        try:
            gate = self._stabilizer.wait_stable(
                "post_action",
                ceiling_ms=int(getattr(cfg, "detect_timeout_ms", 1500) or 1500),
            )
            if gate.obs is not None:
                after = gate.obs
                try:
                    setattr(after, "page_gate_degraded", bool(gate.degraded))
                    setattr(after, "page_ready", bool(gate.stable))
                except Exception:  # noqa: BLE001
                    pass
        except RunCancelled:
            raise
        except Exception:  # noqa: BLE001
            gate = None

        if not self._retry_mode:
            # 先等页面稳、再压栈。**顺序不能反**：没有后置断言的步骤里
            # ``after_obs`` 复用的是动作前那棵树（省一次抓树），拿它压栈等于
            # 把「上一页的签名」记成「当前页」，后续步骤的 BACK 校验会一直比错对象。
            #
            # 包名同样要在压栈前取走：压完 current() 就是动作后的页面，
            # 自己和自己比永远相等，package_changed 这个强信号会被静音。
            prev = self._stack.current()
            expected_pkg = str(getattr(prev, "package", "") or "") if prev is not None else ""
            self._push_frame(after, step.step_id, sr.screenshot)
        else:
            expected_pkg = ""

        package = self._current_package(case)
        detected = self._detector.detect(
            step=step, before=before, after=after, verdict=verdict,
            popup_changed=bool(self._popup_changed), mode=self._mode,
            package=package, expected_package=expected_pkg,
        )
        self._last_deviation = bool(detected.detected)
        if not detected.detected:
            return

        sr.detail = (sr.detail + f" 偏离信号：{detected.cause}").strip()
        # deviation_detected / deviation_back / deviation_recovered 由 BackController 统一发射
        outcome = self._back.recover(
            step=step, verdict=detected, before=before, after=after,
            package=package, mode=self._mode, cancelled=False,
            popup_changed=bool(self._popup_changed),
        )
        if outcome.recovered and outcome.retry and not self._retry_mode:
            self._retry_requested = True
            self._retry_detail = f"偏离纠正：回退 {outcome.backs} 次 + 重跑"
            sr.detail = (sr.detail + " " + self._retry_detail).strip()
        elif not outcome.recovered:
            sr.detail = (sr.detail + f" 偏离回退未成功（{outcome.reason}）").strip()

    def _cold_start(self, step: Step, idx: int, case: CaseIR) -> str:
        """移动端"打开应用"的默认语义：先结束进程，再启动。

        只做 `am start` / `aa start` 不够：App 还活在后台时，启动常常只是把上次
        停留的页面拉回前台——上次停在详情页，这次第一步找首页入口就直接 miss。
        用例必须自己造出确定的初始状态，所以这件事放在执行器里统一保证，
        而不是指望每条用例的作者都记得先写一步 terminate。

        用例里已经显式写了 terminate 的，不再补刀（省 1 秒，也不产生两条"关闭"记录）。
        """
        cfg = getattr(self.config, "cold_start", None)
        if cfg is None or not getattr(cfg, "enabled", True):
            return ""
        if self.driver.platform not in (getattr(cfg, "platforms", None) or []):
            return ""
        if not self.driver.supports(Action.TERMINATE_APP):
            return ""
        app = str(step.args.get("app") or case.app_id
                  or getattr(self.driver, "app_id", "") or "")
        if not app:
            return ""

        prev = case.steps[idx - 2] if idx >= 2 and idx - 2 < len(case.steps) else None
        if prev is not None and prev.action is Action.TERMINATE_APP:
            return "冷启动：上一步已结束进程，直接启动"

        t0 = time.perf_counter()
        try:
            self.driver.terminate_app(app)
        except Exception as e:  # noqa: BLE001
            return f"冷启动跳过：结束进程失败（{e}）"
        settle = max(0, int(getattr(cfg, "settle_ms", 800) or 0))
        self._stabilizer.invalidate()
        if settle:
            # 固定 sleep → 单一 wait_stable（旧值变 ceiling），树-only，不叠加等待
            self._stabilizer.wait_stable("cold_start", ceiling_ms=settle)
        ms = int((time.perf_counter() - t0) * 1000)
        self._emit("cold_start", step_id=step.step_id, index=idx, app=app, ms=ms)
        return f"冷启动：已先结束 {app} 进程（{ms}ms）"

    def _post_launch_settle(self) -> None:
        """启动指令返回后等首帧落定。

        `am start` / `aa start` 都是异步的：返回时界面可能还在转场（白屏），
        这时去抓树只能抓到空白，紧接着的点击和滑动全部落空。冷启动一次就等一次，
        比"每一步都去猜界面准备好没有"便宜，也比在每处调用点补 sleep 干净。
        """
        cfg = getattr(self.config, "cold_start", None)
        if cfg is None or self.driver.platform not in (getattr(cfg, "platforms", None) or []):
            return
        ms = max(0, int(getattr(cfg, "launch_settle_ms", 1800) or 0))
        if not ms:
            return
        # 启动是异步的：只等一次「页面完全加载完」门禁，旧值变 ceiling
        self._stabilizer.invalidate()
        try:
            self._stabilizer.wait_stable("launch", ceiling_ms=ms)
        except Exception:  # noqa: BLE001
            pass

    def _scroll_find(self, step: Step, obs: Observation,
                     idx: int) -> tuple[LocateResult | None, Observation | None, str]:
        """目标不在首屏时上下滑动查找。

        返回（定位结果，找到时的观察，给报告的一句话）。没找到时前两项为 None，
        第三项仍然要说清"滑了几次"——报告里必须能看出"不是没搜，是搜过了"。

        只有移动端做这件事：Web/桌面有 DOM/UIA 能直接查到屏幕外的元素，
        不需要靠手势去"翻"，翻反而会打乱滚动容器的位置。
        """
        cfg = getattr(self.config, "scroll_find", None)
        if cfg is None or not getattr(cfg, "enabled", True):
            return None, None, ""
        if self.driver.platform not in (getattr(cfg, "platforms", None) or []):
            return None, None, ""
        if not step.strategies or not self.driver.supports(Action.SWIPE):
            return None, None, ""

        try:
            out = find_by_scrolling(
                self.driver, step.strategies, (obs.width, obs.height),
                self.config.bbox_tolerance, self._visual_matcher, cfg,
                should_cancel=self.should_cancel,
                avoid_occluded=self.profile.occluded_aware,
                stabilizer=self._stabilizer,
            )
        except RunCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            self._emit("log", level="warn",
                       message=f"{step.step_id} 滑动查找异常，按原样继续: {e}")
            return None, None, ""

        if out.ok and out.obs is not None:
            self._emit("scroll_found", step_id=step.step_id, index=idx,
                       swipes=out.swipes, trail=out.trail_text)
            return (out.loc, out.obs,
                    f"目标不在首屏，滑动 {out.swipes} 次后找到（{out.trail_text}）")

        if out.swipes:
            self._emit("scroll_miss", step_id=step.step_id, index=idx,
                       swipes=out.swipes, trail=out.trail_text, reason=out.reason)
            return None, None, f"已滑动 {out.swipes} 次仍未找到（{out.trail_text}）"
        return None, None, ""

