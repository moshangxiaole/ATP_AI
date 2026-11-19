"""回放内核：把共享 mixin 组合成一个基类。

平台脚本（pc / mobile / web）只覆盖本端专属方法；统一入口 ``agents/executor.py``
仅按 driver.platform 分发到这三个脚本之一。
"""

from atp.agents.executors.base import ExecutorBase
from atp.agents.executors.observe import ObserveMixin
from atp.agents.executors.popup import PopupMixin
from atp.agents.executors.recovery import RecoveryMixin
from atp.agents.executors.steps import StepsMixin

class ReplayCore(ExecutorBase, ObserveMixin, PopupMixin, RecoveryMixin, StepsMixin):
    """三端共用的回放引擎；不含任何平台入口逻辑。"""

    # ---- 平台专属方法的默认空实现；真正的逻辑在 pc/mobile/web 脚本 ----
    def _verify_deviation(self, step, case, before, after_obs, verdict, sr) -> None:
        return None

    def _cold_start(self, step, idx: int, case) -> str:
        return ""

    def _post_launch_settle(self) -> None:
        return None

    def _scroll_find(self, step, obs, idx: int):
        return None, None, ""

__all__ = ["ReplayCore"]
