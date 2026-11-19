"""视频模式投屏：NAL 切分、配置/关键帧编队、HTTP 线协议。"""

from __future__ import annotations

import http.client
import struct
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.config import Config  # noqa: E402
from atp.webapp.cast import video as video_cast  # noqa: E402
from atp.webapp.server import App, Handler  # noqa: E402
from atp.webapp.cast.video import VideoChannel, split_nals  # noqa: E402


def cfg_for(tmp: Path) -> Config:
    cfg = Config(home=tmp)
    cfg.artifacts_dir = str(tmp / "art")
    cfg.cases_dir = str(tmp / "cases")
    cfg.db_path = str(tmp / "t.db")
    cfg.apps_path = str(tmp / "apps.json")
    cfg.persist_runs = False
    return cfg


def _nal(nal_type: int, payload: bytes = b"\x01\x02") -> bytes:
    # NAL 头：F(1)=0, NRI(2)=3, Type(5)=nal_type
    return bytes([((nal_type & 0x1F) | (3 << 5)) & 0xFF]) + payload


def test_split_nals_3_and_4_byte_start_codes():
    sps = _nal(7, b"a")
    pps = _nal(8, b"b")
    idr = _nal(5, b"c")
    stream = b"\x00\x00\x00\x01" + sps + b"\x00\x00\x01" + pps + b"\x00\x00\x00\x01" + idr
    rest, nals = split_nals(stream)
    # 最后一个 NAL 可能不完整：保守留尾（下一包会带下个起始码再交出来）
    assert len(nals) >= 2
    assert nals[0] == sps and nals[1] == pps
    assert rest.startswith(b"\x00\x00\x00\x01")
    # 补上后续数据，最后一个 NAL 也能完整交出
    rest2, nals2 = split_nals(rest + b"\x00\x00\x01" + _nal(1, b"d"))
    assert nals2[0] == idr


def test_split_nals_keeps_incomplete_tail():
    sps = _nal(7, b"a")
    stream = b"\x00\x00\x00\x01" + sps
    rest, nals = split_nals(stream)
    assert nals == []                      # 没有下一个起始码 → 不确定是否完整
    assert rest.startswith(b"\x00\x00\x00\x01")


def test_split_nals_drops_garbage_and_holds_incomplete_tail():
    # 起始码之前的垃圾要丢掉；只有一个起始码时 NAL 可能未到齐 → 留尾
    head, nals = split_nals(b"\x13\x37" + b"\x00\x00\x01" + _nal(8, b"b"))
    assert nals == []
    assert head.startswith(b"\x00\x00\x01")
    # 补上后续数据（下一个起始码）→ 之前的 NAL 完整交出
    head2, nals2 = split_nals(head + b"\x00\x00\x01" + _nal(1, b"d"))
    assert nals2 == [_nal(8, b"b")]


def test_channel_delivers_config_then_skips_until_idr():
    """新观众必须先拿到 SPS/PPS；IDR 之前的普通帧给出去也解不了。"""
    tmp = Path(tempfile.mkdtemp(prefix="atp_video_ch_"))
    ch = VideoChannel("adb", "dev-video", (456, 1024))
    sps, pps = _nal(7, b"a"), _nal(8, b"b")
    try:
        sub = ch.subscribe()
        # NAL 到达顺序：SPS、PPS、普通帧、IDR、普通帧
        for nal in (sps, pps, _nal(1, b"x1"), _nal(5, b"idr"), _nal(1, b"x2")):
            ch._dispatch(nal)
        got = []
        while True:
            try:
                got.append(sub.q.get_nowait())
            except Exception:
                break
        kinds = [video_cast.nal_type(n) for _, _, n in got]
        assert kinds == [7, 8, 1, 5, 1], f"应该全部入队（过滤在写线程），实际 {kinds}"
        assert ch.config_nals() == [sps, pps]
        sub.alive = False
        ch.drain_viewers() if hasattr(ch, "drain_viewers") else None
    finally:
        ch.stop()


def test_channel_resync_on_overflow():
    """消费太慢 → 队列清空 + resync 标记（写线程会重发配置并跳到下一个 IDR）。"""
    ch = VideoChannel("adb", "dev-of", (456, 1024))
    sub = ch.subscribe()
    sub.q = __import__("queue").Queue(maxsize=2)
    try:
        for i in range(10):
            ch._dispatch(_nal(1, bytes([i])))
        assert sub.resync is True, "队列溢出必须标记 resync"
        assert sub.q.qsize() <= 2
    finally:
        ch.stop()


