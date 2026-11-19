"""前后帧比较回归（步骤完成校验的底层）。

判断"这一步到底有没有让界面发生变化"不能靠整图 MD5：光标闪烁、水印动画、
时钟都会让每一帧都不同；也不能只看界面树——桌面上点一下搜索框只改焦点，
树纹丝不动。这里钉住缩略图灰度差的阈值行为。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.agents.perception.frames import DEFAULT_MIN_RATIO, frame_delta_ratio, screen_changed  # noqa: E402


class _Img:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.i = 0

    def make(self, painter) -> Path:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (1920, 1200), "#F5F7FA")
        ImageDraw.Draw(img).rectangle([0, 0, 1920, 80], fill="#2E5BFF")
        painter(ImageDraw.Draw(img))
        self.i += 1
        p = self.tmp / f"f{self.i}.png"
        img.save(p)
        return p


class FrameDeltaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile

        cls.tmp = Path(tempfile.mkdtemp(prefix="atp_frames_"))
        cls.images = _Img(cls.tmp)

    def test_identical_frames_are_zero(self):
        p = self.images.make(lambda d: None)
        self.assertEqual(frame_delta_ratio(p, p), 0.0)
        self.assertFalse(screen_changed(p, p))

    def test_blinking_caret_is_not_a_change(self):
        """输入框光标闪一下只有几个像素，绝不能被当成"界面变了"，
        否则每一步都会被判成"有推进"，防打转就失效了。"""
        base = self.images.make(lambda d: None)
        caret = self.images.make(lambda d: d.rectangle([900, 600, 902, 620],
                                                      fill="#000000"))
        ratio = frame_delta_ratio(base, caret)
        self.assertIsNotNone(ratio)
        self.assertLess(ratio, DEFAULT_MIN_RATIO,
                        f"光标占比 {ratio} 不该达到阈值 {DEFAULT_MIN_RATIO}")
        self.assertFalse(screen_changed(base, caret))

    def test_real_ui_change_is_detected(self):
        """真正翻页/弹层会成大片区变化，必须能认出来。"""
        base = self.images.make(lambda d: None)
        changed = self.images.make(
            lambda d: d.rectangle([300, 200, 1600, 900], fill="#FFFFFF", outline="#333333"))
        self.assertTrue(screen_changed(base, changed))

    def test_missing_file_returns_none(self):
        """读不到图时返回 None（判断不了），调用方好退回树签名，不能抛异常。"""
        p = self.images.make(lambda d: None)
        self.assertIsNone(frame_delta_ratio(p, self.tmp / "nope.png"))
        self.assertIsNone(screen_changed(None, p))

    def test_accepts_raw_bytes(self):
        from PIL import Image

        import io

        p = self.images.make(lambda d: None)
        raw = p.read_bytes()
        self.assertIsNotNone(frame_delta_ratio(raw, p))
        self.assertTrue(Image.open(io.BytesIO(raw)).size, "字节流本身要能解码")


if __name__ == "__main__":
    unittest.main()
