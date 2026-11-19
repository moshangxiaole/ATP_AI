"""视频模式投屏：screenrecord 裸 H264 → 浏览器 WebCodecs 硬解。

为什么不再走「JPEG 逐帧推给 <img>」：那是服务端产一张图、浏览器换一次 src，
浏览器解码跟不上就积压回放（画面时间线倒退），换 src 也有渲染闪烁的可能。
视频流方案：设备端硬件编码 H264，PC 端**零解码**只搬运字节，浏览器用
WebCodecs 硬解后画到 canvas 上——canvas.drawImage 是原子的，不存在
"半张图/旧图回跳"；客户端慢只会掉帧率，绝不会倒退时间线。

协议要点（真机实测，vivo V2283A / Android 15）：
- `screenrecord --output-format=h264` 产出裸 H264 Annex B 流（无帧头）；
- 60fps，IDR 每 10s 一次，SPS/PPS 只在流开始时出现一次；
- 所以**每个新订阅者加入时重启一次 screenrecord**：实测 0.4s 就出首个 IDR；
- 170s time-limit 到期自动重启，重启即新配置（SPS/PPS+IDR）。

触控不在这里：继续走 `/api/device/cast/input`（adb input，实测 ~64ms）。
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field

# NAL 类型
NAL_SLICE = 1
NAL_IDR = 5
NAL_SEI = 6
NAL_SPS = 7
NAL_PPS = 8

START_3 = b"\x00\x00\x01"
START_4 = b"\x00\x00\x00\x01"

_BITRATE = 4_000_000
_TIME_LIMIT = 170       # screenrecord 单次最长录制，到期自动重启
_LONG_SIDE = 1024       # 视频长边（面板最大 ~560px 宽，足够清晰）
_SUB_QUEUE = 512        # 每个观众最多缓存的 NAL 数（满即 resync）
_IDLE_TTL = 20.0        # 没有订阅者后多久杀掉 screenrecord
_RESTART_MIN_AGE = 3.0  # 通道已运行超过这个秒数，新观众加入就重启（拿新 IDR）


def video_size(width: int, height: int) -> tuple[int, int]:
    """按设备真实分辨率等比算出编码尺寸（长边 1024，偶数对齐）。"""
    if width <= 0 or height <= 0:
        return (456, 1024)
    scale = _LONG_SIDE / max(width, height)
    w = max(2, int(width * scale) // 2 * 2)
    h = max(2, int(height * scale) // 2 * 2)
    return (w, h)


def _create_no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def split_nals(buf: bytes) -> tuple[bytes, list[bytes]]:
    """从字节流切出**完整** NAL（不含起始码），返回 (未完成尾部, 完整 NAL 列表)。

    - 同时处理 3/4 字节起始码；
    - 最后一个起始码之后的字节留作尾部（它的 NAL 可能还没到齐，下一轮重切）；
    - 第一个起始码之前的字节视为垃圾丢弃（跨包残留）。
    """
    n = len(buf)
    starts: list[tuple[int, int]] = []     # (起始码起点, NAL 内容起点)
    i = buf.find(START_3)
    while i != -1:
        if i > 0 and buf[i - 1:i] == b"\x00":
            starts.append((i - 1, i + 3))          # 4 字节起始码
        else:
            starts.append((i, i + 3))              # 3 字节起始码
        i = buf.find(START_3, i + 3)
    if not starts:
        return buf[-4:], []
    nals: list[bytes] = []
    for k in range(len(starts) - 1):
        piece = buf[starts[k][1]:starts[k + 1][0]]
        if piece:
            nals.append(piece)
    return buf[starts[-1][0]:], nals


def nal_type(nal: bytes) -> int:
    return (nal[0] & 0x1F) if nal else 0


@dataclass
class _Subscriber:
    """一个观众 = 一个有界 NAL 队列 + resync 标记。"""

    q: "queue.Queue" = field(default_factory=lambda: queue.Queue(maxsize=_SUB_QUEUE))
    resync: bool = True
    alive: bool = True


class VideoChannel:
    """一台设备一条 H264 视频通道（screenrecord 进程 + NAL 解析 + 扇出）。"""

    def __init__(self, adb: str, device_id: str, size: tuple[int, int]):
        self.adb = adb
        self.device_id = device_id
        self.size = size
        self.error = ""
        self.nal_count = 0
        self._lock = threading.Lock()
        self._subs: list[_Subscriber] = []
        self._config_nals: list[bytes] = []   # 最近一次 SPS+PPS（新观众必须先拿到）
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self._last_idle = 0.0
        self._stop = threading.Event()

    # ---------- 对外 ----------
    def status(self) -> dict:
        with self._lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "viewers": len(self._subs),
                "size": list(self.size),
                "error": self.error,
                "age": round(time.time() - self._started_at, 1) if self._started_at else 0,
            }

    def subscribe(self) -> _Subscriber:
        sub = _Subscriber()
        with self._lock:
            self._subs.append(sub)
            running = bool(self._thread and self._thread.is_alive())
            age = time.time() - self._started_at if self._started_at else 1e9
        if not running:
            self._start()
        elif age > _RESTART_MIN_AGE:
            # 通道已在跑：新观众加入就重启，0.4s 拿到新 IDR，
            # 比等下一个 10s 周期的 IDR 快得多。
            self._kill()
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        sub.alive = False
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
            if not self._subs:
                self._last_idle = time.time()

    def stop(self) -> None:
        self._stop.set()
        self._kill()
        with self._lock:
            self._subs.clear()

    # ---------- 内部 ----------
    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        self._started_at = 0.0
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass

    def _spawn(self) -> subprocess.Popen:
        w, h = self.size
        argv = [self.adb, "-s", self.device_id, "exec-out", "screenrecord",
                "--output-format=h264", "--size", f"{w}x{h}",
                "--bit-rate", str(_BITRATE), "--time-limit", str(_TIME_LIMIT), "-"]
        return subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=_create_no_window())  # noqa: S603

    def _start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._pump, daemon=True,
            name=f"atp-video-{self.device_id[:16]}")
        self._thread.start()

    def _pump(self) -> None:
        """读 screenrecord → 切 NAL → 扇出。没人看超过 20s 就退出。"""
        carry = b""
        while not self._stop.is_set():
            with self._lock:
                has_sub = bool(self._subs)
            if not has_sub:
                if self._last_idle and time.time() - self._last_idle > _IDLE_TTL:
                    break
                time.sleep(0.2)
                continue
            self._last_idle = 0.0
            try:
                self._proc = self._spawn()
                self._started_at = time.time()
                carry = b""
                while not self._stop.is_set():
                    with self._lock:
                        if not self._subs:
                            break
                    chunk = self._proc.stdout.read1(65536)
                    if not chunk:
                        break               # time-limit 到期或进程退出
                    carry, nals = split_nals(carry + chunk)
                    for nal in nals:
                        self._dispatch(nal)
            except (OSError, ValueError):
                pass
            finally:
                self._kill()
            if not self._stop.is_set():
                time.sleep(0.3)             # 稍歇再重启

        with self._lock:
            self._subs.clear()

    def config_nals(self) -> list[bytes]:
        """当前生效的 SPS/PPS（新观众解码前必须先收到）。"""
        with self._lock:
            return list(self._config_nals)

    def _dispatch(self, nal: bytes) -> None:
        kind = nal_type(nal)
        if kind == NAL_SPS:
            with self._lock:
                self._config_nals = [nal]      # 新 SPS → 重置配置
        elif kind == NAL_PPS:
            with self._lock:
                self._config_nals.append(nal)
        self.nal_count += 1
        is_config = kind in (NAL_SPS, NAL_PPS)
        is_idr = kind == NAL_IDR
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            if not sub.alive:
                continue
            try:
                sub.q.put_nowait((is_config, is_idr, nal))
            except queue.Full:
                # 消费太慢：清空并标记 resync，写线程会重发配置并跳到下一个 IDR
                while True:
                    try:
                        sub.q.get_nowait()
                    except queue.Empty:
                        break
                sub.resync = True


class VideoHub:
    """按设备共享的视频通道（与 JPEG 的 CastHub 平行，互不干扰）。"""

    def __init__(self, config=None):
        self.adb = getattr(config, "adb", "") or "adb"
        self._channels: dict[str, VideoChannel] = {}
        self._sizes: dict[str, tuple[float, tuple[int, int]]] = {}
        self._lock = threading.Lock()

    def channel(self, device_id: str) -> VideoChannel:
        with self._lock:
            ch = self._channels.get(device_id)
        if ch is not None:
            return ch
        size = self._resolve_size(device_id)
        with self._lock:
            ch = self._channels.get(device_id)      # 双检：并发首连
            if ch is None:
                ch = VideoChannel(self.adb, device_id, size)
                self._channels[device_id] = ch
            return ch

    def _resolve_size(self, device_id: str) -> tuple[int, int]:
        """按设备真实分辨率算编码尺寸（长边 1024），缓存 5 分钟。"""
        with self._lock:
            cached = self._sizes.get(device_id)
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        try:
            from atp.webapp.cast.hub import device_physical_size, Toolchain
            w, h = device_physical_size(Toolchain(adb=self.adb), device_id)
            size = video_size(w, h)
        except Exception:  # noqa: BLE001  探测失败用保守默认
            size = (456, 1024)
        with self._lock:
            self._sizes[device_id] = (time.time(), size)
        return size

    def stop(self, device_id: str) -> None:
        with self._lock:
            ch = self._channels.pop(device_id, None)
        if ch is not None:
            ch.stop()

    def close(self) -> None:
        with self._lock:
            channels, self._channels = self._channels, {}
        for ch in channels.values():
            ch.stop()
