"""视觉兜底：截图 + 模板匹配，且**分辨率无关**。

为什么需要它
------------
无障碍树 / DOM 覆盖不了的界面一直存在：canvas 画的图表、游戏、Flutter 自绘、
被 FLAG_SECURE 屏蔽的银行页面、Electron 里 `webgl` 渲染的播放器。
这些地方没有 id、没有文本、没有可访问节点，只有像素——元素定位在这里彻底失效。

核心难点不是"匹配"，是**换一台机器还能匹配**
--------------------------------------------
1080x2400 的手机上截的图，拿到 720x1600 的机器上，或者 1280x800 的窗口上录的
脚本拿到 1920x1080 最大化窗口里跑，像素坐标全错。传统录屏式的做法是"按原坐标点"，
换分辨率必挂。这里用三层机制解决：

1. **归一化存储**：模板的命中矩形、点击点，一律存 0~1 的相对坐标，
   落地时再乘当前画布尺寸。这一步就吃掉了绝大部分分辨率差异。
2. **基准画布 + 缩放比**：每个模板记下采集时的 `base = (w,h)` 与 dpr，
   回放时算 `scale = 当前尺寸 / base`，缩放在此基础上做多档搜索。
3. **多尺度 NCC 匹配**：UI 并不总是线性缩放（浏览器缩放档位、系统字体放大、
   标题栏高度固定不缩放），所以不只试理论缩放比，而是在 ±scale_range 内
   扫 7 档，取相关系数最高的一档。搜索窗口再额外放宽 pad，容忍滚动位置漂移。

匹配算法：归一化互相关（NCC，即 TM_CCOEFF_NORMED 的口径）
--------------------------------------------------------
NCC 对整体亮暗/对比度变化不敏感（模板与窗口各自减均值），比 SAD/SSD 稳。
不引入 opencv（几十 MB、企业内网装不动），用 numpy 的 FFT 做相关 + 积分图算局部
方差，纯 CPU、单次搜索 20~60ms，够实时。

注意：这里只做"模板存在性 + 位置"，不做语义理解。语义判断仍归 LLM 判定层。
"""

from __future__ import annotations

import hashlib
import io
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from atp.common.stepir import LocatorStrategy
from atp.common.types import Observation, UINode

ASSET_DIR = "assets"


# ---------------------------------------------------------------- 图像基元
def _gray(data: bytes) -> Any:
    """截图 → 单通道 float32 数组。失败返回 None（调用方降级，不抛）。"""
    try:
        import numpy as np

        img = Image.open(io.BytesIO(data)).convert("L")
        return np.asarray(img, dtype=np.float32)
    except Exception:  # noqa: BLE001
        return None


def image_size(data: bytes) -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return (0, 0)


def frame_diff_ratio(a: bytes, b: bytes, tolerance: int = 12) -> float:
    """两张截图的"变化面积占比"（0~1）。

    用来判断视频**是不是真的在播**：节点树看不出这个——播放器和黑屏、暂停画面、
    加载转圈在树里长得一模一样。取两张相隔几百毫秒的截图，比较有多少像素明显变了：
    正片在播通常 >8%，静止页面接近 0%，弹幕/时间戳这种小范围变化只有 1%~2%。

    像素差 > tolerance（默认 12/255）才算"变了"，这样能滤掉编码噪声与轻微抖动。
    """
    try:
        import numpy as np

        ga, gb = _gray(a), _gray(b)
        if ga is None or gb is None or ga.shape != gb.shape or ga.size == 0:
            return 0.0
        changed = np.abs(ga - gb) > tolerance
        return float(changed.mean())
    except Exception:  # noqa: BLE001
        return 0.0


def norm_rect(rect: tuple[int, int, int, int] | list[int],
              size: tuple[int, int]) -> list[float]:
    """像素矩形 → 0~1 归一化矩形（保留 4 位，够 4K 精度）。"""
    w, h = size
    if not w or not h:
        return [0.0, 0.0, 0.0, 0.0]
    x1, y1, x2, y2 = rect
    return [round(x1 / w, 4), round(y1 / h, 4), round(x2 / w, 4), round(y2 / h, 4)]


