"""PC端首跑入口。

只放该端专属的执行策略；共享引擎见 ``runners/core.py``。
统一入口 ``agents/runner.py`` 按 driver.platform 分发到这里。
"""

from __future__ import annotations

from atp.common.profiles import PC, PlatformProfile
from atp.agents.runners.core import RunnerCore

class PcRunner(RunnerCore):
    """桌面端首跑：本地应用台账、窗口最大化与录屏。"""

    @property
    def profile(self) -> PlatformProfile:
        return PC
