"""移动端实时投屏与远程操作：MJPEG 推流 + 点击/滑动/按键回注真机。

帧源为什么要分级（参照 AirtestIDE 的 ScreenProxy.auto_setup）：
早期这里是「每帧起一个 `adb exec-out screencap -p`」，真机 0.8s 一帧，
点一下要等近一秒才看到反应。现在按优先级自动选最快的可用通道：

    h264     screenrecord + ffmpeg        实测 ~28fps   （AirtestIDE 新版 scrcpy 同思路）
    javacap  Yosemite Capture socket     实测 ~4fps    （AirtestIDE 的 javacap）
    adbcap   screencap -p                实测 ~1.2fps  （零依赖兜底）

AirtestIDE 的 minicap 在 SDK 34+ 会崩（私有截图 API 被封），minitouch 在
Android 15 上报 /dev/input Permission denied，所以触控走系统 `input` 命令，
一条 ~100ms，稳定且不依赖往设备里推二进制。

设计要点：
- **一台设备一个抓帧线程**：多个标签页共享同一条流，不会重复抓屏；
- **没人看就停**：最后一位订阅者断开 20 秒后退出，不空耗手机电量；
- **与执行链路解耦**：投屏用独立 adb 进程，不持有 Executor 的 driver；
- **触控坐标归一化**：前端按图片实际渲染区域传 0~1，后端按设备当前
  （含旋转）分辨率换算成物理像素，投屏缩放比例怎么变都不会点偏。
"""

from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

from atp.webapp.cast.sources import (AdbCapSource, FrameSource, H264Source,
                                     JavaCapSource, Toolchain, device_physical_size)

SUPPORTED = {"android", "harmony"}
# fps 是**上限**不是承诺：H264 真机实测 23~26fps，兜底链路由设备截图速度决定。
# 早先默认 12：H264 明明能出 23fps，却被 1/12 的闸门砍成 9fps，用户看到的就是
# 「一点都不丝滑」。现在默认放到上限，真实帧率完全交给帧源决定。
DEFAULT_FPS = 30
MAX_FPS = 30
MIN_INTERVAL = 1.0 / MAX_FPS
MIN_FPS = 1
MAX_WIDTH = 420
# 所有帧源统一输出到这个宽度：javacap(scale=33) / H264(projected 416) /
# adbcap(缩到 416) 在 1260x2800 上都是 416x924。尺寸不一致时浏览器每帧
# 重算 object-fit 的渲染矩形，画面就会忽大忽小地抖（真机复现过）。
FRAME_WIDTH = 416
FRAME_QUALITY = 80
JPEG_QUALITY = 72
BOOST_HOLD = 0.7         # 加速旁路出帧后，主链让位的时间窗，避免两源交替跳帧
IDLE_TTL = 20.0          # 没有订阅者后，抓帧线程的存活时间
PRESENCE_TTL = 3.0       # 设备在线探测结果缓存：轮询 1.5s 一次，探测 3s 一次足够
DEVICE_ID_RE = re.compile(r"[A-Za-z0-9._:\-]{1,128}")

# 回注设备时允许的按键（名字 -> Android keyevent / 鸿蒙 uitest keyEvent）
KEYCODES: dict[str, str] = {
    "back": "4", "home": "3", "recents": "187", "menu": "82",
    "power": "26", "volume_up": "24", "volume_down": "25",
    "enter": "66", "delete": "67",
}
HARMONY_KEYS = {"back": "Back", "home": "Home", "power": "Power"}
_ACTIONS = {"tap", "swipe", "key", "text", "long_press"}


def safe_device_id(value: str) -> str:
    """设备号只进 argv、不进 shell，但仍要挡掉奇怪的字符（含日志/路径注入）。"""
    v = (value or "").strip()
    return v if DEVICE_ID_RE.fullmatch(v) else ""


def encode_frame(raw: bytes, max_width: int = FRAME_WIDTH) -> tuple[bytes, int, int]:
    """PNG/JPEG 原始帧 → 缩放后的 JPEG。"""
    if not raw:
        raise RuntimeError("设备没有返回画面数据")
    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    w, h = img.size
    if w > max_width:
        img = img.resize((max_width, max(1, round(h * max_width / w))), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=False)
    return out.getvalue(), img.size[0], img.size[1]



def normalize_frame(jpeg: bytes, size: tuple[int, int]) -> bytes:
    """把异形帧重采样到基准尺寸——推给浏览器的每一帧必须是同一分辨率。"""
    with Image.open(io.BytesIO(jpeg)) as img:
        if img.size == size:
            return jpeg
        scaled = img.convert("RGB").resize(size, Image.BILINEAR)
    out = io.BytesIO()
    scaled.save(out, format="JPEG", quality=FRAME_QUALITY, optimize=False)
    return out.getvalue()


