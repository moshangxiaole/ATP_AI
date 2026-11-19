"""设备池：本机三端设备 + 远程真机，统一按 platform 取用。"""

from atp.agents.devices.manager import DeviceManager, DeviceNotFound
from atp.agents.devices.remote import RemoteDeviceProvider, build_providers

__all__ = ["DeviceManager", "DeviceNotFound", "RemoteDeviceProvider", "build_providers"]
