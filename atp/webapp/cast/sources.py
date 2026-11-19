"""投屏帧源：一台设备上可选的几条「长连接画面通道」。

参照 AirtestIDE 的 ScreenProxy.auto_setup 思路（按优先级试，能用哪条用哪条），
但针对 Android 12+ 做了调整——AirtestIDE 的 minicap 依赖系统私有截图 API，
在 SDK 34/35 上会直接 abort（真机实测 `PID: ... INFO: Using projection` 后无输出），
它的 minitouch 也因 Android 15 收紧 /dev/input 权限而报 Permission denied。
所以这里用 AirtestIDE 新版 scrcpy 同款的 **H264 视频流** 当主力，
Yosemite javacap 当次选，纯 adb screencap 兜底：

    h264     screenrecord -> ffmpeg -> MJPEG   真机实测 ~28fps / <100ms
    javacap  Yosemite Capture socket           真机实测 ~4fps / 250ms
    adbcap   adb exec-out screencap -p        真机实测 ~1.2fps / 800ms

每条源都是"持续产出 JPEG 帧"的生成器，Hub 只负责共享与推流；
首帧超时或进程退出就换下一条，不会让控制台卡在黑屏。
"""

from __future__ import annotations

import io
import os
import queue
import re
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator

from PIL import Image

_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3}
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_PHYSICAL = re.compile(r"(\d+)x(\d+)")


@dataclass
class Toolchain:
    """帧源需要的外部命令；由 CastHub 注入，单测里可以整包替换。"""

    adb: str = "adb"
    hdc: str = "hdc"
    ffmpeg: str = ""
    run: Callable[[list[str], float], bytes] | None = None
    popen: Callable[..., subprocess.Popen] | None = None

    def argv_run(self, argv: list[str], timeout: float = 8.0) -> bytes:
        if self.run:
            return self.run(argv, timeout)
        p = subprocess.run(argv, capture_output=True, timeout=timeout,
                           creationflags=_CREATE_NO_WINDOW)  # noqa: S603
        if p.returncode != 0:
            raise RuntimeError((p.stderr or b"").decode("utf-8", "ignore")[:160])
        return p.stdout or b""

    def argv_popen(self, argv: list[str], **kwargs) -> subprocess.Popen:
        if self.popen:
            return self.popen(argv, **kwargs)
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.DEVNULL)
        kwargs.setdefault("creationflags", _CREATE_NO_WINDOW)
        return subprocess.Popen(argv, **kwargs)  # noqa: S603


def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def jpeg_size(data: bytes) -> tuple[int, int]:
    """不解码整张图，只从 SOF 段里读宽高。"""
    with Image.open(io.BytesIO(data)) as img:
        return img.size[0], img.size[1]


def device_physical_size(tool: Toolchain, device_id: str) -> tuple[int, int]:
    """`wm size` 的物理分辨率；失败时给一个保守默认值。"""
    out = tool.argv_run([tool.adb, "-s", device_id, "shell", "wm", "size"], 6.0)
    m = _PHYSICAL.search(out.decode("utf-8", "ignore"))
    return (int(m.group(1)), int(m.group(2))) if m else (1080, 1920)


