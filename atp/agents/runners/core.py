"""首跑内核：组合共享 mixin，不含平台入口逻辑。

三端脚本（pc / mobile / web）只覆盖本端专属策略；统一入口
``agents/runner.py`` 只按 driver.platform 分发到这三个脚本之一。
"""

from atp.agents.runners.base import RunnerBase
from atp.agents.runners.setup import SetupMixin
from atp.agents.runners.perceive import PerceiveMixin
from atp.agents.runners.guard import GuardMixin
from atp.agents.runners.steps import StepsMixin
from atp.agents.runners.accept import AcceptMixin
from atp.agents.runners.loop import LoopMixin

class RunnerCore(RunnerBase, SetupMixin, PerceiveMixin, GuardMixin,
                 StepsMixin, AcceptMixin, LoopMixin):
    """三端共用的首跑引擎。"""

__all__ = ["RunnerCore"]
