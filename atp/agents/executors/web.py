"""WEB端回放入口。

只放该端专属的执行策略；共享引擎见 ``executors/core.py``。
统一入口 ``agents/executor.py`` 按 driver.platform 分发到这里。
"""

from __future__ import annotations

from atp.common.profiles import WEB, PlatformProfile
from atp.agents.executors.core import ReplayCore


class WebExecutor(ReplayCore):
    """WEB 端回放：URL 入口与窗口录屏，应用台账不生效。"""

    @property
    def profile(self) -> PlatformProfile:
        return WEB

    def _resolve_app_step(self, step, idx: int, case):
        """WEB 端以 URL 为入口，没有「应用名 → 包名」这一步。"""
        return ""
