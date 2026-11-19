"""执行录屏：PC 端（desktop）/ Web 端执行过程 → MP4。

用户要求：用例执行完成后，在报告页里以 MP4 视频回放执行过程。

三种帧来源（按驱动能力自动选，**绝不让录屏线程碰 Playwright**——
它的 sync API 绑定创建线程，跨线程调用直接 greenlet 崩溃）：

1. **CDP 独立连接**（ElectronCdpDriver / 带调试端口的 Chromium）：
   录屏线程开一条**自己的** websocket 做 `Page.captureScreenshot`，
   与驱动的连接互不干扰，~4fps 真实录屏。
2. **全屏轮询**（Windows UIA/视觉驱动）：`PIL.ImageGrab.grab()` 无状态、
   线程安全，直接后台轮询。
3. **步骤帧兜底**（Playwright 自启动浏览器等线程不安全驱动）：
   不开后台线程，执行器把每步的前后证据截图喂进来（`ingest`），
   按真实时间戳拼成视频——粒度是步骤级，但保证"一定有视频"。

编码统一走 concat demuxer + 每帧真实时长（静止 10 秒就是 10 秒，不造假），
ffmpeg 缺失或一帧都没有时静默跳过——录屏永远不能影响用例执行本身。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from atp.runs.artifacts import RECORDING_FILE

_FRAMES_DIR = "frames"
# 录屏清晰度：用户反馈 1024 长边 + JPEG 70 在报告页放大看很模糊。
# 提到 1600 长边 + JPEG 90 + crf 18（近无损），文字和按钮边框都能看清。
# 可用 ATP_RECORD_LONG_SIDE / ATP_RECORD_QUALITY 覆盖（弱机器调低换体积）。
_LONG_SIDE = int(os.environ.get("ATP_RECORD_LONG_SIDE", "1600"))
_QUALITY = int(os.environ.get("ATP_RECORD_QUALITY", "90"))
_CRF = os.environ.get("ATP_RECORD_CRF", "18")
_MAX_FRAMES = 6000       # 4fps × 25min 封顶，防失控
_MIN_DUR = 0.08
_MAX_DUR = 2.5


def _ffmpeg() -> str:
    """优先环境变量 / PATH，再找 Windows 常见安装位置。"""
    exe = os.environ.get("ATP_FFMPEG", "")
    if exe and Path(exe).exists():
        return exe
    found = shutil.which("ffmpeg")
    if found:
        return found
    for pat in ("C:/Program Files/ffmpeg/bin/ffmpeg.exe",
                "C:/ffmpeg/bin/ffmpeg.exe"):
        if Path(pat).exists():
            return pat
    return ""


def _is_disabled() -> bool:
    return os.environ.get("ATP_RECORD", "").strip().lower() in ("0", "false", "off")


class RunRecorder:
    """一次用例执行对应一个实例；start() 后可用、stop() 后出 MP4。"""

    def __init__(self, driver, art: Path, fps: float = 4.0):
        self.driver = driver
        self.art = art
        self.fps = max(1.0, min(8.0, fps))
        self.frames_dir = art / _FRAMES_DIR
        self.video_path = ""
        self._frames: list[tuple[float, Path]] = []   # (时间戳, 文件)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stopped = False
        self._n = 0
        self._size: tuple[int, int] | None = None   # 统一画布，由第一帧决定

    # ---------- 对外 ----------
    def start(self) -> bool:
        """启动后台帧源（选得到就启动）。返回是否开了后台线程。"""
        if _is_disabled():
            return False
        target = self.frames_dir
        target.mkdir(parents=True, exist_ok=True)
        loop = self._pick_source()
        if loop is None:
            return False             # 兜底模式：只吃 ingest 的步骤帧
        self._thread = threading.Thread(
            target=loop, daemon=True, name="atp-recorder")
        self._thread.start()
        return True

    def ingest(self, png: bytes) -> None:
        """主线程喂帧（证据截图）。任何驱动都能用，是兜底模式的唯一帧源。"""
        if self._stopped or not png:
            return
        self._save(png)

    def stop(self) -> str:
        """停帧源并把已收帧编码成 MP4。幂等；返回视频路径（没有则空串）。"""
        if self._stopped:
            return self.video_path
        self._stopped = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        with self._lock:
            frames = sorted(self._frames)
        if not frames:
            shutil.rmtree(self.frames_dir, ignore_errors=True)
            return ""
        out = self.art / RECORDING_FILE
        try:
            self._encode(frames, out)
            self.video_path = str(out)
        except Exception:  # noqa: BLE001  录屏失败不能影响执行结果
            self.video_path = ""
        finally:
            shutil.rmtree(self.frames_dir, ignore_errors=True)
        return self.video_path

    # ---------- 帧源选择 ----------
    def _pick_source(self):
        """返回后台线程函数；None = 没有线程安全的帧源（走 ingest 兜底）。"""
        # 1) Electron/原生 CDP：驱动手里有 target 的 ws 地址，自己再开一条连接
        target = getattr(self.driver, "_target", None) or {}
        ws_url = target.get("webSocketDebuggerUrl") if isinstance(target, dict) else ""
        if ws_url and getattr(self.driver, "endpoint", ""):
            return lambda: self._cdp_frames(ws_url)
        # 2) Playwright 系（web_cdp / open_desktop 的 CDP 档）：线程不安全，不碰
        if type(self.driver).__name__ in ("WebCdpDriver",):
            return None
        # 3) Windows 桌面（UIA/视觉）：ImageGrab 无状态，线程安全
        if self.driver.platform == "desktop":
            return self._grab_frames
        return None

    # ---------- 后台帧源 ----------
    def _cdp_frames(self, ws_url: str) -> None:
        from atp.agents.platforms.pc.electron import no_proxy_env

        try:
            with no_proxy_env():
                import websocket

                ws = websocket.create_connection(
                    ws_url, timeout=4, suppress_origin=True,
                    http_no_proxy=["127.0.0.1", "localhost", "::1"])
        except Exception:  # noqa: BLE001  连不上就没有视频，绝不能影响执行
            return
        interval = 1.0 / self.fps
        seq = 0
        try:
            while not self._stop.is_set():
                t0 = time.time()
                seq += 1
                try:
                    # fromSurface=False 直接从渲染器取：窗口被遮挡/最小化也不会卡死
                    ws.send(json.dumps({
                        "id": seq, "method": "Page.captureScreenshot",
                        "params": {"format": "jpeg", "quality": 90,
                                   "fromSurface": False}}))
                    data = ""
                    while not self._stop.is_set():
                        msg = json.loads(ws.recv())
                        if msg.get("id") == seq:
                            data = (msg.get("result") or {}).get("data") or ""
                            break
                    if data:
                        self._save(base64.b64decode(data))
                except Exception:  # noqa: BLE001
                    self._stop.wait(0.5)
                rest = interval - (time.time() - t0)
                if rest > 0:
                    self._stop.wait(rest)
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _grab_frames(self) -> None:
        interval = 1.0 / self.fps
        while not self._stop.is_set():
            t0 = time.time()
            try:
                shot = self.driver.screenshot()
                if shot:
                    self._save(shot)
            except Exception:  # noqa: BLE001
                pass
            rest = interval - (time.time() - t0)
            if rest > 0:
                self._stop.wait(rest)

    # ---------- 落盘与编码 ----------
    def _canvas(self, w: int, h: int) -> tuple[int, int]:
        """整条视频的统一画布：由**第一帧**定长边 1024 的偶数尺寸。

        帧源会混装（录屏线程的截屏、执行器喂进来的全分辨率证据截图），
        concat demuxer 遇到尺寸不一致的帧会按第一帧强行拉伸——这就是用户看到的
        「画面形变」。所以所有帧都必须落到同一块画布上：等比缩放 + 黑边填充。
        """
        if self._size is None:
            scale = _LONG_SIDE / max(w, h)
            self._size = (max(2, round(w * scale) // 2 * 2),
                          max(2, round(h * scale) // 2 * 2))
        return self._size

    def _save(self, png: bytes) -> None:
        import io

        from PIL import Image

        with self._lock:
            if self._n >= _MAX_FRAMES:
                return
            self._n += 1
            n = self._n
        try:
            with Image.open(io.BytesIO(png)) as img:
                img = img.convert("RGB")
                cw, ch = self._canvas(img.width, img.height)
                if (img.width, img.height) != (cw, ch):
                    scale = min(cw / img.width, ch / img.height)
                    nw = max(2, round(img.width * scale) // 2 * 2)
                    nh = max(2, round(img.height * scale) // 2 * 2)
                    flat = Image.new("RGB", (cw, ch), (0, 0, 0))
                    flat.paste(img.resize((nw, nh)), ((cw - nw) // 2, (ch - nh) // 2))
                    img = flat
                p = self.frames_dir / f"f_{n:06d}.jpg"
                img.save(p, format="JPEG", quality=_QUALITY)
        except Exception:  # noqa: BLE001  坏帧丢弃
            return
        with self._lock:
            self._frames.append((time.time(), p))

    def _encode(self, frames: list[tuple[float, Path]], out: Path) -> None:
        exe = _ffmpeg()
        if not exe:
            raise RuntimeError("ffmpeg 不可用")
        # concat 清单用相对路径（cwd=frames_dir），绕开中文路径与空格的坑
        lst = self.frames_dir / "list.txt"
        lines = ["ffconcat version 1.0"]
        for i, (ts, p) in enumerate(frames):
            lines.append(f"file '{p.name}'")
            if i + 1 < len(frames):
                d = min(_MAX_DUR, max(_MIN_DUR, frames[i + 1][0] - ts))
                lines.append(f"duration {d:.3f}")
        lines.append(f"file '{frames[-1][1].name}'")   # concat 的坑：末帧要重复一次
        lst.write_text("\n".join(lines), encoding="utf-8")
        try:
            subprocess.run(
                [exe, "-y", "-f", "concat", "-safe", "0", "-i", "list.txt",
                 "-vsync", "vfr", "-c:v", "libx264", "-crf", _CRF, "-preset", "medium",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
                cwd=self.frames_dir, capture_output=True, timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=True)  # noqa: S603
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # concat（变时长）失败就退回定帧率模式：时间轴略失真，但一定出片
            subprocess.run(
                [exe, "-y", "-framerate", str(min(8, self.fps)),
                 "-start_number", "1", "-i", "f_%06d.jpg",
                 "-c:v", "libx264", "-crf", _CRF, "-preset", "medium",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
                cwd=self.frames_dir, capture_output=True, timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=True)  # noqa: S603
