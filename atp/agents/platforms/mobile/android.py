"""Android 驱动（ADB 通道）。

选 ADB 而不是自研无障碍 APK 做主力，原因：CI 友好、无保活问题、天然多设备并行。
无障碍 APK 通道后续接同类接口即可，用例无感切换。
"""

from __future__ import annotations

import base64
import re
import subprocess
import time
import xml.etree.ElementTree as ET

from atp.common.types import Action, UINode
from atp.agents.platforms.base import BaseDriver, NotSupported

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
# dumpsys window 里的前台窗口：mCurrentFocus=Window{e1a4fa5 u0 com.bbk.launcher2/...}
_FOCUS = re.compile(r"mCurrentFocus=Window\{[0-9a-f]+ u\d+ ([A-Za-z0-9_.]+)/")
_ASCII = re.compile(r"[^a-z0-9]+")

_KEYMAP = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "enter": "KEYCODE_ENTER",
    "menu": "KEYCODE_MENU",
    "power": "KEYCODE_POWER",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "delete": "KEYCODE_DEL",
    "tab": "KEYCODE_TAB",
    "move_end": "KEYCODE_MOVE_END",
}

# 能通过广播注入 Unicode 的输入法（ADBKeyBoard 系列）。
# `adb shell input text` 只吃 ASCII，中文必须走这条路。
# 踩过的坑：uiautomator2 自带的 com.github.uiautomator/.AdbKeyboard 名字像但
# 只注册了 send.mock/stop.mock，广播 ADB_INPUT_B64 会静默无效（返回 result=0
# 但一个字都不进去）。所以候选只留真正实现了 ADBKeyBoard 协议的。
_TEXT_IMES = (
    "com.android.adbkeyboard/.AdbIME",
    "org.cloud.sonic.android/.keyboard.SonicKeyboard",
)

# ADBKeyBoard 的广播协议
_BC_INPUT_B64 = "ADB_INPUT_B64"
_BC_CLEAR = "ADB_CLEAR_TEXT"

# 播放类界面刷不出树时，多旧的缓存还能顶一下。3 秒够覆盖"抓树失败→继续本步"这段，
# 又不会被拿来当几秒后的界面状态用——宁可报错，也不能拿过期结构去点坐标。
_TREE_CACHE_TTL = 3.0


