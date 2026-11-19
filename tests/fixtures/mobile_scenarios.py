"""移动端执行优化的 ScenarioDriver 场景夹具（供 QA 编写 test_*.py 时复用）。

覆盖：白屏半树 / 永不稳 / tree_stale / not_idle / 弹窗 / 跳页 / 包名变化 /
不可逆词 / 用户取消。另提供 ``run_baseline(n=10)`` 作为「健康页面」基线入口，
用来比对优化前后的截图次数、抓树次数、点击次数与耗时。

本文件不是测试文件，不含任何断言；所有场景都只依赖标准库 + 项目已有包。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from atp.config import Config
from atp.agents.executor import Executor
from atp.common.stepir import CaseIR, LocatorStrategy, Step
from atp.common.types import UINode
from atp.agents.platforms.base import BaseDriver

W, H = 1080, 1920
APP = "com.example.atp"
OTHER_APP = "com.example.other"
TARGET_TEXT = "目标按钮"

SCENARIOS: dict[str, str] = {
    "healthy": "健康页面：60 个节点、指纹恒定，门禁一次通过",
    "half_tree": "白屏半树：前两拍只有壳子 + 一个附件，第三拍才画完",
    "thin_stable": "薄但稳定的合法小页面（3 个节点，指纹恒定）",
    "never_stable": "永不稳：每次抓树节点文本都在变",
    "tree_stale": "tree_stale=True：缓存树绝不参与点击决策",
    "not_idle": "驱动显式上报 not_idle：立刻判 unsupported_page，不重试",
    "popup": "开屏弹窗：带「跳过」文字按钮，可安全关闭",
    "popup_icon": "无文字 X 图标弹窗（id 带 close）",
    "jump_page": "误触跳到完全不同的页面",
    "package_change": "前台包名从被测应用切到别的应用",
    "irreversible": "界面含不可逆词（确认支付），禁止自动 BACK",
    "cancelled": "用户取消：每轮门禁轮询都应立刻停",
}


# ---------------------------------------------------------------------------
# 树构造
# ---------------------------------------------------------------------------

def stable_tree(count: int = 60, with_target: bool = True) -> UINode:
    """一棵节点数充足、指纹恒定的健康页面树。"""
    rows: list[UINode] = []
    for i in range(count):
        rows.append(UINode(
            cls="TextView", id=f"row_{i}", text=f"字段{i}",
            bounds=(60, 200 + i * 26, 1020, 224 + i * 26), clickable=True,
        ))
    if with_target:
        rows.append(UINode(
            cls="Button", id="btn_target", text=TARGET_TEXT,
            bounds=(400, 1700, 680, 1780), clickable=True,
        ))
    return UINode(cls="Root", bounds=(0, 0, W, H), children=[
        UINode(cls="FrameLayout", id="content", bounds=(0, 100, W, H - 100),
               scrollable=True, children=rows),
    ])


def thin_tree() -> UINode:
    """白屏半树：只有壳子 + 一个「附件」，节点数远小于 min_nodes。"""
    return UINode(cls="Root", bounds=(0, 0, W, H), children=[
        UINode(cls="WebView", bounds=(0, 100, W, H - 100), scrollable=True, children=[
            UINode(cls="TextView", text="附件", bounds=(0, 1700, W, 1780),
                   clickable=True),
        ]),
    ])


def loading_tree() -> UINode:
    """带明确加载标记的树：门禁不允许放行。"""
    rows = [UINode(cls="TextView", text=f"字段{i}",
                   bounds=(60, 200 + i * 26, 1020, 224 + i * 26))
            for i in range(40)]
    rows.append(UINode(cls="ProgressBar", text="加载中",
                       bounds=(400, 1700, 680, 1760)))
    return UINode(cls="Root", bounds=(0, 0, W, H), children=rows)


def popup_tree(with_danger: bool = False) -> UINode:
    """带弹窗的页面：底层仍是健康页面，顶层浮层里有「跳过」。"""
    base = stable_tree(60)
    skip_text = "跳过此广告" if not with_danger else "加入会员 跳过此广告"
    overlay = UINode(cls="FrameLayout", id="popup_container",
                     bounds=(140, 600, 940, 1300), children=[
                         UINode(cls="TextView", text="版本更新提示",
                                bounds=(200, 660, 880, 740)),
                         UINode(cls="Button", id="btn_skip", text=skip_text,
                                bounds=(700, 1120, 900, 1220), clickable=True),
                     ])
    base.children.append(overlay)
    return base


def icon_popup_tree() -> UINode:
    """无文字 X 图标弹窗（资源 id 带 close、尺寸符合角标区间）。"""
    base = stable_tree(60)
    overlay = UINode(cls="FrameLayout", id="ad_dialog",
                     bounds=(140, 600, 940, 1300), children=[
                         UINode(cls="TextView", text="活动推荐",
                                bounds=(200, 660, 880, 740)),
                         UINode(cls="ImageView", id=f"{APP}:id/btnClose",
                                bounds=(860, 620, 960, 720), clickable=True),
                     ])
    base.children.append(overlay)
    return base


def other_page_tree() -> UINode:
    """与首页粗粒度签名差异很大的另一个页面。"""
    rows = [UINode(cls="Text", id=f"other_{i}", text=f"陌生页条目{i}",
                   bounds=(40, 200 + i * 30, 1040, 228 + i * 30))
            for i in range(45)]
    return UINode(cls="Page", bounds=(0, 0, W, H), children=[
        UINode(cls="Column", id="other_root", bounds=(0, 100, W, H - 100),
               children=rows),
    ])


def irreversible_tree() -> UINode:
    """页面上出现「确认支付」，自动 BACK 必须被禁止。"""
    rows = [UINode(cls="TextView", text=f"订单项{i}",
                   bounds=(40, 200 + i * 30, 1040, 228 + i * 30))
            for i in range(45)]
    rows.append(UINode(cls="Button", text="确认支付",
                       bounds=(400, 1700, 680, 1780), clickable=True))
    return UINode(cls="Root", bounds=(0, 0, W, H), children=rows)


def changing_tree(seq: int) -> UINode:
    """每次抓树都不同的「永不稳」页面。"""
    rows = [UINode(cls="TextView", text=f"动态{i}-{seq % 7}",
                   bounds=(40, 200 + i * 30, 1040, 228 + i * 30))
            for i in range(40)]
    return UINode(cls="Root", bounds=(0, 0, W, H), children=rows)


# ---------------------------------------------------------------------------
# ScenarioDriver
# ---------------------------------------------------------------------------

class ScenarioDriver(BaseDriver):
    """可编排的移动端假设备，记录所有感知 / 输入调用次数。"""

    platform = "android"

    def __init__(self, scenario: str = "healthy", device_id: str = "mock-mobile"):
        super().__init__(device_id)
        self.scenario = scenario
        self.dumps = 0
        self.shots = 0
        self.taps: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int, int]] = []
        self.backs: list[str] = []
        self.events: list[dict] = []
        self.foreground_package = APP
        self.cancelled = False
        self._stale = scenario == "tree_stale"
        self._after_tap: Callable[["ScenarioDriver"], None] | None = None
        self._tree: UINode | None = None
        self._seq: list[UINode] = []

        if scenario == "half_tree":
            self._seq = [thin_tree(), thin_tree(), stable_tree()]
        elif scenario == "popup":
            self._tree = popup_tree()
        elif scenario == "popup_icon":
            self._tree = icon_popup_tree()
        elif scenario == "irreversible":
            self._tree = irreversible_tree()
        elif scenario == "loading":
            self._tree = loading_tree()
        elif scenario == "thin_stable":
            self._tree = thin_tree()
        elif scenario == "jump_page":
            self._tree = stable_tree()
            self._after_tap = lambda d: setattr(d, "_tree", other_page_tree())
        elif scenario == "package_change":
            self._tree = stable_tree()
            self._after_tap = lambda d: setattr(d, "foreground_package", OTHER_APP)
        else:
            self._tree = stable_tree()

        if scenario == "not_idle":
            self.not_idle = True
        else:
            self.not_idle = False

    # ---------- 感知 ----------
    def connect(self) -> None:
        self._connected = True

    def screen_size(self) -> tuple[int, int]:
        return W, H

    def screenshot(self) -> bytes:
        self.shots += 1
        return b"\x89PNG\r\n\x1a\n" + bytes([self.shots % 256])

    def dump_tree(self) -> UINode:
        self.dumps += 1
        if self.scenario == "never_stable":
            return changing_tree(self.dumps)
        if self._seq:
            # 依次播放前几拍（白屏半树），播完后一直停在最后一拍（画完的页面）
            return self._seq[min(self.dumps - 1, len(self._seq) - 1)]
        return self._tree if self._tree is not None else stable_tree()

    @property
    def tree_stale(self) -> bool:
        return bool(self._stale)

    @property
    def current_package(self) -> str:
        return self.foreground_package

    # ---------- 输入 ----------
    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))
        if self.scenario in ("popup", "popup_icon"):
            self._tree = stable_tree()
        if self._after_tap is not None:
            hook, self._after_tap = self._after_tap, None
            hook(self)

    def swipe_xy(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.swipes.append((x1, y1, x2, y2, ms))

    def press_key(self, name: str) -> None:
        self.backs.append(name)
        if name == "back":
            self._tree = stable_tree()
            self.foreground_package = APP

    def launch_app(self, app_id: str) -> None:
        self.app_id = app_id
        self.foreground_package = app_id.split("/")[0]

    def terminate_app(self, app_id: str) -> None:
        return None

    # ---------- 辅助 ----------
    def should_cancel(self) -> bool:
        return bool(self.cancelled)

    def event_sink(self) -> Callable[[dict], None]:
        return self.events.append

    def reset_counters(self) -> None:
        self.dumps = 0
        self.shots = 0
        self.taps.clear()
        self.swipes.clear()
        self.backs.clear()
        self.events.clear()


def make_driver(scenario: str = "healthy") -> ScenarioDriver:
    """按场景名造一台假设备；未知场景退化为健康页面。"""
    return ScenarioDriver(scenario if scenario in SCENARIOS or scenario in (
        "loading",) else "healthy")


def make_config(home: Path | None = None, **overrides: Any) -> Config:
    """隔离的测试配置：关掉冷启动 / 台账 / 落库 / 成功截图，避免污染工程目录。"""
    cfg = Config(home=home or Path(tempfile.mkdtemp(prefix="atp_fixture_")))
    cfg.persist_runs = False
    cfg.screenshot_on_success = False
    cfg.cold_start.enabled = False
    cfg.apps.enabled = False
    cfg.popup_memory.readonly = False
    cfg.scroll_find.enabled = False
    for key, value in overrides.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    return cfg


def make_case(n: int = 10, app_id: str = APP,
              target: str = TARGET_TEXT) -> CaseIR:
    """n 步同目标点击用例；定位器用 text 策略，移动端稳定门禁通过即可命中。"""
    steps = [
        Step(
            step_id=f"s{i:02d}", intent=f"点击{target} 第 {i} 次",
            action="tap",
            strategies=[LocatorStrategy.make("text", target)],
            args={},
        )
        for i in range(1, n + 1)
    ]
    return CaseIR(case_id="fixture-case", title="移动端门禁基线",
                  platform="android", app_id=app_id, steps=steps)


def make_executor(driver: ScenarioDriver, cfg: Config | None = None,
                  **kwargs: Any) -> Executor:
    """造一个接好事件钩子的 Executor。"""
    return Executor(driver, config=cfg or make_config(),
                    on_event=driver.event_sink(),
                    should_cancel=driver.should_cancel, **kwargs)


def run_baseline(n: int = 10) -> dict[str, Any]:
    """健康页面跑 n 步，返回感知 / 输入 / 耗时指标，供优化前后对比。

    Returns:
        ``{"steps", "passed", "dumps", "screenshots", "taps", "backs",
        "elapsed_ms", "events"}``；异常时额外带 ``error``。
    """
    with tempfile.TemporaryDirectory(prefix="atp_baseline_") as tmp:
        cfg = make_config(Path(tmp))
        drv = make_driver("healthy")
        exe = make_executor(drv, cfg)
        t0 = time.perf_counter()
        try:
            result = exe.run(make_case(n), mode="replay", run_id="baseline")
            passed = bool(result.passed)
            steps = len(result.steps)
        except Exception as exc:  # noqa: BLE001
            return {"steps": 0, "passed": False, "dumps": drv.dumps,
                    "screenshots": drv.shots, "taps": len(drv.taps),
                    "backs": len(drv.backs),
                    "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                    "events": list(drv.events), "error": f"{type(exc).__name__}: {exc}"}
        return {
            "steps": steps,
            "passed": passed,
            "dumps": drv.dumps,
            "screenshots": drv.shots,
            "taps": len(drv.taps),
            "backs": len(drv.backs),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "events": list(drv.events),
        }


__all__ = [
    "W", "H", "APP", "OTHER_APP", "TARGET_TEXT", "SCENARIOS",
    "ScenarioDriver", "make_driver", "make_config", "make_case", "make_executor",
    "run_baseline", "stable_tree", "thin_tree", "loading_tree", "popup_tree",
    "icon_popup_tree", "other_page_tree", "irreversible_tree", "changing_tree",
]


if __name__ == "__main__":
    import json

    print(json.dumps(run_baseline(10), ensure_ascii=False,
                     default=lambda o: str(o), indent=2))
