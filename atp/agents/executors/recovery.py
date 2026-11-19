"""应用台账（平台专属的冷启动/滑动/偏离回退在 pc/mobile/web）。

共享实现。方法体与原 ``agents/executor.py`` 一致，只换归属文件。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Protocol

from atp.common.profiles import PlatformProfile, profile_for
from atp.runs.artifacts import REPORT_FILE, STEPIR_FILE, new_run_id, run_dir
from atp.config import Config
from atp.agents.platforms.base import BaseDriver
from atp.db.store import CaseDB
from atp.memory.apps import looks_like_package
from atp.agents.perception.deviation import BackController, DeviationDetector
from atp.agents.perception.judge import Judge, JudgeResult, text_corpus
from atp.agents.perception.locator import LocateResult, derive_strategies, describe, occluded_by, resolve
from atp.agents.perception.page_stabilizer import PageFrame, PageStack, PageStabilizer, page_signature
from atp.memory.popups import PopupGuard, dismiss_popups
from atp.memory.popup_memory import PopupContext, PopupMemory
from atp.agents.perception.scroller import find_by_scrolling, trail_text
from atp.common.stepir import (CaseIR, LocatorStrategy, Postcondition, RunResult,
                               ScreenProfile, Step, StepResult)
from atp.common.types import Action, Observation, RunCancelled
from atp.agents.perception.vision import VisualLocator, denorm_rect, frame_diff_ratio, make_point_node

class RecoveryMixin:
    """共享实现。"""

    def _resolve_app_step(self, step: Step, idx: int, case: CaseIR) -> str:
        """「打开应用」先查应用台账：应用名 → 包名。

        台账里有就直接用（回放永远同一条路径、不碰设备）；没有就去设备上找一次
        （`driver.find_app`），找到立刻写回 `apps.json`，下次连找都不用找。
        找不到就按原样继续——绝不猜一个包名去启动，那会把用例带到另一个应用里。
        """
        apps_cfg = getattr(self.config, "apps", None)
        if apps_cfg is None or not getattr(apps_cfg, "enabled", True):
            return ""
        raw = str(step.args.get("app") or "").strip() or case.app_id
        if not raw:
            # 兜底：AI 在桌面端有时不会把应用名写进 args.app / case.app_id（历史上产出过
            # {"app": ""}，甚至整条用例 app_id 为空），直接从用例标题/自然语言里猜一个，
            # 否则 launch_app 会拿空串去 os.startfile("")，必然失败且后续步骤全崩。
            from atp.memory.apps import resolve_from_text

            cand = resolve_from_text(
                case.title or case.nl, registry=self._registry(),
                driver=self.driver, platform=self.driver.platform,
                screen=self._screen.label, on_event=self.on_event,
                discover=self.profile.discover_apps,
            )
            if cand is None:
                return ""
            step.args["app"] = cand.app_id
            if not case.app_id:
                case.app_id = cand.app_id
            self._emit("app_used", step_id=step.step_id, index=idx, name=cand.name,
                       app_id=cand.app_id, source=cand.source)
            return f"应用台账：{cand.name} → {cand.app_id}"
        if looks_like_package(raw.split("/")[0]):
            # 必须写回：args["app"] 可能是空串（AI 规划器产出过 {"app": ""}），
            # 不写回的话 `args.get("app", driver.app_id)` 拿到的是空串而不是默认值，
            # 驱动就会报「缺少包名」——尽管 case.app_id 里明明有包名。
            step.args["app"] = raw
            return ""          # 已经是包名/组件，没什么可翻的

        from atp.memory.apps import resolve_app

        entry = resolve_app(
            raw, registry=self._registry(), driver=self.driver,
            platform=self.driver.platform, screen=self._screen.label,
            on_event=self.on_event,
        )
        if entry is None:
            self._emit("log", level="warn",
                       message=f"{step.step_id} 应用台账里没有「{raw}」，设备上也没找到，按原样执行")
            return ""

        step.args["app"] = entry.app_id
        if not case.app_id:
            case.app_id = entry.app_id
        self._emit("app_used", step_id=step.step_id, index=idx, name=entry.name,
                   app_id=entry.app_id, source=entry.source)
        return f"应用台账：{entry.name} → {entry.app_id}"
