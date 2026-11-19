"""弹窗自动处置的回归测试。

这一层的核心指标不是"能关弹窗"，而是**不点错**：
误点一次可能签到、下单、开通会员，比用例失败严重得多。
所以测试里大半是"必须拒绝"的用例。

不依赖真机、不依赖模型：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.memory.popups import PopupGuard, dismiss_popups  # noqa: E402
from atp.common.types import Observation, UINode  # noqa: E402
from atp.agents.perception.vision import frame_diff_ratio  # noqa: E402


def screen(*items: tuple[str, tuple[int, int, int, int]]) -> Observation:
    """按 (文案, 边界) 拼一棵单层树。"""
    root = UINode(cls="Root", bounds=(0, 0, 1256, 2760))
    for text, bounds in items:
        root.children.append(UINode(cls="Text", text=text, bounds=bounds))
    return Observation(platform="harmony", device_id="hm", width=1256, height=2760,
                       root=root)


def android_tree(*nodes: UINode, width: int = 1260, height: int = 2800,
                 wrapper_cls: str = "Root") -> Observation:
    root = UINode(cls="Root", bounds=(0, 0, width, height))
    holder = UINode(cls=wrapper_cls, bounds=(0, 0, width, height))
    holder.children.extend(nodes)
    root.children.append(holder)
    return Observation(platform="android", device_id="ad", width=width, height=height,
                       root=root)


def icon_close(name: str = "com.app:id/btnClose",
               bounds: tuple[int, int, int, int] = (577, 1892, 682, 1997)) -> UINode:
    return UINode(cls="View", id=name, bounds=bounds, clickable=True)


class TestIconClose(unittest.TestCase):
    """无文字的纯 X 按钮。

    安卓上大量弹窗的关闭按钮就是一个 `<View id=.../btnClose>`，没有文案。
    文字白名单对它无能为力，所以要有一条 id 通道——但必须是**兜底**，
    有文字可依的时候绝不猜。
    """

    def test_icon_close_found_when_no_text(self):
        obs = android_tree(icon_close(),
                           UINode(cls="ImageView", id="com.app:id/ivPromotion",
                                  bounds=(105, 789, 1155, 1839), clickable=True))
        hits = PopupGuard().find(obs)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "close_id")
        self.assertEqual((hits[0].x, hits[0].y), (629, 1944))

    def test_text_channel_wins_over_icon(self):
        # 同时存在「我知道了」和 btnClose 时，只认文字那个：可读语义永远优先
        obs = android_tree(
            UINode(cls="Button", id="com.app:id/btnConfirm", text="我知道了",
                   bounds=(63, 2429, 1197, 2583), clickable=True),
            icon_close(),
        )
        hits = PopupGuard().find(obs)
        self.assertEqual([h.kind for h in hits], ["close"])
        self.assertEqual(hits[0].keyword, "我知道了")

    def test_big_close_named_button_is_not_icon(self):
        # 名字里带 close 但尺寸像整条按钮 → 不是角标 X，图标通道必须拒绝
        obs = android_tree(icon_close("com.app:id/btnClose",
                                      (63, 2429, 1197, 2583)))
        self.assertEqual(PopupGuard().find(obs), [])

    def test_icon_close_inside_list_is_ignored(self):
        # 列表里的 close 更像"删掉这一条"，不属于弹窗处置
        obs = android_tree(icon_close(), wrapper_cls="RecyclerView")
        self.assertEqual(PopupGuard().find(obs), [])


class TestOffscreen(unittest.TestCase):
    """隐藏节点不能参与处置。

    真机回归：示例视频应用 详情页藏着一个 bounds=(-2,0,...) 的「跳过前贴片广告」，
    白名单一命中就去点 (-2,0)——那会拉到系统状态栏，把用例带到别处。
    """

    def test_negative_bounds_ignored(self):
        obs = android_tree(UINode(cls="Text", text="跳过前贴片广告",
                                  bounds=(-2, 0, 0, 0), clickable=True))
        self.assertEqual(PopupGuard().find(obs), [])

    def test_zero_sized_ignored(self):
        obs = android_tree(UINode(cls="Text", text="关闭", bounds=(100, 100, 100, 100),
                                  clickable=True))
        self.assertEqual(PopupGuard().find(obs), [])

    def test_fully_below_screen_ignored(self):
        obs = android_tree(UINode(cls="Text", text="跳过", bounds=(100, 2900, 300, 2960),
                                  clickable=True))
        self.assertEqual(PopupGuard().find(obs), [])

    def test_normal_skip_still_works(self):
        # 反证：可见的「跳过」照常命中，别把修复做成"全都不认"
        obs = android_tree(UINode(cls="Text", text="跳过", bounds=(1000, 200, 1200, 260),
                                  clickable=True))
        hits = PopupGuard().find(obs)
        self.assertEqual([h.keyword for h in hits], ["跳过"])

    def test_immediate_purchase_is_danger(self):
        # 「立刻购买」是示例视频应用 详情页的实际文案，必须和「立即购买」同等对待
        obs = android_tree(UINode(cls="TextView", text="立刻购买",
                                  bounds=(941, 880, 1207, 992), clickable=True))
        self.assertEqual(PopupGuard().find(obs), [])


class TestPopupRefuse(unittest.TestCase):
    """必须拒绝的：诱导位、危险词混排。"""

    def test_membership_skip_bundle_is_refused(self):
        # 示例视频应用 真实结构：一个 Text + 一个可点 Row，点"跳过此广告"的位置会跳会员页
        o = screen(("加入会员 跳过此广告", (586, 178, 1008, 269)))
        self.assertEqual(PopupGuard().find(o), [])

    def test_confirm_payment_never_clicked(self):
        o = screen(("确认支付", (300, 2400, 950, 2520)))
        self.assertEqual(PopupGuard().find(o), [])

    def test_cancel_alone_is_not_a_popup(self):
        # 正常表单里的"取消"会丢数据，不属于弹窗处置范围
        o = screen(("取消", (100, 2400, 400, 2500)))
        self.assertEqual(PopupGuard().find(o), [])

    def test_mixed_danger_blocks_whole_node(self):
        o = screen(("立即开通 关闭", (200, 1200, 1000, 1300)))
        self.assertEqual(PopupGuard().find(o), [])


class TestPopupAccept(unittest.TestCase):
    def test_skip_ad(self):
        o = screen(("跳过此广告", (900, 178, 1080, 269)))
        hits = PopupGuard().find(o)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "skip")
        self.assertEqual((hits[0].x, hits[0].y), (990, 223))

    def test_close_and_later(self):
        for word in ("关闭", "我知道了", "以后再说", "暂不更新"):
            hits = PopupGuard().find(screen((word, (400, 1500, 900, 1600))))
            self.assertEqual(len(hits), 1, word)
            self.assertEqual(hits[0].kind, "close", word)

    def test_permission_dialog_allows(self):
        hits = PopupGuard().find(screen(("仅在使用时允许", (600, 1600, 1200, 1700))))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "allow")

    def test_skip_has_priority_over_close(self):
        o = screen(("关闭", (100, 1200, 300, 1300)),
                   ("跳过", (900, 1200, 1100, 1300)))
        hits = PopupGuard().find(o)
        self.assertEqual(hits[0].kind, "skip")

    def test_limit_caps_hits(self):
        o = screen(*[(f"我知道了{i}", (100, 1000 + i * 100, 600, 1050 + i * 100))
                     for i in range(5)])
        self.assertEqual(len(PopupGuard(limit=2).find(o)), 2)


class FadingDriver:
    """抓树时弹窗还在、再抓一次就没了——模拟"广告自己播完了"。

    真机血案：开屏广告的「跳过」在 (1084,225)，扫描时它还在，点下去时广告已经结束，
    这一点正好是首页的搜索入口，于是用例从"等首页"变成了"进搜索页"。
    """

    def __init__(self, first: Observation, second: Observation):
        self.frames = [first, second]
        self.i = 1        # 传入的 obs 已经是第一帧，之后每次都抓到下一帧
        self.taps: list[tuple[int, int]] = []

    def screen_size(self) -> tuple[int, int]:
        return (1080, 1920)

    def dump_tree(self):
        n = self.frames[min(self.i, len(self.frames) - 1)]
        self.i += 1
        return n.root

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class StaleDriver:
    """界面一直在动（广告/转场），抓树抓不到新的，只能拿缓存。

    这种树上看到的坐标可能已经是两三秒前的界面，拿它点击等于闭眼戳屏幕。
    """

    tree_stale = True

    def __init__(self, obs: Observation):
        self.obs = obs
        self.taps: list[tuple[int, int]] = []

    def screen_size(self) -> tuple[int, int]:
        return (1080, 1920)

    def dump_tree(self):
        return self.obs.root

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class TestPopupRace(unittest.TestCase):
    def test_vanished_popup_is_not_tapped(self):
        with_ad = screen(("跳过", (980, 180, 1100, 270)))
        home = screen(("首页", (40, 2600, 200, 2700)))
        drv = FadingDriver(with_ad, home)
        obs, hits = dismiss_popups(drv, with_ad, PopupGuard())
        self.assertEqual(drv.taps, [])
        self.assertEqual(hits, [])
        self.assertTrue(obs.root.find_by_text("首页"))   # 返回的是最新的那一屏

    def test_uses_fresh_coordinates(self):
        # 广告还在，但位置变了：必须点新位置，不能拿旧坐标戳下去
        old = screen(("跳过", (200, 180, 320, 270)))
        moved = screen(("跳过", (900, 300, 1020, 390)))
        drv = FadingDriver(old, moved)
        _, hits = dismiss_popups(drv, old, PopupGuard())
        self.assertEqual(len(hits), 1)
        self.assertEqual(drv.taps, [(960, 345)])

    def test_stale_tree_blocks_the_tap(self):
        ad = screen(("跳过", (980, 180, 1100, 270)))
        drv = StaleDriver(ad)
        obs, hits = dismiss_popups(drv, ad, PopupGuard())
        self.assertEqual(drv.taps, [])
        self.assertEqual(hits, [])


class TestPopupStateText(unittest.TestCase):
    """真机血案回归：设置列表里「蓝牙, 已关闭」被当成关闭按钮，一点就跳进蓝牙页。

    这类误点的代价比"没关掉弹窗"高一个数量级：整条用例的界面从此错位。
    所以判不清的时候一律按描述处理。
    """

    def test_state_text_is_not_a_close_button(self):
        for label in ("蓝牙, 已关闭", "已关闭", "自动关闭", "WLAN, KLYG", "无法关闭"):
            self.assertEqual(
                PopupGuard().find(screen((label, (100, 1200, 1100, 1300)))), [], label)

    def test_real_close_button_still_works(self):
        hits = PopupGuard().find(screen(("关闭", (900, 300, 1100, 400))))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "close")


class TestPopupPoint(unittest.TestCase):
    def test_point_lands_inside_keyword_when_shared_node(self):
        # "取消   确定" 塞在一个节点里：点"确定"必须落在右半边
        o = screen(("取消     确定", (200, 1200, 1000, 1300)))
        hits = PopupGuard(extra_close_words=["确定"]).find(o)
        self.assertTrue(hits)
        self.assertGreater(hits[0].x, 600)

    def test_extra_words_work(self):
        hits = PopupGuard(extra_close_words=["下次再说吧"]).find(
            screen(("下次再说吧", (300, 1500, 900, 1600))))
        self.assertEqual(len(hits), 1)


class FakeDriver:
    """只记录被点了哪儿，并允许"点完之后界面变了"。"""

    def __init__(self, before: Observation, after: Observation | None = None):
        self.obs = before
        self.after = after or before
        self.taps: list[tuple[int, int]] = []

    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def observe(self) -> Observation:
        return self.obs if not self.taps else self.after


class TestDismissFlow(unittest.TestCase):
    def test_tap_then_reobserve(self):
        before = screen(("跳过此广告", (900, 178, 1080, 269)))
        after = screen(("我的收藏", (100, 400, 800, 500)))
        drv = FakeDriver(before, after)
        events: list[dict] = []

        obs, hits = dismiss_popups(drv, drv.obs, PopupGuard(), on_event=events.append)

        self.assertEqual(drv.taps, [(990, 223)])
        self.assertEqual(len(hits), 1)
        self.assertEqual(events[0]["type"], "popup_dismissed")
        self.assertEqual(events[0]["keyword"], "跳过此广告")
        self.assertIs(obs, after)

    def test_no_hit_no_tap(self):
        drv = FakeDriver(screen(("推荐", (100, 400, 800, 500))))
        obs, hits = dismiss_popups(drv, drv.obs, PopupGuard())
        self.assertEqual(drv.taps, [])
        self.assertEqual(hits, [])
        self.assertIs(obs, drv.obs)

    def test_guard_none_is_noop(self):
        drv = FakeDriver(screen(("跳过此广告", (900, 178, 1080, 269))))
        obs, hits = dismiss_popups(drv, drv.obs, None)
        self.assertEqual(drv.taps, [])
        self.assertEqual(hits, [])


class TestMotion(unittest.TestCase):
    """assert_motion 的底层度量：播放中的画面变化要明显大于静止页面。"""

    @staticmethod
    def _img(seed: int, shift: int = 0) -> bytes:
        from PIL import Image

        w = h = 64
        buf = io.BytesIO()
        img = Image.new("L", (w, h))
        px = img.load()
        for y in range(h):
            for x in range(w):
                px[x, y] = (x * 3 + y * 5 + seed + (40 if shift and (x // 8 + shift) % 3 == 0 else 0)) % 256
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_static_frames_near_zero(self):
        a = self._img(0)
        self.assertLess(frame_diff_ratio(a, a), 0.001)

    def test_changed_frames_detected(self):
        a, b = self._img(0), self._img(0, shift=1)
        self.assertGreater(frame_diff_ratio(a, b), 0.1)

    def test_size_mismatch_returns_zero(self):
        from PIL import Image

        buf = io.BytesIO()
        Image.new("L", (32, 32), 10).save(buf, format="PNG")
        self.assertEqual(frame_diff_ratio(self._img(0), buf.getvalue()), 0.0)


if __name__ == "__main__":
    unittest.main()
