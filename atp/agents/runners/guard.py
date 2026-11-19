"""防打转闸门与步骤效果判定（纯函数为主，便于单测）。
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
class GuardMixin:
    """共享实现。"""
    @staticmethod
    def _input_delivered(step, sr, text: str, node) -> bool:
        """文字确实送进了目标（树盲时唯一可信的信号）。

        除了可编辑控件，Point 节点也算送达：Electron 自绘输入框在 UIA 树里
        根本不是 Edit，point 视觉定位后驱动会 tap 落点 + 清空 + 输入，
        sr.ok 就说明文字真的送出去了（run-20261117-113350：
        s07/s09 打在 Point 上却被判"输入未生效"，于是反复重输）。
        """
        if step.action is not Action.INPUT_TEXT or not sr.ok or not str(text or "").strip():
            return False
        if node is None:
            return False
        if (node.cls or "").lower() == "point":
            return True
        return GuardMixin._looks_editable(node)
    @staticmethod
    def _text_landed(root: UINode, text: str) -> bool:
        """输入的文字到底有没有真的落到界面上（界面树里能看到它）。
        比"画面变了"精确得多：输入两个字只改几十个像素，很容易被判成"没效果"；
        而文字真的出现在树里，就是这一步完成的确凿证据。
        **必须查 value**：桌面 Electron / Chromium 的输入框把用户输入放在
        UIA ValuePattern / DOM input.value 里，控件 Name 仍是占位文案「搜索」。
        只读 text 会永远报"输入未生效"，模型于是一遍遍重输同一个关键词
        （run-20261116-093006：连输 4 次「彭婉」后熔断）。
        """
        want = (text or "").strip()
        if not want:
            return False
        try:
            for n in root.walk():
                if any(want in (f or "") for f in (n.text, n.desc, getattr(n, "value", ""))):
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False
    @staticmethod
    def _looks_editable(node: UINode) -> bool:
        """这个节点像不像输入框（桌面 UIA 的 Edit / DOM 的 input / 无障碍的 EditText）。"""
        from atp.agents.perception.locator import is_editable
        try:
            if is_editable(node.cls):
                return True
        except Exception:  # noqa: BLE001
            pass
        return (node.desc or "") == "可编辑输入区" or node.cls == "VisualTarget"

    @staticmethod
    def _taps_editable(node, obs) -> bool:
        """这一步要点的区域是不是压在输入框上。

        点输入框只会唤起/收起键盘——键盘一开一合会让画面签名变化，把
        "界面没变"的重复判据骗过去（run-20261117-170154：s05~s07 连点
        搜索框三次，每次都被当成新动作）。目标矩形与任何可见输入框相交
        即算压上，不要求节点本身是 Edit（鸿蒙 TextInput 常包在 Stack 里）。
        """
        from atp.agents.perception.locator import is_editable
        if node is None or not getattr(node, "bounds", None) \
                or node.bounds == (0, 0, 0, 0):
            return False
        try:
            x1, y1, x2, y2 = node.bounds
        except (TypeError, ValueError):
            return False
        try:
            for n in obs.root.walk():
                if not is_editable(n.cls) or not n.visible or not n.bounds \
                        or n.bounds == (0, 0, 0, 0):
                    continue
                bx1, by1, bx2, by2 = n.bounds
                if bx1 < x2 and bx2 > x1 and by1 < y2 and by2 > y1:
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False
    @staticmethod
    def _step_rect(step: Step, node: UINode | None,
                   obs: Observation) -> tuple[float, float, float, float] | None:
        """这一步"点在了哪块区域"，用归一化坐标（0~1000）表示。
        point 步骤（视觉定位）每拍都会给出略有抖动的新坐标（同一个搜索栏
        三次点击的 bounds 各不相同），拿 bounds 当签名等于每次都算新动作，
        重复计数永远数不到——run-20261113-173646 连点三次搜索栏就是这么来的。
        """
        if node is not None and node.bounds != (0, 0, 0, 0):
            x1, y1, x2, y2 = node.bounds
            w, h = obs.width or 1, obs.height or 1
            return (x1 / w * 1000, y1 / h * 1000, x2 / w * 1000, y2 / h * 1000)
        for st in step.strategies or []:
            if st.type != "bounds":
                continue
            try:
                x1, y1, x2, y2 = (float(v) for v in st.value)
            except (TypeError, ValueError):
                continue
            return (x1, y1, x2, y2)
        return None
    @staticmethod
    def _same_target(a: tuple[float, float, float, float] | None,
                     b: tuple[float, float, float, float] | None,
                     obs: Observation, tol_px: int = 45) -> bool:
        """两个落点是不是"同一个目标"。
        只比中心点是不够的：同一个输入框，一次按整行框住（250,690,870,750）、
        一次只框住文本框内部（247,694,1000,705），中心差了 60 多像素，
        就会被当成两个目标，于是"点一次输入框、再点一次输入框"照做不误
        （run-20261113-180724 的 s07/s08）。
        所以三个条件满足其一就算同一目标：
        - 中心距离 ≤ 45px（视觉定位抖动）；
        - 重叠面积 / 较小框面积 ≥ 0.6（大框套小框）；
        - IoU ≥ 0.3（两个框大体重合）。
        """
        if a is None or b is None:
            return True     # 拿不到坐标（如按键、返回）就只比动作签名
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        tol_x = tol_px / max(obs.width, 1) * 1000
        tol_y = tol_px / max(obs.height, 1) * 1000
        if (abs((ax1 + ax2) / 2 - (bx1 + bx2) / 2) <= tol_x
                and abs((ay1 + ay2) / 2 - (by1 + by2) / 2) <= tol_y):
            return True
        iw = min(ax2, bx2) - max(ax1, bx1)
        ih = min(ay2, by2) - max(ay1, by1)
        if iw <= 0 or ih <= 0:
            return False
        inter = iw * ih
        area_a = max((ax2 - ax1) * (ay2 - ay1), 1e-6)
        area_b = max((bx2 - bx1) * (by2 - by1), 1e-6)
        if inter / min(area_a, area_b) >= 0.6:
            return True
        return inter / (area_a + area_b - inter) >= 0.3
    @staticmethod
    def _intent_sig(intent: str) -> str:
        """意图文本的归一化签名：去掉空白与常见标点，只留语义。
        模型重复同一个动作时，意图几乎一模一样（"点击【发送】按钮，将已输入的
        测试消息发送出去" 出现两次）。比文本比坐标更能认出"这是在重复"。
        """
        text = str(intent or "")
        for ch in " \t\r\n，。、；：！？【】「」『』（）()《》\"'`~·-—_/\\|,.;:!?":
            text = text.replace(ch, "")
        return text
    @staticmethod
    def _recap(done: list[tuple[str, str]]) -> str:
        """把"已经做完的事"重新摆到模型面前。
        模型往回到前置步骤，多半不是因为它想做错，而是因为它**忘了自己已经做完了**——
        典型场景：搜完关键词点开结果后，应用把搜索框清空，它看到空框就以为没搜，
        于是回到搜索框重来（run-20261113-182603）。这时给一份已完成清单，
        比任何"不要重复"的抽象劝告都管用。
        """
        if not done:
            return ""
        items = "；".join(f"{sid} {intent}" for sid, intent in done[-6:])
        return ("【进度回顾】到目前已经**完成**了：" + items +
                "。这些事都做过了、界面也确实变了，不要再回到它们对应的界面重做；"
                "请对照用例要求，继续做还没做的那一步。")
    @staticmethod
    def _history_line(step: Step, act: str, changed: bool, progressed: bool,
                      ratio: float | None) -> str:
        """回给模型的这一步结论。
        "OK" 是不够的：模型看到 OK 会以为点中了，界面其实纹丝不动。
        把"有没有产生效果"写进历史，模型才可能改招，而不是原地重复。
        """
        head = f"{step.step_id} {act} {step.intent} -> OK"
        if progressed:
            # 进入了新页面要明说：run-20261113-211702 里模型点开彭婉会话后，
            # 历史只有一行 OK，它不知道页面已切换，继续找"搜索结果"直到熔断。
            intent = f"{step.intent}"
            if any(k in intent for k in
                   ("进入", "打开", "查看", "会话", "聊天", "详情", "结果")):
                return (head + "，且界面已切换到新的页面/视图——"
                        "你点击的目标**已经打开**。请基于当前界面找下一步的元素"
                        "（输入栏、按钮等），不要再去点上一个页面的目标或重新搜索。")
            return head
        if not changed:
            detail = "界面没有任何变化"
            if ratio == 0.0:
                detail += "（画面与点击前完全一致）"
        else:
            detail = "画面只有细微变化（焦点框/提示文案级别），不足以说明这一步生效"
        return (head + f"，但{detail}：这次动作没有实质推进，"
                "目标可能已经处于目标状态，不要重复它")
    @staticmethod
    def _commit_like(step: Step) -> bool:
        """这一步是不是"一次性提交"类动作（发送/提交/确认/登录…）。
        这类按钮点一次就达成了目的，再点一次没有任何"第二件事"可做
        （run-20261113-180724 的 s10/s11 连点两次【发送】）。
        而翻页、连续加减这类动作重复是**合法**的，不能一并判死。
        """
        text = f"{step.intent} {step.args.get('text', '')}"
        return any(k in text for k in ("发送", "提交", "确定", "确认", "登录", "保存",
                                       "发布", "支付", "购买", "上传", "注册"))
    @staticmethod
    def _repeat_hint(step: Step, act: str, done: bool = False) -> str:
        """重复动作被跳过时，给模型的下一句话——要具体到"该怎么改"。"""
        text = f"{step.intent} {step.args.get('text', '')}"
        if done:
            # 上一步其实已经成功了，模型还要再来一次（连点两次【发送】就是这个）
            return ("【系统提示】这个动作上一步已经执行成功、界面也确实变了，"
                    "**不需要再做第二遍**（发送/确认/登录这类按钮尤其如此）。"
                    "请直接进入下一步。如果你确认第一次真的没生效，"
                    "或者这本来就是连续操作（例如翻页），"
                    "就在下一次把目的写清楚（换一种说法说明这是第几次/为什么），我才会执行。")
        if any(k in text for k in ("搜索栏", "搜索框", "输入框", "输入栏", "聊天框", "输入区")):
            return ("【系统提示】这个输入位置已经点过了、界面没有任何变化——"
                    "说明它**已经处于可输入状态**，不要再点它。"
                    "直接把要输入的文本用 action=input 发出去（树上没有对应节点时，"
                    "带上 target 说明是哪个输入框即可）。")
        return ("【系统提示】这个动作刚刚做过、界面没有任何变化，已跳过重复执行"
                "（重复不会有新结果）。请换一个节点或换一种做法；"
                "如果目标其实已经达成，直接执行下一步。")