class AndroidAdbDriver(BaseDriver):
    platform = "android"

    def __init__(self, device_id: str, config=None):
        super().__init__(device_id, config)
        self.adb = self.config.adb or "adb"
        self._size: tuple[int, int] = (0, 0)
        self._ime_now: str = ""      # 当前输入法（惰性读取 + 切换后同步）
        self._ime_orig: str = ""     # 用户原本的输入法，close() 时还回去
        self._tree_cache: UINode | None = None
        self._tree_at: float = 0.0
        self._tree_count: int = 0        # 上一屏的节点规模，用来识别"半棵树"
        self._tree_stale: bool = False   # 上一次 dump 是拿缓存顶的
        self._act_cache: dict[str, str] = {}      # 包名 → 入口组件
        self._pkg_cache: dict[str, list[str]] = {}  # "all"/"3p" → 包名列表

    @property
    def tree_stale(self) -> bool:
        return self._tree_stale

    # ---------- 基础 ----------
    def _run(self, args: list[str], binary: bool = False, timeout: int = 30) -> bytes | str:
        cmd = [self.adb, "-s", self.device_id] + args
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "ignore").strip()
            raise RuntimeError(f"adb 失败({proc.returncode}): {err[:200]}")
        return proc.stdout if binary else proc.stdout.decode("utf-8", "ignore")

    def _shell(self, *args: str, timeout: int = 30) -> str:
        return str(self._run(["shell", *args], timeout=timeout))

    def connect(self) -> None:
        out = str(self._run(["get-state"]))
        if "device" not in out:
            raise RuntimeError(f"设备 {self.device_id} 未就绪: {out.strip()}")
        self._connected = True

    # ---------- 感知 ----------
    def screen_size(self) -> tuple[int, int]:
        if self._size[0]:
            return self._size
        try:
            out = self._shell("wm", "size")
            m = re.search(r"(\d+)x(\d+)", out)
            if m:
                self._size = (int(m.group(1)), int(m.group(2)))
        except Exception:
            pass
        if not self._size[0]:
            self._size = (1080, 1920)
        return self._size

    def device_pixel_ratio(self) -> float:
        """densityDpi / 160。截图是物理像素，input tap 也是物理像素，
        所以这里只作档案记录与报告展示用，映射本身靠归一化坐标。"""
        try:
            out = self._shell("wm", "density")
            m = re.search(r"(\d+)", out)
            if m:
                return round(int(m.group(1)) / 160.0, 3)
        except Exception:
            pass
        return 1.0

    def screenshot(self) -> bytes:
        return self._run(["exec-out", "screencap", "-p"], binary=True, timeout=45)  # type: ignore[return-value]

    def dump_tree(self) -> UINode:
        """抓界面树。

        uiautomator 在 dump 之前会 `waitForIdle()`，等界面"安静"下来。视频/直播/
        进度条动画的界面**永远不会 idle**，于是它死等约 10 秒再吐
        `ERROR: could not get idle state.`——这不是偶发失败，重试三遍只是把
        12 秒罚三遍（真机实测 36 秒一步，播放中的断言步就是这么被拖垮的）。

        所以：认出这个确定性失败就立刻放弃重试；有近期缓存树就先用着
        （播放页的结构本来也不变），没有才报错。

        另外还要防"半棵树"：页面切换的瞬间（H5 刚导航、列表还在渲染）能 dump 成功，
        但只有 WebView 壳子加零星几个节点。这种树交给规划器等于让它瞎点——
        真机上就是这么在长表单里打转的。所以树规模突然塌掉时会再抓一次，
        连续两次都这么少才认账。
        """
        remote = "/sdcard/atp_ui.xml"
        root, not_idle = self._dump_once(remote)
        if root is None:
            cached = self._tree_cache
            if cached is not None and time.time() - self._tree_at < _TREE_CACHE_TTL:
                self._tree_stale = True
                return cached
            if not_idle:
                raise RuntimeError(
                    "界面一直不空闲（视频/动画中），uiautomator 无法 dump；"
                    "这类界面请用视觉定位与帧差断言"
                )
            raise RuntimeError("无法获取界面树（可能处于启动动画、系统弹窗或 FLAG_SECURE 界面）")

        for i in range(3):
            if not self._partial(root):
                break
            # 0.35 / 0.9 / 1.5s 递增：H5 表单从白屏到画完要两秒多，
            # 一律 0.35s 重抓只会抓到同一个白屏，然后被当成"真实的空页面"交给模型。
            time.sleep(0.35 + i * 0.55)
            nxt, _ = self._dump_once(remote)
            if nxt is None:
                break
            if _node_count(nxt) > _node_count(root):
                root = nxt
                continue
            # 两屏完全一致 —— 这屏就是这么空（真的空列表页），别再空等
            if nxt.fingerprint() == root.fingerprint():
                break
            root = nxt

        self._tree_count = _node_count(root)
        self._tree_cache = root
        self._tree_at = time.time()
        self._tree_stale = False
        return root

    def _dump_once(self, remote: str) -> tuple[UINode | None, bool]:
        """抓一次并解析。返回（树, 是否因为界面不空闲而失败）。"""
        xml = ""
        not_idle = False
        for attempt in range(3):
            try:
                out = str(self._shell("uiautomator", "dump", remote))
            except RuntimeError:
                out = ""
            if "could not get idle state" in out:
                not_idle = True
                break
            try:
                xml = str(self._shell("cat", remote))
            except RuntimeError:
                xml = ""
            if "<hierarchy" in xml and "node " in xml:
                break
            time.sleep(0.4 + attempt * 0.4)

        if "<hierarchy" not in xml:
            return None, not_idle
        try:
            root_el = ET.fromstring(xml)
        except ET.ParseError as e:
            raise RuntimeError(f"界面树解析失败: {e}") from e
        w, h = self.screen_size()
        return _convert(root_el, (0, 0, w, h)), False

    def _partial(self, root: UINode) -> bool:
        """这棵树像不像"还没渲染完"：只有壳子，或者相比上一屏突然塌了一大截。"""
        n = _node_count(root)
        if n <= 4:
            return True
        prev = getattr(self, "_tree_count", 0)
        return prev >= 10 and n < prev * 0.4

    # ---------- 输入 ----------
    def tap_xy(self, x: int, y: int) -> None:
        self._shell("input", "tap", str(x), str(y))

    def long_press_xy(self, x: int, y: int, ms: int = 800) -> None:
        self._shell("input", "swipe", str(x), str(y), str(x), str(y), str(ms))

    def swipe_xy(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self._shell("input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))

    def input_text(self, text: str) -> None:
        """输入文本。ASCII 走原生 `input text`；含中文等非 ASCII 时切到
        ADBKeyBoard 用广播注入，输入完不忙着切回去（下一步往往就是提交）。
        用户原来的输入法记在 `_ime_orig`，close() 统一还原。
        """
        if not text:
            return
        if text.isascii():
            escaped = (text.replace(" ", "%s").replace("&", r"\&")
                       .replace("<", r"\<").replace(">", r"\>"))
            self._shell("input", "text", escaped)
            return

        ime = self._ensure_text_ime()
        if not ime:
            raise NotSupported(
                "ADB 通道输入中文需要一个 ADBKeyBoard 类输入法（广播注入）。"
                f"设备上没找到 {_TEXT_IMES[0]}，请安装后重试，"
                "或在 atp.config.json 的 android.text_ime 里指定。"
            )
        payload = base64.b64encode(text.encode("utf-8")).decode()
        self._shell("am", "broadcast", "-a", _BC_INPUT_B64, "--es", "msg", payload)
        time.sleep(0.25)

    def clear_text(self) -> None:
        """清空输入框。

        当前是 ADBKeyBoard 时一条广播清干净；否则按聚焦控件的文本长度批量退格
        （`input keyevent` 能一次收多个 keycode，比逐个往返快一个数量级）。
        """
        if self._ime_current() in _TEXT_IMES:
            self._shell("am", "broadcast", "-a", _BC_CLEAR)
            time.sleep(0.15)
            return
        node = self._focused
        n = len((node.text or node.desc or "")) if node is not None else 20
        n = min(120, max(8, n + 4))
        self._shell("input", "keyevent", "KEYCODE_MOVE_END")
        for i in range(0, n, 30):
            batch = min(30, n - i)
            self._shell("input", "keyevent", *(["KEYCODE_DEL"] * batch))

    # ---------- 输入法管理 ----------
    def _ime_current(self) -> str:
        """读一次当前输入法并缓存。IME 切换很慢，不能每次输入都读。"""
        if not self._ime_now:
            try:
                self._ime_now = self._shell("settings", "get", "secure",
                                            "default_input_method").strip()
            except Exception:  # noqa: BLE001
                self._ime_now = ""
        return self._ime_now

    def _switch_ime(self, ime: str) -> bool:
        try:
            self._shell("ime", "enable", ime)
            out = self._shell("ime", "set", ime)
        except Exception:  # noqa: BLE001
            return False
        if "error" in (out or "").lower() or "not" in (out or "").lower():
            return False
        self._ime_now = ime
        time.sleep(0.5)
        return True

    def _ensure_text_ime(self) -> str:
        """确保当前是能注入 Unicode 的输入法，返回它；找不到返回空串。"""
        cur = self._ime_current()
        if cur in _TEXT_IMES and cur:
            return cur
        if not self._ime_orig:
            self._ime_orig = cur      # 先记住用户原来的，别还原成别的
        for cand in _TEXT_IMES:
            if self._switch_ime(cand):
                return cand
        self._ime_now = ""            # 切失败了，别把缓存留在错的状态
        return ""

    def restore_ime(self) -> None:
        """把输入法还给用户。用例跑完不还原，等于把测试机改成测试专用机。"""
        if self._ime_orig and self._ime_orig != self._ime_now:
            self._shell("ime", "set", self._ime_orig)
            self._ime_now = self._ime_orig

    def close(self) -> None:
        try:
            self.restore_ime()
        except Exception:  # noqa: BLE001
            pass
        super().close()

    def press_key(self, name: str) -> None:
        code = _KEYMAP.get(name.lower())
        if not code:
            raise NotSupported(f"未知按键 {name}")
        self._shell("input", "keyevent", code)

    def launch_app(self, app_id: str) -> None:
        """启动应用，并保证落在首页。

        不用 `monkey -p`：它走的是 LAUNCHER intent，App 若自己"记住上次页面"
        （示例视频应用 就会恢复到 SearchActivity），用例的起点就不确定了——首跑时
        第一步找首页搜索入口直接 fail。改成解析真正的入口 Activity，用
        `am start --activity-clear-top` 把上面的页面清掉，每次都是干净的首页。

        `app_id` 允许写成 `pkg/Activity`（应用台账里存了入口组件时直接用，
        省掉一次 `cmd package resolve-activity`）。
        """
        raw = app_id or self.app_id or ""
        pkg = raw.split("/")[0].strip()
        if not pkg:
            raise NotSupported("缺少包名")
        act = raw if "/" in raw else self._launcher_activity(pkg)
        if act:
            self._shell("am", "start", "-n", act, "--activity-clear-top")
            return
        self._shell("monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")

    def _launcher_activity(self, pkg: str) -> str:
        """`com.x.y/.MainActivity`；解析失败返回空串。"""
        if pkg in self._act_cache:
            return self._act_cache[pkg]
        try:
            out = self._shell("cmd", "package", "resolve-activity", "--brief",
                              "-c", "android.intent.category.LAUNCHER", pkg)
        except Exception:  # noqa: BLE001
            return ""
        act = ""
        for line in reversed((out or "").splitlines()):
            line = line.strip()
            if "/" in line and " " not in line:
                act = line
                break
        if act:
            self._act_cache[pkg] = act
        return act

    def terminate_app(self, app_id: str) -> None:
        self._shell("am", "force-stop", (app_id or self.app_id or "").split("/")[0])

    def install(self, path: str) -> None:
        self._run(["install", "-r", "-g", path], timeout=180)

    # ---------- 找应用（应用台账发现用） ----------
    def installed_packages(self, third_party_only: bool = False) -> list[str]:
        """已安装包名列表（进程内缓存：一次执行里可能问好几遍）。"""
        key = "3p" if third_party_only else "all"
        if key in self._pkg_cache:
            return self._pkg_cache[key]
        args = ["pm", "list", "packages"] + (["-3"] if third_party_only else [])
        try:
            out = str(self._shell(*args, timeout=60))
        except Exception:      # noqa: BLE001
            out = ""
        pkgs = [ln.split("package:", 1)[1].strip()
                for ln in out.splitlines() if ln.strip().startswith("package:")]
        if pkgs:
            self._pkg_cache[key] = pkgs
        return pkgs

    def foreground_package(self) -> str:
        """当前前台窗口的包名。解析不到返回空串（别猜）。

        注意是 `dumpsys window`：`dumpsys window windows` 里**没有** mCurrentFocus
        （真机实测 grep -c 为 0），照着 windows 版本写只会永远拿到空串。
        在设备侧用 grep 过一遍，比把整份 dumpsys 拉回本地快一个数量级（0.2s）。
        """
        try:
            out = str(self._shell("dumpsys", "window", "|", "grep", "mCurrentFocus",
                                  timeout=20))
        except Exception:      # noqa: BLE001
            return ""
        hits = _FOCUS.findall(out)
        return hits[-1] if hits else ""

    def find_app(self, name: str) -> tuple[str, str] | None:
        """按应用名找包名，返回（包名, 入口组件）。找不到返回 None。

        三层，从便宜到贵：
          1. 用户直接写的就是包名 / 包名里带关键词（videoapp、samplechat）——查 `pm list packages`；
          2. 桌面图标名（中文名只能走这条，例如「示例聊天应用」→ com.example.chatapp.desktop）；
          3. 找不到就返回 None，让调用方提示手工登记，绝不瞎认一个包。
        """
        key = str(name or "").strip()
        if not key:
            return None

        pkgs = self.installed_packages()
        third = self.installed_packages(third_party_only=True)

        # 1) 直接是包名
        if "." in key and " " not in key:
            if key in pkgs or self._launcher_activity(key):
                act = self._launcher_activity(key)
                if act:
                    return key, act

        # 2) 包名带关键词：videoapp → com.example.videoapp
        pkg = _match_package(third or pkgs, key) or _match_package(pkgs, key)
        if pkg:
            return pkg, self._launcher_activity(pkg)

        # 3) 去桌面找图标
        return self._find_by_launcher(key)

    def _find_by_launcher(self, name: str) -> tuple[str, str] | None:
        """翻桌面找应用图标：点开它，看前台包名变成谁。

        图标节点本身不带目标应用的包名（`package` 属性是桌面的），所以必须
        "点一下再问系统现在是谁在前台"。这是唯一不依赖 launcher 私有实现的通用办法，
        代价是要翻页 + 每次 2~3 秒的 dump——所以结果一定要写进 apps.json 复用。
        """
        from atp.memory.apps import norm_name

        cfg = getattr(self.config, "apps", None)
        pages = max(1, int(getattr(cfg, "scan_pages", 6) or 6))
        folder_budget = max(0, int(getattr(cfg, "scan_folders", 3) or 3))
        deadline = time.time() + float(getattr(cfg, "timeout_s", 90) or 90)
        target = norm_name(name)
        if not target:
            return None

        self.press_key("home")
        time.sleep(0.9)
        launcher_pkg = self.foreground_package()

        for page in range(pages):
            if time.time() > deadline:
                break
            tree = self._safe_tree()
            node = _icon_match(tree, target) if tree is not None else None

            # 图标可能在文件夹里：一层，最多开 folder_budget 个（每页都算）
            if node is None and tree is not None and folder_budget > 0:
                for folder in _folder_nodes(tree):
                    if folder_budget <= 0 or time.time() > deadline:
                        break
                    folder_budget -= 1
                    self.tap_xy(*folder.center)
                    time.sleep(1.0)
                    sub = self._safe_tree()
                    node = _icon_match(sub, target) if sub is not None else None
                    if node is not None:
                        break
                    self.press_key("back")
                    time.sleep(0.7)

            if node is not None:
                self.tap_xy(*node.center)
                pkg = self._wait_foreground(exclude=launcher_pkg, timeout=8.0)
                if pkg:
                    return pkg, self._launcher_activity(pkg)
                # 点下去没换应用（多半是点到了文件夹/空白），继续翻页
                self.press_key("home")
                time.sleep(0.8)

            if page + 1 >= pages:
                break
            w, h = self.screen_size()
            self.swipe_xy(int(w * 0.82), int(h * 0.5), int(w * 0.2), int(h * 0.5), 320)
            time.sleep(0.9)
        return None

    def _safe_tree(self) -> UINode | None:
        try:
            return self.dump_tree()
        except Exception:      # noqa: BLE001 —— 桌面偶尔 dump 失败，翻页重试即可
            return None

    def _wait_foreground(self, exclude: str, timeout: float) -> str:
        """等前台切到别的应用；超时返回空串。"""
        end = time.time() + max(0.5, timeout)
        while time.time() < end:
            pkg = self.foreground_package()
            if pkg and pkg != exclude:
                return pkg
            time.sleep(0.35)
        return ""

    # ---------- 能力裁剪 ----------
    UNSUPPORTED = {Action.OPEN_URL, Action.MAXIMIZE}


def _match_package(pkgs: list[str], key: str) -> str:
    """在包名列表里找和 key 最像的那个（videoapp → com.example.videoapp）。

    只在 key 是"能塞进包名的字母数字"时启用：中文名（示例聊天应用）永远匹配不到，
    硬匹配只会认错包，那种情况交给桌面找图标。
    """
    kn = _ASCII.sub("", str(key or "").casefold())
    if len(kn) < 3:
        return ""
    loose = ""
    for pkg in pkgs:
        p = pkg.casefold()
        if kn == p or kn in p.split("."):
            return pkg                 # 整段命中，最可信
        if not loose and kn in p:
            loose = pkg
    return loose


def _node_count(root: UINode) -> int:
    return sum(1 for _ in root.walk())


def _label(node: UINode) -> str:
    return f"{node.text or ''} {node.desc or ''}".strip()


def _is_folder(node: UINode) -> bool:
    return "文件夹" in _label(node) or "folder" in (node.cls or "").casefold()


def _icon_match(root: UINode | None, target: str) -> UINode | None:
    """在桌面/文件夹里找名字等于 target 的可点节点。

    精确匹配优先于包含匹配：桌面同时有「抖音」和「抖音极速版」时，
    用户说「抖音」就该点第一个，不能点成极速版。
    """
    if root is None or not target:
        return None
    from atp.memory.apps import norm_name

    best: tuple[int, UINode] | None = None
    for node in root.walk():
        if not node.clickable or node.area <= 0 or _is_folder(node):
            continue
        label = norm_name(_label(node))
        if not label or len(label) > 24:      # 长句不是图标名
            continue
        if label == target:
            score = 2
        elif len(target) >= 2 and target in label:
            score = 1
        else:
            continue
        if best is None or score > best[0]:
            best = (score, node)
    return best[1] if best else None


def _folder_nodes(root: UINode) -> list[UINode]:
    return [n for n in root.walk() if n.clickable and n.area > 0 and _is_folder(n)]


def _convert(el: ET.Element, fallback: tuple[int, int, int, int]) -> UINode:
    a = el.attrib
    m = _BOUNDS.match(a.get("bounds", ""))
    bounds = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))) if m else fallback
    n = UINode(
        cls=a.get("class", "").split(".")[-1],
        id=a.get("resource-id", ""),
        text=a.get("text", ""),
        desc=a.get("content-desc", ""),
        bounds=bounds,
        clickable=a.get("clickable") == "true",
        enabled=a.get("enabled") == "true",
        checked=a.get("checked") == "true",
        # scrollable 是滑动查找的准入条件：没有它就只能靠控件名猜，
        # 猜错一次（播放器上滑一下 = 拖进度条）比不滑糟得多。
        scrollable=a.get("scrollable") == "true",
    )
    for child in el:
        if child.tag in ("node", "hierarchy"):
            n.children.append(_convert(child, bounds))
    return n
