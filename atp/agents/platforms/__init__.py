"""三端注册表。

对外只暴露三件事：
- ``profile_for(platform)``：拿到这个端该有的策略（要不要冷启动、录屏、遮挡判定…）；
- ``build(...)``：按平台造驱动；
- ``discover(config)``：按端发现设备。

新增一个端 = 加一个子包（PROFILE + discover + build）+ 在 GROUPS 里加一行。
"""

from __future__ import annotations

from atp.agents.platforms import mobile, pc, web
from atp.agents.platforms.base import BaseDriver, NotSupported
from atp.common.profiles import (MOBILE, PC, WEB, PlatformProfile, all_profiles,
                                 profile_for)
from atp.common.types import DeviceInfo
from atp.config import Config

GROUPS = (mobile, pc, web)


def discover(config: Config) -> list[DeviceInfo]:
    out: list[DeviceInfo] = []
    for group in GROUPS:
        out.extend(group.discover(config))
    return out


def build(platform: str, device_id: str, config: Config, **kwargs) -> BaseDriver:
    for group in GROUPS:
        driver = group.build(platform, device_id, config, **kwargs)
        if driver is not None:
            return driver
    raise NotSupported(f"未知平台 {platform}")


__all__ = ["GROUPS", "BaseDriver", "NotSupported", "PlatformProfile",
           "MOBILE", "PC", "WEB", "all_profiles", "profile_for",
           "discover", "build", "mobile", "pc", "web"]
