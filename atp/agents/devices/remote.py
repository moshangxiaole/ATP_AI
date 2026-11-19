"""远程真机池接入。

对上层只暴露三件事：列设备、占用、归还。具体是自建农机房、云真机厂商还是
一堆 adb connect 的机器，都不影响用例与调度。
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from abc import ABC, abstractmethod

from atp.config import Config, RemoteProviderConfig, tool_path
from atp.common.types import DeviceInfo


class RemoteDeviceProvider(ABC):
    def __init__(self, cfg: RemoteProviderConfig, config: Config):
        self.cfg = cfg
        self.config = config
        self.name = cfg.name
        self.platform = cfg.platform

    @abstractmethod
    def list_devices(self) -> list[DeviceInfo]: ...

    def allocate(self, platform: str) -> DeviceInfo | None:
        """默认策略：挑一台 ready 的。农机房通常需要独占分配，子类覆盖。"""
        for d in self.list_devices():
            if d.platform == platform and d.status in ("ready", "available", "idle"):
                return d
        return None

    def release(self, device_id: str) -> None:
        return None


class AdbTcpProvider(RemoteDeviceProvider):
    """用 adb connect 把网络上的 Android 真机接进来。

    适合内网机架：机器已开机、开了无线调试，控制端能 ssh/路由到。
    缺点是要自己维护 host 列表，优点是零改造、零成本。
    """

    def __init__(self, cfg: RemoteProviderConfig, config: Config):
        super().__init__(cfg, config)
        self.adb = config.adb or tool_path("adb") or "adb"

    def _connect(self) -> None:
        for host in self.cfg.hosts:
            try:
                subprocess.run([self.adb, "connect", host], capture_output=True, timeout=10)
            except Exception:
                continue

    def list_devices(self) -> list[DeviceInfo]:
        self._connect()
        try:
            out = subprocess.run([self.adb, "devices"], capture_output=True, timeout=15)
        except Exception:
            return []
        devices = []
        for line in out.stdout.decode("utf-8", "ignore").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serial = parts[0]
                if serial in self.cfg.hosts:
                    devices.append(
                        DeviceInfo(
                            platform="android", device_id=serial,
                            name=f"remote-{serial}", kind="remote", provider=self.name,
                        )
                    )
        return devices


class HttpFarmProvider(RemoteDeviceProvider):
    """通用 HTTP 农机房适配器。

    约定三个端点（换成你们现有平台时，只要在外面包一层即可）：
      GET  {base}/devices                  -> [{"device_id","platform","status","name"}]
      POST {base}/allocate  {"platform"}   -> {"device_id","platform","name"}
      POST {base}/release   {"device_id"}  -> {}
    """

    def __init__(self, cfg: RemoteProviderConfig, config: Config):
        super().__init__(cfg, config)
        self.base = cfg.base_url.rstrip("/")

    def _call(self, method: str, path: str, payload: dict | None = None, timeout: int = 20):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.cfg.token}"} if self.cfg.token else {}),
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
        return json.loads(body) if body.strip() else {}

    def list_devices(self) -> list[DeviceInfo]:
        try:
            rows = self._call("GET", "/devices")
        except Exception:
            return []
        if isinstance(rows, dict):
            rows = rows.get("devices", [])
        return [
            DeviceInfo(
                platform=r.get("platform", self.platform),
                device_id=r.get("device_id") or r.get("id", ""),
                name=r.get("name", ""),
                kind="remote",
                status=r.get("status", "ready"),
                provider=self.name,
            )
            for r in rows
        ]

    def allocate(self, platform: str) -> DeviceInfo | None:
        try:
            r = self._call("POST", "/allocate", {"platform": platform})
        except Exception:
            return None
        did = r.get("device_id") or r.get("id")
        if not did:
            return None
        return DeviceInfo(
            platform=r.get("platform", platform), device_id=did,
            name=r.get("name", ""), kind="remote", provider=self.name,
        )

    def release(self, device_id: str) -> None:
        try:
            self._call("POST", "/release", {"device_id": device_id})
        except Exception:
            pass


def build_providers(config: Config) -> list[RemoteDeviceProvider]:
    out: list[RemoteDeviceProvider] = []
    for cfg in config.remote_providers:
        if cfg.kind == "adb_tcp":
            out.append(AdbTcpProvider(cfg, config))
        elif cfg.kind == "http_farm":
            out.append(HttpFarmProvider(cfg, config))
    return out
