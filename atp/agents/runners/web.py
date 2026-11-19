"""WEB端首跑入口。

只放该端专属的执行策略；共享引擎见 ``runners/core.py``。
统一入口 ``agents/runner.py`` 按 driver.platform 分发到这里。
"""

from __future__ import annotations

from atp.common.profiles import WEB, PlatformProfile
from atp.agents.runners.core import RunnerCore

class WebRunner(RunnerCore):
    """WEB 端首跑：URL 入口，录屏与最大化，不走应用台账。"""

    @property
    def profile(self) -> PlatformProfile:
        return WEB

    def _resolve_app_name(self, nl, case) -> None:
        """WEB 端以 URL 为入口，没有「应用名 → 包名」"""
        return None
