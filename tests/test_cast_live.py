"""低延迟投屏与投屏回注：H264 帧切分、自动降级、坐标换算、触控 API。"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402
from atp.config import Config  # noqa: E402
from atp.webapp.cast import hub as cast  # noqa: E402
from atp.webapp.cast import sources as cast_sources  # noqa: E402
from atp.webapp.cast.sources import FrameSource  # noqa: E402
from atp.webapp.server import App, Handler  # noqa: E402

# 真实 JPEG（带 SOI/EOI 标记，且可被 PIL 解码）：帧源尺寸不一致时 _publish
# 会按基准尺寸重采样，fake 字节会让 normalize_frame 抛错、整帧被丢弃。
def _real_jpeg(w: int, h: int, color=(40, 90, 200)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG")
    return buf.getvalue()

JPEG_A = _real_jpeg(420, 932)
JPEG_B = _real_jpeg(416, 924)
SIZE = (1260, 2800, 0)


def cfg_for(tmp: Path) -> Config:
    cfg = Config(home=tmp)
    cfg.artifacts_dir = str(tmp / "art")
    cfg.cases_dir = str(tmp / "cases")
    cfg.db_path = str(tmp / "t.db")
    cfg.apps_path = str(tmp / "apps.json")
    cfg.persist_runs = False
    return cfg


class FakeReader:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.i = 0

    def get(self, timeout=0.3):
        if self.i >= len(self.chunks):
            return None
        c = self.chunks[self.i]
        self.i += 1
        return c


class StreamProtocolTest(unittest.TestCase):
    def test_projected_size_is_even_and_keeps_ratio(self):
        w, h = cast_sources.projected_size(1260, 2800, 420)
        self.assertEqual((w % 2, h % 2), (0, 0))
        self.assertLessEqual(w, 420)
        self.assertAlmostEqual(w / h, 1260 / 2800, delta=0.01)
        self.assertEqual(cast_sources.projected_size(0, 0), (420, 932))

    def test_iter_mjpeg_splits_across_chunks(self):
        reader = FakeReader([
            b"garbage", JPEG_A[:3], JPEG_A[3:] + JPEG_B[:4], JPEG_B[4:], b"",
        ])
        frames = list(cast_sources.iter_mjpeg(reader, threading.Event(), first_timeout=1))
        self.assertEqual(frames, [JPEG_A, JPEG_B])

    def test_iter_mjpeg_first_frame_timeout(self):
        stop = threading.Event()
        with self.assertRaises(RuntimeError) as ctx:
            list(cast_sources.iter_mjpeg(FakeReader([]), stop, first_timeout=0.05))
        self.assertIn("首帧", str(ctx.exception))

    def test_patient_stream_waits_for_screen_change(self):
        """加速通道静止时不判超时：喂一串空读之后才来一帧，仍要出帧。"""
        stop = threading.Event()
        reader = FakeReader([None] * 5 + [JPEG_A, b""])
        frames = list(cast_sources.iter_mjpeg(reader, stop, first_timeout=None))
        self.assertEqual(frames, [JPEG_A])

    def test_h264_first_frame_failure_does_not_retry_forever(self):
        tool = cast_sources.Toolchain(adb="adb", ffmpeg="ffmpeg",
                                      run=lambda argv, t: b"Physical size: 1260x2800",
                                      popen=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no device")))
        src = cast_sources.H264Source(tool, "dev-x")
        t0 = time.time()
        with self.assertRaises(RuntimeError) as ctx:
            list(src.frames(threading.Event()))
        self.assertIn("首帧失败", str(ctx.exception))
        self.assertLess(time.time() - t0, 2, "首帧失败应立即降级，不能反复重连")
        src.close()


class ControlCommandTest(unittest.TestCase):
    def test_tap_maps_normalized_point_to_device_pixels(self):
        argv = cast.control_argv("android", "dev-1", {"action": "tap", "x": 0.5, "y": 0.5}, SIZE)
        self.assertEqual(argv[-4:], ["input", "tap", "630", "1400"])

    def test_swipe_includes_duration_and_endpoints(self):
        argv = cast.control_argv("android", "dev-1", {
            "action": "swipe", "x": 0.2, "y": 0.9, "x2": 0.2, "y2": 0.1, "duration": 600,
        }, SIZE)
        self.assertEqual(argv[-7:], ["input", "swipe", "252", "2520", "252", "280", "600"])

    def test_long_press_is_a_same_point_swipe(self):
        argv = cast.control_argv("android", "dev-1", {"action": "long_press", "x": 0.5, "y": 0.5}, SIZE)
        self.assertEqual(argv[4:], ["input", "swipe", "630", "1400", "630", "1400", "800"])

    def test_landscape_swaps_axes_before_mapping(self):
        argv = cast.control_argv("android", "dev-1", {"action": "tap", "x": 0.5, "y": 0.5},
                                 (2800, 1260, 90))
        self.assertEqual(argv[-2:], ["1400", "630"])

    def test_key_whitelist(self):
        self.assertEqual(cast.control_argv("android", "dev-1", {"action": "key", "key": "back"}, SIZE)[-2:],
                         ["keyevent", "4"])
        with self.assertRaises(ValueError):
            cast.control_argv("android", "dev-1", {"action": "key", "key": "rm -rf"}, SIZE)

    def test_text_escapes_spaces_and_rejects_chinese(self):
        argv = cast.control_argv("android", "dev-1", {"action": "text", "text": "hello world"}, SIZE)
        self.assertEqual(argv[-1], "hello%sworld")
        with self.assertRaises(ValueError):
            cast.control_argv("android", "dev-1", {"action": "text", "text": "测试"}, SIZE)

    def test_invalid_coordinates_are_rejected(self):
        for bad in ({"x": 1.4, "y": 0.5}, {"x": -0.1, "y": 0.5}, {"x": "abc", "y": 0.5}):
            with self.assertRaises(ValueError):
                cast.control_argv("android", "dev-1", {"action": "tap", **bad}, SIZE)
        with self.assertRaises(ValueError):
            cast.control_argv("android", "dev-1", {"action": "launch_app"}, SIZE)

    def test_harmony_uses_uitest(self):
        argv = cast.control_argv("harmony", "dev-h", {"action": "tap", "x": 0.5, "y": 0.5}, SIZE,
                                 hdc="hdc")
        self.assertEqual(argv[:4], ["hdc", "-t", "dev-h", "shell"])
        self.assertEqual(argv[4:7], ["uitest", "uiInput", "click"])
        self.assertEqual(cast.control_argv("harmony", "dev-h", {"action": "key", "key": "home"}, SIZE)[-1],
                         "Home")

    def test_display_size_reads_rotation(self):
        def runner(argv, timeout):
            if "wm" in argv:
                return b"Physical size: 1260x2800"
            return b"  mRotation=1 mDeferredRotationPauseCount=0"
        self.assertEqual(cast.display_size("android", "dev", runner=runner), (2800, 1260, 90))


class FrameStabilityTest(unittest.TestCase):
    """抖动回归：混帧时尺寸必须恒定，加速窗口内主链不得插帧。"""

    @staticmethod
    def jpeg(w, h, color=(40, 90, 200)):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (w, h), color).save(buf, format="JPEG")
        return buf.getvalue()

    @staticmethod
    def size(data):
        import io
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return im.size

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_cast_stable_"))
        self.hub = cast.CastHub(cfg_for(self.tmp))
        self.hub.methods = ["adbcap"]
        self.hub.presence = False
        self.hub.boost = False
        self.hub.presence = False
        self.ch = cast._Channel(platform="android", device_id="dev-stable")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_frames_from_different_sources_keep_one_size(self):
        self.hub._publish(self.ch, self.jpeg(416, 924), 416, 924, "javacap")
        self.assertEqual(self.ch.frame_size, (416, 924))
        self.assertEqual(self.size(self.ch.frame), (416, 924))

        # H264 旁路天然是 420x932，必须重采样到主链基准尺寸再推给浏览器
        self.hub._publish(self.ch, self.jpeg(420, 932), 420, 932, "h264+")
        self.assertEqual(self.size(self.ch.frame), (416, 924))
        self.assertEqual(self.ch.frame_size, (416, 924))
        self.assertEqual((self.ch.width, self.ch.height), (416, 924))

    def test_boost_window_suppresses_stale_main_frames(self):
        self.hub._publish(self.ch, self.jpeg(416, 924), 416, 924, "javacap")
        self.hub._publish(self.ch, self.jpeg(416, 924), 416, 924, "h264+")
        seq = self.ch.seq
        # 加速旁路正在推帧，主链的旧帧不该插进来造成时间线来回跳
        self.hub._publish(self.ch, self.jpeg(416, 924, (200, 40, 40)), 416, 924, "javacap")
        self.assertEqual(self.ch.seq, seq)
        # 窗口过后主链恢复
        self.ch.boost_until = 0.0
        self.hub._publish(self.ch, self.jpeg(416, 924, (200, 40, 40)), 416, 924, "javacap")
        self.assertEqual(self.ch.seq, seq + 1)


class BoomSource(FrameSource):
    name = "boom"

    def frames(self, stop):
        raise RuntimeError("首帧失败")
        yield  # pragma: no cover


class GoodSource(FrameSource):
    name = "good"

    def frames(self, stop):
        while not stop.is_set():
            yield JPEG_A, 420, 932
            time.sleep(0.02)


class HubFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_cast_live_"))
        self.hub = cast.CastHub(cfg_for(self.tmp))
        self.hub._sources = lambda ch: [BoomSource(), GoodSource()]
        self.hub.boost = False
        self.hub.presence = False

    def tearDown(self):
        self.hub.stop("android", "dev-fallback")
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_falls_back_to_next_source_and_publishes_frame(self):
        ch = self.hub.subscribe("android", "dev-fallback", fps=20)
        frame, seq = b"", 0
        for _ in range(30):
            # 失败源会先推一次"错误 seq"，继续等到真正出帧的那一拍
            frame, seq, _ = self.hub.wait_frame(ch, seq, timeout=0.3)
            if frame:
                break
        self.assertEqual(frame, JPEG_A)
        self.assertGreater(seq, 0)
        st = self.hub.status("android", "dev-fallback")
        self.assertEqual(st["method"], "good")
        self.assertEqual(st["error"], "")
        self.assertEqual((st["width"], st["height"]), (420, 932))
        self.hub.unsubscribe(ch)

    def test_idle_expiry_stops_the_booster_too(self):
        """没人看之后 producer 退出时必须停掉 H264 旁路，否则 screenrecord 常驻耗电。"""
        import io as _io
        import shutil
        from PIL import Image

        png = _io.BytesIO()
        Image.new("RGB", (60, 120), (10, 20, 30)).save(png, format="PNG")
        tmp = Path(tempfile.mkdtemp(prefix="atp_cast_idle_"))
        hub = cast.CastHub(cfg_for(tmp))
        hub.methods = ["adbcap"]
        hub.presence = False
        hub.boost = False
        try:
            with mock.patch.object(cast, "IDLE_TTL", 0.1), \
                 mock.patch.object(hub, "_grab", return_value=png.getvalue()):
                ch = hub.subscribe("android", "dev-idle", fps=20)
                hub.unsubscribe(ch)
                time.sleep(0.5)
            self.assertTrue(ch.stop.is_set(), "空闲退出没停掉加速旁路")
        finally:
            hub.stop("android", "dev-idle")
            shutil.rmtree(tmp, ignore_errors=True)

    def test_control_caches_display_size(self):
        with mock.patch.object(cast, "display_size", return_value=SIZE) as size, \
             mock.patch.object(cast, "_run", return_value=b"") as run:
            r1 = self.hub.control("android", "dev-1", {"action": "tap", "x": 0.5, "y": 0.5})
            r2 = self.hub.control("android", "dev-1", {"action": "key", "key": "back"})
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertEqual(size.call_count, 1, "分辨率应缓存，别每次点击都跑两次 adb")
        self.assertIn("input tap 630 1400", r1["command"])
        self.assertIn("keyevent 4", r2["command"])
        self.assertEqual(run.call_count, 2)


class FlakyThenGoodSource(FrameSource):
    """模拟 javacap 在页面切换时瞬时抖动几次后恢复（真机复现过的卡顿根因）。"""
    name = "flaky"

    def __init__(self, fails: int = 1):
        self.fails = fails
        self.attempts = 0

    def frames(self, stop):
        self.attempts += 1
        if self.attempts <= self.fails:
            raise RuntimeError("transient surface lost")
        while not stop.is_set():
            yield JPEG_A, 420, 932
            time.sleep(0.02)


class FillerSource(FrameSource):
    """adbcap 间隙 filler：限时让位，不能永久霸占。"""
    name = "adbcap"

    def __init__(self, burst: float = 2.0):
        self.burst = burst

    def frames(self, stop):
        t0 = time.time()
        while not stop.is_set() and time.time() - t0 < self.burst:
            yield JPEG_B, 416, 924
            time.sleep(0.02)


class TransientFailureRecoversTest(unittest.TestCase):
    """回归：主源抖一下绝不能永久掉到慢链；临时故障时由 filler 顶着，主源一恢复立刻接管。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_cast_recover_"))
        self.hub = cast.CastHub(cfg_for(self.tmp))
        self.hub.boost = False
        self.hub.presence = False

    def tearDown(self):
        self.hub.stop("android", "dev-recover")
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_transient_failure_recovers_to_primary_source(self):
        # 主源先抖 1 次（模拟一次页面切换掉帧），随后恢复
        self.hub._sources = lambda ch: [FlakyThenGoodSource(fails=1), FillerSource(burst=2.0)]
        ch = self.hub.subscribe("android", "dev-recover", fps=20)
        try:
            frame, seq = b"", 0
            # 等到 filler 顶上（方法变成 adbcap 且出帧），确认降级兜底生效
            deadline = time.time() + 6
            saw_filler = False
            recovered = False
            while time.time() < deadline:
                frame, seq, _ = self.hub.wait_frame(ch, seq, timeout=0.3)
                m = self.hub.status("android", "dev-recover")["method"]
                if m == "adbcap" and frame.startswith(b"\xff\xd8"):
                    saw_filler = True
                # 主源恢复后方法重新回到 flaky，且推的是有效 JPEG 帧
                if m == "flaky" and frame.startswith(b"\xff\xd8") and frame != JPEG_B:
                    recovered = True
                    break
            self.assertTrue(saw_filler, "主源故障时 filler 没顶上来")
            self.assertTrue(recovered, "主源恢复后仍卡在慢链——永久降级 bug 复发")
        finally:
            self.hub.unsubscribe(ch)


class CastInputHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="atp_cast_input_"))
        cls.app = App(cfg_for(cls.tmp))
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
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _post(self, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/device/cast/input",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read())

    def test_tap_endpoint_injects_device_command(self):
        with mock.patch.object(cast, "display_size", return_value=SIZE), \
             mock.patch.object(cast, "_run", return_value=b"") as run:
            code, body = self._post({"platform": "android", "device": "dev-api",
                                     "action": "tap", "x": 0.5, "y": 0.5})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertIn("input tap 630 1400", body["command"])
        self.assertEqual(run.call_args[0][0][-2:], ["630", "1400"])

    def test_bad_action_returns_400(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/device/cast/input",
            data=json.dumps({"platform": "android", "device": "dev-api", "action": "nope"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=8)
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("不支持的操作", json.loads(ctx.exception.read())["error"])

    def test_status_exposes_method_and_control(self):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/device/cast?platform=android&device=dev-api", timeout=8
        ) as r:
            st = json.loads(r.read())
        self.assertIn("method", st)
        self.assertIn("frame_ms", st)
        self.assertEqual(st["control"], "adb-input")


class PublisherThroughputTest(unittest.TestCase):
    """帧要「出来就推」：不能被请求的 fps 节流砍掉，重复帧也不能重复推。

    真机事故：H264 源实测 23fps，却被 `1.0/ch.fps`（默认 12 → 83ms）的闸门
    砍成 9fps，用户看到的就是「操作缓慢无比，一点都不丝滑」。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_cast_rate_"))
        self.hub = cast.CastHub(cfg_for(self.tmp))
        self.hub.boost = False
        self.hub.presence = False

    def tearDown(self):
        self.hub.stop("android", "dev-rate")
        self.hub.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fast_source_is_not_throttled_by_requested_fps(self):
        """请求 fps=12，但源能出 100fps 时，交付帧率必须远高于 12。"""

        class RapidSource(FrameSource):
            name = "rapid"

            def frames(self, stop):
                for i in range(120):
                    if stop.is_set():
                        return
                    # 每帧颜色都不同，保证字节不同、不会被去重掉
                    yield _real_jpeg(416, 924, (i % 250, 60, 90)), 416, 924
                    time.sleep(0.008)

        src = RapidSource()
        with mock.patch.object(self.hub, "_sources", return_value=[src]):
            ch = self.hub.subscribe("android", "dev-rate", fps=12)
            time.sleep(0.9)
            measured = ch.measured_fps
            seq = ch.seq
        self.assertGreater(seq, 15, "发布帧数太少，疑似被 fps 闸门节流")
        self.assertGreater(measured, 20.0, f"帧率被节流到 {measured}fps，应接近源速率")

    def test_duplicate_frame_is_not_republished(self):
        ch = cast._Channel(platform="android", device_id="dev-dup")
        self.hub._publish(ch, JPEG_A, 420, 932, "javacap")
        self.assertEqual(ch.seq, 1)
        self.hub._publish(ch, JPEG_A, 420, 932, "javacap")
        self.assertEqual(ch.seq, 1, "同一张图被重复推送，白占带宽")

    def test_dedupe_is_per_source_so_two_chains_dont_flip(self):
        """两条链路画质不同：只跟全局上一帧比会让画面在两种画质间无限闪。"""
        with mock.patch.object(cast, "BOOST_HOLD", 0):
            ch = cast._Channel(platform="android", device_id="dev-mix")
            self.hub._publish(ch, JPEG_A, 420, 932, "javacap")
            self.hub._publish(ch, JPEG_B, 416, 924, "h264+")   # 归一化到 420x932
            self.assertEqual(ch.seq, 2)
            # javacap 再吐一次和它自己上一帧相同的图 → 应被去重，而不是把 H264 顶掉
            self.hub._publish(ch, JPEG_A, 420, 932, "javacap")
            self.assertEqual(ch.seq, 2, "去重必须按来源比，否则两条链会互相顶掉")


class ShellInjectTest(unittest.TestCase):
    """触控走常驻 adb shell（省 ~195ms/条的进程启动），挂了要能退回一次性调用。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_shell_"))
        self.hub = cast.CastHub(cfg_for(self.tmp))

    def tearDown(self):
        self.hub.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_control_goes_through_persistent_shell(self):
        payload = {"action": "tap", "x": 0.5, "y": 0.5}
        with mock.patch.object(cast, "display_size", return_value=SIZE), \
             mock.patch.object(cast.AdbShell, "run", return_value=0) as run:
            r = self.hub.control("android", "dev-shell", payload)
        self.assertTrue(r["ok"])
        self.assertEqual(run.call_count, 1)
        self.assertIn("input tap", run.call_args[0][0])

    def test_text_argument_is_quoted_for_the_shell(self):
        """input text 的内容里可能带 ; & $，裸拼会被 shell 当成命令分隔。"""
        with mock.patch.object(cast, "display_size", return_value=SIZE), \
             mock.patch.object(cast.AdbShell, "run", return_value=0) as run:
            self.hub.control("android", "dev-shell", {"action": "text", "text": "a;b"})
        self.assertEqual(run.call_args[0][0], "input text 'a;b'")

    def test_falls_back_to_one_shot_call_when_shell_dies(self):
        fake = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        with mock.patch.object(cast, "display_size", return_value=SIZE), \
             mock.patch.object(cast.AdbShell, "run",
                               side_effect=RuntimeError("设备 shell 无响应")), \
             mock.patch.object(cast.subprocess, "run", return_value=fake) as one_shot:
            r = self.hub.control("android", "dev-shell",
                                 {"action": "tap", "x": 0.5, "y": 0.5})
        self.assertTrue(r["ok"], "常驻 shell 挂掉后必须能退回一次性 adb 调用")
        self.assertEqual(one_shot.call_count, 1)

    def test_nonzero_exit_is_reported_not_silently_retried(self):
        """指令在设备上执行失败要报错，不能偷偷再跑一遍（会重复点一下）。"""
        with mock.patch.object(cast, "display_size", return_value=SIZE), \
             mock.patch.object(cast.AdbShell, "run", return_value=1), \
             mock.patch.object(cast.subprocess, "run") as one_shot:
            with self.assertRaises(RuntimeError):
                self.hub.control("android", "dev-shell", {"action": "key", "key": "back"})
        one_shot.assert_not_called()


class StaticScreenSourceTest(unittest.TestCase):
    """静止画面下 screenrecord 一帧都不编码，但它还活着——不能据此判死重连。

    真机事故：静止 20 秒就把好好的 screenrecord 杀掉，重连又要 2~3 秒；
    用户刚想操作正好撞进这段空窗，感觉就是「一上手先卡两秒」。
    """

    def test_alive_process_extends_first_frame_deadline(self):
        class NullReader:
            def get(self, timeout=0.3):
                time.sleep(0.01)
                return None

        stop = threading.Event()
        calls = {"n": 0}

        def alive():
            calls["n"] += 1
            return calls["n"] <= 3          # 前三次进程还在，之后才算真死

        with self.assertRaises(RuntimeError):
            for _ in cast_sources.iter_mjpeg(NullReader(), stop,
                                             first_timeout=0.03, alive=alive):
                pass
        self.assertGreaterEqual(calls["n"], 3, "进程还活着就该续期等待，而不是直接判死")

    def test_dead_process_still_times_out(self):
        class NullReader:
            def get(self, timeout=0.3):
                time.sleep(0.005)
                return None

        stop = threading.Event()
        t0 = time.time()
        with self.assertRaises(RuntimeError):
            for _ in cast_sources.iter_mjpeg(NullReader(), stop,
                                             first_timeout=0.03,
                                             alive=lambda: False):
                pass
        self.assertLess(time.time() - t0, 2.0)


class StaticScreenNoFlapTest(unittest.TestCase):
    """画面停顿超过 1.5 秒后，绝不能补一张占位图。

    真机事故：`placeholder_sent` 在每张真实帧之后被置回 False，于是「连接初期
    发一张『连接中』占位」变成了「每段 1 秒以上的停顿都补一张」——用户连点几次，
    面板就在真机画面和 NO SIGNAL 黑框之间不停闪。推流端按内容去重后，停顿期间
    本来就不该有新帧，正好把这个隐藏 bug 触发成持续闪烁。
    """

    GAP = 2.5          # 帧与帧之间的停顿，明显大于 1.5s 的占位阈值

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="atp_cast_flap_"))
        cls.app = App(cfg_for(cls.tmp))
        cls.app.cast.boost = False
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.httpd.daemon_threads = True
        cls.httpd.app = cls.app
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.cast.close()
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_gaps_between_real_frames_do_not_trigger_placeholder(self):
        """每 2.5 秒一帧新画面：6 秒内只该看到 3 帧，不该夹带占位图。"""
        gap = self.GAP

        class SlowSource(FrameSource):
            name = "slow"

            def frames(self, stop):
                i = 0
                while not stop.is_set():
                    i += 1
                    # 每次颜色都不同 → 不会被按内容去重掉
                    yield _real_jpeg(416, 924, (i * 29 % 250, 40, 60)), 416, 924
                    time.sleep(gap)

        path = "/api/device/cast/stream?platform=android&device=dev-flap&fps=30"
        with mock.patch.object(self.app.cast, "_sources",
                               return_value=[SlowSource()]):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            try:
                s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                          f"Connection: close\r\n\r\n".encode())
                s.settimeout(0.5)
                data = b""
                deadline = time.time() + 6.0
                while time.time() < deadline:
                    try:
                        chunk = s.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    data += chunk
            finally:
                s.close()
        parts = data.count(b"--atpcast")
        # 6 秒里真实帧只有 3 张（0s / 2.5s / 5s）；旧逻辑会在每段停顿再补一张，
        # 变成 5~6 个 part。
        self.assertGreaterEqual(parts, 2, "真实帧没推出来，测试桩没生效")
        self.assertLessEqual(
            parts, 4,
            f"6 秒内推了 {parts} 个 part —— 停顿被补了占位图，面板会不停闪")


