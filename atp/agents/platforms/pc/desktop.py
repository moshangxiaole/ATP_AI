"""PC 桌面端驱动：三档降级覆盖"所有类型"的桌面应用。

Tier 1  CDP        —— Electron / CEF / 内嵌 Chromium。拿 DOM，最稳，成本最低
Tier 2  UIA        —— Win32 / WPF / WinForms / Qt / 部分 Java（Windows 无障碍树）
Tier 3  视觉       —— canvas、游戏、自绘界面、macOS/Linux 无无障碍时；靠坐标 + 视觉定位

定位策略：能用 Tier 1 绝不用 Tier 2，能用 Tier 2 绝不用 Tier 3。
降级不用人工干预，detect() 会自己判断，用例层完全无感。
"""

from __future__ import annotations

import io
import os
import sys
import time

from atp.common.types import Action, UINode
from atp.agents.platforms.base import BaseDriver, NotSupported
from atp.agents.platforms.web.cdp import WebCdpDriver


def _grab() -> bytes:
    from PIL import ImageGrab

    # 必须和 screen_size() 同一个口径（都取主屏）：早先这里 all_screens=True
    # 截出 3840 宽的双屏图，而 screen_size 报 1920 —— 视觉模板按 3840 归一化、
    # 还原坐标却乘 1920，X 直接砍半，视觉定位点全偏（run-20261113-164346）。
    # 被测应用在回桌面+最大化后就在主屏，副屏（编辑器/控制台）本来也不该入镜。
    img = ImageGrab.grab()
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _screen_size() -> tuple[int, int]:
    from PIL import ImageGrab

    img = ImageGrab.grab()
    return img.width, img.height


