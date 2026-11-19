"""原生 CDP 驱动：不依赖 Playwright，对接任意版本的 Electron / Chromium。

为什么不复用 WebCdpDriver（Playwright）：
Playwright 新版本会用 `Browser.setDownloadBehavior(browserContextId=...)` 初始化默认上下文，
而 Electron 20（Chromium 104）不支持浏览器级上下文管理，连接直接失败。
企业内网里 Electron 20 这类老版本是常态，所以这里用最小 CDP 实现，只依赖协议本身。

覆盖的能力：DOM 快照、截图、鼠标、键盘、文本输入、导航。
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
from contextlib import contextmanager
from typing import Any, Iterator

from atp.common.types import Action, UINode
from atp.agents.platforms.base import BaseDriver

# DOM 快照脚本与 Web 驱动共用
from atp.agents.platforms.web.dom_js import JS_DUMP as _JS_DUMP

_PROXY_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
               "all_proxy", "ALL_PROXY")


@contextmanager
def no_proxy_env() -> Iterator[None]:
    """临时摘掉代理变量：本机回环地址走代理会 502，这是很常见的坑。"""
    saved = {k: os.environ.pop(k, None) for k in _PROXY_KEYS}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


class CdpError(RuntimeError):
    pass


class ElectronCdpDriver(BaseDriver):
    platform = "desktop"

    def __init__(self, device_id: str = "electron", config=None,
                 endpoint: str = "http://127.0.0.1:9333", url: str = "",
                 match_url: str = ""):
        super().__init__(device_id, config)
        self.endpoint = (endpoint or os.environ.get("ATP_CDP_ENDPOINT", "")
                         or "http://127.0.0.1:9333").rstrip("/")
        self.url = url
        self.match_url = match_url
        self._ws = None
        self._seq = 0
        self._target: dict[str, Any] = {}

    # ---------- 连接 ----------
    def _list_targets(self) -> list[dict[str, Any]]:
        with no_proxy_env():
            with urllib.request.urlopen(f"{self.endpoint}/json/list", timeout=10) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))

    def _probe(self, ws_url: str) -> tuple[int, int]:
        """量一下某个 target 的可视面积，用来从多个窗口里挑主窗口。"""
        try:
            import websocket

            with no_proxy_env():
                ws = websocket.create_connection(
                    ws_url, timeout=6,
                    http_no_proxy=["127.0.0.1", "localhost", "::1"],
                    suppress_origin=True,
                )
            try:
                ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                    "params": {"expression":
                                               "[window.innerWidth, window.innerHeight]",
                                               "returnByValue": True}}))
                for _ in range(20):
                    raw = ws.recv()
                    if not raw:
                        break
                    msg = json.loads(raw)
                    if msg.get("id") == 1:
                        v = ((msg.get("result") or {}).get("result") or {}).get("value")
                        if isinstance(v, list) and len(v) == 2:
                            return int(v[0]), int(v[1])
                        break
            finally:
                ws.close()
        except Exception:
            pass
        return 0, 0

    def _pick(self, targets: list[dict[str, Any]]) -> dict[str, Any]:
        pages = [t for t in targets
                 if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not pages:
            raise CdpError(f"{self.endpoint} 上没有 page 目标，无法接管")
        if len(pages) == 1:
            return pages[0]

        want = (self.match_url or self.url).split("?")[0]
        if want:
            for t in pages:
                if want in str(t.get("url", "")):
                    return t

        # Electron 常见附带小工具窗口（图片查看器、预览器等），按可视面积取最大
        best, best_area = pages[0], -1
        for t in pages:
            w, h = self._probe(t["webSocketDebuggerUrl"])
            area = w * h
            if area > best_area:
                best, best_area = t, area
        return best

    def connect(self) -> None:
        try:
            import websocket
        except ImportError as e:
            raise RuntimeError("缺少 websocket-client：pip install websocket-client") from e

        self._target = self._pick(self._list_targets())
        with no_proxy_env():
            self._ws = websocket.create_connection(
                self._target["webSocketDebuggerUrl"], timeout=25,
                http_no_proxy=["127.0.0.1", "localhost", "::1"],
                suppress_origin=True,
            )
        for domain in ("Page", "Runtime"):
            try:
                self.call(f"{domain}.enable")
            except CdpError:
                pass
        self._connected = True

    def close(self) -> None:
        try:
            if self._ws is not None:
                self._ws.close()
        finally:
            self._ws = None
            self._connected = False

    # ---------- 协议 ----------
    def call(self, method: str, timeout: float = 25.0, **params) -> dict[str, Any]:
        if self._ws is None:
            raise CdpError("CDP 未连接")
        self._seq += 1
        mid = self._seq
        try:
            self._ws.send(json.dumps({"id": mid, "method": method, "params": params},
                                     ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            raise CdpError(f"{method} 发送失败: {e}") from e
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = self._ws.recv()
            except Exception as e:  # noqa: BLE001  含 socket 超时
                raise CdpError(f"{method} 等待超时: {type(e).__name__}") from e
            if not raw:
                raise CdpError("CDP 连接已断开")
            msg = json.loads(raw)
            if msg.get("id") != mid:
                continue  # 事件帧，忽略
            if "error" in msg:
                raise CdpError(f"{method} 失败: {msg['error'].get('message')}")
            return msg.get("result") or {}
        raise CdpError(f"{method} 超时")

    def evaluate(self, expression: str, timeout: float = 12.0):
        res = self.call("Runtime.evaluate", timeout=timeout, expression=expression,
                        returnByValue=True, awaitPromise=True)
        if res.get("exceptionDetails"):
            raise CdpError(str(res["exceptionDetails"])[:200])
        return (res.get("result") or {}).get("value")

    # ---------- 感知 ----------
    def screen_size(self) -> tuple[int, int]:
        try:
            v = self.evaluate("[window.innerWidth, window.innerHeight]")
            if isinstance(v, list) and len(v) == 2:
                return int(v[0]), int(v[1])
        except CdpError:
            pass
        return 1280, 800

    def device_pixel_ratio(self) -> float:
        try:
            return float(self.evaluate("window.devicePixelRatio") or 1.0)
        except CdpError:
            return 1.0

    def maximize(self) -> tuple[int, int]:
        """Electron 桌面端同样可以走 CDP 改窗口状态（老版本不支持就保持原样）。"""
        try:
            info = self.call("Browser.getWindowForTarget")
            win = int(info.get("windowId") or 0)
            if win:
                self.call("Browser.setWindowBounds", windowId=win,
                          bounds={"windowState": "normal"})
                self.call("Browser.setWindowBounds", windowId=win,
                          bounds={"windowState": "maximized"})
                time.sleep(0.6)
        except CdpError:
            pass
        return self.screen_size()

    def screenshot(self) -> bytes:
        """截图失败不该让用例中断。

        Chromium 的 captureScreenshot 在窗口被遮挡/最小化时会等待新帧而一直不返回，
        所以这里给短超时并逐级降级（fromSurface=False 直接从渲染器取），最后才放弃。
        """
        variants = (
            {"format": "png"},
            {"format": "png", "fromSurface": False},
            {"format": "jpeg", "quality": 80, "fromSurface": False},
        )
        for params in variants:
            try:
                res = self.call("Page.captureScreenshot", timeout=8, **params)
                data = res.get("data")
                if data:
                    return base64.b64decode(data)
            except CdpError:
                continue
        return b""

    def dump_tree(self) -> UINode:
        # 注意：Runtime.evaluate 拿到的是"表达式"，箭头函数必须显式调用才有返回值
        raw = None
        for attempt in range(3):
            try:
                raw = self.evaluate(f"({_JS_DUMP})()")
                break
            except CdpError:
                if attempt == 2:
                    raw = []
                time.sleep(0.4)
        raw = raw or []
        w, h = self.screen_size()
        root = UINode(cls="Dom", bounds=(0, 0, w, h))
        for item in raw:
            bounds = tuple(item.get("bounds") or (0, 0, 0, 0))
            if len(bounds) != 4:
                continue
            root.children.append(UINode(
                cls=str(item.get("tag") or ""),
                id=str(item.get("id") or ""),
                text=str(item.get("text") or ""),
                value=str(item.get("value") or ""),
                desc=str(item.get("desc") or ""),
                bounds=bounds,  # type: ignore[arg-type]
                clickable=bool(item.get("clickable")),
            ))
        return root

    # ---------- 输入 ----------
    def _mouse(self, kind: str, x: int, y: int, button: str = "left",
               buttons: int = 0, click_count: int = 1) -> None:
        self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y,
                  button=button, buttons=buttons, clickCount=click_count)

    def tap_xy(self, x: int, y: int) -> None:
        self._mouse("mouseMoved", x, y, button="none", buttons=0)
        self._mouse("mousePressed", x, y, buttons=1)
        self._mouse("mouseReleased", x, y, buttons=0)

    def double_tap_xy(self, x: int, y: int) -> None:
        self._mouse("mouseMoved", x, y, button="none", buttons=0)
        self._mouse("mousePressed", x, y, buttons=1, click_count=2)
        self._mouse("mouseReleased", x, y, buttons=0, click_count=2)

    def long_press_xy(self, x: int, y: int, ms: int = 800) -> None:
        self._mouse("mouseMoved", x, y, button="none", buttons=0)
        self._mouse("mousePressed", x, y, buttons=1)
        time.sleep(ms / 1000)
        self._mouse("mouseReleased", x, y, buttons=0)

    def swipe_xy(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        steps = max(4, min(20, ms // 20))
        self._mouse("mouseMoved", x1, y1, button="none", buttons=0)
        self._mouse("mousePressed", x1, y1, buttons=1)
        for i in range(1, steps + 1):
            x = x1 + (x2 - x1) * i // steps
            y = y1 + (y2 - y1) * i // steps
            self._mouse("mouseMoved", x, y, buttons=1)
        self._mouse("mouseReleased", x2, y2, buttons=0)

    def input_text(self, text: str) -> None:
        # insertText 直接把文本塞进焦点元素，不受输入法/键盘布局影响，中文可用
        self.call("Input.insertText", text=text)

    def clear_text(self) -> None:
        self.call("Input.dispatchKeyEvent", type="keyDown", modifiers=2,
                  windowsVirtualKeyCode=65, key="a", code="KeyA")
        self.call("Input.dispatchKeyEvent", type="keyUp", modifiers=2,
                  windowsVirtualKeyCode=65, key="a", code="KeyA")
        self.press_key("delete")

    _KEYS = {
        "enter": (13, "Enter", "Enter"),
        "tab": (9, "Tab", "Tab"),
        "delete": (46, "Delete", "Delete"),
        "backspace": (8, "Backspace", "Backspace"),
        "esc": (27, "Escape", "Escape"),
        "back": (27, "Escape", "Escape"),
        "home": (36, "Home", "Home"),
        "space": (32, " ", "Space"),
        "up": (38, "ArrowUp", "ArrowUp"),
        "down": (40, "ArrowDown", "ArrowDown"),
        "left": (37, "ArrowLeft", "ArrowLeft"),
        "right": (39, "ArrowRight", "ArrowRight"),
    }

    def press_key(self, name: str) -> None:
        code, key, dom_code = self._KEYS.get(name.lower(), (13, "Enter", "Enter"))
        self.call("Input.dispatchKeyEvent", type="keyDown",
                  windowsVirtualKeyCode=code, key=key, code=dom_code)
        self.call("Input.dispatchKeyEvent", type="keyUp",
                  windowsVirtualKeyCode=code, key=key, code=dom_code)

    def open_url(self, url: str) -> None:
        self.call("Page.navigate", url=url)
        time.sleep(0.8)
        # 与应用内跳转一致：先把窗口定死，再采坐标/截图
        self.maximize()

    def wait_idle(self, timeout_ms: int = 1500) -> None:
        """等 readyState 到 complete，最多 timeout_ms；超时就算了，别拖慢用例。"""
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    break
            except CdpError:
                pass
            time.sleep(0.15)
        time.sleep(0.2)

    UNSUPPORTED = {Action.LAUNCH_APP, Action.INSTALL, Action.TERMINATE_APP}

    # ---------- 便捷 ----------
    @property
    def title(self) -> str:
        return str(self._target.get("title", ""))

    @property
    def current_url(self) -> str:
        try:
            return str(self.evaluate("location.href") or "")
        except Exception:  # noqa: BLE001
            return str(self._target.get("url", ""))
