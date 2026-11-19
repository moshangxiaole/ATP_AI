"""AI 首跑执行器 · 统一入口（仅做平台分发）。

按 ``driver.platform`` 把首跑请求分发到三端脚本之一：

- ``runners/mobile.py`` 移动端：包名解析 / 冷启动 / 页面门禁
- ``runners/pc.py``     PC 端：本地应用台账 / 窗口最大化 / 录屏
- ``runners/web.py``    WEB 端：URL 入口 / 录屏，不走应用台账

本文件不写执行逻辑。共享引擎在 ``runners/core.py``（组合各职责 mixin）。
对外仍导出 ``AiRunner`` / ``LlmPlanner`` / ``indexed_nodes`` 等符号，
既有调用方（CLI / 控制台 / 测试）无需改 import。
"""

from __future__ import annotations

import time

from atp.agents.planner import (LlmHealer, LlmPlanner, Planner,  # noqa: F401
                                focus_keywords, indexed_nodes, rectify_target)
from atp.agents.runners.core import RunnerCore
from atp.agents.runners.mobile import MobileRunner
from atp.agents.runners.pc import PcRunner
from atp.agents.runners.web import WebRunner
from atp.common.profiles import profile_for

_IMPL = {
    "mobile": MobileRunner,
    "pc": PcRunner,
    "web": WebRunner,
}


class AiRunner(RunnerCore):
    """平台无关的构造入口：造完即返回对应端的具体执行器。"""

    def __new__(cls, *args, **kwargs):
        if cls is AiRunner:
            driver = args[0] if args else kwargs.get("driver")
            impl = _IMPL.get(profile_for(getattr(driver, "platform", "")).key)
            if impl is not None:
                obj = object.__new__(impl)
                obj.__init__(*args, **kwargs)
                return obj
        return object.__new__(cls)


__all__ = ["AiRunner", "LlmHealer", "LlmPlanner", "Planner", "indexed_nodes",
           "focus_keywords", "rectify_target", "RunnerCore",
           "MobileRunner", "PcRunner", "WebRunner"]
