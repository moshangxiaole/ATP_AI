"""执行录屏（PC 端 / Web 端 → MP4）：帧源选择、编码、报告嵌入、执行器集成。"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.runs.recorder import RunRecorder, _ffmpeg  # noqa: E402
from atp.common.stepir import CaseIR, Step  # noqa: E402
from atp.common.types import Action, UINode  # noqa: E402

needs_ffmpeg = unittest.skipIf(not _ffmpeg(), "本机没有 ffmpeg，跳过录屏编码测试")

W, H = 800, 1200
PKG = "com.example.chatapp.desktop"


def _png(color=(120, 40, 40), size=(W, H)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


# 类名伪装成真的 Playwright 驱动：_pick_source 按 __name__ 判定线程安全性
_FakeWebPlaywright = type("WebCdpDriver", (object,), {})


class _PlaywrightLikeWeb(_FakeWebPlaywright):
    """Playwright 系 web 驱动桩：录制器应判定它「不能开后台线程」。"""

    platform = "web"

    def __init__(self):
        self.device_id = "fake-web"
        self.shot = b""

    def screenshot(self) -> bytes:
        return self.shot


class _GrabDesktop:
    """Windows 桌面驱动桩：ImageGrab 式无状态截图，线程安全。"""

    platform = "desktop"
    device_id = "fake-desktop"

    def __init__(self):
        self.count = 0

    def screenshot(self) -> bytes:
        self.count += 1
        return _png((30, 90, 160))


@needs_ffmpeg
class TestRecorder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_rec_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_playwright_web_uses_ingest_fallback(self):
        """Playwright 系驱动不能开后台线程：只吃 ingest 的步骤帧也能出 MP4。"""
        drv = _PlaywrightLikeWeb()
        rec = RunRecorder(drv, self.tmp)
        self.assertFalse(rec.start(), "Playwright 系驱动不该开后台线程")
        for c in ((200, 0, 0), (0, 200, 0), (0, 0, 200)):
            rec.ingest(_png(c))
            time.sleep(0.12)
        out = rec.stop()
        self.assertTrue(out, "应当生成 recording.mp4")
        self.assertGreater(Path(out).stat().st_size, 1000)
        self.assertFalse((self.tmp / "frames").exists(), "中间帧目录该清掉")

    def test_desktop_grab_thread_records(self):
        """桌面驱动：后台线程轮询截图，1 秒内应抓到多帧。"""
        drv = _GrabDesktop()
        rec = RunRecorder(drv, self.tmp, fps=6)
        self.assertTrue(rec.start())
        time.sleep(1.0)
        out = rec.stop()
        self.assertTrue(out)
        self.assertGreaterEqual(drv.count, 2, "后台线程应真正在轮询截图")

    def test_stop_is_idempotent(self):
        rec = RunRecorder(_PlaywrightLikeWeb(), self.tmp)
        rec.start()
        rec.ingest(_png())
        first = rec.stop()
        self.assertEqual(rec.stop(), first)

    def test_no_frames_no_video(self):
        rec = RunRecorder(_PlaywrightLikeWeb(), self.tmp)
        rec.start()
        rec.ingest(b"")                       # 空帧该被忽略
        self.assertEqual(rec.stop(), "")
        self.assertFalse((self.tmp / "recording.mp4").exists())

    def test_mixed_frame_sizes_one_canvas(self):
        """真机事故：证据截图（全分辨率）与录屏线程帧混装，concat 按第一帧
        强行拉伸 —— 用户看到「画面形变」。所有帧必须落到统一画布上。"""
        import json
        import os
        import subprocess

        rec = RunRecorder(_PlaywrightLikeWeb(), self.tmp)
        rec.start()
        rec.ingest(_png((200, 0, 0), size=(1260, 2800)))   # 手机比例的证据截图
        rec.ingest(_png((0, 200, 0), size=(640, 360)))     # 视口帧
        rec.ingest(_png((0, 0, 200), size=(800, 1200)))
        out = rec.stop()
        self.assertTrue(out)

        exe = _ffmpeg()
        probe = shutil.which("ffprobe") or os.path.join(os.path.dirname(exe),
                                                        "ffprobe.exe")
        r = subprocess.run(
            [probe, "-v", "error", "-show_entries", "stream=width,height",
             "-of", "json", out], capture_output=True, timeout=30)  # noqa: S603
        streams = json.loads(r.stdout.decode("utf-8", "ignore")).get("streams") or []
        sizes = {(s.get("width"), s.get("height")) for s in streams}
        self.assertEqual(len(sizes), 1, f"视频流尺寸必须唯一，实际 {sizes}")
        w, h = sizes.pop()
        # 第一帧 1260x2800 定画布：长边对齐到录制常量、宽高偶数
        from atp.runs.recorder import _LONG_SIDE
        self.assertEqual(max(w, h), _LONG_SIDE)
        self.assertEqual(w % 2, 0)
        self.assertEqual(h % 2, 0)
        self.assertAlmostEqual(w / h, 1260 / 2800, delta=0.01)


def _cfg_for(tmp: Path):
    from atp.config import Config

    cfg = Config(home=ROOT)
    cfg.artifacts_dir = str(tmp / "art")
    cfg.db_path = str(tmp / "t.db")
    cfg.apps_path = str(tmp / "apps.json")
    cfg.persist_runs = False
    cfg.screenshot_on_success = False
    cfg.cold_start.settle_ms = 0
    cfg.cold_start.launch_settle_ms = 0
    return cfg


@needs_ffmpeg
class TestExecutorIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_rec_exec_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _web_driver(self):
        from atp.agents.platforms.base import BaseDriver

        class FakeWeb(BaseDriver):
            """和 test_apps.FakePhone 同款最小桩，platform 换成 web。"""

            platform = "web"

            def __init__(self):
                super().__init__("fake-web")
                self.calls: list[tuple[str, str]] = []

            def connect(self) -> None:
                self._connected = True

            def screen_size(self) -> tuple[int, int]:
                return W, H

            def screenshot(self) -> bytes:
                return _png((30, 90, 160))

            def dump_tree(self) -> UINode:
                return UINode(cls="Root", bounds=(0, 0, W, H),
                              children=[UINode(cls="TextView", text="首页",
                                               bounds=(0, 0, W, 200))])

            def launch_app(self, app_id: str) -> None:
                self.calls.append(("launch", app_id))

            def terminate_app(self, app_id: str) -> None:
                self.calls.append(("terminate", app_id))

        return FakeWeb()

    def test_web_run_leaves_recording_and_report_video(self):
        """执行器集成：web 用例跑完 → artifacts/recording.mp4 + 报告内嵌播放器。"""
        from atp.agents.executor import Executor
        from atp.agents.perception.judge import Judge

        case = CaseIR(case_id="rec-case", title="录屏用例", platform="web", app_id=PKG)
        case.steps = [Step(step_id="s01", intent="打开【示例聊天应用】应用",
                           action=Action.LAUNCH_APP, args={"app": PKG})]

        drv = self._web_driver()
        Executor(drv, _cfg_for(self.tmp), Judge(), None).run(case, mode="replay")

        arts = sorted((self.tmp / "art").glob("rec-case-*"))
        self.assertTrue(arts, "执行产物目录应存在")
        mp4 = arts[-1] / "recording.mp4"
        self.assertTrue(mp4.exists(), "执行完成后应有 recording.mp4")
        self.assertGreater(mp4.stat().st_size, 1000)
        html = (arts[-1] / "report.html").read_text(encoding="utf-8")
        self.assertIn("artifacts/recording.mp4", html, "报告应内嵌视频播放器")


if __name__ == "__main__":
    unittest.main()