class _FakeStdout:
    """假 screenrecord 输出：按序吐出预置字节块。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read1(self, n=65536):
        if not self._chunks:
            time.sleep(0.05)
            return b""                       # 模拟画面静止（流静默）
        return self._chunks.pop(0)


class _FakeProc:
    def __init__(self, chunks):
        self.stdout = _FakeStdout(chunks)

    def kill(self):
        pass


def test_video_endpoint_framing():
    """/video 输出必须是「4 字节大端长度 + NAL」，且先配置后 IDR。"""
    tmp = Path(tempfile.mkdtemp(prefix="atp_video_http_"))
    app = App(cfg_for(tmp))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    httpd.app = app
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    sps, pps = _nal(7, b"a"), _nal(8, b"b")
    idr, delta = _nal(5, b"c"), _nal(1, b"d")
    delta2 = _nal(1, b"e")
    # 假 screenrecord 的输出是 Annex B 裸流（带起始码）；共享同一份序列，
    # 进程重启也不会重复播放，保证测试确定性。
    stream_chunks = [
        b"\x00\x00\x00\x01" + sps,
        b"\x00\x00\x01" + pps,
        b"\x00\x00\x00\x01" + idr,
        b"\x00\x00\x01" + delta,
        b"\x00\x00\x01" + delta2,
        b"",                       # 之后画面静止：流静默
    ]

    def fake_spawn(self):
        return _FakeProc(stream_chunks)

    try:
        with mock.patch.object(VideoChannel, "_spawn", fake_spawn):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            conn.request("GET", "/api/device/cast/video?platform=android&device=dev-v")
            r = conn.getresponse()
            assert r.status == 200
            assert r.getheader("Content-Type") == "video/h264"
            body = b""
            deadline = time.time() + 6
            while time.time() < deadline:
                try:
                    piece = r.read1(65536)
                except Exception:
                    break
                if not piece:
                    break
                body += piece
                # 收齐 4 条记录就够了，别等静默期结束
                off = 0
                count = 0
                while off + 4 <= len(body):
                    (ln,) = struct.unpack(">I", body[off:off + 4])
                    if off + 4 + ln > len(body):
                        break
                    count += 1
                    off += 4 + ln
                if count >= 4:
                    break
            conn.close()

        # 解析记录
        off = 0
        records = []
        while off + 4 <= len(body):
            (ln,) = struct.unpack(">I", body[off:off + 4])
            if off + 4 + ln > len(body):
                break
            records.append(body[off + 4:off + 4 + ln])
            off += 4 + ln
        kinds = [video_cast.nal_type(n) for n in records]
        # resync：先重发配置(SPS/PPS)，跳过 IDR 之前的普通帧，之后正常推流
        assert kinds[:4] == [7, 8, 5, 1], f"帧序应为 SPS,PPS,IDR,delta，实际 {kinds}"
    finally:
        app.video.stop("dev-v")
        httpd.shutdown()
        httpd.server_close()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_frame_endpoint_returns_jpeg_poster():
    """视频模式的海报帧：/frame 必须返回一张真 JPEG。

    真机事故：路由没实现 → 前端 fetch 404 → canvas 永远黑屏
    （screenrecord 静止时连 SPS 都不发，海报帧是连接初期的唯一画面）。
    """
    import io

    from PIL import Image

    tmp = Path(tempfile.mkdtemp(prefix="atp_video_frame_"))
    app = App(cfg_for(tmp))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    httpd.app = app
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        png = io.BytesIO()
        Image.new("RGB", (416, 924), (40, 80, 120)).save(png, format="PNG")
        with mock.patch.object(app.cast, "_grab", return_value=png.getvalue()):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/device/cast/frame?platform=android&device=dev-f")
            r = conn.getresponse()
            body = r.read()
            conn.close()
        assert r.status == 200
        assert r.getheader("Content-Type") == "image/jpeg"
        assert body[:2] == b"\xff\xd8" and len(body) > 1000, "海报帧不是有效 JPEG"
    finally:
        app.video.close()
        app.cast.close()
        httpd.shutdown()
        httpd.server_close()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