class LatestFramePullTest(unittest.TestCase):
    """拉取模式：每次只给「当前最新」一帧，客户端慢也不会让时间线倒退。

    这是 MJPEG 长连接的替代方案。长连接是服务端往浏览器灌帧，浏览器解码跟不上
    就会在内部排队、回放几秒前的旧画面（真机：用户已回到消息首页，面板还在重播
    聊天详情→通讯录→我的）。拉取模式把节奏交给客户端，结构上不可能积压。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="atp_cast_pull_"))
        cls.app = App(cfg_for(cls.tmp))
        cls.app.cast.boost = False
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.httpd.daemon_threads = True
        cls.httpd.app = cls.app
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.cast.close()
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_latest_returns_newest_frame_and_releases_viewer(self):
        class RapidSource(FrameSource):
            name = "rapid"

            def frames(self, stop):
                i = 0
                while not stop.is_set():
                    i += 1
                    yield _real_jpeg(416, 924, (i * 13 % 250, 70, 50)), 416, 924
                    time.sleep(0.02)

        url = (f"http://127.0.0.1:{self.port}/api/device/cast/latest"
               f"?platform=android&device=dev-pull")
        seqs = []
        with mock.patch.object(self.app.cast, "_sources", return_value=[RapidSource()]):
            for _ in range(3):
                with urllib.request.urlopen(url, timeout=10) as r:
                    self.assertEqual(r.status, 200)
                    self.assertEqual(r.headers["Content-Type"], "image/jpeg")
                    self.assertTrue(r.read().startswith(b"\xff\xd8"))
                    seqs.append(int(r.headers["X-Frame-Seq"]))
                time.sleep(0.12)
        self.assertGreater(seqs[0], 0, "第一个请求就该拿到已经存在的帧")
        self.assertEqual(seqs, sorted(seqs), "序号必须单调递增，绝不能回退到旧帧")
        self.assertGreater(seqs[-1], seqs[0], "画面在变，序号应该往前走")
        # 每个请求都要把订阅还回去，否则抓帧线程永远不退、白耗设备电
        self.assertEqual(self.app.cast.status("android", "dev-pull")["viewers"], 0)

    def test_no_new_frame_returns_204_after_holding(self):
        """画面没变时挂住 1.5s 再回 204，而不是反复传同一张图。"""

        class OneShotSource(FrameSource):
            name = "once"

            def frames(self, stop):
                yield _real_jpeg(416, 924, (11, 22, 33)), 416, 924
                while not stop.is_set():
                    time.sleep(0.05)

        base = (f"http://127.0.0.1:{self.port}/api/device/cast/latest"
                f"?platform=android&device=dev-hold")
        with mock.patch.object(self.app.cast, "_sources",
                               return_value=[OneShotSource()]):
            with urllib.request.urlopen(base, timeout=10) as r:
                self.assertEqual(r.status, 200)
                seq = int(r.headers["X-Frame-Seq"])
                r.read()
            t0 = time.time()
            with urllib.request.urlopen(f"{base}&after={seq}", timeout=10) as r:
                self.assertEqual(r.status, 204, "没有新帧就该回 204，别再传一遍图")
            held = time.time() - t0
        self.assertGreaterEqual(held, 1.2, f"只挂了 {held:.2f}s，等于让客户端空转轮询")

    def test_latest_rejects_bad_device(self):
        url = (f"http://127.0.0.1:{self.port}/api/device/cast/latest"
               f"?platform=desktop&device=dev-bad")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(url, timeout=8)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
