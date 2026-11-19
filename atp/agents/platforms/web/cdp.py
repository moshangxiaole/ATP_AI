"""Web / Chromium 系桌面端驱动（CDP）。

Electron、CEF、内嵌 Chromium 的客户端，只要启动时带上
    --remote-debugging-port=9222
就能被 Playwright 直接接管，拿到 DOM 与选择器——稳定性接近 Web，AI 成本接近零。
这是整个 PC 端投产比最高的一条路，务必优先验证。
"""

from __future__ import annotations

import os
import time

from atp.common.types import Action, UINode
from atp.agents.platforms.base import BaseDriver, NotSupported

from atp.agents.platforms.web.dom_js import JS_DUMP as _JS_DUMP

# Playwright 的 evaluate 期望"表达式"；箭头函数字面量必须显式调用才会返回结果
JS_DUMP_IIFE = f"({_JS_DUMP})()"


class WebCdpDriver(BaseDriver):
    platform = "web"

    def __init__(self, device_id: str = "", config=None, endpoint: str = "", url: str = ""):
        super().__init__(device_id or "chromium", config)
        self.endpoint = endpoint or os.environ.get("ATP_CDP_ENDPOINT", "")
        self.url = url
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._cdp_sess = None

    # ---------- 生命周期 ----------
    def connect(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError("缺少 playwright：pip install playwright && playwright install chromium") from e

        self._pw = sync_playwright().start()
        if self.endpoint:
            self._browser = self._pw.chromium.connect_over_cdp(self.endpoint)
            ctxs = self._browser.contexts
            self._ctx = ctxs[0] if ctxs else self._browser.new_context()
            pages = self._ctx.pages
            self._page = pages[0] if pages else self._ctx.new_page()
        else:
            launch_args = {"headless": bool(os.environ.get("ATP_HEADLESS"))}
            try:
                # 优先用本机已装 Chrome：免去 playwright install 下载几百 MB
                self._browser = self._pw.chromium.launch(channel="chrome", **launch_args)
            except Exception:
                self._browser = self._pw.chromium.launch(**launch_args)
            # no_viewport=True 是关键：让视口跟随真实窗口大小，
            # 否则窗口最大化之后视口仍是 1440x900，截图与坐标对不上。
            self._ctx = self._browser.new_context(no_viewport=True)
            self._page = self._ctx.new_page()
            if self.url:
                self._page.goto(self.url, wait_until="domcontentloaded")
                self.maximize()
        self._page.set_default_timeout(15000)
        self._connected = True

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        finally:
            self._connected = False

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("驱动未连接")
        return self._page

    @property
    def current_url(self) -> str:
        try:
            return self.page.url or ""
        except Exception:  # noqa: BLE001
            return ""

    # ---------- 感知 ----------
    def _retry(self, fn, attempts: int = 3, delay: float = 0.6):
        """导航/跳转期间执行上下文会被销毁，页面类操作需要重试。"""
        last: Exception | None = None
        for i in range(attempts):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(delay * (i + 1))
        if last is not None:
            raise last
        return None

    def screen_size(self) -> tuple[int, int]:
        """优先读真实视口：窗口最大化之后 innerWidth/innerHeight 才是坐标的真值。"""
        try:
            v = self.page.evaluate("[window.innerWidth, window.innerHeight]")
            if isinstance(v, list) and len(v) == 2 and int(v[0]) > 0:
                return int(v[0]), int(v[1])
        except Exception:  # noqa: BLE001
            pass
        vp = self.page.viewport_size or {"width": 1440, "height": 900}
        return int(vp["width"]), int(vp["height"])

    def device_pixel_ratio(self) -> float:
        try:
            return float(self.page.evaluate("window.devicePixelRatio") or 1.0)
        except Exception:  # noqa: BLE001
            return 1.0

    # ---------- 窗口 ----------
    def _cdp(self):
        if self._cdp_sess is None:
            self._cdp_sess = self.page.context.new_cdp_session(self.page)
        return self._cdp_sess

    def maximize(self) -> tuple[int, int]:
        """最大化浏览器窗口（CDP Browser.setWindowBounds）。

        为什么要先 normal 再 maximized：Chrome 在已经是 maximized 的状态下
        再次 setWindowBounds(maximized) 不会触发重排，窗口尺寸就停在中间态。
        走一遍 normal 才能确保真的铺满。
        """
        try:
            sess = self._cdp()
            info = sess.send("Browser.getWindowForTarget", {})
            win = int(info.get("windowId") or 0)
            if win:
                sess.send("Browser.setWindowBounds",
                          {"windowId": win, "bounds": {"windowState": "normal"}})
                sess.send("Browser.setWindowBounds",
                          {"windowId": win, "bounds": {"windowState": "maximized"}})
                time.sleep(0.6)
                self.page.wait_for_timeout(200)
        except Exception:  # noqa: BLE001
            # 无头模式 / 远端 CDP 不允许改窗口：保持当前视口，把决定权交给尺寸档案
            pass
        return self.screen_size()

    def screenshot(self) -> bytes:
        return self._retry(lambda: self.page.screenshot(type="png")) or b""

    def dump_tree(self) -> UINode:
        raw = self._retry(lambda: self.page.evaluate(JS_DUMP_IIFE)) or []
        w, h = self.screen_size()
        root = UINode(cls="Dom", bounds=(0, 0, w, h))
        for item in raw:
            root.children.append(
                UINode(
                    cls=item["tag"],
                    id=item["id"],
                    text=item["text"],
                    value=str(item.get("value") or ""),
                    desc=item["desc"],
                    bounds=tuple(item["bounds"]),  # type: ignore[arg-type]
                    clickable=bool(item["clickable"]),
                )
            )
        return root

    # ---------- 输入 ----------
    def tap_xy(self, x: int, y: int) -> None:
        self.page.mouse.click(x, y)

    def double_tap_xy(self, x: int, y: int) -> None:
        self.page.mouse.dblclick(x, y)

    def long_press_xy(self, x: int, y: int, ms: int = 800) -> None:
        self.page.mouse.move(x, y)
        self.page.mouse.down()
        self.page.wait_for_timeout(ms)
        self.page.mouse.up()

    def swipe_xy(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.page.mouse.move(x1, y1)
        self.page.mouse.down()
        self.page.mouse.move(x2, y2, steps=10)
        self.page.mouse.up()

    def input_text(self, text: str) -> None:
        self.page.keyboard.type(text)

    def clear_text(self) -> None:
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Delete")

    def press_key(self, name: str) -> None:
        mapping = {"back": "Alt+Left", "home": "Home", "enter": "Enter",
                   "tab": "Tab", "delete": "Delete"}
        self.page.keyboard.press(mapping.get(name.lower(), name))

    def open_url(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # 打开即最大化：先定画布，再采坐标。顺序反了就会"按小窗口录、按大窗口放"。
        self.maximize()
        self.wait_idle(6000)

    def wait_idle(self, timeout_ms: int = 1500) -> None:
        try:
            self.page.wait_for_load_state("load", timeout=timeout_ms)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.35)

    UNSUPPORTED = {Action.LAUNCH_APP, Action.INSTALL}
