"""WEB 端（浏览器页面）。

WEB 的入口是 URL：先 open_url，再最大化固定画布，坐标归一化才有意义。
接管已有浏览器实例时复用 Electron 的 CDP 驱动，只是把 platform 标成 web。
"""

from __future__ import annotations

from atp.agents.platforms.pc.electron import ElectronCdpDriver
from atp.agents.platforms.web.cdp import WebCdpDriver
from atp.common.profiles import WEB
from atp.common.types import DeviceInfo
from atp.config import Config

PROFILE = WEB


def discover(config: Config) -> list[DeviceInfo]:
    return [DeviceInfo(platform="web", device_id="chromium",
                       name="Chromium", kind="virtual")]


def build(platform: str, device_id: str, config: Config, **kwargs):
    if platform != "web":
        return None
    endpoint = kwargs.get("endpoint", "")
    if endpoint:
        # 接管已开的浏览器/Electron 实例：原生 CDP 兼容老版本 Chromium
        driver = ElectronCdpDriver(device_id, config, endpoint=endpoint,
                                   url=kwargs.get("url", ""))
        driver.platform = "web"
        return driver
    return WebCdpDriver(device_id, config, endpoint=endpoint, url=kwargs.get("url", ""))


__all__ = ["PROFILE", "discover", "build", "WebCdpDriver"]
