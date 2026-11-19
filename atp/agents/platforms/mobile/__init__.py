"""移动端（Android / HarmonyOS）。

两件事归这里：
1. 端策略 PROFILE —— 冷启动、页面门禁、遮挡判定、去设备上找应用；
2. 端接入 —— 设备发现与驱动构造（adb / hdc）。
"""

from __future__ import annotations

import subprocess

from atp.agents.platforms.mobile.android import AndroidAdbDriver
from atp.agents.platforms.mobile.harmony import HarmonyHdcDriver
from atp.common.profiles import MOBILE
from atp.common.types import DeviceInfo
from atp.config import Config

PROFILE = MOBILE


def discover(config: Config) -> list[DeviceInfo]:
    return _android(config) + _harmony(config)


def build(platform: str, device_id: str, config: Config, **kwargs) -> AndroidAdbDriver | HarmonyHdcDriver | None:
    """按平台造驱动；不是移动端返回 None，交给下一个端包判断。"""
    if platform == "android":
        return AndroidAdbDriver(device_id, config)
    if platform == "harmony":
        return HarmonyHdcDriver(device_id, config)
    return None


def _android(config: Config) -> list[DeviceInfo]:
    adb = config.adb
    if not adb:
        return []
    try:
        proc = subprocess.run([adb, "devices"], capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001 —— 没装 adb / 超时都只是"这台机器上没有安卓设备"
        return []
    out: list[DeviceInfo] = []
    for line in proc.stdout.decode("utf-8", "ignore").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serial = parts[0]
            remote = ":" in serial
            out.append(DeviceInfo(
                platform="android",
                device_id=serial,
                name=("remote-" if remote else "") + serial,
                kind="remote" if remote else "local",
            ))
    return out


def _harmony(config: Config) -> list[DeviceInfo]:
    hdc = config.hdc
    if not hdc:
        return []
    try:
        proc = subprocess.run([hdc, "list", "targets"], capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001
        return []
    out: list[DeviceInfo] = []
    for line in proc.stdout.decode("utf-8", "ignore").splitlines():
        sid = line.strip()
        if not sid or "Empty" in sid or "[" in sid:
            continue
        out.append(DeviceInfo(platform="harmony", device_id=sid,
                              name=f"harmony-{sid}", kind="local"))
    return out


__all__ = ["PROFILE", "discover", "build", "AndroidAdbDriver", "HarmonyHdcDriver"]