def denorm_rect(rect: list[float] | tuple[float, ...],
                size: tuple[int, int]) -> tuple[int, int, int, int]:
    """0~1 归一化矩形 → 当前画布像素矩形。这一步就是"分辨率映射"。"""
    w, h = size
    x1, y1, x2, y2 = rect
    return (int(round(x1 * w)), int(round(y1 * h)), int(round(x2 * w)), int(round(y2 * h)))


def _next_fast(n: int) -> int:
    """下一个 2^a·3^b·5^c，让 FFT 别退化到质数长度。"""
    n = max(2, int(n))
    best = 1
    while best < n:
        best *= 2
    p2 = 1
    while p2 < best:
        p3 = p2
        while p3 < best:
            p5 = p3
            while p5 < n:
                p5 *= 5
            if p5 < best:
                best = p5
            p3 *= 3
        p2 *= 2
    return best


def ncc_map(img: Any, tpl: Any) -> Any:
    """归一化互相关图。返回 (ih-th+1, iw-tw+1)，值域 -1~1。

    推导：NCC = Σ(I-Ī)(T-T̄) / (√Σ(I-Ī)² · √Σ(T-T̄)²)
    其中 Σ(T-T̄)=0，所以分子等于 Σ I·(T-T̄)，可以用一次 FFT 相关算出；
    分母的 Σ(I-Ī)² 用积分图 O(1) 得到每个窗口的局部方差。
    """
    import numpy as np

    # 必须转 float64 再算：float32 累加算均值会有残差，Σ(T-T̄) 不为 0 时
    # 分子里会多出一项 Ī·Σ(T-T̄)，NCC 能算出 1.4 这种不可能的值（实测踩过）。
    img = np.asarray(img, dtype=np.float64)
    tpl = np.asarray(tpl, dtype=np.float64)

    ih, iw = img.shape
    th, tw = tpl.shape
    if th > ih or tw > iw or th < 4 or tw < 4:
        return None

    t0 = tpl - tpl.mean()
    tn = float(np.sqrt((t0 * t0).sum()))
    if tn < 1e-6:  # 纯色模板没有信息量
        return None

    fh = _next_fast(ih + th - 1)
    fw = _next_fast(iw + tw - 1)
    F = np.fft.rfft2(img, s=(fh, fw))
    T = np.fft.rfft2(t0[::-1, ::-1], s=(fh, fw))
    corr = np.fft.irfft2(F * T, s=(fh, fw))[th - 1:ih, tw - 1:iw]

    def box_sum(a: Any) -> Any:
        c = np.cumsum(np.cumsum(a, axis=0), axis=1)
        c = np.pad(c, ((1, 0), (1, 0)))
        return c[th:, tw:] - c[:-th, tw:] - c[th:, :-tw] + c[:-th, :-tw]

    n = float(th * tw)
    s1 = box_sum(img)
    s2 = box_sum(img * img)
    var = s2 - s1 * s1 / n
    np.maximum(var, 1e-6, out=var)
    ncc = corr / (np.sqrt(var) * tn)
    # 数值误差可能把边界顶出去，物理上 NCC 不会超出 [-1, 1]
    return np.clip(ncc, -1.0, 1.0)


