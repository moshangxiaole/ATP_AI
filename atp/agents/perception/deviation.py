"""误触 / 进入非步骤页的偏离检测与自动回退（零模型）。

只做两件事：**判得像不像走错页**、**能不能安全地按一次返回**。

* 页面身份只看 ``coarse_sig``（排除 bounds），因此滑动、列表刷新、输入、软键盘
  造成的 ``fine_fp`` 变化绝不触发 BACK（AC-1.10）。
* 回退前必须过 9 条独立禁止清单；任一命中就返回 ``can_back=False``，
  ``press_key("back")`` 调用次数严格为 0。
* 回退后重新走页面稳定门禁，并与 ``stack[-2]`` 的粗粒度签名比对，
  Jaccard ≥ ``recover_match_threshold`` 且包名一致才算真的回去了。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from atp.agents.perception.judge import text_corpus
from atp.agents.perception.page_stabilizer import PageFrame, PageStack, PageStabilizer, jaccard, node_count, page_signature
from atp.common.types import RunCancelled

__all__ = [
    "DeviationVerdict",
    "RecoveryOutcome",
    "DeviationDetector",
    "BackController",
    "DEFAULT_IRREVERSIBLE_MARKERS",
    "DEFAULT_BLOCK_ACTIONS",
]

#: 命中这些词说明这一步可能已经产生不可逆后果，绝不自动 BACK。
DEFAULT_IRREVERSIBLE_MARKERS: tuple[str, ...] = (
    "支付", "提交", "下单", "删除", "转账", "注销", "确认支付", "立即购买",
    "发送", "提交订单", "支付成功", "提交成功", "订单已生成",
)

DEFAULT_BLOCK_ACTIONS: tuple[str, ...] = (
    "back", "home", "terminate_app", "launch_app", "open_url",
)


@dataclass
class DeviationVerdict:
    """一次偏离检测结论。"""

    detected: bool = False
    cause: str = ""
    coarse_similarity: float = 1.0
    can_back: bool = False
    blocked_by: str = ""


@dataclass
class RecoveryOutcome:
    """一次自动回退的结果。"""

    recovered: bool = False
    backs: int = 0
    reason: str = ""
    retry: bool = False


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return bool(getattr(value, "stable", False)) and not bool(getattr(value, "degraded", False))


def _opt(obj: Any, name: str, default: Any) -> Any:
    return getattr(obj, name, default)


def _action_name(step: Any) -> str:
    action = getattr(step, "action", None)
    value = getattr(action, "value", None)
    return str(value or action or "").lower()


def _coarse_of(obs: Any, dynamic_markers: Any) -> frozenset:
    cached = getattr(obs, "coarse_sig", None)
    if isinstance(cached, (set, frozenset)) and cached:
        return frozenset(cached)
    root = getattr(obs, "root", None)
    try:
        return page_signature(root, dynamic_markers)
    except Exception:  # noqa: BLE001
        return frozenset()


def _skeleton(sig: Any) -> frozenset:
    """只留容器骨架（cls/id/层级），用来识别「同页列表刷新」而不是跳页。"""
    try:
        return frozenset(t for t in sig if t and t[0] == "c")
    except TypeError:
        return frozenset()


def _text_of(obs: Any) -> str:
    try:
        return text_corpus(getattr(obs, "root", None))
    except Exception:  # noqa: BLE001
        return ""


def _gate_stable(stabilizer: Any, stable: Any, before: Any, after: Any) -> bool:
    """门禁是否可信：任何 degraded / tree_stale / 超时都视为不可信。"""
    if stable is None:
        stable = getattr(stabilizer, "last_result", None)
    ok = _as_bool(stable, False)
    for obs in (before, after):
        if obs is None:
            continue
        if bool(getattr(obs, "page_gate_degraded", False)):
            ok = False
        if getattr(obs, "page_ready", True) is False:
            ok = False
    driver = getattr(stabilizer, "_driver", None)
    if driver is not None and bool(getattr(driver, "tree_stale", False)):
        ok = False
    return ok


class DeviationDetector:
    """基于粗粒度签名 + 强/弱信号组合的偏离检测器。"""

    def __init__(self, cfg: Any, stack: PageStack, stabilizer: PageStabilizer) -> None:
        self.cfg = cfg
        self._dc = getattr(cfg, "deviation", cfg)
        self._pr = getattr(cfg, "page_ready", None)
        self.stack = stack
        self.stabilizer = stabilizer

    def _markers(self) -> Any:
        return _opt(self._pr, "dynamic_markers", ()) if self._pr is not None else ()

    def detect(
        self,
        *,
        step: Any,
        before: Any,
        after: Any,
        verdict: Any,
        popup_changed: bool = False,
        mode: str = "replay",
        package: str = "",
        expected_package: Optional[str] = None,
    ) -> DeviationVerdict:
        """判断这一步是不是把界面带到了「非步骤页」。

        Args:
            step: 当前步骤。
            before: 动作前观察（门禁通过后的那一帧）。
            after: 动作后观察。
            verdict: ``JudgeResult``（后置断言结论）。
            popup_changed: 本轮是否处置过弹窗。
            mode: ``replay`` / ``ai``。
            package: 前台包名（可得时）。
            expected_package: 「动作之前那一页」的包名。调用方若已把动作后的帧
                压栈，必须显式传进来——否则 ``stack.current()`` 是动作后的页面，
                包名和自己比永远相等，``package_changed`` 这个强信号就废了。
                传 ``None`` 时退回读栈顶（保持既有单测语义）。
        """
        if not bool(_opt(self._dc, "enabled", True)):
            return DeviationVerdict(cause="not_enabled")
        if mode == "ai":
            return DeviationVerdict(cause="ai_mode")

        before_sig = _coarse_of(before, self._markers())
        after_sig = _coarse_of(after, self._markers())
        sim = jaccard(before_sig, after_sig)
        before_nodes = node_count(getattr(before, "root", None))
        after_nodes = node_count(getattr(after, "root", None))
        min_nodes = int(_opt(self._dc, "coarse_sig_min_nodes", 8) or 8)
        out = DeviationVerdict(coarse_similarity=sim)

        # 证据不足：薄树 / 白屏半树 / 空签名，一律不判偏离（AC-1.11）。
        if before_nodes < min_nodes or after_nodes < min_nodes or not before_sig or not after_sig:
            out.cause = "insufficient_evidence"
            return out

        # 正常变化：断言已过、弹窗处置、门禁不可信、同页输入/滑动/列表刷新。
        post = getattr(step, "postcondition", None)
        has_assertion = str(getattr(post, "kind", "none") or "none") != "none"
        if has_assertion and bool(getattr(verdict, "passed", False)):
            out.cause = "assertion_passed"
            return out
        if popup_changed:
            out.cause = "popup_dismissed"
            return out
        if not _gate_stable(self.stabilizer, None, before, after):
            out.cause = "gate_degraded"
            return out
        if bool((getattr(step, "args", None) or {}).get("expect_navigation")):
            out.cause = "expect_navigation"
            return out
        if sim >= 0.98 or before_sig == after_sig:
            # fine_fp 变（bounds/滚动/输入）但 coarse_sig 不变 → 绝不 BACK
            out.cause = "coarse_unchanged"
            return out
        # 容器骨架（cls/id/层级）没变 = 还是同一个页面，只是列表刷新 / 滚动 /
        # 输入后文案变了。AC-1.10：这类情况绝不 BACK，哪怕粗粒度文本全换了。
        skeleton_sim = jaccard(_skeleton(before_sig), _skeleton(after_sig))
        if skeleton_sim >= 0.80:
            out.cause = "same_page_structure"
            return out

        threshold = float(_opt(self._dc, "sim_threshold", 0.35) or 0.35)
        weak: list[str] = []
        strong: list[str] = []
        if sim < threshold:
            weak.append("coarse_similarity_low")
        if before_nodes >= 8 and after_nodes < before_nodes * 0.5:
            weak.append("node_count_collapse")
        # 只从 coarse_sig 派生弱信号：fine_fp（含 bounds）专用于稳定判定，
        # 绝不拿滚动 / 键盘 / 动画造成的指纹变化当作「跳页」证据。
        if skeleton_sim < 0.50:
            weak.append("container_skeleton_changed")

        expected_pkg = ""
        if expected_package is not None:
            expected_pkg = str(expected_package or "")
        else:
            current = self.stack.current()
            if current is not None:
                expected_pkg = str(getattr(current, "package", "") or "")
        if package and expected_pkg and package != expected_pkg:
            strong.append("package_changed")

        post = getattr(step, "postcondition", None)
        witness = str(getattr(post, "witness", "") or "")
        if witness and witness not in _text_of(after):
            strong.append("witness_missing")

        if not strong and len(weak) < 2:
            out.cause = "weak_signals_insufficient"
            return out

        out.detected = True
        out.cause = "deviation:" + ("+".join(strong) if strong else "+".join(weak))
        dummy = DeviationVerdict(detected=True, cause=out.cause, coarse_similarity=sim)
        can_back, blocked = self.check_back_guard(
            step=step, verdict=dummy, before=before, after=after, stack=self.stack,
            package=package, popup_changed=popup_changed, stable=True,
            mode=mode, cancelled=False,
        )
        out.can_back = can_back
        out.blocked_by = blocked
        return out

    def check_back_guard(
        self,
        *,
        step: Any,
        verdict: Any,
        before: Any,
        after: Any,
        stack: PageStack,
        package: str,
        popup_changed: bool,
        stable: Any,
        mode: str,
        cancelled: bool,
    ) -> tuple[bool, str]:
        """9 条禁止清单，每条一个独立 early-return；任一命中绝不按 BACK。"""
        # ① 动作本身就在操作导航栈 / 启动 / 打开链接
        name = _action_name(step)
        args = getattr(step, "args", None) or {}
        post = getattr(step, "postcondition", None)
        has_assertion = str(getattr(post, "kind", "none") or "none") != "none"
        blocked_actions = [str(a).lower() for a in (
            _opt(self._dc, "block_actions", DEFAULT_BLOCK_ACTIONS) or DEFAULT_BLOCK_ACTIONS
        )]
        if name in blocked_actions:
            return False, "blocked_action"
        if name == "press_key" and str(args.get("key", "") or "").lower() in ("back", "backspace"):
            return False, "press_key_back"

        # ② 后置断言已通过 / 用例显式声明这一步会跳页
        if (has_assertion and bool(getattr(verdict, "passed", False))) or \
                str(getattr(verdict, "cause", "")) in (
                    "assertion_passed", "expect_navigation", "normal_change"):
            return False, "assertion_passed_or_expected_navigation"
        if bool(args.get("expect_navigation")):
            return False, "expect_navigation"

        # ③ 树文本命中不可逆词：宁可判失败，也不能替用户回退一个已提交的页面
        markers = [str(m) for m in (
            _opt(self._dc, "irreversible_markers", DEFAULT_IRREVERSIBLE_MARKERS)
            or DEFAULT_IRREVERSIBLE_MARKERS
        )]
        corpus = _text_of(before) + "\n" + _text_of(after)
        for marker in markers:
            if marker and marker in corpus:
                return False, "irreversible_marker:" + marker

        # ④ 本轮处置过弹窗（界面变化是预期内的），且签名节点确已消失
        if popup_changed and bool(getattr(before, "popup_signature_disappeared", True)):
            return False, "popup_dismissed"

        # ⑤ 页面稳定门禁不可信：缓存树 / 超时 / degraded / tree_stale
        if not _gate_stable(self.stabilizer, stable, before, after):
            return False, "gate_untrusted"

        # ⑥ 栈里没有「上一页」可比，BACK 无处可回
        if stack is None or len(stack) < 2:
            return False, "stack_too_shallow"

        # ⑦ 前台包名已不在被测应用：BACK 可能直接退出 App，不能自动做
        current = stack.current()
        expected = str(getattr(current, "package", "") or "") if current is not None else ""
        if package and expected and package != expected:
            return False, "package_out_of_app"

        # ⑧ 语义 / 视觉断言或 assert_motion 进行中
        kind = str(getattr(post, "kind", "") or "")
        method = str(getattr(post, "method", "") or "")
        if kind in ("assert_screen", "assert_motion") or method == "llm":
            return False, "semantic_or_motion_assertion"
        if bool(getattr(after, "semantic_assertion", False)):
            return False, "semantic_assertion_running"

        # ⑨ 用户已取消，或 AI 首跑模式（只写不读记忆、不自动 BACK）
        if cancelled:
            return False, "cancelled"
        if mode == "ai":
            return False, "ai_mode"

        if not bool(_opt(self._dc, "auto_back", True)):
            return False, "auto_back_disabled"
        return True, ""


class BackController:
    """按 BACK → 重新门禁 → 与上一帧粗粒度签名比对 → 成功才请求重跑。"""

    def __init__(
        self,
        driver: Any,
        detector: DeviationDetector,
        stack: PageStack,
        stabilizer: PageStabilizer,
        cfg: Any,
        on_event: Optional[Callable[..., None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.driver = driver
        self.detector = detector
        self.stack = stack
        self.stabilizer = stabilizer
        self.cfg = cfg
        self._dc = getattr(cfg, "deviation", cfg)
        self._on_event = on_event
        self._should_cancel = should_cancel
        self._backs_this_run = 0

    def _fire(self, name: str, **payload: Any) -> None:
        cb = self._on_event
        if cb is None:
            return
        try:
            cb(name, **payload)
            return
        except TypeError:
            pass
        except Exception:  # noqa: BLE001
            return
        try:
            cb({"type": name, **payload})
        except Exception:  # noqa: BLE001
            return

    def _cancel(self) -> bool:
        cb = self._should_cancel
        if cb is None:
            return False
        try:
            return bool(cb())
        except Exception:  # noqa: BLE001
            return False

    def _current_package(self) -> str:
        for key in ("foreground_package", "current_package", "package_name", "app_id"):
            value = getattr(self.driver, key, "")
            if isinstance(value, str) and value:
                return value
            if callable(value):
                try:
                    return str(value() or "")
                except Exception:  # noqa: BLE001
                    return ""
        return ""

    def back_and_verify(self, target: PageFrame, nth: int) -> bool:
        """按一次 BACK 并校验是否真的回到了 ``target`` 页。

        单步上限 ``max_back_per_step``、整 run 上限 ``max_back_per_run``；
        校验只比 ``coarse_sig``（排除 bounds），并要求包名一致。
        """
        per_step = int(_opt(self._dc, "max_back_per_step", 2) or 2)
        per_run = int(_opt(self._dc, "max_back_per_run", 3) or 3)
        if nth > per_step or self._backs_this_run >= per_run:
            return False
        if self._cancel():
            return False

        self._fire("deviation_back", nth=nth, target_frame=getattr(target, "frame_id", ""))
        try:
            self.driver.press_key("back")
        except Exception as exc:  # noqa: BLE001
            self._fire("deviation_recovery_failed", nth=nth, reason=f"press_key:{exc}"[:120])
            return False
        self._backs_this_run += 1
        try:
            self.stabilizer.invalidate()
            result = self.stabilizer.wait_stable(
                "back_verify",
                ceiling_ms=int(_opt(self._dc, "detect_timeout_ms", 1500) or 1500),
            )
        except RunCancelled:
            raise
        except Exception:  # noqa: BLE001
            return False
        if not result.stable or result.degraded:
            return False

        threshold = float(_opt(self._dc, "recover_match_threshold", 0.80) or 0.80)
        sim = jaccard(result.coarse_sig, getattr(target, "coarse_sig", frozenset()))
        if sim < threshold:
            return False
        expected_pkg = str(getattr(target, "package", "") or "")
        if expected_pkg:
            current_pkg = self._current_package()
            if current_pkg and current_pkg != expected_pkg:
                return False
        return True

    def recover(
        self,
        *,
        step: Any,
        verdict: DeviationVerdict,
        before: Any,
        after: Any,
        package: str = "",
        mode: str = "replay",
        cancelled: bool = False,
        popup_changed: bool = False,
    ) -> RecoveryOutcome:
        """检测 → 守卫 → 回退 → 校验；成功时 ``retry=True`` 请求重跑当前步骤恰好 1 次。"""
        if not verdict.detected:
            return RecoveryOutcome(reason=verdict.cause or "no_deviation")
        self._fire("deviation_detected", cause=verdict.cause,
                   coarse_similarity=round(verdict.coarse_similarity, 4),
                   can_back=verdict.can_back, blocked_by=verdict.blocked_by)

        can_back, blocked = self.detector.check_back_guard(
            step=step, verdict=verdict, before=before, after=after, stack=self.stack,
            package=package, popup_changed=popup_changed,
            stable=getattr(self.stabilizer, "last_result", None),
            mode=mode, cancelled=cancelled,
        )
        if not can_back:
            self._fire("deviation_recovery_failed", reason=blocked, backs=0)
            return RecoveryOutcome(recovered=False, backs=0, reason=blocked)

        target = self.stack.previous()
        if target is None:
            self._fire("deviation_recovery_failed", reason="no_target_frame", backs=0)
            return RecoveryOutcome(reason="no_target_frame")

        max_back = int(_opt(self._dc, "max_back_per_step", 2) or 2)
        used = 0
        for nth in range(1, max_back + 1):
            if self._cancel():
                break
            used = nth
            if self.back_and_verify(target, nth):
                # 回到上一页：弹出当前帧，避免后续步骤拿错基线
                try:
                    self.stack.pop()
                except Exception:  # noqa: BLE001
                    pass
                retry = bool(_opt(self._dc, "retry_current_step", 1))
                self._fire("deviation_recovered", backs=nth,
                           target_frame=getattr(target, "frame_id", ""), retry=retry)
                return RecoveryOutcome(recovered=True, backs=nth,
                                       reason="回到上一页并校验通过", retry=retry)
        self._fire("deviation_recovery_failed", reason="back_verify_failed", backs=used)
        return RecoveryOutcome(recovered=False, backs=used, reason="back_verify_failed")

    def reset_run(self) -> None:
        """每个 run 开始时清零整 run 的 BACK 预算。"""
        self._backs_this_run = 0
