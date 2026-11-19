"""移动端首跑入口。

只放该端专属的执行策略；共享引擎见 ``runners/core.py``。
统一入口 ``agents/runner.py`` 按 driver.platform 分发到这里。
"""

from __future__ import annotations

from atp.common.profiles import MOBILE, PlatformProfile
from atp.agents.runners.core import RunnerCore

class MobileRunner(RunnerCore):
    """移动端首跑：包名解析、冷启动、页面门禁。"""

    @property
    def profile(self) -> PlatformProfile:
        return MOBILE