def projected_size(w: int, h: int, max_width: int = 420) -> tuple[int, int]:
    """H264 编码器要求宽高都是偶数，且尽量贴合原比例。"""
    if w <= 0 or h <= 0:
        return 420, 932
    scale = min(1.0, max_width / w)
    pw = max(2, int(w * scale) // 2 * 2)
    ph = max(2, int(h * scale) // 2 * 2)
    return pw, ph


class _PipeReader:
    """Windows 的 select 不支持匿名管道，用读线程 + 队列才能及时响应停止。"""

    def __init__(self, stream):
        self._q: queue.Queue[bytes] = queue.Queue()
        self._stream = stream
        self._t = threading.Thread(target=self._work, daemon=True, name="atp-cast-reader")
        self._t.start()

    def _work(self) -> None:
        # BufferedReader.read(n) 会**一直等到读满 n 字节**，静止画面时
        # ffmpeg 每秒只吐几百字节，首帧就永远等不出来。read1 只做一次
        # 底层读，有多少拿多少，这才是低延迟流该用的姿势。
        read = getattr(self._stream, "read1", None) or self._stream.read
        try:
            while True:
                chunk = read(65536)
                if not chunk:
                    break
                self._q.put(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self._q.put(b"")

    def get(self, timeout: float = 0.3) -> bytes | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None


def iter_mjpeg(reader: _PipeReader, stop: threading.Event,
               first_timeout: float | None = 9.0,
               alive: Callable[[], bool] | None = None) -> Iterator[bytes]:
    """从 ffmpeg 的 MJPEG 输出里切出一张张 JPEG。

    `None` = 暂时没数据（静止画面很正常），`b""` = 流结束。
    只在**首帧**上做超时判定；`first_timeout=None` 表示无限等——
    screenrecord 在画面完全静止时一帧都不吐，作为"加速通道"必须耐心等
    到用户点一下让画面变化，不能自己把链路判死。

    `alive` 给了进程存活性判断：超时到点时如果**进程还活着**，说明它只是
    在等画面变化（静止画面下 screenrecord 本来就不编码），这时续一个超时
    周期继续等；只有进程真的死了才判死。早先这里直接 raise，结果是静止
    20 秒就把好好的 screenrecord 杀掉重连，重连要 2~3 秒——用户刚想操作，
    恰好撞上这段空窗，体验就是「一上手先卡两秒」。
    """
    buf = b""
    first = True
    deadline = time.time() + first_timeout if first_timeout is not None else None
    while not stop.is_set():
        chunk = reader.get(0.3)
        if chunk is None:
            if first and deadline is not None and time.time() > deadline:
                if alive is not None and alive():
                    deadline = time.time() + first_timeout
                    continue
                raise RuntimeError("H264 流没有产出首帧")
            continue
        if not chunk:
            if first:
                raise RuntimeError("H264 流在首帧前就结束了")
            return
        buf += chunk
        while True:
            start = buf.find(b"\xff\xd8")
            if start < 0:
                buf = buf[-2:] if len(buf) > 2 else buf
                break
            end = buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                buf = buf[start:]
                break
            frame = buf[start:end + 2]
            buf = buf[end + 2:]
            first = False
            yield frame
        if len(buf) > 8 * 1024 * 1024:      # 异常流兜底，避免内存无限增长
            buf = b""


class FrameSource:
    name = "base"

    def frames(self, stop: threading.Event) -> Iterator[tuple[bytes, int, int]]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class H264Source(FrameSource):
    """Android 自带 screenrecord 输出 H264，PC 端 ffmpeg 转 MJPEG。"""

    name = "h264"

    def __init__(self, tool: Toolchain, device_id: str, max_width: int = 420,
                 bitrate: int = 1_500_000, time_limit: int = 170,
                 patient: bool = False, first_timeout: float | None = None):
        self.tool = tool
        self.device_id = device_id
        self.max_width = max_width
        self.bitrate = bitrate
        self.time_limit = time_limit
        self.patient = patient        # 加速通道：静止时不判超时，等画面变化
        # 首帧超时：None 表示无限等（patient 加速旁路用）；否则到点判死并重连。
        # 设一个较大的有限值，既能容忍画面长时间静止，又能在 screenrecord 卡死时自愈。
        self.first_timeout = first_timeout
        self._procs: list[subprocess.Popen] = []
        self._stop = threading.Event()
        w, h = device_physical_size(tool, device_id)
        self.width, self.height = projected_size(w, h, max_width)

    def frames(self, stop: threading.Event) -> Iterator[tuple[bytes, int, int]]:
        if not self.tool.ffmpeg:
            raise RuntimeError("未找到 ffmpeg，无法解码 H264")
        failures = 0
        while not stop.is_set() and not self._stop.is_set():
            got = False
            try:
                sr = self.tool.argv_popen([
                    self.tool.adb, "-s", self.device_id, "exec-out", "screenrecord",
                    "--output-format=h264", "--size", f"{self.width}x{self.height}",
                    "--bit-rate", str(self.bitrate), "--time-limit", str(self.time_limit), "-",
                ])
                ff = self.tool.argv_popen([
                    self.tool.ffmpeg, "-loglevel", "error",
                    "-f", "h264", "-r", "25", "-i", "pipe:0",
                    "-f", "mjpeg", "-q:v", "5", "pipe:1",
                ], stdin=sr.stdout)
                self._procs = [sr, ff]
                first_timeout = self.first_timeout if self.first_timeout is not None \
                    else (None if self.patient else 9.0)
                for jpeg in iter_mjpeg(
                        _PipeReader(ff.stdout), stop, first_timeout,
                        # 进程还活着 = 只是画面静止（screenrecord 静止时不编码），
                        # 别把它当故障杀掉重连。
                        alive=lambda: sr.poll() is None and ff.poll() is None):
                    got = True
                    yield jpeg, self.width, self.height
            except GeneratorExit:
                return
            except Exception as e:  # noqa: BLE001
                if not got:
                    # 首帧都出不来说明这条链路在这台设备上根本不可用，
                    # 别在这里反复重连 27 秒——直接交给 Hub 切下一条。
                    raise RuntimeError(f"H264 首帧失败: {str(e)[:120]}") from e
                failures += 1
                if failures >= 3:
                    raise RuntimeError("H264 投屏连续失败，切到备用链路") from e
            finally:
                self._kill()
            if stop.is_set() or self._stop.is_set():
                return
            failures = 0                    # 跑满 time-limit 后重连，属正常
            time.sleep(0.3)

    def _kill(self) -> None:
        for p in self._procs:
            try:
                p.kill()
            except (OSError, AttributeError):
                pass
        self._procs = []

    def close(self) -> None:
        self._stop.set()
        self._kill()


class JavaCapSource(FrameSource):
    """AirtestIDE 的 Yosemite Capture：adb forward + JPEG socket 长连接。"""

    name = "javacap"
    PKG = "com.netease.nie.yosemite"
    CAPTURE = "com.netease.nie.yosemite.Capture"

    def __init__(self, tool: Toolchain, device_id: str, scale: int = 33):
        self.tool = tool
        self.device_id = device_id
        self.scale = max(10, min(100, scale))
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._port = 0
        self._name = ""
        self._stop = threading.Event()

    def _setup(self, stop: threading.Event) -> None:
        apk = self.tool.argv_run(
            [self.tool.adb, "-s", self.device_id, "shell", "pm", "path", self.PKG], 6.0
        ).decode("utf-8", "ignore").strip()
        if not apk.startswith("package:"):
            raise RuntimeError("设备未安装 Yosemite.apk，javacap 不可用")
        apk_path = apk.split(":", 1)[1].strip()
        self._port = free_port()
        self._name = "atp_jc_" + re.sub(r"[^A-Za-z0-9]", "", self.device_id)[-16:]
        self.tool.argv_run(
            [self.tool.adb, "-s", self.device_id, "forward",
             f"tcp:{self._port}", f"localabstract:{self._name}"], 6.0
        )
        cmd = (f"CLASSPATH={apk_path} exec app_process /system/bin {self.CAPTURE} "
               f"--scale {self.scale} --socket {self._name} -lazy 2>&1")
        self._proc = self.tool.argv_popen(
            [self.tool.adb, "-s", self.device_id, "shell", cmd],
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 6.0
        ready = False
        while time.time() < deadline and not stop.is_set():
            line = self._proc.stdout.readline() if self._proc.stdout else b""
            if not line:
                break
            if b"listening" in line.lower():
                ready = True
                break
        if not ready:
            raise RuntimeError("javacap 服务启动超时或未监听")
        if self._proc.poll() is not None:
            raise RuntimeError("javacap 服务立即退出")
        self._sock = socket.create_connection(("127.0.0.1", self._port), timeout=5)
        self._sock.settimeout(0.5)
        header = self._recv_exact(24, stop)
        if len(header) != 24:
            raise RuntimeError("javacap 握手失败")

    def _recv_exact(self, n: int, stop: threading.Event) -> bytes:
        buf = b""
        while len(buf) < n and not stop.is_set() and not self._stop.is_set():
            try:
                part = self._sock.recv(n - len(buf))  # type: ignore[union-attr]
            except socket.timeout:
                continue
            if not part:
                break
            buf += part
        return buf

    def frames(self, stop: threading.Event) -> Iterator[tuple[bytes, int, int]]:
        self._setup(stop)
        while not stop.is_set() and not self._stop.is_set():
            self._sock.sendall(b"1")  # type: ignore[union-attr]
            head = self._recv_exact(4, stop)
            if len(head) < 4:
                if stop.is_set() or self._stop.is_set():
                    return
                raise RuntimeError("javacap 连接断开")
            size = struct.unpack("<I", head)[0]
            if not 0 < size < 12 * 1024 * 1024:
                raise RuntimeError(f"javacap 帧长度异常: {size}")
            data = self._recv_exact(size, stop)
            if len(data) < size:
                if stop.is_set() or self._stop.is_set():
                    return
                raise RuntimeError("javacap 帧被截断")
            if not data.startswith(b"\xff\xd8"):
                raise RuntimeError("javacap 返回的不是 JPEG")
            w, h = jpeg_size(data)
            yield data, w, h

    def close(self) -> None:
        self._stop.set()
        for close in (lambda: self._sock and self._sock.close(),
                      lambda: self._proc and self._proc.kill()):
            try:
                close()
            except (OSError, AttributeError):
                pass
        if self._port:
            try:
                self.tool.argv_run([self.tool.adb, "-s", self.device_id,
                                    "forward", "--remove", f"tcp:{self._port}"], 4.0)
            except Exception:  # noqa: BLE001
                pass


class AdbCapSource(FrameSource):
    """零依赖兜底：每次一条 `screencap -p`，慢但永远可用。

    `burst` > 0 时只运行这么久就主动退出，把主源让出来——它只作为主源
    临时故障时的「间隙 filler」，不能永久霸占抓帧线程，否则一旦 javacap
    抖一下，整场投屏就掉到 0.8s/帧的慢链上再也不回来（真机复现过的卡顿）。
    """

    name = "adbcap"

    def __init__(self, grab: Callable[[], bytes], encode: Callable[[bytes], tuple[bytes, int, int]],
                 interval: float = 0.5, burst: float = 0):
        self.grab = grab
        self.encode = encode
        self.interval = interval
        self.burst = burst

    def frames(self, stop: threading.Event) -> Iterator[tuple[bytes, int, int]]:
        while not stop.is_set():
            t0 = time.time()
            jpeg, w, h = self.encode(self.grab())
            yield jpeg, w, h
            wait = self.interval - (time.time() - t0)
            while wait > 0 and not stop.is_set():
                time.sleep(min(0.2, wait))
                wait = self.interval - (time.time() - t0)


__all__ = ["AdbCapSource", "FrameSource", "H264Source", "JavaCapSource", "Toolchain",
           "device_physical_size", "iter_mjpeg", "jpeg_size", "projected_size", "_PipeReader"]
