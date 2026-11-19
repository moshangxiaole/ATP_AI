"""开场准备：应用解析、冷启动、最大化、录屏、版本与证据。
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
class SetupMixin:
    """共享实现。"""
    def _resolve_app_name(self, nl: NLCase, case: CaseIR) -> None:
        """把自然语言里的「打开 xx」解析成启动目标，写回 nl.app_id / case.app_id。
        移动端解析成包名；桌面端解析成本地应用的启动路径（台账里登记的 exe 路径，
        `os.startfile` 直接拉起）。Web 端用 URL 打开、走 open_url，不走这里。
        解析顺序是「台账 → 设备上找 → 写回台账」，所以同一台机器上只有第一次要翻。
        """
        apps_cfg = getattr(self.config, "apps", None)
        if apps_cfg is None or not getattr(apps_cfg, "enabled", True):
            return
        # Web 端用 open_url 打开地址，不在这里解析；其余端（移动 / PC）
        # 都需要在「打开 xx」这句话里把"应用名"翻成可启动的目标。
        if not self.profile.app_by_name:
            return
        from atp.memory.apps import AppRegistry, guess_app_names, looks_like_package, resolve_app, resolve_from_text
        registry = AppRegistry.from_config(self.config)
        hint = str(nl.app_id or "").strip()
        if hint and looks_like_package(hint.split("/")[0]):
            # 已经给了包名/组件：不用去设备上找，顺手把"名字↔包名"记进台账，
            # 下次用户说「打开示例聊天应用」就能直接命中
            names = guess_app_names(nl.nl or nl.title)
            entry = registry.remember(self.driver.platform,
                                      names[0] if names else hint, hint, source="ai")
            nl.app_id = hint
            case.app_id = hint
            self._emit("app_used", name=entry.name, app_id=entry.app_id, source="given")
            return
        kwargs = dict(registry=registry, driver=self.driver,
                      platform=self.driver.platform, screen="",
                      # 桌面端没有"包管理器"概念，只从台账认启动路径，不扫设备；
                      # Web 端根本不进这里。移动端才允许去设备上找一次。
                      discover=self.profile.discover_apps)
        entry = resolve_app(hint, **kwargs) if hint else None
        if entry is None:
            entry = resolve_from_text(nl.nl or nl.title, **kwargs)
        if entry is None:
            self._emit("log", level="warn",
                       message=f"没能在台账或设备上认出「{hint or '要打开的应用'}」，"
                               f"这次不注入启动步骤；可在 apps.json 里手工登记应用名与包名")
            return
        nl.app_id = entry.app_id
        case.app_id = entry.app_id
        self._emit("app_used", name=entry.name, app_id=entry.app_id, source=entry.source)
    def _terminate_first(self, nl: NLCase, case: CaseIR, result: RunResult, art: Path) -> None:
        """移动端首跑的开场白：结束进程，保证从确定的首页起步。
        和回放端是同一套语义（Executor._cold_start）——用例里显式记一步 terminate，
        回放时执行器看到它就不会再补一刀，两条路径的步骤序列因此完全一致。
        """
        cfg = getattr(self.config, "cold_start", None)
        if cfg is None or not getattr(cfg, "enabled", True):
            return
        # 冷启动是移动端语义（杀进程再进）；PC/WEB 没有这一步
        if not self.profile.cold_start:
            return
        if self.driver.platform not in (getattr(cfg, "platforms", None) or []):
            return
        if not self.driver.supports(Action.TERMINATE_APP):
            return
        pkg = (nl.app_id or "").split("/")[0]
        sid = f"s{len(case.steps) + 1:02d}"
        idx = len(case.steps) + 1
        intent = f"结束 {pkg} 进程，保证冷启动"
        self._emit("step_started", index=idx, total=0, step_id=sid, intent=intent,
                   action=Action.TERMINATE_APP.value)
        sr = self._simple(Action.TERMINATE_APP, {"app": nl.app_id}, intent, art,
                          idx - 1, step_id=sid)
        case.steps.append(Step(step_id=sid, intent=intent, action=Action.TERMINATE_APP,
                               args={"app": nl.app_id}, source="ai"))
        result.steps.append(sr)
        self._emit("step_finished", index=idx, total=0, result=sr.model_dump(), evidence={})
        settle = max(0, int(getattr(cfg, "settle_ms", 800) or 0)) / 1000.0
        if settle and sr.status != "error":
            time.sleep(settle)
    def _settle_ready(self, obs: Observation) -> Observation:
        """规划前确认这一屏真的画完了（``PageStabilizer.wait_stable`` 的 thin 包装）。
        真机事故：点【请假申请单】后 H5 表单还在白屏渲染，树里只有壳子加一个「附件」，
        模型只能挑到「附件」点下去，于是整条用例跑到上传附件页，之后再怎么滑都回不来。
        等待统一交给页面门禁：树-only 轮询、连续两次指纹一致、节点数不塌、无加载标记；
        稳定后只取一次截图。``_thin_ok`` 继续保留：同一屏确认过是空页面就不再重复等。
        """
        if not self.profile.page_gate:
            return obs
        def count(o: Observation) -> int:
            return sum(1 for _ in o.root.walk())
        def looks_thin(n: int) -> bool:
            prev = getattr(self, "_ready_nodes", 0)
            return n < 20 or (prev >= 20 and n < prev * 0.5)
        self._check_cancel()
        n = count(obs)
        # 同一屏已经等过一轮还是这么空 —— 它就是空页面，别再每步都等
        if obs.fingerprint in self._thin_ok:
            self._ready_nodes = n
            return obs
        res = self._stabilizer().wait_stable("ai_ready")
        if res.obs is not None and (res.stable or res.reason == "thin_stable"):
            obs = res.obs
            try:
                obs.screenshot = self.driver.screenshot()
            except Exception:  # noqa: BLE001
                pass
            if not looks_thin(count(obs)):
                self._emit("log", level="info",
                           message=f"界面画完了（{count(obs)} 个节点），再交给规划器")
        else:
            # 门禁超时 / 不可信：认它就是这屏真实的样子，记下来免得反复等
            self._thin_ok.add(obs.fingerprint)
            try:
                obs = self.driver.observe()
            except Exception:  # noqa: BLE001
                pass
        if not res.stable:
            self._thin_ok.add(obs.fingerprint)
        self._ready_nodes = count(obs)
        return obs
    def _maximize_step(self, case: CaseIR, result: RunResult, art) -> None:
        """把"最大化窗口"记成一条显式步骤：报告里看得见，回放时也会重放。"""
        sid = f"s{len(case.steps) + 1:02d}"
        idx = len(case.steps) + 1
        self._emit("step_started", index=idx, total=0, step_id=sid,
                   intent="最大化窗口并采集屏幕尺寸", action=Action.MAXIMIZE.value)
        sr = self._simple(Action.MAXIMIZE, {}, "最大化窗口", art, idx - 1, step_id=sid)
        case.steps.append(Step(step_id=sid, intent="最大化窗口，固定画布尺寸",
                               action=Action.MAXIMIZE, source="ai"))
        result.steps.append(sr)
        prof = self._profile(self.driver.observe())
        sr.detail = (sr.detail + f" → {prof.width}x{prof.height}"
                     f" (dpr {prof.dpr:g})").strip()
        self._emit("step_finished", index=idx, total=0, result=sr.model_dump(),
                   evidence={})
        self._emit("screen", width=prof.width, height=prof.height, dpr=prof.dpr,
                   label=prof.label, scale_x=1.0, scale_y=1.0,
                   base_width=prof.width, base_height=prof.height)
    def _start_recorder(self, platform: str, art: Path):
        """PC 端 / Web 端 AI 执行也录屏（与回放执行器同一套 RunRecorder）。"""
        if not profile_for(platform).record_video:
            return None
        from atp.runs.recorder import RunRecorder, _is_disabled
        if _is_disabled():
            return None
        rec = RunRecorder(self.driver, art)
        self._recorder = rec
        try:
            rec.start()
        except Exception:  # noqa: BLE001  录屏起不来不影响执行
            self._recorder = None
            return None
        return rec
    def _app_version(self, nl: NLCase) -> str:
        return str(nl.variables.get("app_version", ""))
    def _capture_witness(self, before: Observation) -> str:
        """从「断言前后的界面文本差异」里挑一条证据，供回放做零成本自证。
        只挑**新出现的**短句：太短没信息量（"首页"这种到处都有），太长容易带上
        动态数据（时间、用户名、单号），换个账号或改个版就失效。
        挑不到就返回空串——宁可回放多问一次模型，也不留一条假证据。
        """
        try:
            self.driver.wait_idle(800)
        except Exception:  # noqa: BLE001
            pass
        try:
            after = self.driver.observe()
        except Exception:  # noqa: BLE001
            return ""
        old = {(n.text or n.desc or "").strip() for n in before.root.walk()}
        best = ""
        for node in after.root.walk():
            t = (node.text or node.desc or "").strip()
            if not (4 <= len(t) <= 20) or t in old:
                continue
            # 带数字的八成是时间/单号/金额，别抄成证据
            if re.search(r"\d", t) or not re.search(r"[\u4e00-\u9fa5]{2,}", t):
                continue
            if len(t) > len(best):
                best = t
        return best
    def _witness_from(self, obs: Observation) -> str:
        """从最终画面挑一条显著短句当验收证据（不做前后差分）。
        与 `_capture_witness` 同一套过滤：太短没信息量、带数字的多是动态数据。
        挑不到返回空串——宁可回放时多问一次模型，也不留假证据。
        """
        best = ""
        for node in obs.root.walk():
            t = (node.text or node.desc or "").strip()
            if not (4 <= len(t) <= 20):
                continue
            if re.search(r"\d", t) or not re.search(r"[\u4e00-\u9fa5]{2,}", t):
                continue
            if len(t) > len(best):
                best = t
        return best
