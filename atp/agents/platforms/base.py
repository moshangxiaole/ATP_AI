"""平台接入契约。

四端驱动继承 BaseDriver，只需实现 4 个原语（connect/screenshot/dump_tree/screen_size）
和若干输入原语，上层执行器永远只调用 perform()。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from atp.config import Config
from atp.common.stepir import ScreenProfile
from atp.common.types import Action, ActionResult, Observation, UINode


class NotSupported(RuntimeError):
    pass


class BaseDriver(ABC):
    platform: str = "generic"

    # 输入前是否先清空目标框。真机上搜索框几乎总残留上一次的关键词、草稿、
    # 联系人名，直接 input_text 会拼成"示例演员示例电视剧"，搜索结果直接空。
    # 清空只对"看起来是输入框"的节点做（见 _looks_editable），
    # 否则 tap 歪了之后 Ctrl+A/Delete 可能删掉别处的正文。
    clear_before_input: bool = True

    def __init__(self, device_id: str, config: Config | None = None):
        self.device_id = device_id
        self.config = config or Config()
        self._connected = False
        self.app_id = ""
        self._focused: UINode | None = None

    # ---------- 生命周期 ----------
    @abstractmethod
    def connect(self) -> None: ...

    def close(self) -> None:
        self._connected = False

    # ---------- 感知 ----------
    @abstractmethod
    def dump_tree(self) -> UINode: ...

    @abstractmethod
    def screenshot(self) -> bytes: ...

    @abstractmethod
    def screen_size(self) -> tuple[int, int]: ...

    def device_pixel_ratio(self) -> float:
        """逻辑像素 → 物理像素的比例。Web 上等于 devicePixelRatio，
        手机会是 densityDpi/160。截图与点击坐标系不一致时，缩放靠它兜底。"""
        return 1.0

    def screen_profile(self) -> ScreenProfile:
        """当前画布档案：用例沉淀的基准 + 回放的缩放比都从这里取。"""
        w, h = self.screen_size()
        return ScreenProfile(width=int(w), height=int(h),
                             dpr=float(self.device_pixel_ratio()),
                             platform=self.platform,
                             label=f"{self.platform}-{int(w)}x{int(h)}")

    def maximize(self) -> tuple[int, int]:
        """把窗口放到最大并返回新的画布尺寸。

        只有窗口化的端点（Web / Electron / 桌面）能实现；移动端天然全屏，
        调用方应先用 supports(Action.MAXIMIZE) 判断，别直接依赖异常。
        """
        raise NotSupported(f"{self.platform} 不支持窗口最大化")

    def observe(self) -> Observation:
        w, h = self.screen_size()
        return Observation(
            platform=self.platform,
            device_id=self.device_id,
            width=w,
            height=h,
            root=self.dump_tree(),
            screenshot=self.screenshot(),
            url=self.current_url,
        )

    @property
    def current_url(self) -> str:
        """有地址栏概念的驱动（Web/Electron）覆盖它，用于地址断言。"""
        return ""

    def wait_idle(self, timeout_ms: int = 1500) -> None:
        """动作触发页面跳转/重渲染后，等界面稳定再截图。

        默认空实现；有页面概念的驱动（Web/Electron）应覆盖，
        否则报告里"点击后"的截图会拍在跳转完成前，看起来像没生效。
        """

    @property
    def tree_stale(self) -> bool:
        """上一次抓到的树是不是"缓存顶的"（界面还在动，抓不到新的）。

        调用方拿到 True 时不能拿它做点击决策：缓存树可能已经是两三秒前的界面，
        坐标落点完全可能是另一个页面。移动端实测踩过：开屏广告的「跳过」坐标
        在广告结束的那一刻正好是首页的搜索入口。
        """
        return False

    # ---------- 输入原语 ----------
    def tap_xy(self, x: int, y: int) -> None:
        raise NotSupported(f"{self.platform} 不支持 tap_xy")

    def long_press_xy(self, x: int, y: int, ms: int = 800) -> None:
        raise NotSupported(f"{self.platform} 不支持 long_press_xy")

    def double_tap_xy(self, x: int, y: int) -> None:
        self.tap_xy(x, y)
        time.sleep(0.08)
        self.tap_xy(x, y)

    def swipe_xy(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        raise NotSupported(f"{self.platform} 不支持 swipe_xy")

    def input_text(self, text: str) -> None:
        raise NotSupported(f"{self.platform} 不支持 input_text")

    def clear_text(self) -> None:
        raise NotSupported(f"{self.platform} 不支持 clear_text")

    # ---------- 输入前的清空 ----------
    def _looks_editable(self, node: UINode | None) -> bool:
        """这个节点像不像输入框。

        只对"像输入框"的节点清空：tap 落在列表行、正文区上时，
        Ctrl+A/Delete（桌面、Web）会选中整页并删掉正文，代价远大于"输入拼在一起"。
        """
        if node is None:
            return False
        from atp.agents.perception.locator import is_editable

        if is_editable(node.cls):
            return True
        if (node.desc or "") == "可编辑输入区":      # 驱动统一钩出来的输入区描述
            return True
        # desc 存的是平台的资源标识（Android resource-id / UIA AutomationId），
        # "searchEdit""inputBox" 这类名字只有从这里才看得出来。
        desc = (node.desc or "").lower()
        if desc and any(k in desc for k in ("edit", "input", "textfield", "search")):
            return True
        ident = (node.id or "").lower()
        return any(k in ident for k in ("edit", "input", "textfield", "search", "et_"))

    def _clear_if_editable(self, node: UINode | None, force: bool = False) -> bool:
        """输入框里可能有上一次的搜索词/草稿，先清干净。

        `force=True`（INPUT_TEXT 专用）：模型既然选择在这里打字，tap 落点后
        无条件 Ctrl+A+Delete 再输入——Electron 自绘输入框在 UIA 树里只是
        一个 Group / Point，`_looks_editable` 认不出来，不清空就会把残留
        文字和新内容拼在一起（run-20261117-113350 的三轮重复输入）。
        非输入动作仍走原来的可编辑判定，避免误删别处正文。
        """
        if not self.clear_before_input:
            return False
        if force:
            # 输入动作强制清空，但排除明确的静态正文/列表行——
            # 在 TextView 上 Ctrl+A+Delete 会删掉别处内容（安全边界）。
            static_cls = {"textview", "text", "static", "label", "image", "imagerview"}
            if (node is not None and (node.cls or "").lower() in static_cls
                    and not self._looks_editable(node)):
                return False
        elif not self._looks_editable(node):
            return False
        try:
            self.clear_text()
            return True
        except NotSupported:
            return False
        except Exception:  # noqa: BLE001
            return False

    def press_key(self, name: str) -> None:
        raise NotSupported(f"{self.platform} 不支持 press_key")

    def launch_app(self, app_id: str) -> None:
        raise NotSupported(f"{self.platform} 不支持 launch_app")

    def terminate_app(self, app_id: str) -> None:
        raise NotSupported(f"{self.platform} 不支持 terminate_app")

    def find_app(self, name: str) -> tuple[str, str] | None:
        """按应用名在设备上找包名，返回（包名, 入口组件）；找不到返回 None。

        只有移动端有意义（Web/桌面没有"应用包名"这个概念），所以默认不支持。
        实现方必须是"找不到就说找不到"，不允许猜：猜错的包名会启动一个无关应用，
        比报错难查得多。结果由调用方写进 apps.json 复用。
        """
        return None

    def install(self, path: str) -> None:
        raise NotSupported(f"{self.platform} 不支持 install")

    def open_url(self, url: str) -> None:
        raise NotSupported(f"{self.platform} 不支持 open_url")

    # ---------- 统一执行入口 ----------
    def perform(self, action: Action, node: UINode | None = None,
                args: dict[str, Any] | None = None) -> ActionResult:
        args = args or {}
        try:
            if action in (Action.TAP, Action.LONG_PRESS, Action.DOUBLE_TAP,
                          Action.INPUT_TEXT, Action.CLEAR_TEXT):
                if node is None:
                    return ActionResult(False, action.value, error="缺少目标元素")
                self._focused = node
                x, y = node.center

                if action is Action.TAP:
                    self.tap_xy(x, y)
                elif action is Action.LONG_PRESS:
                    self.long_press_xy(x, y, int(args.get("duration_ms", 800)))
                elif action is Action.DOUBLE_TAP:
                    self.double_tap_xy(x, y)
                elif action is Action.INPUT_TEXT:
                    self.tap_xy(x, y)
                    text = str(args.get("text", ""))
                    # 所有输入都先清空再输入：一次"有残留"就够让整条用例
                    # 搜索结果全错（run-20261117-113350 用户明确要求）。
                    # force=True 让自绘输入框（树里只是 Group/Point）也清。
                    cleared = self._clear_if_editable(node, force=True)
                    if text:
                        self.input_text(text)
                    if cleared:
                        return ActionResult(True, action.value,
                                            f"命中 {node.label!r}（输入前已清空）")
                elif action is Action.CLEAR_TEXT:
                    self.tap_xy(x, y)
                    self.clear_text()
                return ActionResult(True, action.value, f"命中 {node.label!r}")

            if action is Action.SWIPE:
                # direction 说的是"手指往哪划"：up = 手指上滑 = 看到更下面的内容。
                # 这是滑动查找的方向口径，四个端必须一致，否则"往下翻"会翻反。
                direction = args.get("direction", "up")
                # 一次半屏、600ms：0.6 屏 + 300ms 会被系统当成"甩"，抬手后还会滑很远，
                # 目标正好被推过视野。滑动查找自己用的是更小的步幅（scroll_find.swipe_ratio）
                distance = int(args.get("distance", 0.5 * self.screen_size()[1]))
                w, h = self.screen_size()
                cx, cy = w // 2, h // 2
                vectors = {
                    "up": (cx, cy + distance // 2, cx, cy - distance // 2),
                    "down": (cx, cy - distance // 2, cx, cy + distance // 2),
                    "left": (w // 2 + distance // 2, cy, w // 2 - distance // 2, cy),
                    "right": (w // 2 - distance // 2, cy, w // 2 + distance // 2, cy),
                }
                x1, y1, x2, y2 = vectors.get(direction, vectors["up"])
                self.swipe_xy(x1, y1, x2, y2, int(args.get("duration_ms", 600)))
                return ActionResult(True, action.value, f"向{direction}滑动")

            if action is Action.LAUNCH_APP:
                # `or` 不是多余：AI 规划器会产出 args={"app": ""}（键存在值为空），
                # get 的默认值取不到，必须显式兜底，否则驱动收到空包名直接报错。
                self.launch_app(str(args.get("app") or self.app_id or ""))
                return ActionResult(True, action.value, "已启动")

            if action is Action.TERMINATE_APP:
                self.terminate_app(str(args.get("app") or self.app_id or ""))
                return ActionResult(True, action.value, "已关闭")

            if action is Action.INSTALL:
                self.install(str(args.get("path", "")))
                return ActionResult(True, action.value, "已安装")

            if action is Action.OPEN_URL:
                self.open_url(str(args.get("url", "")))
                return ActionResult(True, action.value, "已打开")

            if action is Action.MAXIMIZE:
                w, h = self.maximize()
                return ActionResult(True, action.value, f"已最大化 {w}x{h}")

            if action is Action.PRESS_KEY:
                self.press_key(str(args.get("key", "back")))
                return ActionResult(True, action.value, str(args.get("key")))

            if action is Action.BACK:
                self.press_key("back")
                return ActionResult(True, action.value, "返回")

            if action is Action.HOME:
                self.press_key("home")
                return ActionResult(True, action.value, "回桌面")

            if action in (Action.WAIT_FOR, Action.SCREENSHOT, Action.SCROLL_TO):
                return ActionResult(True, action.value, "无需输入操作")

            return ActionResult(False, action.value, error="未实现的动作")
        except NotSupported as e:
            return ActionResult(False, action.value, error=str(e))
        except Exception as e:  # 驱动层任何异常都不能让整轮崩掉
            return ActionResult(False, action.value, error=f"{type(e).__name__}: {e}")

    # 子类把不支持的动作登记在这里，执行器会提前跳过而不是运行时报错
    UNSUPPORTED: set[Action] = set()

    def supports(self, action: Action) -> bool:
        if action in self.UNSUPPORTED:
            return False
        # 登记表容易漏：再按"是否真的覆写了原语"兜一层。
        # 否则像测试假设备这种什么都继承自基类的驱动会被判定为"支持最大化"，
        # 一调就抛 NotSupported，白白多一条失败步骤。
        if action is Action.MAXIMIZE:
            return type(self).maximize is not BaseDriver.maximize
        if action is Action.OPEN_URL:
            return type(self).open_url is not BaseDriver.open_url
        # 冷启动 / 滑动查找都靠这两组原语，先问一句再动手，
        # 免得在测试假设备、Web、桌面这类端上白跑一遍异常。
        if action is Action.LAUNCH_APP:
            return type(self).launch_app is not BaseDriver.launch_app
        if action is Action.TERMINATE_APP:
            return type(self).terminate_app is not BaseDriver.terminate_app
        if action is Action.SWIPE:
            return type(self).swipe_xy is not BaseDriver.swipe_xy
        return True