# ---------------------------------------------------------------- Tier 2: UIA
class DesktopUiaDriver(BaseDriver):
    """Windows 无障碍(UIA)驱动。控件级操作，比坐标稳得多。"""

    platform = "desktop"

    def __init__(self, device_id: str = "desktop", config=None, window: str = ""):
        super().__init__(device_id, config)
        self.window_name = window or os.environ.get("ATP_WINDOW", "")
        self._auto = None
        self._controls: dict[str, object] = {}
        self._root_ctrl = None

    def connect(self) -> None:
        try:
            import uiautomation as auto
        except ImportError as e:
            raise RuntimeError("缺少 uiautomation：pip install uiautomation") from e
        auto.SetGlobalSearchTimeout(3)
        self._auto = auto
        self._root_ctrl = self._pick_window()
        self._connected = True

    def _pick_window(self):
        auto = self._auto
        if self.window_name:
            ctrl = auto.WindowControl(searchDepth=2, RegexName=self.window_name)
            if ctrl.Exists(3):
                return ctrl
            raise RuntimeError(f"未找到窗口: {self.window_name}")
        ctrl = auto.GetForegroundControl()
        # 从焦点控件往上找到顶层窗口
        for _ in range(12):
            parent = ctrl.GetParentControl()
            if parent is None or parent.ControlTypeName == "PaneControl" and parent.GetParentControl() is None:
                break
            ctrl = parent
        return ctrl

    def screen_size(self) -> tuple[int, int]:
        return _screen_size()

    def maximize(self) -> tuple[int, int]:
        """UIA 窗口最大化。最大化之后 BoundingRectangle 才是全屏，坐标先定后采。"""
        ctrl = self._root_ctrl
        if ctrl is None:
            raise NotSupported("尚未锁定窗口，无法最大化")
        done = False
        try:
            wp = ctrl.GetWindowPattern()
            if wp is not None:
                wp.SetWindowVisualState(1)  # 1 = Maximized
                done = True
        except Exception:  # noqa: BLE001
            done = False
        if not done:
            try:
                ctrl.Maximize()
                done = True
            except Exception:  # noqa: BLE001
                done = False
        if not done:
            raise NotSupported("该窗口不支持最大化")
        time.sleep(0.5)
        return self.screen_size()

    def screenshot(self) -> bytes:
        return _grab()

    def dump_tree(self) -> UINode:
        root = self._dump_once()
        # Electron/Chromium 翻页后，新 renderer 的无障碍树要延迟一会儿才暴露：
        # 第一次 dump 常常只拿到一个光杆窗口（run-20261113-205329：点进会话后
        # 模型看到"节点列表为空"，以为界面坏了直接 done）。等它长出来。
        if self._hollow(root):
            self.focus_app()          # 拉回前台，顺便戳一下 a11y
            for _ in range(3):
                time.sleep(0.45)
                root = self._dump_once()
                if not self._hollow(root):
                    return root
            # 还不行：窗口句柄可能真死了（切后台/最小化/重启），重新认一次
            try:
                self._root_ctrl = self._pick_window()
            except Exception:  # noqa: BLE001
                pass
            for _ in range(2):
                time.sleep(0.5)
                root = self._dump_once()
                if not self._hollow(root):
                    return root
        return root

    @staticmethod
    def _hollow(root: UINode) -> bool:
        """这棵树是不是只剩空壳。

        判据：带文字的节点 <= 1（通常只剩窗口标题）。真实业务页面（聊天、
        列表、表单）少则十几条多则上百条，光杆窗口整棵树只有一个 Name。
        误判的代价只是多 dump 一两遍（~1s），漏判的代价是模型直接放弃。
        """
        nodes = list(root.walk())
        # 排除框架噪声：run-20261117-124001 的树有 `156544`（数字句柄）和
        # `SampleDesktop`（窗口类名）两个文字，旧的 <=1 判据被越过，空壳没触发自愈。
        noise = {"SampleDesktop", "示例聊天应用", "示例聊天应用-测试版", "Desktop", "Screen", "Window"}
        with_text = [
            n for n in nodes
            if (n.text or "").strip()
            and not n.text.strip().isdigit()
            and len(n.text.strip()) > 1
            and n.text.strip() not in noise
        ]
        return len(nodes) <= 3 or not with_text

    def _dump_once(self) -> UINode:
        w, h = self.screen_size()
        root = UINode(cls="Desktop", bounds=(0, 0, w, h))
        if self._root_ctrl is None:
            return root
        self._controls.clear()
        counter = [0]
        main = self._to_node(self._root_ctrl, 0, counter)
        if main is not None:
            root.children.append(main)
        # 同一进程的其它顶层窗口也要进树：更新提示 / 确认框常常是独立 window，
        # 只 dump 主窗口的话弹窗守卫看不见「下次再说」、模型也点不到它的按钮
        # （run-20261113-164346，「发现新版本」弹窗从头挂到尾没人关）。
        try:
            pid = getattr(self._root_ctrl, "ProcessId", None)
            rid = self._root_ctrl.GetRuntimeId()
            for win in self._auto.GetRootControl().GetChildren():
                try:
                    if win.ControlTypeName != "WindowControl" or win.ProcessId != pid:
                        continue
                    if win.GetRuntimeId() == rid:
                        continue
                    # Chromium 会留一堆零尺寸的隐藏窗口，进树只会挤掉真实内容
                    rect = win.BoundingRectangle
                    if rect.width() <= 0 or rect.height() <= 0:
                        continue
                    sub = self._to_node(win, 1, counter)
                    if sub is not None:
                        root.children.append(sub)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        return root

    # Chromium 会把鼠标悬停提示也暴露成 ToolTip 控件，文案与真实条目一模一样。
    # 点它没有任何反应，模型却会反复选中它（run-20261113-164346：连点 6 次 ToolTip
    # 里的「联系人乙」，页面纹丝不动）。这类纯展示层不进树。
    _SKIP_CLASSES = {"ToolTip"}

    def _to_node(self, ctrl, depth: int, counter: list[int]) -> UINode | None:
        cls_name = str(getattr(ctrl, "ControlTypeName", "") or "")
        if cls_name.replace("Control", "") in self._SKIP_CLASSES:
            return None
        counter[0] += 1
        try:
            r = ctrl.BoundingRectangle
            bounds = (int(r.left), int(r.top), int(r.right), int(r.bottom))
        except Exception:
            bounds = (0, 0, 0, 0)

        # RuntimeId 是列表（例如 [42, 1001]），只取 [0] 会得到进程号——
        # 同一窗口所有控件共用它，id 全是 uia://42，_controls 互相覆盖，
        # 模型在节点列表里也分不出「发送」和「附件」（run-20261117-110950）。
        # 用完整 id 链；拿不到时退化到遍历序号，保证同一次 dump 内唯一。
        key = ""
        try:
            rid = ctrl.GetRuntimeId()
            rid = list(rid) if rid else []
            key = "uia://" + "-".join(str(v) for v in rid) if rid else f"uia://n{counter[0]}"
        except Exception:
            key = f"uia://n{counter[0]}"
        if key:
            self._controls[key] = ctrl

        # Name 为空的 Button 在模型眼里就是一串「text=''」，只能靠 point 猜。
        # 依次用 Name → LegacyIAccessible.Name → AutomationId → 局部自动化属性补文案。
        label = str(getattr(ctrl, "Name", "") or "").strip()
        if not label:
            try:
                lp = ctrl.GetLegacyIAccessiblePattern()
                if lp is not None:
                    label = str(getattr(lp, "CurrentName", "") or "").strip()
            except Exception:  # noqa: BLE001 —— 控件不支持该模式
                pass
        if not label:
            label = str(getattr(ctrl, "AutomationId", "") or "").strip()
        if not label and "Button" in cls_name:
            # Electron 常见：按钮 Name 空、AutomationId 空，但 aria-label 在
            # LegacyIAccessible 的 Description 里；再退一步用控件类型占位。
            try:
                lp = ctrl.GetLegacyIAccessiblePattern()
                if lp is not None:
                    label = str(getattr(lp, "CurrentDescription", "") or "").strip()
            except Exception:  # noqa: BLE001
                pass

        n = UINode(
            cls=cls_name.replace("Control", ""),
            id=key,
            text=label[:40],
            desc=str(getattr(ctrl, "AutomationId", "") or ""),
            bounds=bounds,
            clickable=bool(getattr(ctrl, "IsKeyboardFocusable", False)),
            enabled=bool(getattr(ctrl, "IsEnabled", True)),
        )
        # 输入框当前值：UIA 的 Name 常是占位文案（「搜索」），用户真正输入的
        # 内容只在 ValuePattern / LegacyIAccessible 里。漏读会让模型永远看不到
        # 自己刚输的关键词，从而一遍遍重输（run-20261116-093006）。
        if "Edit" in cls_name:
            for getter in ("GetValuePattern", "GetLegacyIAccessiblePattern"):
                try:
                    pattern = getattr(ctrl, getter)()
                    if pattern is not None:
                        current = str(getattr(pattern, "CurrentValue", "") or "")
                        if current:
                            n.value = current[:120]
                            break
                except Exception:  # noqa: BLE001 —— 控件不支持该模式，换下一种
                    continue

        # 焦点位：判断"是不是已经点到这个输入框了"的唯一可靠信号。
        # 点一下搜索框，界面树本身是不变的（还是那几行），只有焦点变了。
        try:
            n.focused = bool(getattr(ctrl, "HasKeyboardFocus", False))
        except Exception:  # noqa: BLE001
            pass
        # 可滚动性：UIA ScrollPattern 是最准的口径。「滑动前先看页面能不能滑」
        # 的闸门（scroller / AI swipe 守卫）在桌面上全靠这一位，否则一律当成
        # 不可滑，长列表永远翻不动。
        try:
            sp = ctrl.GetScrollPattern()
            if sp is not None:
                n.scrollable = bool(sp.IsVerticallyScrollable)
        except Exception:  # noqa: BLE001 —— 没有滚动模式的控件保持 False
            pass

        if depth < 14 and counter[0] < 400:
            try:
                for child in ctrl.GetChildren():
                    if child is not None:
                        sub = self._to_node(child, depth + 1, counter)
                        if sub is not None:
                            n.children.append(sub)
            except Exception:
                pass
        return n

    # ---------- 输入 ----------
    def focus_app(self) -> None:
        """把被测窗口拉回前台。

        桌面端最大的干扰不是弹窗在应用内，而是**别的进程**突然冒出来抢焦点：
        run-20261113-164346 执行到一半，百度网盘弹出「上传完成」+「拖拽至此上传」
        浮层，之后每一次点击第一下都只是在激活那个窗口，落不到搜索结果的
        「联系人乙」上——界面纹丝不动，模型连点 6 次被判原地打转。
        """
        if self._root_ctrl is None or self._auto is None:
            return
        try:
            fg = self._auto.GetForegroundControl()
            if fg is not None and fg.ProcessId == self._root_ctrl.ProcessId:
                return
        except Exception:  # noqa: BLE001 —— 拿不到前台就按"需要拉回"处理
            pass
        try:
            self._root_ctrl.SetActive()
            time.sleep(0.15)
        except Exception:  # noqa: BLE001
            # Windows 的前台锁会拦 SetActive，退回 Win32 SetForegroundWindow
            try:
                self._auto.SetForegroundWindow(self._root_ctrl.NativeWindowHandle)
                time.sleep(0.15)
            except Exception:  # noqa: BLE001
                try:
                    self._root_ctrl.SetFocus()
                except Exception:  # noqa: BLE001
                    pass

    def tap_xy(self, x: int, y: int) -> None:
        self.focus_app()
        self._auto.Click(x, y)

    def double_tap_xy(self, x: int, y: int) -> None:
        self.focus_app()
        self._auto.DoubleClick(x, y)

    def long_press_xy(self, x: int, y: int, ms: int = 800) -> None:
        self.focus_app()
        self._auto.RightClick(x, y)

    def swipe_xy(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.focus_app()
        dx, dy = x2 - x1, y2 - y1
        # 桌面端的"滑动"语义是滚动列表/页面（找长列表里的条目），不是移动端手势：
        # 用鼠标滚轮实现——滚不动时画面不变、无任何副作用。DragDrop 在桌面端
        # 会选中文字/拖动文件，风险远大于收益（run-20261113-211702：模型想滚动
        # 会话列表找联系人，被当成拖拽处理）。
        if abs(dy) >= abs(dx):
            try:
                self._auto.MoveTo(x2, y2, waitTime=0.05)
            except Exception:  # noqa: BLE001 —— 移不动就在当前位置滚
                pass
            # 1 滚轮格 ≈ 100px，向下挥(dy<0)=内容上移=滚轮向下
            wheel = max(1, min(12, abs(dy) // 100))
            if dy < 0:
                self._auto.WheelDown(wheel)
            else:
                self._auto.WheelUp(wheel)
        else:
            self._auto.DragDrop(x1, y1, x2, y2)

    def input_text(self, text: str) -> None:
        if not text:
            return
        self.focus_app()
        # 中文逐字符 SendKeys 会被 Electron 自绘输入框静默丢弃；优先走剪贴板。
        try:
            self._auto.SetClipboardText(text)
        except Exception:  # noqa: BLE001 —— 老版本 uiautomation 没有剪贴板 API
            self._auto.SendKeys(text, waitTime=0.02)
        else:
            self._auto.SendKeys("{Ctrl}v", waitTime=0.05)

    def clear_text(self) -> None:
        self.focus_app()
        self._auto.SendKeys("{Ctrl}a{Delete}")

    def press_key(self, name: str) -> None:
        self.focus_app()
        mapping = {"back": "{Esc}", "home": "{Win}", "enter": "{Enter}",
                   "tab": "{Tab}", "delete": "{Delete}"}
        self._auto.SendKeys(mapping.get(name.lower(), "{" + name + "}"))

    def perform(self, action: Action, node: UINode | None = None, args=None):
        # 点击统一走屏幕坐标（bounds 中心），而不是 UIA 控件级 Click：
        # Chromium/Electron 的控件 Click 走 GetClickablePoint，落在网页内容上
        # 经常不触发页面自己的点击处理器——run-20261113-205329 的 s04 点了
        # 搜索结果节点，下拉面板纹丝不动。人手就是拿鼠标点坐标，最稳。
        if action in (Action.TAP,) and node is not None and node.id in self._controls:
            x1, y1, x2, y2 = node.bounds
            if x2 > x1 and y2 > y1 and x2 > 0 and y2 > 0:
                self.focus_app()
                self._auto.Click((x1 + x2) // 2, (y1 + y2) // 2)
                return super().perform(Action.WAIT_FOR, None, {"ms": 0})
            # bounds 无效（零尺寸/隐藏控件）才退回控件级点击
            try:
                self.focus_app()
                self._controls[node.id].Click(simulateMove=False)
                return super().perform(Action.WAIT_FOR, None, {"ms": 0})
            except Exception:
                pass
        return super().perform(action, node, args)

    def launch_app(self, app_id: str) -> None:
        target = str(app_id or "").strip()
        if not target:
            raise RuntimeError("未指定要启动的应用（app_id 为空）：请在 apps.json 的 "
                               "desktop 分区登记应用名与启动路径")
        # 台账里 desktop 的 app_id 存的是 exe 路径；JSON 里写的是正斜杠，
        # Windows 上统一成反斜杠最稳（os.startfile 对正斜杠时好时坏）。
        if ":" in target or "\\" in target or target.startswith("/"):
            target = os.path.normpath(target)
        # 「一键回桌面，仅打开被测应用」：先把所有顶层窗口最小化，排除浏览器/
        # 聊天工具等无关页面对树和截图的干扰（run-20261113-154411：ATP 控制台
        # 浏览器占了半屏，模型一直在两块界面之间来回认错目标）。
        self._minimize_all_windows()
        os.startfile(target)  # type: ignore[attr-defined]
        # 启动后重新锁定前台窗口：connect() 时锁的是当时的窗口，不刷新下一句
        # dump_tree 还会停在旧窗口上，控件级点击会落到错误目标。os.startfile
        # 一般会顺带把新应用提到前台，等它起来再重新取一次即可（失败则保留旧句柄）。
        time.sleep(1.0)
        try:
            self._root_ctrl = self._pick_window()
        except Exception:  # noqa: BLE001 —— 取不到新窗口不该让 launch 报错
            pass
        # 被测应用直接铺满屏幕：跟「打开 URL 即最大化」同一语义，坐标分母稳定，
        # 也避免残留窗口跟被测界面并排干扰视觉定位（失败不拦 launch）。
        try:
            if self._root_ctrl is not None:
                wp = self._root_ctrl.GetWindowPattern()
                if wp is not None:
                    wp.SetWindowVisualState(1)  # 1 = Maximized
        except Exception:  # noqa: BLE001
            pass

    def _minimize_all_windows(self) -> None:
        """把所有顶层窗口最小化（等价"回到桌面"）。

        不用 Win+D：那是开关式的，桌面已经干净时再按会把窗口全弹回来；
        逐个最小化天然幂等。取不到窗口模式（无 UIA）就安静跳过，不拦启动。

        注意 UIA 的 WindowVisualState 枚举是 **Normal=0 / Maximized=1 / Minimized=2**：
        早先错写成 0，等于把窗口"恢复原状"，回桌面从来没生效过
        （run-20261113-164346 截图里 VS Code / 控制台仍在屏上）。
        """
        auto = self._auto
        if auto is None:
            return
        try:
            for win in auto.GetRootControl().GetChildren():
                try:
                    if win.ControlTypeName not in ("WindowControl", "PaneControl"):
                        continue
                    wp = win.GetWindowPattern()
                    if wp is not None:
                        wp.SetWindowVisualState(2)  # 2 = Minimized
                except Exception:  # noqa: BLE001 —— 不可最小化的窗口直接跳过
                    continue
        except Exception:  # noqa: BLE001
            pass

    UNSUPPORTED = {Action.INSTALL, Action.OPEN_URL, Action.TERMINATE_APP}


# ---------------------------------------------------------------- Tier 3: 视觉
class DesktopVisionDriver(BaseDriver):
    """纯视觉兜底：无节点树，只能按坐标操作。

    配合 AI 视觉定位（返回归一化坐标）即可驱动任何自绘界面，
    但每条用例的成本会显著高于前两档——只在前两档不可用时使用。
    """

    platform = "desktop"

    def __init__(self, device_id: str = "desktop-vision", config=None):
        super().__init__(device_id, config)

    def connect(self) -> None:
        try:
            import pyautogui  # noqa: F401
        except ImportError as e:
            raise RuntimeError("缺少 pyautogui：pip install pyautogui") from e
        self._connected = True

    def screen_size(self) -> tuple[int, int]:
        return _screen_size()

    def screenshot(self) -> bytes:
        return _grab()

    def dump_tree(self) -> UINode:
        w, h = self.screen_size()
        return UINode(cls="Screen", bounds=(0, 0, w, h))

    def tap_xy(self, x: int, y: int) -> None:
        import pyautogui

        pyautogui.click(x, y)

    def double_tap_xy(self, x: int, y: int) -> None:
        import pyautogui

        pyautogui.doubleClick(x, y)

    def long_press_xy(self, x: int, y: int, ms: int = 800) -> None:
        import pyautogui

        pyautogui.mouseDown(x, y)
        pyautogui.sleep(ms / 1000)
        pyautogui.mouseUp()

    def swipe_xy(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        import pyautogui

        pyautogui.moveTo(x1, y1)
        pyautogui.dragTo(x2, y2, duration=max(0.1, ms / 1000))

    def input_text(self, text: str) -> None:
        import pyautogui

        pyautogui.write(text)

    def clear_text(self) -> None:
        import pyautogui

        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("delete")

    def press_key(self, name: str) -> None:
        import pyautogui

        pyautogui.press(name.lower())

    UNSUPPORTED = {Action.LAUNCH_APP, Action.INSTALL, Action.OPEN_URL, Action.TERMINATE_APP,
                   Action.MAXIMIZE}


# ---------------------------------------------------------------- 路由
def open_desktop(device_id: str = "desktop", config=None,
                 endpoint: str = "", url: str = "", window: str = "") -> BaseDriver:
    """按可用性自动选档：CDP > UIA > 视觉。"""
    ep = endpoint or os.environ.get("ATP_CDP_ENDPOINT", "")
    if ep:
        drv = WebCdpDriver(device_id=device_id, config=config, endpoint=ep, url=url)
        drv.platform = "desktop"
        return drv

    if sys.platform == "win32":
        try:
            import uiautomation  # noqa: F401

            return DesktopUiaDriver(device_id=device_id, config=config, window=window)
        except ImportError:
            pass

    return DesktopVisionDriver(device_id=device_id, config=config)
