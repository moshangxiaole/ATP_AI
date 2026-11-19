"""跨层数据契约：节点、观测、动作与设备模型。
这里的类型不含任何驱动或平台逻辑，保证七层之间只传数据、不传实现。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator


class RunCancelled(RuntimeError):
    """用户主动中止执行。

    从事件回调里抛出，穿过执行器向上冒泡——这是唯一一种允许打断执行流程的异常，
    其余异常在 emit 处一律吞掉，避免观测侧的问题拖垮用例执行。
    """


class Action(str, Enum):
    """统一动作集：四端驱动都必须实现这些，上层完全无感。"""

    LAUNCH_APP = "launch_app"
    TERMINATE_APP = "terminate_app"
    INSTALL = "install"
    TAP = "tap"
    LONG_PRESS = "long_press"
    DOUBLE_TAP = "double_tap"
    INPUT_TEXT = "input_text"
    CLEAR_TEXT = "clear_text"
    SWIPE = "swipe"
    SCROLL_TO = "scroll_to"
    BACK = "back"
    HOME = "home"
    PRESS_KEY = "press_key"
    WAIT_FOR = "wait_for"
    SCREENSHOT = "screenshot"
    OPEN_URL = "open_url"
    # 最大化窗口：Web 用例的固定第二拍。屏幕尺寸直接决定所有归一化坐标的映射，
    # 不先固定画布就去采坐标，换个窗口大小整条用例都会偏。
    MAXIMIZE = "maximize"

    @property
    def needs_target(self) -> bool:
        return self in TARGET_ACTIONS


# 需要先定位到元素才能执行的动作。
# wait_for 不在此列：它的语义是"等界面满足断言"，靠后置断言完成，不需要目标元素。
TARGET_ACTIONS = {
    Action.TAP,
    Action.LONG_PRESS,
    Action.DOUBLE_TAP,
    Action.INPUT_TEXT,
    Action.CLEAR_TEXT,
    Action.SCROLL_TO,
}


@dataclass
class UINode:
    """平台无关的界面节点。Android 的无障碍树、鸿蒙的 layout、UIA 的控件树、
    DOM 的 role 树，全部归一化成这个结构，定位器与判定器只认它。"""

    cls: str = ""
    id: str = ""
    text: str = ""
    # 输入框里"当前实际填了什么"。UIA/DOM 的控件 Name 常常只是占位文案
    # （搜索框的「搜索」），真正的值在 ValuePattern / input.value 里。
    # 必须与 text 分开：text 参与定位器与文案匹配，value 只表示运行时状态，
    # 否则「搜索框里已经有彭婉」会被当成又一个可点的「彭婉」结果。
    value: str = ""
    desc: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    clickable: bool = False
    enabled: bool = True
    checked: bool = False
    # 这个控件自己能不能滚（RecyclerView / ScrollView / List / Swiper …）。
    # 滑动查找靠它判断"当前页面滚不滚得动"，否则在播放器、弹窗、纯静态页上
    # 乱滑一次就可能退出页面或拖到进度条。
    scrollable: bool = False
    # 有没有键盘焦点。用来判断"这一步是不是其实已经做完了"：模型常先点一下输入框
    # 再点一下再点一下（界面没有任何变化），而"已经聚焦"就是最可靠的完成信号。
    focused: bool = False
    children: list["UINode"] = field(default_factory=list)

    # ---------- 结构 ----------
    def walk(self) -> Iterator["UINode"]:
        yield self
        for c in self.children:
            yield from c.walk()

    def flatten(self) -> list["UINode"]:
        return list(self.walk())

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    @property
    def area(self) -> int:
        x1, y1, x2, y2 = self.bounds
        return max(0, x2 - x1) * max(0, y2 - y1)

    @property
    def visible(self) -> bool:
        """能不能真的点到它。

        uiautomator 对**滚动出视口的节点**并不会省略，而是把 bounds 报成
        (0,0,0,0)（H5 表单、长列表都这样）。这种节点"在树里但不在屏幕上"：
        按它去点等于点左上角 (0,0)，界面一动不动，然后被误判成"已找到、已点击"。
        定位器必须把它当作没找到，滑动查找才会被触发。
        """
        return self.area > 0

    @property
    def label(self) -> str:
        return self.text or self.desc or self.id or self.cls

    def contains(self, x: int, y: int, tol: int = 0) -> bool:
        x1, y1, x2, y2 = self.bounds
        return (x1 - tol) <= x <= (x2 + tol) and (y1 - tol) <= y <= (y2 + tol)

    # ---------- 检索 ----------
    def find_by_id(self, value: str) -> list["UINode"]:
        return [n for n in self.walk() if n.id == value]

    def find_by_text(self, value: str, contains: bool = False) -> list["UINode"]:
        out = []
        for n in self.walk():
            for f in (n.text, n.desc, n.value):
                if not f:
                    continue
                if (value in f) if contains else (f == value):
                    out.append(n)
                    break
        return out

    def find_smallest_at(self, x: int, y: int, tol: int = 0) -> "UINode | None":
        hits = [n for n in self.walk() if n.contains(x, y, tol) and n.area > 0]
        return min(hits, key=lambda n: n.area) if hits else None

    # ---------- 指纹与提示词 ----------
    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for n in self.walk():
            h.update(f"{n.cls}|{n.id}|{n.text}|{n.value}|{n.desc}|{n.bounds}".encode())
        return h.hexdigest()[:16]

    def to_prompt(self, max_nodes: int = 90) -> str:
        """裁剪成给模型看的扁平列表：只保留可交互或有文本的节点。"""
        lines = []
        for n in self.walk():
            if not (n.clickable or n.text or n.desc or n.id or n.value):
                continue
            if len(lines) >= max_nodes:
                lines.append("... (truncated)")
                break
            x, y = n.center
            flag = "可点击" if n.clickable else "静态"
            label = n.text or n.desc or ""
            val = f" value={n.value!r}" if n.value else ""
            lines.append(
                f"[{len(lines)}] {flag} cls={n.cls} id={n.id or '-'} "
                f"text={label!r}{val} center=({x},{y}) bounds={n.bounds}"
            )
        return "\n".join(lines)


@dataclass
class Observation:
    """一次界面观测：节点树 + 截图 + 画布尺寸，是规划、判定、报告的共同输入。"""
    platform: str
    device_id: str
    width: int = 0
    height: int = 0
    root: UINode = field(default_factory=UINode)
    screenshot: bytes = b""
    url: str = ""            # web / Electron 才有；用于零成本的地址断言
    taken_at: float = field(default_factory=time.time)

    @property
    def fingerprint(self) -> str:
        return self.root.fingerprint()


@dataclass
class ActionResult:
    """单次动作的执行结果，包含成功标志与驱动返回的细节。"""
    ok: bool
    action: str
    detail: str = ""
    error: str = ""


@dataclass
class DeviceInfo:
    """设备池发现的一台设备：平台、ID、来源与在线状态。"""
    platform: str
    device_id: str
    name: str = ""
    kind: str = "local"  # local | remote | virtual
    status: str = "ready"
    provider: str = ""

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "device_id": self.device_id,
            "name": self.name or self.device_id,
            "kind": self.kind,
            "status": self.status,
            "provider": self.provider,
        }