@dataclass
class VisualHit:
    """一次视觉定位的结果。坐标同时给归一化与当前像素两套。"""

    rect_norm: list[float]
    rect_px: tuple[int, int, int, int]
    score: float
    scale: float
    tpl_size: tuple[int, int]

    @property
    def center_px(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.rect_px
        return ((x1 + x2) // 2, (y1 + y2) // 2)


# ---------------------------------------------------------------- 采集
def _slug(text: str, fallback: str = "target") -> str:
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]+", "", text or "")[:16]
    return s or fallback


def capture_template(
    screenshot: bytes,
    rect_px: tuple[int, int, int, int],
    *,
    cases_dir: Path,
    case_id: str,
    step_id: str,
    label: str = "",
    dpr: float = 1.0,
    platform: str = "",
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """从截图里裁出目标区域当模板，写入 cases/assets/，返回 (策略value, 资产元数据)。

    裁剪留 2px 内边距：UI 元素边框常被 anti-alias 吃掉，带一点背景反而更好对齐。
    """
    size = image_size(screenshot)
    if not size[0] or not size[1]:
        return None
    x1, y1, x2, y2 = rect_px
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    # 夹到画布内，避免裁到黑边
    x1, y1 = max(0, x1 - 2), max(0, y1 - 2)
    x2, y2 = min(size[0], x2 + 2), min(size[1], y2 + 2)
    try:
        im = Image.open(io.BytesIO(screenshot)).convert("RGB").crop((x1, y1, x2, y2))
    except Exception:  # noqa: BLE001
        return None

    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    png = buf.getvalue()
    sha = hashlib.sha1(png).hexdigest()[:8]
    folder = Path(cases_dir) / ASSET_DIR / _safe(case_id)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{step_id}_{_slug(label)}_{sha}.png"
    path = folder / name
    try:
        path.write_bytes(png)
    except OSError:
        return None

    rel = f"{ASSET_DIR}/{_safe(case_id)}/{name}"
    rect_n = norm_rect((x1, y1, x2, y2), size)
    value = {
        "asset": rel,
        "rect": rect_n,
        "base": [size[0], size[1]],
        "threshold": 0.82,
        "pad": 0.22,
        "dpr": round(float(dpr or 1.0), 3),
        "label": label or step_id,
        "platform": platform,
    }
    asset = {
        "case_id": case_id, "step_id": step_id, "name": name, "rel_path": rel,
        "sha1": sha, "base_width": size[0], "base_height": size[1],
        "dpr": round(float(dpr or 1.0), 3), "norm_rect": rect_n,
    }
    return value, asset


def _safe(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fa5]", "_", name or "case")[:48] or "case"


# ---------------------------------------------------------------- 定位
class VisualLocator:
    """把一条 visual 定位策略解成当前界面上的像素矩形。"""

    def __init__(self, cases_dir: Path, vision_cfg: Any = None):
        self.cases_dir = Path(cases_dir)
        self.cfg = vision_cfg
        self._cache: dict[str, Any] = {}

    # ---------- 模板读取 ----------
    def asset_path(self, rel: str) -> Path | None:
        if not rel:
            return None
        p = (self.cases_dir / rel).resolve()
        root = self.cases_dir.resolve()
        if root not in p.parents:  # 防目录穿越
            return None
        return p if p.exists() else None

    def _template(self, rel: str) -> Any:
        if rel in self._cache:
            return self._cache[rel]
        p = self.asset_path(rel)
        arr = None
        if p is not None:
            try:
                arr = _gray(p.read_bytes())
            except Exception:  # noqa: BLE001
                arr = None
        self._cache[rel] = arr
        return arr

    # ---------- 匹配 ----------
    def locate(self, value: dict[str, Any], obs: Observation,
               screen: Any = None) -> VisualHit | None:
        """在 obs 的截图里找模板，返回命中矩形（含归一化坐标）。"""
        if not obs.screenshot:
            return None
        rel = str(value.get("asset", ""))
        tpl = self._template(rel)
        if tpl is None:
            return None
        screen_arr = _gray(obs.screenshot)
        if screen_arr is None:
            return None

        cfg = self.cfg
        threshold = float(value.get("threshold") or getattr(cfg, "threshold", 0.82))
        pad = float(value.get("pad") if value.get("pad") is not None
                    else getattr(cfg, "pad", 0.22))
        base = value.get("base") or []
        if not (isinstance(base, (list, tuple)) and len(base) == 2 and base[0] and base[1]):
            base = [screen_arr.shape[1], screen_arr.shape[0]]
        expect = value.get("rect") or []

        scales = self._scales(screen_arr.shape[::-1], (int(base[0]), int(base[1])), cfg)
        best: VisualHit | None = None
        search = self._search_window(screen_arr.shape[::-1], base, expect, pad)

        for sx, sy in scales:
            tw = max(6, int(round(tpl.shape[1] * sx)))
            th = max(6, int(round(tpl.shape[0] * sy)))
            if tw > screen_arr.shape[1] or th > screen_arr.shape[0]:
                continue
            try:
                tpl_s = self._resize(tpl, (tw, th))
            except Exception:  # noqa: BLE001
                continue
            x0, y0, x1, y1 = search
            if x1 - x0 < tw or y1 - y0 < th:
                continue
            win = screen_arr[y0:y1, x0:x1]
            m = ncc_map(win, tpl_s)
            if m is None or not m.size:
                continue
            idx = int(m.argmax())
            score = float(m.ravel()[idx])
            if best is None or score > best.score:
                yy, xx = divmod(idx, m.shape[1])
                px = (x0 + xx, y0 + yy, x0 + xx + tw, y0 + yy + th)
                cw, ch = screen_arr.shape[1], screen_arr.shape[0]
                best = VisualHit(rect_norm=norm_rect(px, (cw, ch)), rect_px=px,
                                 score=round(score, 4), scale=round(math.sqrt(sx * sy), 4),
                                 tpl_size=(tw, th))
            if score >= 0.985:  # 几乎逐像素一致，没必要再试其它尺度
                break

        if best is None or best.score < threshold:
            return None
        return best

    @staticmethod
    def _resize(arr: Any, size: tuple[int, int]) -> Any:
        import numpy as np

        im = Image.fromarray(arr.astype("uint8"), mode="L").resize(size, Image.LANCZOS)
        return np.asarray(im, dtype=np.float32)

    @staticmethod
    def _scales(cur: tuple[int, int], base: tuple[int, int], cfg: Any) -> list[tuple[float, float]]:
        """候选缩放比（x、y 各自一个），按"最可能命中"的顺序排。

        两件事同时要照顾：
        - **等比缩放**（同一台机器换窗口大小、`1920x1080 → 2560x1440`）：x/y 比例相同，
          用几何平均做中心、±range 内扫几档，兜住浏览器缩放档位与系统字体差异；
        - **非等比缩放**（换设备、换宽高比，`16:9 → 4:3`）：x/y 比例不同，只试等比必然失败。
          所以先显式试一次理论 (sx, sy)，再试两次折中，最后才铺等比档位。
        """
        steps = max(1, int(getattr(cfg, "scale_steps", 7) or 7))
        rng = float(getattr(cfg, "scale_range", 0.18) or 0.18)
        sx = cur[0] / max(1, base[0])
        sy = cur[1] / max(1, base[1])
        mid = math.sqrt(max(sx, 1e-6) * max(sy, 1e-6))

        out: list[tuple[float, float]] = [(round(sx, 4), round(sy, 4))]
        if abs(sx - sy) > 0.04:
            out.append((round(sx, 4), round(mid, 4)))
            out.append((round(mid, 4), round(sy, 4)))
        if steps > 1 and rng > 0:
            lo, hi = mid * (1 - rng), mid * (1 + rng)
            uniform = [round(lo + (hi - lo) * i / (steps - 1), 4) for i in range(steps)]
            uniform.sort(key=lambda s: abs(s - mid))
            out.extend((s, s) for s in uniform)
        return out or [(round(mid, 4), round(mid, 4))]

    @staticmethod
    def _search_window(cur: tuple[int, int], base: list, expect: list,
                       pad: float) -> tuple[int, int, int, int]:
        """优先在预测位置附近搜（几十毫秒）；没有预测坐标就搜全屏（慢但能救）。"""
        cw, ch = cur
        if not (isinstance(expect, (list, tuple)) and len(expect) == 4):
            return (0, 0, cw, ch)
        x1, y1, x2, y2 = denorm_rect(expect, (cw, ch))
        bw = max(24, x2 - x1)
        bh = max(24, y2 - y1)
        mx = int(max(48, bw * (1 + pad * 2) - bw))
        my = int(max(48, bh * (1 + pad * 2) - bh))
        return (max(0, x1 - mx), max(0, y1 - my), min(cw, x2 + mx), min(ch, y2 + my))


def visual_strategy(value: dict[str, Any]) -> LocatorStrategy:
    return LocatorStrategy.make("visual", value)


def make_point_node(rect_px: tuple[int, int, int, int], label: str = "") -> UINode:
    """视觉命中后合成一个可点击节点，让上层 perform() 无需区分来源。"""
    return UINode(cls="VisualTarget", text=label or "图像目标",
                  bounds=rect_px, clickable=True, desc="图像识别命中")


__all__ = [
    "ASSET_DIR",
    "VisualHit",
    "VisualLocator",
    "capture_template",
    "denorm_rect",
    "image_size",
    "make_point_node",
    "ncc_map",
    "norm_rect",
    "visual_strategy",
]
