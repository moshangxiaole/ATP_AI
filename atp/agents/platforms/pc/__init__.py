"""PC 端（Windows 桌面应用 / Electron 客户端）。

PC 端的特点：没有"包名"概念，启动目标是本地 exe 路径；窗口可以录屏；
滑动是鼠标滚轮，滚不动也没有副作用，所以不强制要求 scrollable 容器。
"""

from __future__ import annotations

from atp.agents.platforms.pc.desktop import DesktopUiaDriver, DesktopVisionDriver, open_desktop
from atp.agents.platforms.pc.electron import ElectronCdpDriver
from atp.common.profiles import PC
from atp.common.types import DeviceInfo
from atp.config import Config

PROFILE = PC


def discover(config: Config) -> list[DeviceInfo]:
    return [DeviceInfo(platform="desktop", device_id="desktop",
                       name="本机桌面", kind="virtual")]


def build(platform: str, device_id: str, config: Config, **kwargs):
    """desktop 两种接法：已有 CDP 端点走 Electron，否则按 UIA / 视觉自动选档。"""
    if platform != "desktop":
        return None
    endpoint = kwargs.get("endpoint", "")
    if endpoint:
        return ElectronCdpDriver(device_id, config, endpoint=endpoint,
                                 url=kwargs.get("url", ""),
                                 match_url=kwargs.get("match_url", ""))
    return open_desktop(device_id, config,
                        endpoint=endpoint,
                        url=kwargs.get("url", ""),
                        window=kwargs.get("window", ""))


__all__ = ["PROFILE", "discover", "build",
           "DesktopUiaDriver", "DesktopVisionDriver", "ElectronCdpDriver", "open_desktop"]
