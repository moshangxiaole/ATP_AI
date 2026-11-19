"""PC端回放入口。

只放该端专属的执行策略；共享引擎见 ``executors/core.py``。
统一入口 ``agents/executor.py`` 按 driver.platform 分发到这里。
"""

from __future__ import annotations

from atp.common.profiles import PC, PlatformProfile
from atp.agents.executors.core import ReplayCore


class PcExecutor(ReplayCore):
    """桌面端回放：窗口录屏，无冷启动/偏离回退。"""

    @property
    def profile(self) -> PlatformProfile:
        return PC
