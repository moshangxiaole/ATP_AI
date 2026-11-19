"""视觉定位与分辨率映射的回归测试。

不联网、不依赖 opencv：用 Pillow 现画"界面"，验证三件事
1. 归一化坐标在任意分辨率下都指向同一块 UI；
2. 模板匹配能在缩放后的画面上找到目标（±pad 的位移也能兜住）；
3. 匹配不到时不瞎点（阈值必须真的生效）。
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from atp.config import VisionConfig  # noqa: E402
from atp.agents.perception.locator import LocatorStrategy, resolve  # noqa: E402
from atp.common.stepir import CaseIR, Step  # noqa: E402
from atp.common.types import Action, Observation, UINode  # noqa: E402
from atp.agents.perception.vision import (  # noqa: E402
    VisualLocator, capture_template, denorm_rect, image_size, norm_rect,
)

TMP = ROOT / "build_tmp" / "vision"

# 目标按钮在 1080x1920 画布上的像素矩形（后面按比例验算）
BASE = (1080, 1920)
BTN = (216, 1152, 486, 1267)          # 归一化约 (0.2,0.6)-(0.45,0.66)
BTN_NORM = [round(v, 3) for v in norm_rect(BTN, BASE)]


def draw_ui(size: tuple[int, int]) -> bytes:
    """画一张有辨识度的界面：标题条 + 表格线 + 一个带图标的按钮 + 噪点文字块。

    缩放之后细节会被重采样，正好用来验证匹配算法不是"逐像素相等"那种脆弱实现。
    """
    w, h = size
    img = Image.new("RGB", size, (245, 246, 250))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, int(h * 0.06)], fill=(32, 96, 178))
    for i in range(8):
        y = int(h * (0.10 + i * 0.05))
        d.line([(int(w * 0.08), y), (int(w * 0.92), y)], fill=(205, 210, 220), width=2)
    x1, y1, x2, y2 = BTN
    d.rounded_rectangle([x1, y1, x2, y2], radius=8, fill=(64, 158, 255))
    d.ellipse([x1 + 16, y1 + 15, x1 + 46, y1 + 45], fill=(255, 255, 255))
    d.rectangle([x1 + 60, y1 + 22, x2 - 20, y1 + 38], fill=(255, 255, 255))
    d.rectangle([x1 + 60, y1 + 46, x1 + 150, y1 + 58], fill=(220, 235, 255))
    d.rectangle([int(w * 0.1), int(h * 0.8), int(w * 0.9), int(h * 0.86)],
                outline=(180, 190, 205), width=3)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def resize_png(data: bytes, size: tuple[int, int]) -> bytes:
    buf = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGB").resize(size, Image.LANCZOS).save(buf, "PNG")
    return buf.getvalue()


def obs_of(data: bytes, size: tuple[int, int], platform: str = "web") -> Observation:
    """构造一次观测：坐标空间按 CSS 像素给（与真实驱动一致），截图为物理像素。"""
    return Observation(platform=platform, device_id="t", width=size[0], height=size[1],
                       root=UINode(cls="Root", bounds=(0, 0, size[0], size[1])),
                       screenshot=data)


class TestNormalization(unittest.TestCase):
    def test_roundtrip_scales_with_canvas(self):
        """同一归一化矩形，在 720p / 4K 上映射出的像素位置必须保持相对位置一致。"""
        for size in [(1080, 1920), (720, 1280), (1440, 2560)]:
            x1, y1, x2, y2 = denorm_rect(BTN_NORM, size)
            self.assertAlmostEqual(x1 / size[0], BTN_NORM[0], places=3)
            self.assertAlmostEqual((x1 + x2) / 2 / size[0], (BTN_NORM[0] + BTN_NORM[2]) / 2,
                                   places=2)
            self.assertAlmostEqual(y2 / size[1], BTN_NORM[3], places=3)

    def test_degenerate_size(self):
        self.assertEqual(norm_rect((1, 2, 3, 4), (0, 0)), [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(denorm_rect([0.5, 0.5, 0.5, 0.5], (100, 100)), (50, 50, 50, 50))


class TestTemplateMatch(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        self.shot = draw_ui(BASE)
        cap = capture_template(self.shot, BTN, cases_dir=TMP, case_id="v1",
                              step_id="s03", label="提交按钮")
        self.assertIsNotNone(cap)
        self.value, self.asset = cap
        self.locator = VisualLocator(TMP, VisionConfig())

    def test_asset_written_with_normalized_rect(self):
        path = TMP / self.asset["rel_path"]
        self.assertTrue(path.exists())
        self.assertGreater(len(path.read_bytes()), 200)
        rect = self.value["rect"]
        self.assertEqual(len(rect), 4)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in rect))
        # 模板图尺寸 ≈ 目标矩形（含 2px 内边距）
        tw, th = image_size(path.read_bytes())
        self.assertAlmostEqual(tw, BTN[2] - BTN[0] + 4, delta=2)
        self.assertAlmostEqual(th, BTN[3] - BTN[1] + 4, delta=2)

    def test_same_resolution_hits(self):
        hit = self.locator.locate(self.value, obs_of(self.shot, BASE))
        self.assertIsNotNone(hit)
        self.assertGreater(hit.score, 0.95)
        cx, cy = hit.center_px
        self.assertAlmostEqual((cx / BASE[0]), (BTN[0] + BTN[2]) / 2 / BASE[0], places=2)
        self.assertAlmostEqual((cy / BASE[1]), (BTN[1] + BTN[3]) / 2 / BASE[1], places=2)

    def test_scaled_down_screen_still_hits(self):
        """1080x1920 录的模板，放到 720x1280 上跑——这就是换终端的真实场景。"""
        small = (720, 1280)
        hit = self.locator.locate(self.value, obs_of(resize_png(self.shot, small), small))
        self.assertIsNotNone(hit, "缩放后应当仍能匹配")
        self.assertGreater(hit.score, 0.8)
        expect = denorm_rect(BTN_NORM, small)
        got = hit.rect_px
        for a, b in zip(got, expect):
            self.assertAlmostEqual(a, b, delta=max(6, small[0] * 0.02))

    def test_scaled_up_screen_still_hits(self):
        big = (1440, 2560)
        hit = self.locator.locate(self.value, obs_of(resize_png(self.shot, big), big))
        self.assertIsNotNone(hit)
        expect = denorm_rect(BTN_NORM, big)
        self.assertAlmostEqual(hit.center_px[0], (expect[0] + expect[2]) // 2, delta=12)

    def test_scroll_drift_tolerated(self):
        """内容整体下移 40px（滚动条/标题栏差异）：靠 pad 搜索窗口仍然能找到。"""
        img = Image.open(io.BytesIO(self.shot))
        canvas = Image.new("RGB", BASE, (245, 246, 250))
        canvas.paste(img, (0, 40))
        buf = io.BytesIO()
        canvas.save(buf, "PNG")
        hit = self.locator.locate(self.value, obs_of(buf.getvalue(), BASE))
        self.assertIsNotNone(hit, "小幅位移不该导致定位失败")
        self.assertAlmostEqual(hit.rect_px[1], BTN[1] + 40, delta=6)

    def test_wrong_screen_rejected_by_threshold(self):
        """换了一张完全不相干的界面：必须返回 None，宁可报警也不能瞎点。"""
        noise = Image.new("RGB", BASE, (250, 250, 250))
        d = ImageDraw.Draw(noise)
        for i in range(60):
            d.line([(0, i * 30), (BASE[0], i * 30 + 20)], fill=(240, 240, 240))
        buf = io.BytesIO()
        noise.save(buf, "PNG")
        self.assertIsNone(self.locator.locate(self.value, obs_of(buf.getvalue(), BASE)))

    def test_missing_asset_returns_none(self):
        bad = dict(self.value)
        bad["asset"] = "assets/v1/不存在.png"
        self.assertIsNone(self.locator.locate(bad, obs_of(self.shot, BASE)))

    def test_traversal_blocked(self):
        bad = dict(self.value)
        bad["asset"] = "../../atp.config.json"
        self.assertIsNone(self.locator.locate(bad, obs_of(self.shot, BASE)))


class TestResolveWithVisual(unittest.TestCase):
    """定位器接入：visual 策略命中后，合成的节点坐标必须落在当前画布坐标空间。"""

    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        self.shot = draw_ui(BASE)
        self.value, _ = capture_template(self.shot, BTN, cases_dir=TMP,
                                        case_id="v2", step_id="s01", label="按钮")
        self.locator = VisualLocator(TMP, VisionConfig())

    def _matcher(self, obs: Observation):
        from atp.agents.perception.vision import denorm_rect as dn, make_point_node

        def matcher(strategy: LocatorStrategy):
            hit = self.locator.locate(strategy.value, obs)
            if hit is None:
                return None, 0.0, 1.0
            return make_point_node(dn(hit.rect_norm, (obs.width, obs.height))), hit.score, hit.scale

        return matcher

    def test_visual_only_strategy_resolves(self):
        small = (720, 1280)
        obs = obs_of(resize_png(self.shot, small), small)
        st = LocatorStrategy.make("visual", self.value)
        res = resolve(obs.root, [st], (obs.width, obs.height),
                      visual_matcher=self._matcher(obs))
        self.assertTrue(res.ok)
        self.assertGreater(res.match_score, 0.8)
        self.assertGreater(res.scale, 0.4)
        x1, y1, x2, y2 = res.node.bounds
        # 点击点换算到当前画布（720x1280）后，应当仍在按钮范围内
        self.assertLess(abs((x1 + x2) / 2 / small[0] - (BTN_NORM[0] + BTN_NORM[2]) / 2), 0.02)

    def test_without_matcher_falls_through(self):
        obs = obs_of(self.shot, BASE)
        st = LocatorStrategy.make("visual", self.value)
        res = resolve(obs.root, [st], (obs.width, obs.height))
        self.assertFalse(res.ok)

    def test_strong_locator_wins_over_visual(self):
        """有 id 的时候不该走图像识别：优先级保证了这一点。"""
        obs = obs_of(self.shot, BASE)
        obs.root.children = [UINode(cls="Button", id="pkg:id/btn_submit",
                                    bounds=(BTN[0], BTN[1], BTN[2], BTN[3]), clickable=True)]
        res = resolve(obs.root, [
            LocatorStrategy.make("a11y_id", "pkg:id/btn_submit"),
            LocatorStrategy.make("visual", self.value),
        ], (obs.width, obs.height), visual_matcher=self._matcher(obs))
        self.assertTrue(res.ok)
        self.assertEqual(res.strategy.type, "a11y_id")


class TestStepIRVisual(unittest.TestCase):
    def test_visual_strategy_survives_roundtrip(self):
        case = CaseIR(case_id="v3", title="视觉用例", platform="web")
        case.steps.append(Step(
            step_id="s01", intent="点 canvas 按钮", action=Action.TAP,
            strategies=[LocatorStrategy.make("visual", {
                "asset": "assets/v3/s01.png", "rect": [0.2, 0.6, 0.45, 0.66],
                "base": [1080, 1920], "threshold": 0.82, "label": "提交",
            })],
        ))
        p = TMP / "v3.stepir.json"
        case.save(p)
        back = CaseIR.load(p)
        self.assertEqual(back.steps[0].visual["asset"], "assets/v3/s01.png")
        self.assertEqual(back.steps[0].visual["base"], [1080, 1920])

    def test_compiler_renders_tap_image(self):
        from atp.scriptgen.compiler import to_python

        case = CaseIR(case_id="v4", title="视觉脚本", platform="web")
        case.steps.append(Step(
            step_id="s01", intent="点 canvas 按钮", action=Action.TAP,
            strategies=[LocatorStrategy.make("visual", {
                "asset": "assets/v4/s01.png", "rect": [0.2, 0.6, 0.45, 0.66],
                "base": [1080, 1920], "label": "提交"})],
        ))
        code = to_python(case, "v4.stepir.json")
        self.assertIn("S.tap_image(asset='assets/v4/s01.png'", code)
        compile(code, "v4.py", "exec")


if __name__ == "__main__":
    unittest.main(verbosity=2)
