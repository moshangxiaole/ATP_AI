"""可执行脚本运行时。

沉淀出来的 .py 不是"又一个框架"，它是 StepIR 的另一种写法：
Script 内部仍然构建同一个 CaseIR，交给同一个 Executor。
所以脚本和用例库永远同步，不会出现两套真相。
"""

from __future__ import annotations

from pathlib import Path

from atp.config import Config
from atp.agents.executor import Executor
from atp.agents.perception.judge import Judge
from atp.agents.perception.locator import derive_strategies
from atp.common.stepir import CaseIR, Postcondition, Step
from atp.common.types import Action, Observation
from atp.agents.devices.manager import DeviceManager


class Script:
    def __init__(self, platform: str = "android", app: str = "",
                 title: str = "", case_id: str = ""):
        self.case = CaseIR(
            case_id=case_id or "script",
            title=title or case_id or "script",
            platform=platform,
            app_id=app,
            created_by="human",
        )
        self._last: list[str] = []

    # ---------- 内部 ----------
    def _add(self, action: Action, by: str = "", value="", args: dict | None = None,
             post: Postcondition | None = None, intent: str = "",
             node_cls: str = "") -> "Script":
        from atp.common.stepir import LocatorStrategy

        sid = f"s{len(self.case.steps) + 1:02d}"
        strategies = [LocatorStrategy.make(by, value, node_cls)] if by else []
        self.case.steps.append(
            Step(
                step_id=sid,
                intent=intent or action.value,
                action=action,
                strategies=strategies,
                args=args or {},
                postcondition=post or Postcondition(),
                source="human",
            )
        )
        return self

    @staticmethod
    def _post(kind: str, claim: str, method: str = "dsl",
              witness: str = "", min_ratio: float = 0.02) -> Postcondition:
        return Postcondition(kind=kind, claim=claim, method=method,  # type: ignore[arg-type]
                             witness=witness, min_ratio=min_ratio)

    # ---------- 动作 ----------
    def launch(self, app: str = "") -> "Script":
        return self._add(Action.LAUNCH_APP, args={"app": app or self.case.app_id},
                         intent=f"启动应用 {app or self.case.app_id}")

    def terminate(self, app: str = "") -> "Script":
        """结束应用进程。移动端用例的开场白：先杀干净，再冷启动。

        就算不写这一步，执行器也会在 launch 之前自动补（见 Executor._cold_start）；
        显式写出来是为了报告里看得见"这一步确实从干净状态起步"。
        """
        target = app or self.case.app_id
        return self._add(Action.TERMINATE_APP, args={"app": target},
                         intent=f"结束应用进程 {target}")

    def open_url(self, url: str) -> "Script":
        """打开地址。驱动内部会在导航完成后最大化窗口并固定画布尺寸。"""
        self.case.variables["url"] = url
        return self._add(Action.OPEN_URL, args={"url": url}, intent=f"打开 {url}")

    def maximize(self) -> "Script":
        return self._add(Action.MAXIMIZE, intent="最大化窗口，固定画布尺寸")

    def tap_image(self, asset: str, rect: list[float], base: tuple[int, ...] = (),
                  label: str = "", threshold: float = 0.82, pad: float = 0.22) -> "Script":
        """图像识别点击：`rect` 是 0~1 归一化矩形，换分辨率照样能点中。

        这是"元素定位不住"时的最后一级兜底（canvas、纯图界面、被屏蔽的无障碍树）。
        """
        value = {
            "asset": asset, "rect": list(rect), "base": list(base),
            "threshold": threshold, "pad": pad, "label": label or asset,
        }
        return self._add(Action.TAP, "visual", value,
                         intent=f"图像识别点击 {label or asset}")

    def tap(self, by: str = "a11y_id", value="", intent: str = "",
            check: tuple[str, str] | None = None, node_cls: str = "") -> "Script":
        post = self._post(check[0], check[1]) if check else None
        return self._add(Action.TAP, by, value, post=post,
                         intent=intent or f"点击 {value}", node_cls=node_cls)

    def input(self, text: str, by: str = "", value="", intent: str = "",
              node_cls: str = "") -> "Script":
        return self._add(Action.INPUT_TEXT, by, value, args={"text": text},
                         intent=intent or f"输入 {text}", node_cls=node_cls)

    def clear(self, by: str = "", value="", node_cls: str = "") -> "Script":
        return self._add(Action.CLEAR_TEXT, by, value, intent="清空输入框", node_cls=node_cls)

    def swipe(self, direction: str = "up") -> "Script":
        return self._add(Action.SWIPE, args={"direction": direction},
                         intent=f"向{direction}滑动")

    def back(self) -> "Script":
        return self._add(Action.BACK, intent="返回")

    def home(self) -> "Script":
        return self._add(Action.HOME, intent="回到桌面")

    def key(self, name: str) -> "Script":
        return self._add(Action.PRESS_KEY, args={"key": name}, intent=f"按键 {name}")

    def wait(self, text: str, timeout_ms: int = 15000) -> "Script":
        s = self._add(Action.WAIT_FOR, post=self._post("assert_visible", text),
                      intent=f"等待「{text}」出现")
        self.case.steps[-1].timeout_ms = timeout_ms
        return s

    def check(self, kind: str = "assert_text", claim: str = "",
              method: str = "dsl", witness: str = "", min_ratio: float = 0.02) -> "Script":
        """断言。`witness` 是首跑抄下来的界面证据文本，命中就不必调模型；
        `kind='assert_motion'` 时用 `min_ratio` 规定"画面至少变多少"才算在播放。"""
        return self._add(Action.WAIT_FOR,
                         post=self._post(kind, claim, method, witness, min_ratio),
                         intent=f"断言 {claim}")

    def shot(self) -> "Script":
        return self._add(Action.SCREENSHOT, intent="截图")

    def raw(self, action: str, **args) -> "Script":
        """兜底：直接下发一个原子动作，供特殊场景使用。"""
        return self._add(Action(action), args=args, intent=f"动作 {action}")

    # ---------- 执行 ----------
    def build(self) -> CaseIR:
        return self.case

    def run(self, device_id: str = "", config: Config | None = None,
            persist: str | Path | None = None, driver=None,
            healer=None) -> "RunResult":  # noqa: F821
        cfg = config or Config.load()
        if driver is not None:
            drv = driver
            drv.connect()
            try:
                return Executor(drv, cfg, Judge(), healer).run(
                    self.case, mode="replay",
                    persist_path=Path(persist) if persist else None,
                )
            finally:
                drv.close()

        mgr = DeviceManager(cfg)
        with mgr.lease(self.case.platform, device_id) as d:
            return Executor(d, cfg, Judge(), healer).run(
                self.case, mode="replay",
                persist_path=Path(persist) if persist else None,
            )


def record_strategies(step: Step, obs: Observation, node) -> None:
    """把首跑时看到的节点固化成定位器集合（AI 沉淀用）。"""
    step.strategies = derive_strategies(node, obs.root, (obs.width, obs.height))
