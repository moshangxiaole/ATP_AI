"""设备投屏：抓帧、缩放编码、共享订阅、MJPEG 流与 HTTP 接口。"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.config import Config  # noqa: E402
from atp.webapp.cast import hub as cast  # noqa: E402
from atp.webapp.server import App, Handler  # noqa: E402
from PIL import Image  # noqa: E402


def png_bytes(w: int = 240, h: int = 480) -> bytes:
    img = Image.new("RGB", (w, h), (40, 90, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def cfg_for(tmp: Path) -> Config:
    cfg = Config(home=tmp)
    cfg.artifacts_dir = str(tmp / "art")
    cfg.cases_dir = str(tmp / "cases")
    cfg.db_path = str(tmp / "t.db")
    cfg.apps_path = str(tmp / "apps.json")
    cfg.persist_runs = False
    return cfg


class FrameCodecTest(unittest.TestCase):
    def test_encode_scales_and_returns_jpeg(self):
        jpeg, w, h = cast.encode_frame(png_bytes(1200, 2400), max_width=420)
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        self.assertEqual((w, h), (420, 840))
        self.assertLess(len(jpeg), 200_000)

    def test_encode_rejects_empty(self):
        with self.assertRaises(RuntimeError):
            cast.encode_frame(b"")

    def test_error_frame_is_jpeg(self):
        self.assertTrue(cast.error_frame("unauthorized").startswith(b"\xff\xd8"))

    def test_device_id_validation(self):
        self.assertEqual(cast.safe_device_id("DEMO_ANDROID_SERIAL"), "DEMO_ANDROID_SERIAL")
        self.assertEqual(cast.safe_device_id("127.0.0.1:5555"), "127.0.0.1:5555")
        self.assertEqual(cast.safe_device_id("x; rm -rf /"), "")


class GrabCommandTest(unittest.TestCase):
    def test_android_uses_exec_out_screencap(self):
        with mock.patch.object(cast, "_run", return_value=png_bytes()) as run:
            raw = cast.grab_android("adb", "dev-1")
        self.assertTrue(raw.startswith(b"\x89PNG"))
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["adb", "-s"])
        self.assertIn("exec-out", argv)
        self.assertIn("screencap", argv)

    def test_bad_response_reports_device_state(self):
        with mock.patch.object(cast, "_run", return_value=b"device unauthorized"):
            with self.assertRaises(RuntimeError) as ctx:
                cast.grab_android("adb", "dev-1")
        self.assertIn("device unauthorized", str(ctx.exception))


class HubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_cast_"))
        self.hub = cast.CastHub(cfg_for(self.tmp))
        # 单测不碰真机：把帧源链固定成零依赖的 adbcap（_grab 由各用例 mock），
        # 也关掉 H264 加速旁路，否则会去连真机 screenrecord。
        self.hub.methods = ["adbcap"]
        self.hub.presence = False
        self.hub.boost = False
        self.raw = png_bytes()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unsupported_platform_has_no_channel(self):
        self.assertIsNone(self.hub.subscribe("desktop", "x"))
        st = self.hub.status("desktop", "x")
        self.assertFalse(st["supported"])
        self.assertIn("暂不支持投屏", st["error"])

    def test_shared_channel_pushes_frames(self):
        with mock.patch.object(self.hub, "_grab", return_value=self.raw):
            ch = self.hub.subscribe("android", "dev-1", fps=8)
            self.assertIsNotNone(ch)
            frame, seq, err = self.hub.wait_frame(ch, 0, timeout=3)
            self.assertTrue(frame.startswith(b"\xff\xd8"), "没拿到 JPEG 帧")
            self.assertGreater(seq, 0)
            self.assertEqual(err, "")
            st = self.hub.status("android", "dev-1")
            self.assertTrue(st["active"])
            self.assertEqual(st["width"], 240)   # 源宽小于 420，不放大
            self.hub.unsubscribe(ch)
            self.assertEqual(self.hub.status("android", "dev-1")["viewers"], 0)

    def test_error_is_exposed_in_status(self):
        with mock.patch.object(self.hub, "_grab", side_effect=RuntimeError("设备离线")):
            ch = self.hub.subscribe("android", "dev-2", fps=8)
            _, seq, err = self.hub.wait_frame(ch, 0, timeout=3)
            self.assertGreater(seq, 0)
            self.assertIn("设备离线", err)
            self.assertIn("设备离线", self.hub.status("android", "dev-2")["error"])
            self.hub.unsubscribe(ch)

    def test_stop_removes_channel(self):
        with mock.patch.object(self.hub, "_grab", return_value=self.raw):
            ch = self.hub.subscribe("android", "dev-3", fps=8)
            self.hub.wait_frame(ch, 0, timeout=3)
            self.assertTrue(self.hub.stop("android", "dev-3")["stopped"])
            self.assertEqual(self.hub.status("android", "dev-3")["viewers"], 0)
            self.assertFalse(self.hub.stop("android", "dev-3")["stopped"])  # 重复停是安全的


class PresenceTest(unittest.TestCase):
    """拔 USB 后 status() 要给出 present=false，前端据此自动断开。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_cast_p_"))
        self.hub = cast.CastHub(cfg_for(self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_probe_presence_parses_adb_devices(self):
        out = b"List of devices attached\nDEMO_ANDROID_SERIAL\tdevice\nABC\toffline\n"
        with mock.patch.object(cast, "_run", return_value=out):
            self.assertTrue(self.hub._probe_presence("android", "DEMO_ANDROID_SERIAL"))
            self.assertFalse(self.hub._probe_presence("android", "ABC"))       # offline 不算在线
            self.assertFalse(self.hub._probe_presence("android", "NOPE"))

    def test_probe_presence_harmony(self):
        out = b"Connect Server OK\nDEMO_HARMONY_SERIAL\n"
        with mock.patch.object(cast, "_run", return_value=out):
            self.assertTrue(self.hub._probe_presence("harmony", "DEMO_HARMONY_SERIAL"))
            self.assertFalse(self.hub._probe_presence("harmony", "GONE"))

    def test_probe_error_means_absent(self):
        with mock.patch.object(cast, "_run", side_effect=RuntimeError("hdc 不存在")):
            self.assertFalse(self.hub._probe_presence("harmony", "DEMO_HARMONY_SERIAL"))

    def test_status_reports_disconnected_device(self):
        self.hub.methods = ["adbcap"]
        self.hub.boost = False
        with mock.patch.object(self.hub, "_grab", return_value=png_bytes()), \
                mock.patch.object(self.hub, "_present", return_value=False):
            self.hub.subscribe("android", "dev-gone", fps=8)
            st = self.hub.status("android", "dev-gone")
            self.assertFalse(st["present"])
            self.assertIn("设备已断开", st["error"])

    def test_status_present_true_when_device_alive(self):
        self.hub.methods = ["adbcap"]
        self.hub.boost = False
        with mock.patch.object(self.hub, "_grab", return_value=png_bytes()), \
                mock.patch.object(self.hub, "_present", return_value=True):
            self.hub.subscribe("android", "dev-ok", fps=8)
            st = self.hub.status("android", "dev-ok")
            self.assertTrue(st["present"])
            self.assertEqual(st["error"], "")

    def test_presence_cache_avoids_repeat_probe(self):
        self.hub.methods = ["adbcap"]
        self.hub.boost = False
        with mock.patch.object(self.hub, "_grab", return_value=png_bytes()), \
                mock.patch.object(self.hub, "_probe_presence", return_value=True) as pp:
            self.hub.subscribe("android", "dev-cache", fps=8)
            self.hub.status("android", "dev-cache")
            self.hub.status("android", "dev-cache")     # 3s 缓存：第二个周期不再探测
        self.assertEqual(pp.call_count, 1)


class CastHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="atp_cast_http_"))
        cls.cfg = cfg_for(cls.tmp)
        cls.app = App(cls.cfg)
        cls.app.cast = cast.CastHub(cls.cfg)
        cls.app.cast.methods = ["adbcap"]
        cls.app.cast.presence = False
        cls.app.cast.boost = False
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.httpd.daemon_threads = True
        cls.httpd.app = cls.app
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=8) as r:
            return r.status, r.headers, r.read()

    def test_state_exposes_cast_capability(self):
        _, _, body = self._get("/api/state")
        capability = json.loads(body)["cast"]
        self.assertIn("android", capability["supported"])
        self.assertGreaterEqual(capability["default_fps"], 1)

    def test_status_endpoint(self):
        _, _, body = self._get("/api/device/cast?platform=android&device=dev-http")
        st = json.loads(body)
        self.assertTrue(st["supported"])
        self.assertIn("/api/device/cast/stream", st["stream_url"])

    def test_frame_endpoint_returns_jpeg(self):
        raw = png_bytes()
        with mock.patch.object(self.app.cast, "_grab", return_value=raw):
            code, headers, body = self._get("/api/device/cast/frame?platform=android&device=dev-http")
        self.assertEqual(code, 200)
        self.assertEqual(headers.get_content_type(), "image/jpeg")
        self.assertTrue(body.startswith(b"\xff\xd8"))

    def test_stream_endpoint_is_multipart_jpeg(self):
        raw = png_bytes()
        with mock.patch.object(self.app.cast, "_grab", return_value=raw):
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/device/cast/stream"
                "?platform=android&device=dev-stream&fps=8",
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                self.assertIn("multipart/x-mixed-replace", r.headers.get("Content-Type"))
                head = r.read(1024)
                r.close()
        self.assertIn(b"--atpcast", head)
        self.assertIn(b"Content-Type: image/jpeg", head)


if __name__ == "__main__":
    unittest.main()
