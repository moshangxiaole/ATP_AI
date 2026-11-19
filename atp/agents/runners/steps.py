"""把模型决策翻译成步骤、视觉定位、执行与简单动作。
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


def _normalize_key(raw: str) -> str:
    """模型给的按键名 → 驱动认识的键名（含中文与别名）。

    默认键必须选 enter 而不是 back：模型发 action=key 十有八九是想触发搜索/
    发送，误按 back 会退出当前页、把已填内容全丢掉。
    """
    s = str(raw or "").strip().lower()
    if not s:
        return "enter"
    if any(k in s for k in ("enter", "回车", "确认", "发送", "搜索", "↵")):
        return "enter"
    if any(k in s for k in ("back", "返回", "后退", "返回键")):
        return "back"
    if any(k in s for k in ("tab", "制表")):
        return "tab"
    if any(k in s for k in ("esc", "escape", "取消", "退出")):
        return "esc"
    if any(k in s for k in ("delete", "删除", "退格", "backspace")):
        return "delete"
    # 已经是 {Enter} / {ESC} 这类 SendKeys 语法：去掉花括号
    return s.strip("{}") if len(s) <= 16 else "enter"


class StepsMixin:
    """共享实现。"""
    def _to_step(self, idx: int, act: str, node: UINode | None,
                 decision: dict, obs: Observation) -> Step | None:
        sid = f"s{idx:02d}"
        intent = str(decision.get("intent") or act)
        if act in ("tap", "input") and node is None:
            # 树上没有对应节点，但模型给了 target：走视觉定位（point 路径）。
            # 早先这里直接 return None，模型"在搜索栏输入联系人乙"的决定被静默丢掉，
            # 它只会收到一句「跳过非法动作 input」，于是回头接着点搜索栏——
            # run-20261113-173646 连点三次就是这么来的。
            target = str(decision.get("target") or "").strip()
            if act == "input":
                if not target or not str(decision.get("text") or "").strip():
                    return None
                return self._point_step(idx, decision, obs)
            if target:
                return self._point_step(idx, decision, obs)
            return None
        if act == "input":
            # 模型给的 text 可能是 null：`str(None)` 会把字面量 "None" 打进输入框
            # （run-20261113-180724 的 s04 真把 "None" 输入了搜索框）。
            # 没有文本就说明它的意图其实是"点一下"，退化成 tap，别制造脏输入。
            text = str(decision.get("text") or "").strip()
            if text:
                action, args = Action.INPUT_TEXT, {"text": text}
            else:
                act = "tap"
                action, args = Action.TAP, {}
                if "input" in intent.lower():
                    intent = intent.replace("input", "点击")
        elif act == "tap":
            action, args = Action.TAP, {}
        elif act == "swipe":
            action, args = Action.SWIPE, {"direction": decision.get("direction", "up")}
        elif act == "back":
            action, args = Action.BACK, {}
        elif act == "home":
            action, args = Action.HOME, {}
        elif act == "key":
            # 模型把按键名填在 text 里（schema 只定义了 text，没有 key 字段）：
            # {"action":"key","text":"Enter","intent":"按回车触发搜索"}。
            # 只读 decision["key"] 会拿到默认 "back"——两次「回车」全变成返回，
            # 搜索不触发、消息发不出（run-20261117-110950 直接原因）。
            raw_key = str(decision.get("key") or decision.get("text") or "").strip()
            key = _normalize_key(raw_key or intent)
            action, args = Action.PRESS_KEY, {"key": key}
        elif act == "launch":
            # 模型中途自己发的 launch 常常不带 app（决策里没有这个字段）。
            # 兜底顺序：决策显式给的 → 用例级 app_id → 驱动当前应用。
            # 全空会直接打到驱动层报「未指定要启动的应用」（run-20261113-163446）。
            action, args = Action.LAUNCH_APP, {
                "app": str(decision.get("app") or self._case_app_id
                           or getattr(self.driver, "app_id", "") or "")}
        elif act == "open_url":
            args = {"url": str(decision.get("url", ""))}
            if not args["url"]:
                return None
            action = Action.OPEN_URL
        elif act == "point":
            return self._point_step(idx, decision, obs)
        else:
            return None
        strategies = derive_strategies(node, obs.root, (obs.width, obs.height)) if node else []
        if node is not None:
            strategies = self._maybe_visual(sid, node, strategies, obs)
        return Step(step_id=sid, intent=intent, action=action,
                    strategies=strategies, args=args, source="ai")
    def _point_step(self, idx: int, decision: dict, obs: Observation) -> Step | None:
        """action=point：树上没有目标，改用「截图 + 视觉 grounding + 模板沉淀」。
        首跑时调一次多模态模型指位置，之后回放纯本地匹配 —— 这是 canvas 类界面
        唯一能做到"首跑花钱、回放免费"的路径。
        """
        from atp.agents.perception.vision import capture_template, denorm_rect, image_size, visual_strategy
        target = str(decision.get("target") or decision.get("intent") or "").strip()
        if not target or not obs.screenshot:
            return None
        grounder = VisionGrounder(self.client)
        if not grounder.available:
            self._emit("log", level="warn",
                       message=f"「{target}」只能靠图像识别定位，但模型未配置，无法定位")
            return None
        hit = grounder.find(obs.screenshot, target)
        if hit is None:
            self._emit("log", level="warn",
                       message=f"图像识别没找到「{target}」，跳过这一步（不瞎点）")
            return None
        sid = f"s{idx:02d}"
        rect_norm = hit["rect"]
        sw, sh = image_size(obs.screenshot)
        px = denorm_rect(rect_norm, (sw, sh))
        strategies: list[LocatorStrategy] = [
            LocatorStrategy.make("bounds", [int(v * 1000) for v in rect_norm])
        ]
        cap = capture_template(
            obs.screenshot, px, cases_dir=self.config.cases, case_id=self._case_id,
            step_id=sid, label=target, dpr=self.driver.device_pixel_ratio(),
            platform=obs.platform,
        )
        if cap is not None:
            value, asset = cap
            self._assets.append(asset)
            strategies.append(visual_strategy(value))
            self._emit("log", level="info",
                       message=f"图像定位「{target}」置信度 {hit['confidence']:.2f}，"
                               f"已沉淀模板 {asset['name']}")
        else:
            self._emit("log", level="warn",
                       message=f"「{target}」定位到了但截取模板失败，只保留归一化坐标")
        # 同样要防 `str(None)`：模型给 null 时只能是"点一下"，不能输入 "None"
        text = str(decision.get("text") or "").strip()
        action = Action.INPUT_TEXT if text else Action.TAP
        args = {"text": text} if text else {}
        return Step(step_id=sid, intent=str(decision.get("intent") or f"图像定位并点击 {target}"),
                    action=action, strategies=strategies, args=args, source="ai")
    def _maybe_visual(self, sid: str, node: UINode, strategies: list[LocatorStrategy],
                      obs: Observation) -> list[LocatorStrategy]:
        """给"定位不住的节点"补一张模板图 + 归一化坐标。
        判定口径（宁少勿多，模板图是兜底不是主路）：
        - 一条强定位（id/文本/描述/xpath）都没有，只剩坐标 —— 换分辨率必挂，必须留图；
        - 控件本身是 canvas/img/svg 这类纯像素容器 —— 树上永远没线索，必须留图；
        - 其余情况（有稳定 id 或文案）不采图，省磁盘也省匹配时间。
        """
        cfg = self.config.vision
        if not cfg.enabled or not obs.screenshot:
            return strategies
        has_strong = any(s.type in _STRONG_LOCATORS for s in strategies)
        pixel_only = (node.cls or "").lower() in _VISUAL_CLASSES
        if not pixel_only:
            if has_strong:
                return strategies
            if not cfg.capture_for_weak_nodes:
                return strategies
        cap = capture_template(
            obs.screenshot, node.bounds,
            cases_dir=self.config.cases, case_id=self._case_id, step_id=sid,
            label=node.text or node.desc or node.cls or sid,
            dpr=self.driver.device_pixel_ratio(), platform=obs.platform,
        )
        if cap is None:
            return strategies
        value, asset = cap
        self._assets.append(asset)
        out = list(strategies)
        out.append(visual_strategy(value))
        self._emit("log", level="info",
                   message=f"{sid} 无可访问属性，已沉淀视觉模板 {asset['name']}")
        return out
    def _execute(self, step: Step, node: UINode | None, obs: Observation,
                 art: Path, idx: int) -> StepResult:
        self._check_cancel()
        t0 = time.perf_counter()
        sr = StepResult(step_id=step.step_id, intent=step.intent, action=step.action.value,
                        ai_used=True)  # AI 模式下每一步都由 AI 接管
        scroll_note = ""
        before = f"{idx:02d}_{step.step_id}_before.png"
        if obs.screenshot:
            (art / before).write_bytes(obs.screenshot)
            step.evidence.before = before
            sr.screenshot = before
            rec = getattr(self, "_recorder", None)
            if rec is not None:
                try:
                    rec.ingest(obs.screenshot)
                except Exception:  # noqa: BLE001
                    pass
        if step.action is Action.WAIT_FOR:
            from atp.agents.perception.judge import Judge
            self._action_request(step, None)
            verdict = Judge(self.client).check(step.postcondition, obs)
            sr.judge_method = verdict.method
            sr.judge_score = verdict.score
            sr.status = "passed" if verdict.passed else "failed"
            sr.error = "" if verdict.passed else verdict.detail
            sr.detail = verdict.detail
            sr.latency_ms = int((time.perf_counter() - t0) * 1000)
            self._action_result(step, ok=verdict.passed, detail=verdict.detail,
                                error=sr.error, latency_ms=sr.latency_ms,
                                judge=verdict.method, score=verdict.score)
            return sr
        # 打开/关闭应用：AI 首跑这条路径不经过 Executor._resolve_app_step，
        # 模型中途发的 launch 若 args.app 为空，空串会直打驱动层报错
        # （run-20261113-163446：s01 已成功启动，s03 又发一次空 app 的 launch，
        # 一步失败整场被判失败）。这里按用例级 app_id 兜底，别让冗余动作毁掉整条。
        if step.action in (Action.LAUNCH_APP, Action.TERMINATE_APP):
            if not str(step.args.get("app") or "").strip():
                app = self._case_app_id or str(getattr(self.driver, "app_id", "") or "")
                if app:
                    step.args["app"] = app
        # point 步骤没有 node_index —— 目标只存在于 strategies 里（归一化坐标 + 模板图）。
        # 早先这里直接把 node=None 传给 perform，TAP 就会报"缺少目标元素"：
        # 视觉都把位置指出来了，却因为没回填成节点而白干一步。
        # 另外，规划时看到的节点现在可能已经被滚出视野（bounds 全 0）、
        # 或被底部固定栏盖住（真机：点「请选择」跳进了「附件」页）。
        # 这两种情况都按"没定位到"重来一遍，绝不拿错坐标去点。
        from atp.agents.perception.locator import occluded_by
        mobile = self.profile.occluded_aware
        blocked = (node is not None and node.visible and mobile
                   and occluded_by(obs.root, node) is not None)
        if step.needs_target and (node is None or not node.visible or blocked):
            loc = resolve(obs.root, step.strategies, (obs.width, obs.height),
                          self.config.bbox_tolerance, self._visual_matcher(obs),
                          avoid_occluded=mobile)
            # 滑动查找按配置的平台表走：移动端一直开；桌面端补上后，长列表里
            # 树没查到的条目（搜索结果被滚出视野）也有机会翻一屏找到。
            sf_cfg = getattr(self.config, "scroll_find", None)
            sf_ok = (sf_cfg is not None and getattr(sf_cfg, "enabled", True)
                     and self.driver.platform in (getattr(sf_cfg, "platforms", None) or []))
            if loc.node is None and sf_ok:
                # 「当前页找不到，就一定滑动」——这条铁律在回放执行器里有，
                # 首跑路径也必须一样。早先首跑直接报"被遮挡/不在可视区"就退出，
                # 结果模型连一次滑动都没试过，整条用例卡在长表单第一屏。
                from atp.agents.perception.scroller import find_by_scrolling
                # 内层的 cfg 重新取一次保持原逻辑；sf_ok 只负责平台闸门
                cfg = getattr(self.config, "scroll_find", None)
                if cfg is not None and getattr(cfg, "enabled", True) and step.strategies:
                    try:
                        out = find_by_scrolling(
                            self.driver, step.strategies, (obs.width, obs.height),
                            self.config.bbox_tolerance, self._visual_matcher, cfg,
                            should_cancel=self.should_cancel, avoid_occluded=mobile,
                            stabilizer=self._stabilizer(),
                        )
                    except Exception as e:  # noqa: BLE001
                        self._emit("log", level="warn",
                                   message=f"{step.step_id} 滑动查找异常: {e}")
                        out = None
                    if out is not None and out.ok and out.obs is not None:
                        loc, obs = out.loc, out.obs
                        scroll_note = f"目标不在首屏，滑动 {out.swipes} 次后找到"
                        self._emit("log", level="info", message=scroll_note)
                    elif out is not None:
                        scroll_note = f"已滑动 {out.swipes} 次仍未找到：{out.reason}"
                        self._emit("log", level="warn", message=scroll_note)
            if loc.node is None:
                sr.status = "error"
                if scroll_note:
                    sr.error = scroll_note
                elif blocked:
                    sr.error = "目标被底部固定栏遮挡，需要先滑动让它露出来"
                elif loc.offscreen:
                    sr.error = f"目标不在可视区域内（{loc.offscreen}），需要先滑动"
                else:
                    sr.error = loc.reason or "未定位到目标元素"
                sr.detail = "尝试过: " + ", ".join(
                    f"{k}{'✓' if v else '✗'}" for k, v in loc.attempts[:4])
                sr.latency_ms = int((time.perf_counter() - t0) * 1000)
                self._action_result(step, ok=False, detail=sr.detail,
                                    error=sr.error, latency_ms=sr.latency_ms)
                return sr
            node = loc.node
            sr.strategy_used = f"{loc.strategy.type}:{loc.strategy.value}" if loc.strategy else ""
            if scroll_note:
                sr.detail = scroll_note
            if loc.strategy is not None and loc.strategy.type == "visual":
                sr.strategy_used = f"visual:{loc.strategy.value.get('label', '')}"
        self._action_request(step, node, sr.strategy_used)
        t_perform = time.perf_counter()
        res = self.driver.perform(step.action, node, step.args)
        perform_ms = int((time.perf_counter() - t_perform) * 1000)
        if not res.ok:
            sr.status = "error"
            sr.error = res.error
            sr.latency_ms = int((time.perf_counter() - t0) * 1000)
            self._action_result(step, ok=False, detail=res.detail,
                                error=res.error, latency_ms=sr.latency_ms)
            return sr
        sr.strategy_used = sr.strategy_used or (step.strategies[0].type if step.strategies else "")
        sr.node_label = node.label if node else ""
        sr.detail = " · ".join(x for x in (scroll_note, sr.detail, res.detail) if x)
        try:
            self.driver.wait_idle()
        except Exception:
            pass
        after = f"{idx:02d}_{step.step_id}_after.png"
        shot = self._settle_shot()
        if shot:
            (art / after).write_bytes(shot)
            step.evidence.after = after
            sr.screenshot = after
            rec = getattr(self, "_recorder", None)
            if rec is not None:
                try:
                    rec.ingest(shot)
                except Exception:  # noqa: BLE001
                    pass
        sr.latency_ms = int((time.perf_counter() - t0) * 1000)
        self._action_result(step, ok=True, detail=f"{sr.detail} · 驱动耗时 {perform_ms}ms",
                            latency_ms=sr.latency_ms)
        return sr
    def _simple(self, action: Action, args: dict, intent: str,
                art: Path, idx: int, step_id: str = "") -> StepResult:
        t0 = time.perf_counter()
        sr = StepResult(step_id=step_id or f"s{idx + 1:02d}", intent=intent,
                        action=action.value)
        self._log_event("action_request", {
            "step_id": sr.step_id, "action": action.value, "intent": intent,
            "args": dict(args), "node": None, "strategies": [],
            "platform": str(getattr(self.driver, "platform", "") or ""),
        })
        res = self.driver.perform(action, None, args)
        sr.status = "passed" if res.ok else "error"
        sr.error = res.error
        sr.detail = res.detail
        sr.latency_ms = int((time.perf_counter() - t0) * 1000)
        self._log_event("action_result", {
            "step_id": sr.step_id, "action": action.value,
            "ok": res.ok, "detail": res.detail, "error": res.error,
            "latency_ms": sr.latency_ms,
        })
        return sr
