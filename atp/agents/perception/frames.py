"""截图前后帧比较：判断"这一步到底有没有让界面发生变化"。

为什么需要它：界面树的签名只能反映**结构**变化，而桌面/Electron 应用里大量动作
（点搜索框获得焦点、点开关、hover 出下拉）根本不改结构。反过来，用整张图的
MD5 比也不可靠——光标闪烁、水印动画、时钟都会让每一帧都不同。

折中做法：缩到小尺寸灰度图再比，只看"成片的"差异。光标闪一下在 320×200 上
通常只有 1~2 个像素，占比远低于阈值；真正翻页/弹层/换页会几十个百分点。
"""

from __future__ import annotations

from pathlib import Path

# 缩略图尺寸：小到能吃掉抖动与抗锯齿噪声，大到仍能分辨"整块区域换了内容"
_W, _H = 320, 200
# 单个像素的灰度差超过这个值才算"变了"，避免压缩噪声被当成变化
_PIXEL_DELTA = 18
# 变化像素占比阈值：0.05%（320×200 上约 32 个像素）。
#
# 这个数是拿真实 run 的截图标出来的（run-20261113-173646）：
#   点了没反应的两步：0.0031% / 0.0000%
#   真的换了界面的一步：5.13%
#   只有一行 toast 文案变了：0.10%
# 阈值放在 0.05%，既能把"点了等于没点"和"只是提示文案变了"分开，
# 又远离光标闪烁那种 1~2 个像素的噪声（约 0.002%）。
DEFAULT_MIN_RATIO = 0.0005
# "有实质推进"的阈值：1%。
# 0.05%~1% 之间属于"画面动了点但没真推进"——焦点框、hover、一行 toast 都在这个区间。
# 这两种口径必须分开：判断"这一步有没有反应"用 0.05%（否则连 toast 都看不见），
# 判断"要不要把同一动作算成重复"用 1%（否则点一下搜索框、焦点框亮一下就算新进展，
# 模型会一直点下去——正是 run-20261113-173646 的病根）。
PROGRESS_MIN_RATIO = 0.01


def frame_delta_ratio(before: str | Path | bytes | None,
                      after: str | Path | bytes | None) -> float | None:
    """两张截图的像素变化比例（0~1）。读不了/尺寸不一致就返回 None。"""
    try:
        from io import BytesIO

        from PIL import Image, ImageChops
    except Exception:  # noqa: BLE001 - 没装 Pillow 就当作"判断不了"
        return None

    def _open(src):
        if src is None:
            return None
        if isinstance(src, (bytes, bytearray)):
            return Image.open(BytesIO(src))
        p = Path(src)
        if not p.exists():
            return None
        return Image.open(p)

    try:
        a = _open(before)
        b = _open(after)
        if a is None or b is None:
            return None
        a = a.convert("L").resize((_W, _H))
        b = b.convert("L").resize((_W, _H))
        diff = ImageChops.difference(a, b)
        # 用 histogram 统计，比逐像素遍历快一个量级
        hist = diff.histogram()
        changed = sum(hist[_PIXEL_DELTA + 1:])
        return changed / float(_W * _H)
    except Exception:  # noqa: BLE001 - 损坏的 PNG 不该把执行带崩
        return None


def screen_changed(before: str | Path | bytes | None,
                   after: str | Path | bytes | None,
                   min_ratio: float = DEFAULT_MIN_RATIO) -> bool | None:
    """界面是否真的变了。None 表示判断不了（缺图/解码失败）。"""
    ratio = frame_delta_ratio(before, after)
    if ratio is None:
        return None
    return ratio >= min_ratio
