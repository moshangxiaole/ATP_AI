"""跨层共享的"纯数据"。

只有"谁都要用、且不依赖任何一层"的东西才能放这里：
动作与界面节点（types）、用例 IR（stepir）、端能力矩阵（profiles）。
"""

from atp.common.profiles import (MOBILE, PC, WEB, PlatformProfile, all_profiles,
                                 profile_for)
from atp.common.stepir import (CaseIR, Evidence, LocatorStrategy, NLCase,
                               Postcondition, RunResult, ScreenProfile, Step,
                               StepResult)
from atp.common.types import (Action, ActionResult, DeviceInfo, Observation,
                              RunCancelled, UINode)

__all__ = [
    "Action", "ActionResult", "DeviceInfo", "Observation", "RunCancelled", "UINode",
    "CaseIR", "Evidence", "LocatorStrategy", "NLCase", "Postcondition",
    "RunResult", "ScreenProfile", "Step", "StepResult",
    "PlatformProfile", "MOBILE", "PC", "WEB", "profile_for", "all_profiles",
]
