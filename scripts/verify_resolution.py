"""跨分辨率验证：同一套模板，在两种画布尺寸上都要点得中。

为什么单独写一个脚本而不是单测：
单测用 PIL 合成的"假界面"，能证明算法对；但证明不了**驱动层**真的对——
真实浏览器截图有没有 DPR、最大化之后视口有没有跟上、点击坐标走的是 CSS 还是物理像素，
这些只有接上真浏览器才能确认。

这个脚本跑两条链路，都不依赖被测系统：
  1) Web：真 Chrome + canvas 页面（DOM 里只有一个 <canvas>，没有按钮节点）
     在最大化窗口下采模板 → 用 CDP 把视口改成另一种分辨率 → 再定位并点击 → 页面自证命中
  2) 移动端：模拟 1080x2400 手机采集 → 720x1600 手机回放（走同一个 Executor / CaseIR / 用例库）

用法：python scripts/verify_resolution.py
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from atp.config import Config, VisionConfig  # noqa: E402
from atp.agents.executor import Executor  # noqa: E402
from atp.agents.perception.locator import resolve  # noqa: E402
from atp.common.stepir import CaseIR, LocatorStrategy, ScreenProfile, Step  # noqa: E402
from atp.common.types import Action, UINode  # noqa: E402
from atp.agents.perception.vision import (  # noqa: E402
    VisualLocator, capture_template, denorm_rect, image_size, make_point_node,
)
from atp.agents.platforms.base import BaseDriver  # noqa: E402
from atp.db.store import CaseDB  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "canvas_login.html"
TMP = ROOT / "build_tmp" / "verify"
OK, BAD = "PASS", "FAIL"


def line(tag: str, name: str, detail: str = "") -> None:
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- Web 真机链路
def verify_web() -> bool:
    from atp.agents.platforms.web.cdp import WebCdpDriver

    print("\n=== 1. Web：真 Chrome + canvas 页面（元素定位彻底失效的场景）")
    cfg = Config(home=ROOT)
    cfg.cases_dir = str(TMP / "cases")
    cfg.db_path = str(TMP / "verify.db")
    (TMP / "cases").mkdir(parents=True, exist_ok=True)

    drv = WebCdpDriver(config=cfg, url=FIXTURE.as_uri())
    drv.connect()
    ok = True
    try:
        prof = drv.screen_profile()
        line(OK if prof.width > 200 else BAD, "最大化后的画布",
             f"{prof.width}x{prof.height} dpr={prof.dpr} ({prof.label})")
        ok &= prof.width > 200

        obs = drv.observe()
        sw, sh = image_size(obs.screenshot)
        line(OK if abs(sw / obs.width - 1) < 0.35 else BAD, "截图与坐标一致性",
             f"截图 {sw}x{sh} · 坐标空间 {obs.width}x{obs.height}")
        ok &= abs(sw / obs.width - 1) < 0.35

        btn = drv.page.evaluate("window.__btn")
        scale = sw / obs.width
        px = tuple(int(round(v * scale)) for v in (btn["x1"], btn["y1"], btn["x2"], btn["y2"]))
        cap = capture_template(obs.screenshot, px, cases_dir=cfg.cases, case_id="verify_web",
                               step_id="s01", label="登录按钮", dpr=prof.dpr, platform="web")
        if cap is None:
            line(BAD, "模板采集", "裁图失败")
            return False
        value, asset = cap
        line(OK, "模板采集", f"{asset['name']} rect={value['rect']} base={value['base']}")

        # 换一块屏：CDP 直接把视口改成 900x620（模拟另一台终端）
        sess = drv._cdp()
        sess.send("Emulation.setDeviceMetricsOverride",
                  {"width": 900, "height": 620, "deviceScaleFactor": 1, "mobile": False})
        time.sleep(0.6)
        drv.page.evaluate("window.dispatchEvent(new Event('resize'))")
        time.sleep(0.3)
        obs2 = drv.observe()
        line(OK, "切换分辨率", f"{obs.width}x{obs.height} → {obs2.width}x{obs2.height}")

        locator = VisualLocator(cfg.cases, VisionConfig())
        hit = locator.locate(value, obs2)
        if hit is None:
            line(BAD, "图像识别（换分辨率后）", "未命中")
            return False
        cn = (hit.rect_norm[0] + hit.rect_norm[2]) / 2
        en = (value["rect"][0] + value["rect"][2]) / 2
        dx = abs(cn - en)
        line(OK if dx < 0.02 else BAD, "图像识别（换分辨率后）",
             f"score={hit.score} scale={hit.scale} 归一化偏差 {dx:.4f}")
        ok &= dx < 0.02

        # 用执行器的同一套逻辑把命中框换算成点击坐标并点击
        node = make_point_node(denorm_rect(hit.rect_norm, (obs2.width, obs2.height)), "登录")
        res = drv.perform(Action.TAP, node)
        time.sleep(0.35)
        hits = drv.page.evaluate("window.__hits") or []
        if not hits:
            line(BAD, "点击落地", "页面没收到点击")
            return False
        last = hits[-1]
        btn2 = drv.page.evaluate("window.__btn")
        inside = (btn2["x1"] <= last["x"] <= btn2["x2"]
                  and btn2["y1"] <= last["y"] <= btn2["y2"])
        line(OK if inside else BAD, "跨分辨率点击命中",
             f"点击 ({last['x']},{last['y']}) · 按钮 {btn2} · perform={res.ok}")
        ok &= inside

        # 再换一次：极端宽高比，确认 pad 搜索窗口不吃死
        sess.send("Emulation.setDeviceMetricsOverride",
                  {"width": 1600, "height": 700, "deviceScaleFactor": 1, "mobile": False})
        time.sleep(0.6)
        drv.page.evaluate("window.dispatchEvent(new Event('resize'))")
        time.sleep(0.3)
        obs3 = drv.observe()
        hit3 = locator.locate(value, obs3)
        node3 = make_point_node(denorm_rect(hit3.rect_norm, (obs3.width, obs3.height))) \
            if hit3 else None
        if node3 is not None:
            drv.perform(Action.TAP, node3)
            time.sleep(0.3)
            hits = drv.page.evaluate("window.__hits") or []
            btn3 = drv.page.evaluate("window.__btn")
            last = hits[-1]
            inside3 = (btn3["x1"] <= last["x"] <= btn3["x2"]
                       and btn3["y1"] <= last["y"] <= btn3["y2"])
            line(OK if inside3 else BAD, "极端宽高比（1600x700）",
                 f"点击 ({last['x']},{last['y']}) 命中={inside3}")
            ok &= inside3
        else:
            line(BAD, "极端宽高比（1600x700）", "未命中")
            ok = False
    finally:
        try:
            drv.close()
        except Exception:  # noqa: BLE001
            pass
    return ok


# ---------------------------------------------------------------- 移动端链路
class FakePhone(BaseDriver):
    """模拟一台手机：整屏都是自绘界面（无障碍树只有一个根节点）。

    用它跑真实的 Executor：验证 CaseIR → 定位 → 视觉匹配 → 点击坐标 这条链路
    在两种屏幕尺寸之间切换时依然成立。真机接入时把截图/点击换成 adb 即可。
    """

    platform = "android"

    def __init__(self, size: tuple[int, int], config=None):
        super().__init__(f"fake-{size[0]}x{size[1]}", config)
        self.size = size
        self.taps: list[tuple[int, int]] = []
        self._btn = None

    # 画一屏：顶部标题 + 卡片 + 登录按钮（位置用归一化常量）
    BTN = (0.42, 0.52, 0.58, 0.60)

    def connect(self) -> None:
        self._connected = True

    def screen_size(self) -> tuple[int, int]:
        return self.size

    def screenshot(self) -> bytes:
        w, h = self.size
        img = Image.new("RGB", (w, h), (238, 241, 246))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, w, int(h * 0.08)], fill=(32, 96, 178))
        d.rectangle([int(w * 0.2), int(h * 0.18), int(w * 0.8), int(h * 0.68)],
                    fill=(255, 255, 255), outline=(213, 218, 227))
        x1, y1, x2, y2 = (int(w * self.BTN[0]), int(h * self.BTN[1]),
                          int(w * self.BTN[2]), int(h * self.BTN[3]))
        self._btn = (x1, y1, x2, y2)
        d.rounded_rectangle([x1, y1, x2, y2], radius=max(4, (y2 - y1) // 2),
                            fill=(64, 158, 255))
        r = max(4, min((y2 - y1) // 4, (x2 - x1) // 8))
        d.ellipse([x1 + r, (y1 + y2) // 2 - r, x1 + 3 * r, (y1 + y2) // 2 + r],
                  fill=(255, 255, 255))
        d.rectangle([x1 + 4 * r, (y1 + y2) // 2 - r // 2,
                     max(x1 + 5 * r, x2 - 2 * r), (y1 + y2) // 2 + r // 2],
                    fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def dump_tree(self) -> UINode:
        w, h = self.size
        return UINode(cls="Screen", bounds=(0, 0, w, h))

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def pressed_button(self) -> bool:
        if not self.taps or not self._btn:
            return False
        x, y = self.taps[-1]
        x1, y1, x2, y2 = self._btn
        return x1 <= x <= x2 and y1 <= y <= y2

    UNSUPPORTED = {Action.OPEN_URL, Action.MAXIMIZE}


def verify_mobile() -> bool:
    print("\n=== 2. 移动端：1080x2400 采集 → 720x1600 回放（真实 Executor 链路）")
    cfg = Config(home=ROOT)
    cfg.cases_dir = str(TMP / "cases")
    cfg.artifacts_dir = str(TMP / "artifacts")
    cfg.db_path = str(TMP / "verify.db")
    cfg.allow_healing = False
    ok = True

    big, small = (1080, 2400), (720, 1600)
    phone = FakePhone(big, cfg)
    phone.connect()
    obs = phone.observe()
    scale = image_size(obs.screenshot)[0] / obs.width
    px = tuple(int(round(v * scale)) for v in phone._btn)
    cap = capture_template(obs.screenshot, px, cases_dir=cfg.cases, case_id="verify_phone",
                           step_id="s01", label="登录", platform="android")
    if cap is None:
        line(BAD, "模板采集", "裁图失败")
        return False
    value, asset = cap
    line(OK, "模板采集", f"{asset['name']} 基准 {value['base']} rect={value['rect']}")

    case = CaseIR(case_id="verify_phone", title="手机登录（图像识别）", platform="android")
    case.screen = ScreenProfile(width=big[0], height=big[1], dpr=3.0,
                                label=f"android-{big[0]}x{big[1]}", platform="android")
    case.visual_assets = [asset]
    case.steps = [Step(step_id="s01", intent="点击登录（图像识别）", action=Action.TAP,
                       strategies=[LocatorStrategy.make("visual", value)])]
    case.save(TMP / "cases" / "verify_phone.stepir.json")

    small_phone = FakePhone(small, cfg)
    small_phone.connect()
    result = Executor(small_phone, cfg).run(case, mode="replay",
                                            persist_path=TMP / "cases" / "verify_phone.stepir.json")
    step = result.steps[0]
    line(OK if result.passed else BAD, "720x1600 回放", f"status={step.status} {step.detail}")
    ok &= result.passed
    line(OK if small_phone.pressed_button() else BAD, "点击落在按钮内",
         f"tap={small_phone.taps[-1] if small_phone.taps else None} 按钮={small_phone._btn}")
    ok &= small_phone.pressed_button()

    # 落库检查：视觉模板与执行记录都要有
    db = CaseDB(cfg.db_file)
    info = db.info()
    runs = db.runs(case_id="verify_phone")
    assets = db.assets("verify_phone")
    line(OK if assets else BAD, "视觉模板入库", f"case_assets {len(assets)} 行")
    line(OK if runs else BAD, "执行记录入库",
         f"runs {len(runs)} 行 · 画布 {runs[0]['screen_w']}x{runs[0]['screen_h']}" if runs else "-")
    line(OK, "库表行数", " ".join(f"{k}={v}" for k, v in info["counts"].items()))
    db.close()
    ok &= bool(assets) and bool(runs)
    return ok


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    print("=" * 66)
    print("ATP 跨分辨率视觉定位验证")
    print(f"工程 {ROOT}")
    print("=" * 66)
    web_ok = verify_web()
    mobile_ok = verify_mobile()
    print("\n" + "=" * 66)
    print(f"  结果：Web {'通过' if web_ok else '失败'} · 移动端 {'通过' if mobile_ok else '失败'}")
    print("=" * 66)
    return 0 if (web_ok and mobile_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
