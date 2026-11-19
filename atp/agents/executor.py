"""回放执行器 · 统一入口（仅做平台分发）。

按 ``driver.platform`` 把执行请求分发到三端脚本之一：

- ``executors/mobile.py`` 移动端：冷启动 / 滑动查找 / 偏离回退
- ``executors/pc.py``     PC 端：窗口录屏，无冷启动
- ``executors/web.py``    WEB 端：URL 入口，应用台账不生效

本文件不写任何执行逻辑。共享引擎在 ``executors/core.py``（组合各职责 mixin），
对外仍导出 ``Executor`` / ``discriminative_post`` / ``recompile_strategies``，
既有调用方（CLI / 控制台 / 报告 / 测试）无需改 import。
"""

from __future__ import annotations

from atp.agents.executors.base import discriminative_post
from atp.agents.executors.base import EventSink, Healer
from atp.agents.executors.core import ReplayCore
from atp.agents.executors.mobile import MobileExecutor
from atp.agents.executors.pc import PcExecutor
from atp.agents.executors.steps import recompile_strategies
from atp.agents.executors.web import WebExecutor
from atp.common.profiles import profile_for

_IMPL = {
    "mobile": MobileExecutor,
    "pc": PcExecutor,
    "web": WebExecutor,
}


class Executor(ReplayCore):
    """平台无关的构造入口：造完即返回对应端的具体执行器。"""

    def __new__(cls, *args, **kwargs):
        if cls is Executor:
            driver = args[0] if args else kwargs.get("driver")
            impl = _IMPL.get(profile_for(getattr(driver, "platform", "")).key)
            if impl is not None:
                # 返回的不是 cls 的实例，Python 不会自动调 __init__，这里手动走一遍。
                obj = object.__new__(impl)
                obj.__init__(*args, **kwargs)
                return obj
        return object.__new__(cls)


__all__ = ["Executor", "discriminative_post", "recompile_strategies",
           "EventSink", "Healer", "ReplayCore",
           "MobileExecutor", "PcExecutor", "WebExecutor"]
