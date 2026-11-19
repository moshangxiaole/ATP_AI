"""设备池：本地 + 远程统一调度。

端相关的知识（怎么发现设备、怎么造驱动）都放在 ``atp.agents.platforms`` 的
三个端包里；这里只负责**编排**：先列本机三端设备，再挂上远程真机池，
取用时按 platform 转给对应端包。
"""

from __future__ import annotations

import contextlib
from typing import Iterator

from atp.agents.devices.remote import RemoteDeviceProvider, build_providers
from atp.agents.platforms import BaseDriver, NotSupported, mobile, pc, web
from atp.common.profiles import profile_for
from atp.common.types import DeviceInfo
from atp.config import Config


class DeviceNotFound(RuntimeError):
    pass


class DeviceManager:
    def __init__(self, config: Config):
        self.config = config
        self.providers: list[RemoteDeviceProvider] = build_providers(config)

    # ---------- 发现 ----------
    def discover(self) -> list[DeviceInfo]:
        """本机三端 + 远程池。顺序固定：移动端、远程、PC、WEB，控制台列表才稳定。"""
        out: list[DeviceInfo] = []
        out.extend(mobile.discover(self.config))
        for provider in self.providers:
            out.extend(provider.list_devices())
        out.extend(pc.discover(self.config))
        out.extend(web.discover(self.config))
        return out

    # ---------- 取用 ----------
    def resolve(self, platform: str, device_id: str = "", prefer_remote: bool = False) -> DeviceInfo:
        if device_id:
            return DeviceInfo(platform=platform, device_id=device_id, name=device_id)

        # PC / WEB 是"本机虚拟设备"，不需要探测直接给
        if profile_for(platform).virtual:
            return DeviceInfo(platform=platform, device_id=device_id or platform, kind="virtual")

        local = [d for d in self.discover() if d.platform == platform]
        if prefer_remote:
            remote = [d for d in local if d.kind == "remote"]
            if remote:
                return remote[0]
        ready = [d for d in local if d.status in ("ready", "device", "available", "idle")]
        if ready:
            return ready[0]
        raise DeviceNotFound(
            f"没有可用的 {platform} 设备。已发现: "
            + (", ".join(f"{d.device_id}({d.kind})" for d in local) or "无")
        )

    def open(self, platform: str, device_id: str = "", prefer_remote: bool = False,
             **kwargs) -> BaseDriver:
        info = self.resolve(platform, device_id, prefer_remote)
        return self._build(info, **kwargs)

    def _build(self, info: DeviceInfo, **kwargs) -> BaseDriver:
        try:
            return _build_driver(info.platform, info.device_id, self.config, **kwargs)
        except NotSupported as e:
            raise DeviceNotFound(f"未知平台 {info.platform}") from e

    @contextlib.contextmanager
    def lease(self, platform: str, device_id: str = "", prefer_remote: bool = False,
              **kwargs) -> Iterator[BaseDriver]:
        """用完自动归还：远程设备会调 provider.release()。"""
        info = self.resolve(platform, device_id, prefer_remote)
        driver = self._build(info, **kwargs)
        driver.connect()
        try:
            yield driver
        finally:
            try:
                driver.close()
            finally:
                if info.kind == "remote":
                    for provider in self.providers:
                        provider.release(info.device_id)


def _build_driver(platform: str, device_id: str, config: Config, **kwargs) -> BaseDriver:
    from atp.agents.platforms import build as build_driver  # 延迟导入，避免循环

    return build_driver(platform, device_id, config, **kwargs)
