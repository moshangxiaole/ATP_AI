"""三端分层回归：端能力矩阵 + 驱动构造 + 设备发现。

这套测试盯着"端知识只有一处"这条约束：
- 端策略集中在 `atp/common/profiles.py`，取值必须和历史上的硬编码判断一致；
- 驱动构造统一走 `atp.agents.platforms.build`，执行链里不再出现平台名；
- 设备发现按端拆分后，本机仍然稳定给出 桌面 + 浏览器 两台虚拟设备。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.agents.platforms import build, discover  # noqa: E402
from atp.agents.platforms.base import BaseDriver, NotSupported  # noqa: E402
from atp.agents.platforms.mobile import AndroidAdbDriver, HarmonyHdcDriver  # noqa: E402
from atp.agents.platforms.web import WebCdpDriver  # noqa: E402
from atp.common.profiles import (MOBILE, PC, UNKNOWN, WEB, all_profiles,
                                 profile_for)  # noqa: E402
from atp.config import Config  # noqa: E402


class ProfileTest(unittest.TestCase):
    def test_platforms_map_to_three_ends(self):
        self.assertIs(profile_for("android"), MOBILE)
        self.assertIs(profile_for("harmony"), MOBILE)
        self.assertIs(profile_for("ios"), MOBILE)
        self.assertIs(profile_for("desktop"), PC)
        self.assertIs(profile_for("web"), WEB)

    def test_unknown_platform_is_conservative(self):
        self.assertIs(profile_for("mock"), UNKNOWN)
        self.assertIs(profile_for(""), UNKNOWN)
        self.assertIs(profile_for(None), UNKNOWN)
        # 未知端不能凭空获得任何"特权"：不冷启动、不录屏、不去设备上找应用
        self.assertFalse(UNKNOWN.cold_start)
        self.assertFalse(UNKNOWN.record_video)
        self.assertFalse(UNKNOWN.discover_apps)
        self.assertTrue(UNKNOWN.scroll_needs_container)

    def test_mobile_owns_mobile_only_behaviour(self):
        self.assertTrue(MOBILE.cold_start)
        self.assertTrue(MOBILE.page_gate)
        self.assertTrue(MOBILE.occluded_aware)
        self.assertTrue(MOBILE.deviation_recovery)
        self.assertTrue(MOBILE.popup_memory)
        self.assertFalse(MOBILE.record_video)      # 真实设备靠投屏，不录 MP4
        self.assertFalse(MOBILE.maximize_after_open)
        for profile in (PC, WEB):
            self.assertFalse(profile.cold_start)
            self.assertFalse(profile.page_gate)
            self.assertFalse(profile.occluded_aware)
            self.assertFalse(profile.popup_memory)

    def test_pc_and_web_record_and_maximize(self):
        for profile in (PC, WEB):
            self.assertTrue(profile.record_video)
            self.assertTrue(profile.maximize_after_open)
            self.assertTrue(profile.url_entry)

    def test_only_pc_skips_container_check(self):
        # 桌面 swipe 是滚轮，滚不动没有副作用，所以不必先证明"可滑"
        self.assertFalse(PC.scroll_needs_container)
        self.assertTrue(WEB.scroll_needs_container)
        self.assertTrue(MOBILE.scroll_needs_container)

    def test_web_does_not_use_app_registry(self):
        self.assertFalse(WEB.app_by_name)
        self.assertTrue(PC.app_by_name)     # PC 认 exe 路径
        self.assertTrue(MOBILE.app_by_name)  # 移动端认包名

    def test_registry_is_consistent(self):
        profiles = all_profiles()
        self.assertEqual([p.key for p in profiles], ["mobile", "pc", "web"])
        seen: dict[str, str] = {}
        for profile in profiles:
            self.assertTrue(profile.platforms)
            for platform in profile.platforms:
                self.assertNotIn(platform, seen, f"{platform} 同时属于两个端")
                seen[platform] = profile.key


class DriverBuildTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_mobile_builders(self):
        self.assertIsInstance(build("android", "emulator-5554", self.cfg), AndroidAdbDriver)
        self.assertIsInstance(build("harmony", "H0D6R2", self.cfg), HarmonyHdcDriver)

    def test_web_builder(self):
        driver = build("web", "chromium", self.cfg)
        self.assertIsInstance(driver, WebCdpDriver)
        self.assertEqual(driver.platform, "web")

    def test_pc_builder_returns_a_driver(self):
        driver = build("desktop", "desktop", self.cfg)
        self.assertIsInstance(driver, BaseDriver)
        self.assertEqual(driver.platform, "desktop")

    def test_unknown_platform_raises(self):
        with self.assertRaises(NotSupported):
            build("mock", "x", self.cfg)


class DiscoverTest(unittest.TestCase):
    def test_local_virtual_devices_always_listed(self):
        found = {d.platform for d in discover(Config())}
        self.assertIn("desktop", found)
        self.assertIn("web", found)


if __name__ == "__main__":
    unittest.main()
