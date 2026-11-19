"""端能力矩阵：PC / 移动 / WEB 三端的执行策略。

平台差异不该以 `platform in ("android", "harmony")` 的形式散落在执行代码里，
那样"加一个端"要改十几处。这里把差异收敛成一张表：执行链路只问
`profile_for(platform).xxx`，具体取值由端决定。

放 `common` 而不是 `agents`，是因为 memory（弹窗记忆）、runs（录屏）、
cli（应用台账同步）也要问同一张表，而它们不应该为了一个布尔值去 import 驱动。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformProfile:
    """一个端的执行策略。字段都是布尔开关，默认值是"最保守的通用端"。"""

    key: str                      # pc | mobile | web
    name: str                     # 展示名
    platforms: tuple[str, ...]    # 归入本端的 driver.platform

    # ---------- 启动 ----------
    cold_start: bool = False      # 打开应用前先结束进程，造出确定的初始状态
    app_by_name: bool = False     # 「打开 xx」能翻成启动目标（包名 / exe 路径）
    discover_apps: bool = False   # 台账没登记时，允许去设备上找一次
    url_entry: bool = True        # 入口是 URL（open_url）
    virtual: bool = False         # 不需要真机发现，直接就是一台虚拟设备

    # ---------- 观察 ----------
    page_gate: bool = False       # 规划前等页面加载完成（树-only 门禁）
    occluded_aware: bool = False  # 排除"被底部固定栏盖住"的目标
    scroll_needs_container: bool = True   # 滑动前要求树里有 scrollable 容器
    deviation_recovery: bool = False      # 误触/进错页时自动 BACK 回退
    popup_memory: bool = False    # 弹窗长程记忆与快路径（只对移动端有意义）

    # ---------- 产物 ----------
    record_video: bool = False    # 执行过程录屏（PC / WEB 有窗口可录）
    maximize_after_open: bool = False     # 打开页面后立刻最大化并重采画布

    def __contains__(self, platform: object) -> bool:
        return platform in self.platforms


MOBILE = PlatformProfile(
    key="mobile",
    name="移动端",
    platforms=("android", "harmony", "ios"),
    cold_start=True,
    app_by_name=True,
    discover_apps=True,
    url_entry=False,
    page_gate=True,
    occluded_aware=True,
    scroll_needs_container=True,
    deviation_recovery=True,
    popup_memory=True,
    record_video=False,
    maximize_after_open=False,
)

PC = PlatformProfile(
    key="pc",
    name="PC 端",
    platforms=("desktop",),
    cold_start=False,
    app_by_name=True,        # 应用名 → 台账里的 exe 路径
    discover_apps=False,     # 桌面没有包管理器，只认台账
    url_entry=True,
    virtual=True,
    page_gate=False,
    occluded_aware=False,
    # 桌面 swipe 是鼠标滚轮，滚不动也没有副作用，不必先证明"可滑"
    scroll_needs_container=False,
    deviation_recovery=False,
    popup_memory=False,
    record_video=True,
    maximize_after_open=True,
)

WEB = PlatformProfile(
    key="web",
    name="WEB 端",
    platforms=("web",),
    cold_start=False,
    app_by_name=False,       # 用 URL 打开，不走应用台账
    discover_apps=False,
    url_entry=True,
    virtual=True,
    page_gate=False,
    occluded_aware=False,
    scroll_needs_container=True,
    deviation_recovery=False,
    popup_memory=False,
    record_video=True,
    maximize_after_open=True,
)

UNKNOWN = PlatformProfile(key="unknown", name="未知端", platforms=())

_ALL: tuple[PlatformProfile, ...] = (MOBILE, PC, WEB)
_REGISTRY: dict[str, PlatformProfile] = {
    platform: profile for profile in _ALL for platform in profile.platforms
}


def profile_for(platform: str | None) -> PlatformProfile:
    return _REGISTRY.get(str(platform or ""), UNKNOWN)


def all_profiles() -> list[PlatformProfile]:
    return list(_ALL)


__all__ = ["PlatformProfile", "MOBILE", "PC", "WEB", "UNKNOWN",
           "profile_for", "all_profiles"]
