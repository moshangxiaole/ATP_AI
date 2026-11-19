"""观察与页面稳定：弹窗、截图、画布、空树判定、滚动能力。
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
class PerceiveMixin:
    """共享实现。"""
    def _stabilizer(self) -> PageStabilizer:
        """首跑与回放共用同一个页面门禁实现（树-only 轮询 + 单次截图）。"""
        if self._stab is None:
            self._stab = PageStabilizer(
                self.driver, self.config,
                should_cancel=self._gate_cancel,
                on_event=self._gate_event,
            )
        return self._stab
    def _clear_popups(self, obs: Observation) -> Observation:
        """首跑时同样要清弹窗：开屏广告、青少年模式、权限申请，模型处理不了也不该处理。
        AI 模式的记忆上下文是「只写不读」：首跑负责沉淀候选，绝不在模型还没看清
        界面时替它走零决策快路径。
        """
        pc = getattr(self.config, "popup", None)
        if pc is not None and not pc.enabled:
            return obs
        guard = PopupGuard(
            extra_close_words=list(getattr(pc, "extra_close_words", []) or []),
            limit=getattr(pc, "max_per_step", 3) or 3,
        )
        obs, hits = dismiss_popups(
            self.driver, obs, guard,
            settle_ms=getattr(pc, "settle_ms", 600), on_event=self._popup_event,
            memory=self._popup_memory(), stabilizer=self._stabilizer(),
            ctx=self._popup_ctx(),
        )
        if hits:
            names = "、".join(h["keyword"] for h in hits[:3])
            self._emit("log", level="info", message=f"自动关闭弹窗：{names}")
        return obs
    def _visual_matcher(self, obs: Observation):
        """首跑时同样需要视觉兜底：point 步骤沉淀的模板要能当场用起来。"""
        cfg = getattr(self.config, "vision", None)
        if cfg is None or not cfg.enabled:
            return None
        from atp.agents.perception.vision import VisualLocator, denorm_rect, make_point_node
        if getattr(self, "_visual_locator", None) is None:
            self._visual_locator = VisualLocator(self.config.cases, cfg)
        def matcher(strategy: LocatorStrategy):
            value = strategy.value
            if not isinstance(value, dict):
                return None, 0.0, 1.0
            hit = self._visual_locator.locate(value, obs)
            if hit is None:
                return None, 0.0, 1.0
            px = denorm_rect(hit.rect_norm, (obs.width, obs.height))
            return make_point_node(px, str(value.get("label", ""))), hit.score, hit.scale
        return matcher
    def _settle_obs(self, max_wait: float = 8.0) -> Observation:
        """等界面稳定再验收：刚点完就截图，拍到的还是上一屏。
        统一走 ``PageStabilizer.wait_stable`` 的树-only 轮询，稳定后只取一次截图；
        失败时降级为一次 ``driver.observe()``，绝不叠加多轮等待。
        """
        self._check_cancel()
        res = self._stabilizer().wait_stable("ai_accept",
                                             ceiling_ms=int(max(1.0, max_wait) * 1000))
        if res.obs is not None and (res.stable or res.reason == "thin_stable"):
            obs = res.obs
            try:
                obs.screenshot = self.driver.screenshot()
            except Exception:  # noqa: BLE001
                pass
            return obs
        return self.driver.observe()
    def _settle_shot(self, max_wait_ms: int = 1500,
                     quiet_ms: int = 180) -> bytes:
        """等画面稳下来再拍"这一步之后"的截图。
        固定 sleep(0.15) 是在赌：Electron / H5 的异步渲染、聊天消息上屏、弹层动画
        常常要 300~800ms 才落定，拍早了拿到的是**动画中间帧**——
        run-20261113-180724 就是这样：s10 点发送，前后差 8.2%（多半是动画），
        而 s11 的 before 又和 s10 的 after 差了 1.6%，说明界面还在动。
        拿这种帧去判"这一步有没有效果"，结论必然是错的，模型也会被误导着再点一次。
        做法：连续两拍差异小于阈值就认为稳定（阈值用"画面动了"那一档，
        光标闪烁、水印微动都不算），最多等 max_wait_ms，超时就用手上最后一帧。
        """
        shot = self.driver.screenshot()
        if not shot:
            return b""
        deadline = time.perf_counter() + max_wait_ms / 1000.0
        while time.perf_counter() < deadline:
            self._check_cancel()
            time.sleep(quiet_ms / 1000.0)
            cur = self.driver.screenshot()
            if not cur:
                return shot
            # 先比"有没有动"，动了就再等一轮；连续两拍一致 = 稳了
            if frame_delta_ratio(shot, cur) is None:
                return cur
            if frame_delta_ratio(shot, cur) < DEFAULT_MIN_RATIO:
                return cur
            shot = cur
        return shot
    @staticmethod
    def _hollow_obs(obs: Observation) -> bool:
        """观察是不是空壳（读不到任何有业务含义的节点）。

        与驱动的空树自愈是两层：驱动负责等 a11y 树长出来；这层管自愈失败后
        别让模型把"读不到"理解成"界面坏了"然后 done/重启。

        判据不能用"带文字节点 <= 1"：run-20261117-124001 的树里有 `156544` 和
        `SampleDesktop` 两个框架噪声，刚好越过阈值被当成正常页面，模型直接 done。
        这里排除纯数字、单字、窗口名这类噪声，只看有没有真正的业务文字/控件。
        """
        try:
            nodes = list(obs.root.walk())
        except Exception:  # noqa: BLE001
            return False
        if len(nodes) <= 3:
            return True
        noise = {"SampleDesktop", "示例聊天应用", "示例聊天应用-测试版", "Desktop", "Screen", "Window"}
        meaningful = 0
        for n in nodes:
            txt = (n.text or "").strip()
            if not txt or txt.isdigit() or len(txt) <= 1 or txt in noise:
                continue
            meaningful += 1
        return meaningful == 0
    def _page_scrollable(self, obs: Observation) -> bool:
        """当前页面有没有"真的滑得动"的容器。
        移动端：硬滑会把界面滑乱（滑到进度条/退出页面），树里报 scrollable
        的容器才算数，签不出来宁可当成不可滑。
        桌面端：swipe 已实现为**鼠标滚轮**（agents/platforms/pc/desktop.py），滚不动时
        画面不变、无副作用——UIA 树对 Electron/网页容器经常不报 ScrollPattern
        （run-20261113-211702：会话列表明明能滚，被闸拦死 3 次直接熔断），
        所以桌面一律放行，滚没滚交给"步骤效果校验"判断。
        """
        if not self.profile.scroll_needs_container:
            return True
        from atp.agents.perception.scroller import has_scrollable
        try:
            return has_scrollable(obs.root)
        except Exception:  # noqa: BLE001
            return False
    def _profile(self, obs: Observation):
        getter = getattr(self.driver, "screen_profile", None)
        if callable(getter):
            try:
                prof = getter()
                if getattr(prof, "ok", False):
                    return prof
            except Exception:  # noqa: BLE001
                pass
        from atp.common.stepir import ScreenProfile
        return ScreenProfile(width=obs.width, height=obs.height,
                             platform=obs.platform,
                             label=f"{obs.platform}-{obs.width}x{obs.height}")