def error_frame(message: str = "", width: int = FRAME_WIDTH,
                height: int = 924) -> bytes:
    """抓不到画面时推一张占位图，避免浏览器一直转圈。

    尺寸必须和真实帧一致，否则连接初期推一张异形占位图也会让画面闪一下。
    """
    img = Image.new("RGB", (width, height), (16, 24, 40))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((24, 24, width - 24, height - 24), radius=18,
                        outline=(70, 92, 130), width=2)
    d.text((48, height // 2 - 30), "NO SIGNAL", fill=(205, 218, 240))
    d.text((48, height // 2 + 4), "screencast unavailable", fill=(140, 158, 190))
    d.text((48, height // 2 + 34), message[:32].encode("ascii", "ignore").decode(),
           fill=(120, 138, 170))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=80)
    return out.getvalue()


def _run(argv: list[str], timeout: float) -> bytes:
    kwargs: dict = {"capture_output": True, "timeout": timeout}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(argv, **kwargs)  # noqa: S603
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "ignore").strip()[:160]
        raise RuntimeError(err or f"{Path(argv[0]).name} 返回 {proc.returncode}")
    return proc.stdout or b""


# 留一份原始引用：单测会把 _run 换成假实现表示「别碰真机」，
# 那种情况下常驻 shell 也必须一起停用，否则测试会真的去起 adb。
_RUN_REAL = _run


class AdbShell:
    """常驻一条 `adb shell`，把每条指令的 adb 进程启动开销省掉。

    真机实测（Windows）：`adb shell true` 就要 **195ms**，`adb shell input tap`
    236ms —— 也就是说一次点击里 80% 的时间花在拉起 adb.exe 上，真正落到设备
    执行只有 ~40ms。把 shell 常驻下来，点击延迟从 236ms 降到几十毫秒，操作才
    谈得上「跟手」。

    每条指令后面补一个 `echo __ATPDONE__<exit>__` 当回执，靠它判定执行完成与
    退出码；失败（设备掉线/shell 卡死）时抛异常，由调用方退回一次性 adb 调用。
    """

    TOKEN = b"__ATPDONE__"

    def __init__(self, adb: str, device_id: str):
        self.adb = adb
        self.device_id = device_id
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._proc: subprocess.Popen | None = None
        self._sent = 0
        self._acks = 0
        self._code = 0
        self._alive = False

    # ---------- 生命周期 ----------
    def _start(self) -> None:
        kwargs: dict = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                        "stderr": subprocess.STDOUT}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(  # noqa: S603
            [self.adb, "-s", self.device_id, "shell"], **kwargs)
        with self._cond:
            self._sent = 0
            self._acks = 0
            self._code = 0
            self._alive = True
        threading.Thread(target=self._pump, args=(self._proc,), daemon=True,
                         name="atp-adb-shell").start()

    def _pump(self, proc: subprocess.Popen) -> None:
        buf = b""
        read = getattr(proc.stdout, "read1", None) or proc.stdout.read
        try:
            while True:
                chunk = read(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    i = buf.find(self.TOKEN)
                    if i < 0:
                        break
                    j = buf.find(b"__", i + len(self.TOKEN))
                    if j < 0:
                        break
                    try:
                        code = int(buf[i + len(self.TOKEN):j] or b"0")
                    except ValueError:
                        code = 0
                    buf = buf[j + 2:]
                    with self._cond:
                        self._acks += 1
                        self._code = code
                        self._cond.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                self._alive = False
                self._cond.notify_all()

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        with self._cond:
            self._alive = False
        if proc is not None:
            for act in (lambda: proc.stdin and proc.stdin.close(), proc.kill):
                try:
                    act()
                except (OSError, AttributeError, ValueError):
                    pass

    def close(self) -> None:
        with self._lock:
            self._kill()

    # ---------- 执行 ----------
    def run(self, command: str, timeout: float = 6.0) -> int:
        """执行一条 shell 命令，返回退出码；超时/掉线抛 RuntimeError。"""
        with self._lock:
            if not self._alive or self._proc is None or self._proc.poll() is not None:
                self._kill()
                self._start()
            with self._cond:
                target = self._sent + 1
                self._sent = target
            line = f"{{ {command}; }} >/dev/null 2>&1; echo __ATPDONE__$?__\n"
            try:
                self._proc.stdin.write(line.encode("utf-8"))  # type: ignore[union-attr]
                self._proc.stdin.flush()                      # type: ignore[union-attr]
            except (OSError, ValueError, AttributeError) as e:
                self._kill()
                raise RuntimeError(f"设备 shell 写入失败: {e}") from e
            with self._cond:
                if not self._cond.wait_for(lambda: self._acks >= target, timeout):
                    self._kill()
                    raise RuntimeError("设备 shell 无响应")
                return self._code


def _ffmpeg() -> str:
    """优先环境变量 / PATH，再找 Windows 常见安装位置。"""
    override = os.environ.get("ATP_FFMPEG", "").strip()
    if override and (Path(override).is_file() or shutil.which(override)):
        return override
    found = shutil.which("ffmpeg")
    if found:
        return found
    for p in (r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
              r"C:\ffmpeg\bin\ffmpeg.exe", "/usr/local/bin/ffmpeg"):
        if Path(p).is_file():
            return p
    return ""


def grab_android(adb: str, device_id: str, timeout: float = 8.0) -> bytes:
    if not adb:
        raise RuntimeError("未找到 adb，请在配置里指定 adb 路径")
    raw = _run([adb, "-s", device_id, "exec-out", "screencap", "-p"], timeout)
    if not raw.startswith(b"\x89PNG") and not raw.startswith(b"\xff\xd8"):
        hint = raw[:120].decode("utf-8", "ignore").strip()
        raise RuntimeError(f"设备 {device_id} 画面抓取失败（可能离线/未授权）：{hint or '空响应'}")
    return raw


def grab_harmony(hdc: str, device_id: str, timeout: float = 8.0) -> bytes:
    """鸿蒙走 snapshot_display + file recv（hdc 没有 exec-out）。"""
    if not hdc:
        raise RuntimeError("未找到 hdc，请在配置里指定 hdc 路径")
    remote = "/data/local/tmp/atp_cast.jpeg"
    tag = re.sub(r"[^A-Za-z0-9_-]", "_", safe_device_id(device_id) or "dev")
    local = Path(tempfile.gettempdir()) / f"atp_cast_{tag}.jpeg"
    _run([hdc, "-t", device_id, "shell", "snapshot_display", "-f", remote], timeout)
    _run([hdc, "-t", device_id, "file", "recv", remote, str(local)], timeout)
    raw = local.read_bytes()
    if not raw:
        raise RuntimeError(f"鸿蒙设备 {device_id} 返回了空画面")
    return raw


# ---------------------------------------------------------------- 触控回注

def display_size(platform: str, device_id: str, adb: str = "adb",
                 hdc: str = "hdc", runner=None) -> tuple[int, int, int]:
    """返回 (当前显示宽, 高, 旋转角度)。

    `wm size` 给的是物理尺寸，横屏时 `input tap` 用的是旋转后的坐标系，
    所以要从 `dumpsys window` 的 mRotation 把宽高换过来。
    """
    run = runner or _run
    if platform == "harmony":
        try:
            out = run([hdc, "-t", device_id, "shell", "wm", "size"], 5.0)
            m = re.search(rb"(\d{3,5})x(\d{3,5})", out)
            if m:
                return int(m.group(1)), int(m.group(2)), 0
        except Exception:  # noqa: BLE001
            pass
        return 1260, 2800, 0
    w, h = device_physical_size(Toolchain(adb=adb, run=run), device_id)
    rotation = 0
    try:
        dump = run([adb, "-s", device_id, "shell", "dumpsys", "window"], 5.0)
        m = re.search(rb"mRotation=(\d)", dump)
        if m:
            rotation = int(m.group(1)) * 90
    except Exception:  # noqa: BLE001
        pass
    if rotation in (90, 270):
        w, h = h, w
    return w, h, rotation


def control_argv(platform: str, device_id: str, payload: dict,
                 size: tuple[int, int, int], adb: str = "adb", hdc: str = "hdc") -> list[str]:
    """把前端的归一化指令翻译成一条设备命令（纯函数，便于单测）。

    坐标一律是 0~1 的浮点数；只有 `text` 允许字符串，且拒绝中文
    （`input text` 发不了中文，硬发会静默丢字）。
    """
    action = str(payload.get("action") or "")
    if action not in _ACTIONS:
        raise ValueError(f"不支持的操作: {action}")
    w, h, _ = size
    if w <= 0 or h <= 0:
        raise ValueError("设备分辨率无效")

    def num(key: str, default: float = 0.0) -> float:
        try:
            v = float(payload.get(key, default))
        except (TypeError, ValueError) as e:
            raise ValueError(f"坐标 {key} 不是数字") from e
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"坐标 {key} 必须在 0~1 之间")
        return v

    def px(key: str, total: int, default: float = 0.0) -> int:
        return max(0, min(total - 1, round(num(key, default) * total)))

    base = [hdc, "-t", device_id, "shell"] if platform == "harmony" \
        else [adb, "-s", device_id, "shell"]

    if action == "key":
        key = str(payload.get("key") or "back")
        if platform == "harmony":
            if key not in HARMONY_KEYS:
                raise ValueError(f"鸿蒙不支持按键: {key}")
            return base + ["uitest", "uiInput", "keyEvent", HARMONY_KEYS[key]]
        if key not in KEYCODES:
            raise ValueError(f"不支持按键: {key}")
        return base + ["input", "keyevent", KEYCODES[key]]

    if action == "text":
        text = str(payload.get("text") or "")[:200]
        if not text:
            raise ValueError("输入内容为空")
        if any(ord(c) > 127 for c in text):
            raise ValueError("投屏输入暂不支持中文，请在设备端输入法里输入")
        safe = re.sub(r"[^A-Za-z0-9 _.,:;!?@#$%&*+\-/=]", "", text)
        if platform == "harmony":
            return base + ["uitest", "uiInput", "inputText", str(w // 2), str(h // 2), safe]
        return base + ["input", "text", safe.replace(" ", "%s")]

    x, y = px("x", w), px("y", h)
    if action == "tap":
        if platform == "harmony":
            return base + ["uitest", "uiInput", "click", str(x), str(y)]
        return base + ["input", "tap", str(x), str(y)]
    if action == "long_press":
        if platform == "harmony":
            return base + ["uitest", "uiInput", "longClick", str(x), str(y)]
        duration = max(400, min(3000, int(payload.get("duration") or 800)))
        return base + ["input", "swipe", str(x), str(y), str(x), str(y), str(duration)]

    x2, y2 = px("x2", w, float(payload.get("x", 0))), px("y2", h, float(payload.get("y", 0)))
    duration = max(80, min(2000, int(payload.get("duration") or 300)))
    if platform == "harmony":
        return base + ["uitest", "uiInput", "swipe", str(x), str(y), str(x2), str(y2)]
    return base + ["input", "swipe", str(x), str(y), str(x2), str(y2), str(duration)]


@dataclass
class _Channel:
    platform: str
    device_id: str
    fps: int = DEFAULT_FPS
    cond: threading.Condition = field(default_factory=threading.Condition)
    stop: threading.Event = field(default_factory=threading.Event)
    seq: int = 0
    frame: bytes = b""
    width: int = 0
    height: int = 0
    frame_size: tuple[int, int] = (0, 0)   # 本次投屏统一的输出尺寸
    boost_until: float = 0.0               # 加速旁路最近接管到的时刻
    error: str = ""
    method: str = ""
    frame_ms: int = 0
    measured_fps: float = 0.0
    updated_at: float = 0.0
    viewers: int = 0
    last_poll: float = 0.0
    thread: threading.Thread | None = None
    booster_started: bool = False
    # 每个帧源最后一次推出去的图，用于「同一张图不重推」。
    # screencap/ffmpeg 都会按固定节奏重复吐出静止画面，不去重的话会把
    # 带宽和浏览器解码全浪费在重复图上。
    last_frames: dict[str, bytes] = field(default_factory=dict)
    _stamps: list[float] = field(default_factory=list)


class CastHub:
    """按设备共享的抓帧线程池 + 触控回注入口。"""

    def __init__(self, config=None):
        self.config = config
        self.adb = getattr(config, "adb", "") or "adb"
        self.hdc = getattr(config, "hdc", "") or "hdc"
        self.ffmpeg = _ffmpeg()
        self._lock = threading.Lock()
        self._channels: dict[tuple[str, str], _Channel] = {}
        self._sizes: dict[tuple[str, str], tuple[float, tuple[int, int, int]]] = {}
        # 设备在线探测缓存（拔 USB 后 status() 能立刻给出 present=false）
        self._presence: dict[tuple[str, str], tuple[float, bool]] = {}
        # 单测置 False：不要在用例里去跑真的 adb/hdc（设备号都是假的）
        self.presence = True
        # 每台设备一条常驻 adb shell，省掉每次点击 195ms 的进程启动开销
        self._shells: dict[str, AdbShell] = {}
        # 允许用 ATP_CAST_METHODS=h264,javacap,adbcap 固定链路（排障/单测用）
        forced = os.environ.get("ATP_CAST_METHODS", "").strip()
        self.methods: list[str] | None = [m.strip() for m in forced.split(",") if m.strip()] or None
        # H264 加速通道：静止无帧时由 javacap 保底，画面变化时它抢先出帧
        self.boost = os.environ.get("ATP_CAST_BOOST", "1").strip().lower() not in ("0", "false", "off")
        # 常驻 adb shell 回注触控（关掉就退回每条指令起一个 adb，慢约 195ms/条）
        self.shell_inject = os.environ.get("ATP_CAST_SHELL", "1").strip().lower() \
            not in ("0", "false", "off")

    # ---------- 对外 ----------
    def status(self, platform: str, device_id: str) -> dict:
        did = safe_device_id(device_id)
        supported = platform in SUPPORTED
        with self._lock:
            ch = self._channels.get((platform, did))
        base = {
            "platform": platform,
            "device_id": did,
            "supported": supported,
            "active": bool(ch and ch.viewers > 0),
            "viewers": ch.viewers if ch else 0,
            "fps": ch.measured_fps if ch else 0.0,
            "target_fps": ch.fps if ch else DEFAULT_FPS,
            "width": ch.width if ch else 0,
            "height": ch.height if ch else 0,
            "error": ch.error if ch else "" if not supported else "",
            "method": ch.method if ch else "",
            "boost": self.boost and bool(self.ffmpeg),
            "frame_size": list(ch.frame_size) if ch else [0, 0],
            "frame_ms": ch.frame_ms if ch else 0,
            "control": "adb-input" if platform == "android" else "hdc-uitest",
            "updated_at": ch.updated_at if ch else 0.0,
            "stream_url": f"/api/device/cast/stream?platform={platform}&device={did}" if did else "",
        }
        # 正在投屏的通道顺带探测设备是否还在线（3s 缓存）：拔 USB 后前端
        # 轮询 1.5s 一次，最多 2 个周期就能拿到 present=false 触发自动断开。
        present = True
        if supported and did and ch is not None and getattr(self, "presence", True):
            present = self._present(platform, did)
        base["present"] = present
        if supported and did and not present:
            base["error"] = "设备已断开连接（USB 已移除或设备离线）"
        if not supported:
            base["error"] = "该平台暂不支持投屏，请用 Android / 鸿蒙真机"
        elif not did:
            base["error"] = "未指定设备"
        return base

    def subscribe(self, platform: str, device_id: str, fps: int = DEFAULT_FPS) -> _Channel | None:
        did = safe_device_id(device_id)
        if platform not in SUPPORTED or not did:
            return None
        fps = max(MIN_FPS, min(MAX_FPS, int(fps or DEFAULT_FPS)))
        with self._lock:
            ch = self._channels.get((platform, did))
            if ch is None:
                ch = _Channel(platform=platform, device_id=device_id, fps=fps)
                self._channels[(platform, did)] = ch
            else:
                ch.stop.clear()
            ch.fps = fps
            ch.viewers += 1
            ch.last_poll = time.time()
            need_start = ch.thread is None or not ch.thread.is_alive()
            if need_start:
                ch.thread = threading.Thread(
                    target=self._producer, args=(ch,), daemon=True,
                    name=f"atp-cast-{platform}-{did[:16]}",
                )
                ch.thread.start()
        return ch

    def unsubscribe(self, ch: _Channel | None) -> None:
        if ch is None:
            return
        with ch.cond:
            ch.viewers = max(0, ch.viewers - 1)
            ch.cond.notify_all()
        if ch.viewers == 0:
            ch.last_poll = time.time()

    def wait_frame(self, ch: _Channel, after_seq: int, timeout: float = 5.0):
        """等一帧新的画面；返回 (jpeg, seq, error)。静止画面超时会返回上一帧。"""
        deadline = time.time() + timeout
        with ch.cond:
            while ch.seq <= after_seq and time.time() < deadline:
                ch.cond.wait(min(0.5, max(0.0, deadline - time.time())))
            ch.last_poll = time.time()
            return ch.frame, ch.seq, ch.error

    def stop(self, platform: str, device_id: str) -> dict:
        did = safe_device_id(device_id)
        with self._lock:
            ch = self._channels.pop((platform, did), None)
        if ch is not None:
            ch.stop.set()
            with ch.cond:
                ch.viewers = 0
                ch.cond.notify_all()
        return {"stopped": ch is not None}

    def placeholder(self, ch: _Channel, message: str) -> bytes:
        """占位图与真实帧同尺寸，避免连接初期闪一下。"""
        w, h = ch.frame_size if ch.frame_size != (0, 0) else (FRAME_WIDTH, 924)
        return error_frame(message, w, h)

    def grab_once(self, platform: str, device_id: str) -> bytes:
        """单帧同步抓取（调试/降级用，不进订阅池）。"""
        did = safe_device_id(device_id)
        if platform not in SUPPORTED or not did:
            raise RuntimeError("平台不支持投屏或设备号无效")
        raw = self._grab(platform, did)
        jpeg, _, _ = encode_frame(raw)
        return jpeg

    def control(self, platform: str, device_id: str, payload: dict) -> dict:
        """把面板上的点击/滑动/按键回注到真机。"""
        did = safe_device_id(device_id)
        if platform not in SUPPORTED or not did:
            raise ValueError("平台不支持操作或设备号无效")
        # 先校验指令，别为了一条非法请求还去跑两次 adb 查分辨率
        if str(payload.get("action") or "") not in _ACTIONS:
            raise ValueError(f"不支持的操作: {payload.get('action')}")
        key = (platform, did)
        with self._lock:
            cached = self._sizes.get(key)
        if cached and time.time() - cached[0] < 60:
            size = cached[1]
        else:
            size = display_size(platform, did, self.adb, self.hdc, runner=_run)
            with self._lock:
                self._sizes[key] = (time.time(), size)
        argv = control_argv(platform, did, payload, size, self.adb, self.hdc)
        t0 = time.perf_counter()
        self._inject(platform, did, argv)
        return {"ok": True, "action": payload.get("action"), "command": " ".join(argv[2:]),
                "size": {"width": size[0], "height": size[1], "rotation": size[2]},
                "latency_ms": int((time.perf_counter() - t0) * 1000)}

    # ---------- 内部 ----------
    def _probe_presence(self, platform: str, device_id: str) -> bool:
        """设备此刻是否在线（拔 USB / 掉线立刻能发现）。

        adb devices 里只有 state == device 才算在线（unauthorized/offline
        虽然出现在列表里，但抓不了帧）；hdc list targets 直接匹配序列号。
        """
        try:
            if platform == "harmony":
                out = _run([self.hdc, "list", "targets"], 6.0).decode("utf-8", "ignore")
                return device_id in out.split()
            out = _run([self.adb, "devices"], 6.0).decode("utf-8", "ignore")
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == device_id:
                    return parts[1] == "device"
            return False
        except Exception:  # noqa: BLE001  工具缺失/超时：按不在线处理，前端走断开流程
            return False

    def _present(self, platform: str, device_id: str) -> bool:
        key = (platform, device_id)
        with self._lock:
            cached = self._presence.get(key)
        if cached and time.time() - cached[0] < PRESENCE_TTL:
            return cached[1]
        ok = self._probe_presence(platform, device_id)
        with self._lock:
            self._presence[key] = (time.time(), ok)
        return ok

    def _shell(self, device_id: str) -> AdbShell:
        with self._lock:
            sh = self._shells.get(device_id)
            if sh is None:
                sh = AdbShell(self.adb, device_id)
                self._shells[device_id] = sh
            return sh

    def _inject(self, platform: str, device_id: str, argv: list[str]) -> None:
        """把指令送回设备执行。

        Android 优先走常驻 shell（省 ~195ms/条的 adb 进程启动）；
        鸿蒙、shell 掉线/超时、以及单测假 runner 的情况下一律退回一次性调用。
        """
        use_shell = (self.shell_inject and platform == "android"
                     and len(argv) > 4 and argv[3] == "shell"
                     and _run is _RUN_REAL)
        if use_shell:
            # argv[4:] 是 shell 里真正要执行的命令；用 shlex 重新组装成安全的一行
            # （input text 的内容可能带 ; & $ 之类，必须引用住，不能裸拼）。
            line = shlex.join(argv[4:])
            try:
                code = self._shell(device_id).run(line)
            except Exception:  # noqa: BLE001  常驻 shell 挂了：丢掉，退回一次性调用
                with self._lock:
                    dead = self._shells.pop(device_id, None)
                if dead is not None:
                    dead.close()
            else:
                if code != 0:
                    raise RuntimeError(f"设备执行失败（exit {code}）")
                return
        _run(argv, 8.0)

    def close(self) -> None:
        """收掉所有常驻 shell（控制台退出/单测清理用）。"""
        with self._lock:
            shells, self._shells = self._shells, {}
        for sh in shells.values():
            sh.close()

    def _grab(self, platform: str, device_id: str) -> bytes:
        if platform == "android":
            return grab_android(self.adb, device_id)
        if platform == "harmony":
            return grab_harmony(self.hdc, device_id)
        raise RuntimeError(f"平台 {platform} 不支持投屏")

    def _tool(self) -> Toolchain:
        # 用 lambda 动态查模块全局，单测 mock.patch(cast, "_run") 才能生效
        return Toolchain(adb=self.adb, hdc=self.hdc, ffmpeg=self.ffmpeg,
                         run=lambda argv, timeout: _run(argv, timeout))

    def _sources(self, ch: _Channel) -> list[FrameSource]:
        # 主链必须是"主动请求就有帧"的通道：screenrecord 在画面静止时
        # 一帧都不吐，只能作为加速旁路（_booster），不能当主源，否则黑屏。
        names = self.methods or (["javacap", "adbcap"] if ch.platform == "android"
                                 else ["adbcap"])
        tool = self._tool()
        out: list[FrameSource] = []
        for name in names:
            if name == "h264":
                if not tool.ffmpeg or ch.platform != "android":
                    continue
                out.append(H264Source(tool, ch.device_id, FRAME_WIDTH, patient=True))
            elif name == "javacap":
                if ch.platform != "android":
                    continue
                out.append(JavaCapSource(tool, ch.device_id, scale=33))
            elif name == "adbcap":
                out.append(AdbCapSource(
                    grab=lambda: self._grab(ch.platform, ch.device_id),
                    encode=encode_frame,
                    interval=max(MIN_INTERVAL, 1.0 / max(1, ch.fps)),
                ))
        if not out:
            out = [AdbCapSource(grab=lambda: self._grab(ch.platform, ch.device_id),
                               encode=encode_frame, interval=0.5)]
        # 只有存在更优主源（javacap）时才把 adbcap 设为「间隙 filler」：
        # 主源临时故障时它只跑 2s 就主动让位，避免永久掉到 0.8s/帧慢链。
        # 若 adbcap 是独苗，不设 burst，否则每 2s 重启一次慢链更卡。
        if any(s.name != "adbcap" for s in out):
            for s in out:
                if s.name == "adbcap":
                    s.burst = 2.0
        return out

    def _publish(self, ch: _Channel, jpeg: bytes, w: int, h: int, method: str) -> None:
        now = time.time()
        # 画面变化时 H264 旁路正在连续推帧，此时主链的旧帧再插进来会让画面
        # 时间线来回跳（用户看到的就是"一操作就猛抖"）。给旁路一个短窗口独占。
        if method != "h264+" and now < ch.boost_until:
            return
        with ch.cond:
            if ch.frame_size == (0, 0):
                ch.frame_size = (w, h)      # 本次投屏的基准尺寸，之后所有帧都对齐它
            elif (w, h) != ch.frame_size:
                try:
                    jpeg = normalize_frame(jpeg, ch.frame_size)
                except Exception:  # noqa: BLE001  # 坏帧宁可丢，也不能推异形帧引起抖动
                    return
                w, h = ch.frame_size
            # 去重必须**按来源**比：不同链路（H264 的 q=5 / javacap 缩放后重编码）
            # 同一画面编出来的字节不同，只跟全局上一帧比会导致两条链无限互相
            # 「顶掉」，画面在两种画质间来回闪。
            prev = ch.last_frames.get(method)
            if prev is not None and len(prev) == len(jpeg) and prev == jpeg:
                return                      # 重复帧：无视觉收益，白占带宽
            ch.last_frames[method] = jpeg
            if len(ch.last_frames) > 4:
                for k in list(ch.last_frames)[:-4]:
                    ch.last_frames.pop(k, None)
            if method == "h264+":
                ch.boost_until = now + BOOST_HOLD
            if ch.frame and ch.updated_at:
                ch.frame_ms = max(1, int((now - ch.updated_at) * 1000))
            ch.frame, ch.width, ch.height = jpeg, w, h
            ch.method, ch.error = method, ""
            ch.seq += 1
            ch.updated_at = now
            ch._stamps.append(now)
            ch._stamps = ch._stamps[-20:]
            if len(ch._stamps) >= 2:
                span = ch._stamps[-1] - ch._stamps[0]
                ch.measured_fps = round((len(ch._stamps) - 1) / span, 1) if span > 0 else 0.0
            ch.cond.notify_all()

    def _fail(self, ch: _Channel, message: str) -> None:
        with ch.cond:
            ch.error = str(message)[:180]
            ch.seq += 1          # 唤醒订阅者，让前端及时显示降级/错误
            ch.cond.notify_all()

    def _booster(self, ch: _Channel) -> None:
        """H264 旁路：画面一变就抢在主链前面推一帧，把点击回显压到 100ms 级。

        静止时它一帧都不产（screenrecord 只在画面变化时编码），主链 javacap
        继续 4fps 保底，所以既不黑屏也不空耗流量。
        `first_timeout=20` 让它在 screenrecord 卡死时自动重连自愈，而不是
        永远阻塞在等首帧上（卡死时主链 javacap 仍在工作，不影响投屏，但
        screenrecord 空挂着会白耗设备编码器）。
        """
        tool = self._tool()
        while not ch.stop.is_set():
            src = H264Source(tool, ch.device_id, FRAME_WIDTH, patient=True,
                            first_timeout=20.0)
            try:
                for jpeg, w, h in src.frames(ch.stop):
                    # 这里**不做**帧率节流：ffmpeg 解码出来的帧是成簇到达的
                    # （真机实测：每簇 3~4 帧、间隔 ~115ms）。加一个 33ms 的闸门
                    # 会把每簇砍到只剩 1 帧，交付率从 30fps 掉到 8.6fps——反而更卡。
                    # 交付节奏交给前端拉取控制（拿一帧、画完、再拿下一帧）。
                    self._publish(ch, jpeg, w, h, "h264+")
                    if ch.stop.is_set():
                        return
            except Exception:  # noqa: BLE001  # 旁路失败不影响主链
                pass
            finally:
                src.close()
            if ch.stop.is_set():
                return
            time.sleep(1.0)

    def _producer(self, ch: _Channel) -> None:
        """按优先级跑帧源，临时故障短退避后立即重试，绝不永久降级。

        早先的写法一旦 javacap 在某次页面切换时抖一下（socket 断/超时），
        就永久掉到 0.8s/帧的 adbcap 兜底链，整场投屏都卡；而且切换期间源
        之间换手的空档会让 MJPEG 流卡 5 秒。新逻辑：
        - 任一源失败只做「短退避」（0.4~4s 递增），紧接着重新尝试更优源；
        - adbcap 作为间隙 filler 只跑 `burst` 秒就主动让位，主源一恢复立刻接管；
        - 所以页面切换造成的瞬时掉帧最多持续一个 filler 窗口（~2s），随后自愈。
        """
        try:
            if (ch.platform == "android" and self.boost and self.ffmpeg
                    and not (self.methods and "h264" in self.methods)
                    and not ch.booster_started):
                ch.booster_started = True
                threading.Thread(target=self._booster, args=(ch,), daemon=True,
                                 name=f"atp-cast-boost-{ch.device_id[:12]}").start()
            sources = self._sources(ch)
            backoff: dict[str, float] = {s.name: 0.0 for s in sources}
            fails: dict[str, int] = {s.name: 0 for s in sources}
            # 这里只挡「30fps 硬上限」，不再按订阅时请求的 fps 节流。
            # 早先写成 1.0/ch.fps（默认 12fps ≈ 83ms），而 H264 每 16ms 就出一帧，
            # 结果是 23fps 的源被硬生生砍到 9fps——那才是「一点都不丝滑」的真凶。
            interval = MIN_INTERVAL
            while not ch.stop.is_set():
                if ch.viewers <= 0 and time.time() - ch.last_poll > IDLE_TTL:
                    break
                now = time.time()
                # 始终优先选「不在退避期」的最高优先级源，保证 javacap 一恢复就接管
                chosen = next((s for s in sources if now >= backoff[s.name]), None)
                if chosen is None:
                    time.sleep(0.3)
                    continue
                ch.method = chosen.name
                burst = getattr(chosen, "burst", 0) or 0
                burst_deadline = (now + burst) if burst else None
                got = False
                last_push = 0.0
                try:
                    for jpeg, w, h in chosen.frames(ch.stop):
                        got = True
                        fails[chosen.name] = 0
                        tnow = time.time()
                        # 长连接帧源是无限生成器，空闲判定必须在帧循环里做，
                        # 否则"没人看 20 秒就停"永远等不到生成器返回。
                        if ch.viewers <= 0 and tnow - ch.last_poll > IDLE_TTL:
                            ch.stop.set()
                            break
                        if tnow - last_push >= interval:
                            self._publish(ch, jpeg, w, h, chosen.name)
                            last_push = tnow
                        if ch.stop.is_set():
                            break
                        # filler 跑满窗口就主动让位，让主源有机会重新接管
                        if burst_deadline and tnow > burst_deadline:
                            break
                except Exception as e:  # noqa: BLE001
                    fails[chosen.name] += 1
                    backoff[chosen.name] = time.time() + min(4.0, 0.5 * fails[chosen.name])
                    self._fail(ch, f"{chosen.name}: {e}")
                finally:
                    chosen.close()
                if ch.stop.is_set():
                    break
                # 一条帧都没出（首帧超时/进程立即退出）：更短退避，快速让位给更快链路
                if not got:
                    backoff[chosen.name] = time.time() + 0.4
                # 没有可用源时短暂退避，避免疯狂重启进程
                if all(time.time() < backoff[s.name] for s in sources):
                    time.sleep(0.6)
        finally:
            # 自然过期（没人看 20 秒）也要停掉 H264 加速旁路，
            # 否则 screenrecord 会一直挂在设备上空耗电量和编码器。
            ch.stop.set()
            with self._lock:
                key = (ch.platform, safe_device_id(ch.device_id))
                if self._channels.get(key) is ch:
                    self._channels.pop(key, None)


__all__ = ["CastHub", "DEFAULT_FPS", "KEYCODES", "MAX_FPS", "SUPPORTED",
           "control_argv", "display_size", "encode_frame", "error_frame", "safe_device_id"]
