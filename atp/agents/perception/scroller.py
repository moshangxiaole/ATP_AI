"""找不到目标时滑动搜索。

真机上的"定位失败"有一半根本不是定位器坏了：消息列表里的群聊、商品列表里的条目、
设置页深处的开关，**首屏本来就不展示**。这时让 AI 去修定位器是错的——它在当前这屏
也看不见目标，只能猜一个不相干的节点，猜出来的定位器回放必然再错一次，还污染用例库。

正确做法是先滑：上下（必要时左右）滑，直到目标出现在视野里再定位。
成本上也说得通：一次滑动 0.3 秒，一次 AI 修复要几十秒。

三条安全边界，都是为了"宁可不滑，也不能滑出事故"：

1. **只在滚得动的界面上滑**。节点树报 `scrollable=true`，或控件类型是
   RecyclerView / ListView / ScrollView / List / Swiper 这类容器，才动。
   播放器、纯静态页、弹窗上滑一下可能就是退出/拖动进度条/切 tab；
2. **到底/到顶就停**：连续几次滑动界面指纹完全没变，说明已经到头，别白滑；
3. **左右滑默认关闭**：横滑太容易触发侧滑删除、翻页、切换 tab，副作用不可控，
   需要时在配置里显式打开 `scroll_find.horizontal`。

顺序也有讲究：先"向手指上方滑"（看更下面的内容，绝大多数场景目标在下面），到底了
再反向滑回来，并且把已经翻下去的屏数补回来再多滑几屏，这样才能真扫到上半部分。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from atp.agents.perception.locator import LocateResult, resolve
from atp.common.types import Observation, RunCancelled, UINode

# 能滚的控件类型。节点树没报 scrollable 时（鸿蒙 dumpLayout 时有时无）靠它兜底：
# Android 的 RecyclerView/ListView/ScrollView/ViewPager，鸿蒙的 List/Scroll/Grid/
# Swiper/WaterFlow/Tabs，以及 WebView 里的长页面。
_SCROLLY = re.compile(
    r"scroll|recycler|listview|gridview|waterfall|waterflow|viewpager|swiper|webview|"
    r"^list$|listitem|table",
    re.I,
)

# 前进方向：手指往这边划，看到的是"更后面"的内容
_FORWARD = {"up", "left"}
_ARROW = {"up": "↑", "down": "↓", "left": "←", "right": "→"}
_DIRECTIONS = ("up", "down", "left", "right")

# "一次都没滑动"时的退避等待（秒）：先短后长，够覆盖冷启动转场 + 首帧绘制
_READY_BACKOFF = (1.0, 1.6)


@dataclass
class ScrollOutcome:
    """一次滑动搜索的结果。"""

    loc: LocateResult | None = None
    obs: Observation | None = None
    swipes: int = 0
    trail: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.loc is not None and self.loc.ok

    @property
    def trail_text(self) -> str:
        return "".join(_ARROW.get(d, d) for d in self.trail)


def trail_text(trail: list[str]) -> str:
    return "".join(_ARROW.get(d, d) for d in trail)


def has_scrollable(root: UINode) -> bool:
    """这棵树上有没有能滚的容器。"""
    for n in root.walk():
        if n.scrollable or _SCROLLY.search(n.cls or ""):
            return True
    return False


def observe_tree(driver, with_shot: bool = False) -> Observation:
    """只要节点树时别顺手截图。

    Android 一次 screencap 0.5~1 秒、鸿蒙还要 file recv 一趟，滑 8 次就是白扔
    十几秒。只有策略里带视觉模板、真的要比图时才取。
    """
    w, h = driver.screen_size()
    return Observation(
        platform=getattr(driver, "platform", ""),
        device_id=getattr(driver, "device_id", ""),
        width=int(w), height=int(h),
        root=driver.dump_tree(),
        screenshot=driver.screenshot() if with_shot else b"",
        url=getattr(driver, "current_url", ""),
    )


def find_by_scrolling(
    driver,
    strategies: list,
    screen: tuple[int, int] = (0, 0),
    tol: int = 12,
    visual_matcher_factory: Callable[[Observation], Any] | None = None,
    cfg: Any = None,
    on_swipe: Callable[[dict], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    avoid_occluded: bool = False,
    stabilizer: Any = None,
) -> ScrollOutcome:
    """滑动查找目标元素，找到了返回它的定位结果与当时的观察。

    `visual_matcher_factory(obs)` 而不是 `visual_matcher`：视觉模板匹配依赖截图，
    滑完之后截图已经变了，必须按新的观察重建匹配器，否则会拿旧画面去比。

    ``stabilizer`` 非空时，滑动后的固定 ``time.sleep(settle)`` 换成树-only 稳定采样
    （``wait_stable``，上限仍为 settle）；为 None 时保持原行为逐字不变。
    """
    if cfg is not None and not getattr(cfg, "enabled", True):
        return ScrollOutcome(reason="滑动查找已关闭")
    if not strategies:
        return ScrollOutcome(reason="没有可用定位策略")

    max_swipes = max(1, int(getattr(cfg, "max_swipes", 8) or 8))
    settle = max(0, int(getattr(cfg, "settle_ms", 450) or 0)) / 1000.0
    edge_stop = max(1, int(getattr(cfg, "edge_stop", 2) or 2))
    directions = [d for d in (getattr(cfg, "directions", None) or ["up", "down"])
                  if d in _DIRECTIONS] or ["up"]
    if not getattr(cfg, "horizontal", False):
        vertical = [d for d in directions if d in ("up", "down")]
        directions = vertical or ["up"]

    with_shot = any(getattr(s, "type", "") == "visual" for s in strategies)
    try:
        obs = observe_tree(driver, with_shot=with_shot)
    except Exception as e:  # noqa: BLE001
        return ScrollOutcome(reason=f"无法获取界面: {e}")

    if not getattr(cfg, "assume_scrollable", False) and not has_scrollable(obs.root):
        return ScrollOutcome(obs=obs, reason="当前界面没有可滚动容器，不滑动")

    ms = int(getattr(cfg, "swipe_ms", 700) or 700)
    ratio = float(getattr(cfg, "swipe_ratio", 0.35) or 0.35)
    trail: list[str] = []
    forward = 0          # 已经往"更后面"翻了几屏，反向扫时要补回来
    moved_any = False    # 界面到底动没动过——没动过就不是"到底"，是还没就绪
    prev_fp = obs.fingerprint

    for phase, direction in enumerate(directions):
        # 第一段按 max_swipes 扫；之后每一段要把之前翻下去的补回来，
        # 再往前多扫 max_swipes，否则永远只在起点附近打转。
        budget = max_swipes if phase == 0 else min(2 * max_swipes, forward + max_swipes)
        idle = 0
        while budget > 0:
            if should_cancel is not None:
                try:
                    if should_cancel():
                        raise RunCancelled("用户取消执行")
                except RunCancelled:
                    raise
                except Exception:  # noqa: BLE001
                    pass
            budget -= 1
            try:
                obs = _swipe_and_observe(driver, obs, direction, ms, settle,
                                         with_shot, ratio, stabilizer=stabilizer)
            except Exception as e:  # noqa: BLE001
                return ScrollOutcome(obs=obs, swipes=len(trail), trail=trail,
                                     reason=f"滑动失败: {e}")
            trail.append(direction)
            if on_swipe is not None:
                try:
                    on_swipe({"direction": direction, "index": len(trail)})
                except Exception:  # noqa: BLE001
                    pass

            matcher = visual_matcher_factory(obs) if visual_matcher_factory else None
            loc = resolve(obs.root, strategies, (obs.width, obs.height), tol, matcher,
                          avoid_occluded=avoid_occluded)
            if loc.ok:
                return ScrollOutcome(loc=loc, obs=obs, swipes=len(trail), trail=trail)

            if obs.fingerprint != prev_fp:
                moved_any = True
                idle = 0
                forward = max(0, forward + (1 if direction in _FORWARD else -1))
                prev_fp = obs.fingerprint
                continue

            idle += 1
            if idle < edge_stop:
                prev_fp = obs.fingerprint
                continue
            # 连续几次都没变化。两种情况必须分开：
            # - 已经滚过（到底/到顶了）→ 收手；
            # - 一次都没滚过 → 多半是界面还在转场、首帧没画完（真机实测：冷启动后
            #   立刻滑设置页，两下滑动全落在白屏上）。这种情况按退避再试，不能当成到头。
            if moved_any:
                break
            for wait in _READY_BACKOFF:
                time.sleep(wait)
                try:
                    obs = _swipe_and_observe(driver, obs, direction, max(ms, 900),
                                             settle + 0.4, with_shot, min(ratio, 0.3),
                                             stabilizer=stabilizer)
                except Exception as e:  # noqa: BLE001
                    return ScrollOutcome(obs=obs, swipes=len(trail), trail=trail,
                                         reason=f"滑动失败: {e}")
                matcher = visual_matcher_factory(obs) if visual_matcher_factory else None
                loc = resolve(obs.root, strategies, (obs.width, obs.height), tol, matcher,
                              avoid_occluded=avoid_occluded)
                if loc.ok:
                    trail.append(direction)
                    return ScrollOutcome(loc=loc, obs=obs, swipes=len(trail), trail=trail)
                if obs.fingerprint != prev_fp:
                    trail.append(direction)
                    moved_any = True
                    idle = 0
                    prev_fp = obs.fingerprint
                    break
            else:
                return ScrollOutcome(obs=obs, swipes=len(trail), trail=trail,
                                     reason="滑动后界面始终没有变化（可能已到尽头，或界面还没就绪）")

    return ScrollOutcome(obs=obs, swipes=len(trail), trail=trail,
                         reason=f"滑了 {len(trail)} 次仍未找到")


# ---------------------------------------------------------------- 内部
def _swipe_and_observe(driver, obs: Observation, direction: str, ms: int,
                       settle: float, with_shot: bool,
                       ratio: float = 0.35, stabilizer: Any = None) -> Observation:
    _swipe_once(driver, obs, direction, ms, ratio)
    if stabilizer is not None and settle > 0:
        # 等待合并到单一 wait_stable：滑动后只做树-only 稳定采样，不串联固定 sleep。
        # settle=0 时保持原语义「完全不等」，不额外引入门禁等待。
        try:
            stabilizer.invalidate()
            res = stabilizer.wait_stable("scroll_settle",
                                         ceiling_ms=int(max(0.0, settle) * 1000))
            if res.obs is not None and not with_shot:
                return res.obs
        except RunCancelled:
            raise
        except Exception:  # noqa: BLE001
            pass
    elif settle:
        time.sleep(settle)
    return observe_tree(driver, with_shot=with_shot)


def _swipe_once(driver, obs: Observation, direction: str, ms: int = 700,
                ratio: float = 0.35) -> None:
    """在"可滚容器"的中心小步拖一下，而不是一次甩一屏。

    三条都是真机踩出来的：

    1. **手势区域取容器中心，不取屏幕正中**。列表常常只占下半屏（上面是标题栏/横幅），
       划屏幕正中可能落在横幅上，滑了也白滑；
    2. **步幅只有容器高度的 35%**。按 55% 走时，一屏的距离足以把目标行整个推出去——
       滑完这一下，目标正好已经滚到视野上方，抓树就再也看不见它，于是"越滑越找不到"。
       步幅小于视口高度，保证上一屏底部的内容仍留在视野里，目标不可能被跨过去；
    3. **匀速 700ms、端点内收**。260ms 拖 1400px 会被系统当成"甩"，一次跨两三屏还带惯性；
       从屏幕最上沿（<12% 高度）起手则容易下拉通知栏、触发返回手势。
    """
    w, h = obs.width, obs.height
    if not w or not h:
        w, h = driver.screen_size()
    w, h = int(w), int(h)
    x1, y1, x2, y2 = _gesture_area(obs, (w, h))

    vertical = direction in ("up", "down")
    lo, hi = (y1, y2) if vertical else (x1, x2)
    full = h if vertical else w
    room = max(1, hi - lo)

    # 步幅：容器高度的 ratio，且不超过整屏 45%；容器特别矮时也至少动 10% 屏
    ratio = min(max(float(ratio or 0.35), 0.12), 0.6)
    dist = int(min(room * ratio, full * 0.45))
    dist = max(dist, int(full * 0.10))
    dist = min(dist, max(1, room - 2)) if room > 12 else dist
    half = max(1, dist // 2)

    # 容器太矮（不到步幅的一点二倍）时退回整屏，但手势仍然居中，不从边上起手
    if room < dist * 1.2:
        lo, hi = 0, full
    mid = (lo + hi) // 2
    edge = int(min(full * 0.12, 72))
    cx = min(max((x1 + x2) // 2, 8), max(8, w - 8))
    cy = min(max((y1 + y2) // 2, 8), max(8, h - 8))

    if vertical:
        a, b = (mid + half, mid - half) if direction == "up" else (mid - half, mid + half)
        if max(a, b) > h - edge:
            a, b = h - edge, h - edge - dist
        if min(a, b) < edge:
            a, b = edge, edge + dist
        driver.swipe_xy(cx, a, cx, b, ms)
    else:
        a, b = (mid + half, mid - half) if direction == "left" else (mid - half, mid + half)
        if max(a, b) > w - edge:
            a, b = w - edge, w - edge - dist
        if min(a, b) < edge:
            a, b = edge, edge + dist
        driver.swipe_xy(a, cy, b, cy, ms)


def _gesture_area(obs: Observation, screen: tuple[int, int]) -> tuple[int, int, int, int]:
    """面积最大的可滚容器；找不到就用整屏。"""
    w, h = screen
    best: UINode | None = None
    for n in obs.root.walk():
        if not (n.scrollable or _SCROLLY.search(n.cls or "")):
            continue
        x1, y1, x2, y2 = n.bounds
        if x2 - x1 < w * 0.3 or y2 - y1 < h * 0.12:
            continue   # 太小的"列表"多半是横向标签条，手势落上去没用
        if best is None or n.area > best.area:
            best = n
    return best.bounds if best is not None else (0, 0, w, h)


__all__ = ["ScrollOutcome", "find_by_scrolling", "has_scrollable", "observe_tree", "trail_text"]
