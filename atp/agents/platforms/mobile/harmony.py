"""HarmonyOS 驱动（HDC 通道）。

说明：HarmonyOS NEXT 已不向三方应用开放无障碍的节点查询与手势注入能力，
因此这里走 HDC + UiTest 的"外挂式"路线——Agent 跑在 PC，通过 hdc 调系统 uitest 服务。
不要试图用自研无障碍 APK 替代，那条路在 NEXT 上是死的。
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from atp.common.types import Action, UINode
from atp.agents.platforms.base import BaseDriver, NotSupported

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
# bm dump -a 输出里的 bundleName（前面可能带 `Bundle Count: N` 之类表头）
_BUNDLE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
# bm dump -n <bundle> 里的应用名，不同版本字段名不同，都抓一遍
_LABELS = re.compile(r'"(?:label|appName|applicationName|name)"\s*:\s*"([^"]{1,40})"')

# OpenHarmony KeyCode：keyEvent 只认数字或 Back/Home/Power
_KEYCODE_BACK = 2
_KEYCODE_DEL = 2055
_KEYCODE_ENTER = 2054
_KEYCODE_A = 2017
_KEYCODE_CTRL_LEFT = 2072

_KEYMAP = {
    "back": "Back",
    "home": "Home",
    "power": "Power",
    "volume_up": "VolumeUp",
    "volume_down": "VolumeDown",
    "enter": str(_KEYCODE_ENTER),
    "delete": str(_KEYCODE_DEL),
}


class HarmonyHdcDriver(BaseDriver):
    platform = "harmony"

    def __init__(self, device_id: str, config=None):
        super().__init__(device_id, config)
        self.hdc = self.config.hdc or "hdc"
        self._size = (0, 0)
        self._bundles: list[str] = []   # 已装 bundle 缓存（找应用时用）
        self._abilities: dict[str, str] = {}   # bundle → 入口 Ability 缓存
        self._tmp = Path(tempfile.gettempdir()) / "atp_harmony"
        self._tmp.mkdir(exist_ok=True)

    # ---------- 基础 ----------
    def _run(self, args: list[str], timeout: int = 30) -> bytes:
        cmd = [self.hdc, "-t", self.device_id] + args
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode("utf-8", "ignore").strip()[:200] or "hdc 执行失败")
        return proc.stdout

    def _shell(self, *args: str, timeout: int = 30) -> str:
        return self._run(["shell", *args], timeout=timeout).decode("utf-8", "ignore")

    def connect(self) -> None:
        out = subprocess.run([self.hdc, "list", "targets"], capture_output=True, timeout=15)
        targets = out.stdout.decode("utf-8", "ignore")
        if self.device_id not in targets:
            raise RuntimeError(f"鸿蒙设备 {self.device_id} 不在线，当前: {targets.strip() or '空'}")
        self._connected = True

    # ---------- 感知 ----------
    def screen_size(self) -> tuple[int, int]:
        if self._size[0]:
            return self._size
        try:
            root = self.dump_tree()
            x1, y1, x2, y2 = root.bounds
            if x2 - x1 > 0:
                self._size = (x2 - x1, y2 - y1)
        except Exception:
            pass
        return self._size or (1260, 2720)

    def screenshot(self) -> bytes:
        remote = "/data/local/tmp/atp_shot.jpeg"
        local = self._tmp / "shot.jpeg"
        self._shell("snapshot_display", "-f", remote)
        self._run(["file", "recv", remote, str(local)], timeout=45)
        raw = local.read_bytes()
        try:
            from PIL import Image

            buf = io.BytesIO()
            Image.open(io.BytesIO(raw)).convert("RGB").save(buf, format="PNG", optimize=True)
            return buf.getvalue()
        except Exception:
            return raw

    def dump_tree(self) -> UINode:
        remote = "/data/local/tmp/atp_layout.json"
        local = self._tmp / "layout.json"
        last_err = ""
        for attempt in range(3):
            try:
                self._shell("uitest", "dumpLayout", "-p", remote)
                self._run(["file", "recv", remote, str(local)], timeout=30)
                data = json.loads(local.read_text(encoding="utf-8"))
                root = _convert(data)
                if root.children:
                    return root
            except Exception as e:
                last_err = str(e)
            time.sleep(0.4 + attempt * 0.4)
        raise RuntimeError(f"无法获取鸿蒙界面布局: {last_err}")

    # ---------- 输入 ----------
    def tap_xy(self, x: int, y: int) -> None:
        self._shell("uitest", "uiInput", "click", str(x), str(y))

    def long_press_xy(self, x: int, y: int, ms: int = 800) -> None:
        self._shell("uitest", "uiInput", "longClick", str(x), str(y))

    def swipe_xy(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self._shell("uitest", "uiInput", "swipe", str(x1), str(y1), str(x2), str(y2))

    def input_text(self, text: str) -> None:
        """向**当前聚焦的控件**注入文本。

        `uitest uiInput inputText` 的 x/y 是"输入目标点"，不是随便一个坐标。
        早先这里传的是屏幕中心，实测在示例视频应用搜索框上直接丢字——文字进去了但落不到
        输入框里，UI 上什么都看不到。必须用聚焦控件的中心。
        """
        if not text:
            return
        node = self._focused
        if node is not None and node.bounds[2] > node.bounds[0]:
            x, y = node.center
        else:
            w, h = self.screen_size()
            x, y = w // 2, h // 2
        self._shell("uitest", "uiInput", "inputText", str(x), str(y), text)

    def clear_text(self) -> None:
        """清空输入框。

        鸿蒙的 keyEvent 有两个坑，都实测踩过：
        1. 只认 keyID 或 Back/Home/Power，写 "Delete" 会**静默无效**（返回 No Error 但什么都不做）；
        2. 退格是 KeyCode 2055，但对中文输入法下已上屏的文本，退格会被输入法吞掉一部分——
           发 19 次退格，15 个字符的框里还剩 6 个。

        所以先用组合键 Ctrl+A（2072+2017）全选，再退格一次清掉选中的内容；
        组合键没生效时再退回逐字退格。
        """
        self._shell("uitest", "uiInput", "keyEvent", str(_KEYCODE_CTRL_LEFT),
                    str(_KEYCODE_A))
        time.sleep(0.2)
        self._shell("uitest", "uiInput", "keyEvent", str(_KEYCODE_DEL))

        node = self._focused
        left = len((node.text or node.desc or "")) if node is not None else 0
        for _ in range(min(120, left + 4)):
            self._shell("uitest", "uiInput", "keyEvent", str(_KEYCODE_DEL))

    def press_key(self, name: str) -> None:
        code = _KEYMAP.get(name.lower())
        if not code:
            raise NotSupported(f"未知按键 {name}")
        self._shell("uitest", "uiInput", "keyEvent", code)

    def launch_app(self, app_id: str) -> None:
        """启动应用。ability 缺失时自动解析入口 Ability。

        为什么不能只 `-b bundle`：鸿蒙对不带 `-a` 的隐式启动会解析失败
        （Error 10103101），系统还会弹「暂无可用打开方式」对话框——而
        hdc/aa 失败时 exit 仍是 0，不校验输出就会误报「已启动」，AI 带着
        错误假设继续跑，整场执行全偏（run-20261117-164437-238b 的事故）。
        """
        bundle, _, ability = (app_id or self.app_id).partition("/")
        if not bundle:
            raise NotSupported("缺少 bundleName，格式：com.example.app/EntryAbility")
        if not ability:
            ability = self._entry_ability(bundle)
        args = ["aa", "start", "-b", bundle]
        if ability:
            args += ["-a", ability]
        out = self._shell(*args)
        # exit code 不可信（失败也是 0），必须看输出：成功是
        # "start ability successfully."，失败是 "error: failed to start ability."
        if re.search(r"error|failed", out, re.I):
            raise RuntimeError(f"启动 {bundle} 失败: {out.strip()[:180]}")

    def _entry_ability(self, bundle: str) -> str:
        """从 `bm dump -n` 的 JSON 里解析入口 Ability（进程内缓存）。

        输出里 ability 数组的第一项通常就是入口（EntryAbility），名字
        一律以 Ability 结尾，用这个特征抓第一个命中的。
        """
        cached = self._abilities.get(bundle)
        if cached is not None:
            return cached
        try:
            out = self._shell("bm", "dump", "-n", bundle, timeout=60)
        except Exception:  # noqa: BLE001
            out = ""
        m = re.search(r'"name"\s*:\s*"([A-Za-z][A-Za-z0-9_]*Ability)"', out)
        ability = m.group(1) if m else ""
        self._abilities[bundle] = ability
        return ability

    def terminate_app(self, app_id: str) -> None:
        self._shell("aa", "force-stop", (app_id or self.app_id).split("/")[0])

    # ---------- 找应用（应用台账发现用） ----------
    def installed_bundles(self) -> list[str]:
        """已装 bundle 列表（进程内缓存）。"""
        if self._bundles:
            return self._bundles
        try:
            out = self._shell("bm", "dump", "-a", timeout=60)
        except Exception:      # noqa: BLE001
            out = ""
        bundles = [ln.strip() for ln in out.splitlines() if _BUNDLE.match(ln.strip())]
        if bundles:
            self._bundles = bundles
        return bundles

    def find_app(self, name: str) -> tuple[str, str] | None:
        """按应用名找 bundleName，返回（bundleName, ability）；找不到返回 None。

        鸿蒙的 bundleName 和 Android 包名一样是 `com.x.y` 形状，但中文应用名
        同样不在 bundle 列表里，得读 `bm dump -n <bundle>` 里的 label。
        这里逐个读 label（有超时预算），命中就返回；读不到 label 的实现
        （部分 ROM 会裁掉）则只能靠 bundleName 关键词兜底。
        """
        key = str(name or "").strip()
        if not key:
            return None
        bundles = self.installed_bundles()

        # 1) 用户直接写的就是 bundleName
        if "." in key and " " not in key and key in bundles:
            return key, ""

        # 2) bundleName 里带关键词
        kn = re.sub(r"[^a-z0-9]+", "", key.casefold())
        if len(kn) >= 3:
            for bundle in bundles:
                b = bundle.casefold()
                if kn == b or kn in b.split(".") or kn in b:
                    return bundle, ""

        # 3) 读 label 逐个比对
        from atp.memory.apps import norm_name

        target = norm_name(key)
        cfg = getattr(self.config, "apps", None)
        deadline = time.time() + float(getattr(cfg, "timeout_s", 90) or 90)
        for bundle in bundles:
            if time.time() > deadline:
                break
            try:
                out = self._shell("bm", "dump", "-n", bundle, timeout=20)
            except Exception:      # noqa: BLE001
                continue
            for label in _LABELS.findall(out or ""):
                n = norm_name(label)
                if n and (n == target or (len(target) >= 2 and target in n)):
                    return bundle, ""
        return None

    def install(self, path: str) -> None:
        self._shell("bm", "install", "-p", path)

    UNSUPPORTED = {Action.OPEN_URL, Action.MAXIMIZE}


def _attr(node: dict) -> dict:
    a = node.get("attributes")
    return a if isinstance(a, dict) else node


def _convert(node: dict) -> UINode:
    a = _attr(node)
    m = _BOUNDS.match(str(a.get("bounds", "")))
    bounds = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))) if m else (0, 0, 0, 0)
    n = UINode(
        cls=str(a.get("type") or a.get("class") or ""),
        id=str(a.get("id") or a.get("accessibilityId") or ""),
        text=str(a.get("text") or ""),
        desc=str(a.get("description") or a.get("hint") or ""),
        bounds=bounds,
        clickable=str(a.get("clickable", "")).lower() == "true",
        enabled=str(a.get("enabled", "true")).lower() == "true",
        checked=str(a.get("checked", "")).lower() == "true",
        # 鸿蒙 dumpLayout 的 scrollable 时有时无（有的版本压根不带），
        # 所以滑动查找还会用控件名（List / Scroll / Grid / Swiper）兜一层。
        scrollable=str(a.get("scrollable", "")).lower() == "true",
    )
    for child in node.get("children") or []:
        if isinstance(child, dict):
            n.children.append(_convert(child))
    return n
