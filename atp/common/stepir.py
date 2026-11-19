"""StepIR：步骤即数据。

一次 AI 执行产出的唯一真相源。回放脚本、YAML、报告全部由它生成，
反过来源码改动不会污染它。这是整套系统能"沉淀"的前提。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from atp.common.types import Action

LocatorType = Literal[
    "a11y_id",   # resource-id / 鸿蒙 id / DOM id —— 最稳
    "text",      # 可见文本
    "desc",      # contentDescription / aria-label
    "xpath",     # 路径表达式（支持子集）
    "bounds",    # 归一化坐标（兜底）
    "visual",    # 视觉模板（canvas / 屏蔽树 / 游戏）
]

# 定位器默认权重：数字越高越优先尝试
LOCATOR_PRIORITY: dict[str, float] = {
    "a11y_id": 1.0,
    "text": 0.8,
    "desc": 0.75,
    "xpath": 0.4,
    "bounds": 0.2,
    "visual": 0.1,
}


class ScreenProfile(BaseModel):
    """一次执行（或一条用例的基准）所用画布的尺寸档案。

    分辨率无关的全部秘密都在这里：
    - 沉淀时记下 `width/height`（基准画布）+ `dpr`；
    - 回放时再取一次当前画布，算出 `scale_x/scale_y`；
    - 坐标一律以 0~1 归一化存储，落到哪台机器上再乘回去。

    这样"1080p 上录的脚本"能在 4K、能在平板、能在 720p 的远程真机上跑，
    不需要为每个分辨率各录一份。
    """

    width: int = 0
    height: int = 0
    dpr: float = 1.0
    label: str = ""            # 例如 web-maximized-1920x1040 / android-1080x2400
    platform: str = ""
    # 运行期字段：本次画布相对基准的缩放比（回放时写入，报告与数据库都用它）
    scale_x: float = 1.0
    scale_y: float = 1.0

    @property
    def ok(self) -> bool:
        return self.width > 0 and self.height > 0

    def scaled_from(self, base: "ScreenProfile") -> "ScreenProfile":
        """以 base 为基准，算出当前画布的缩放比。"""
        if base.width and base.height:
            self.scale_x = round(self.width / base.width, 4)
            self.scale_y = round(self.height / base.height, 4)
        return self

    def as_dict(self) -> dict:
        return {
            "width": self.width, "height": self.height, "dpr": self.dpr,
            "label": self.label, "platform": self.platform,
            "scale_x": self.scale_x, "scale_y": self.scale_y,
        }


class LocatorStrategy(BaseModel):
    type: LocatorType
    # a11y_id/text/desc/xpath 用字符串；bounds 用归一化四元组；
    # visual 用 dict（asset/rect/base/threshold/pad/label）
    value: str | list[int] | dict[str, Any]
    node_cls: str = ""           # 控件类型限定：同文案的输入框与按钮靠它区分
    score: float = 0.5           # 动态权重，命中加分、连续 miss 减分
    hits: int = 0
    misses: int = 0

    @classmethod
    def make(cls, type_: LocatorType, value: str | list[int] | dict[str, Any],
             node_cls: str = "") -> "LocatorStrategy":
        return cls(type=type_, value=value, node_cls=node_cls,
                   score=LOCATOR_PRIORITY.get(type_, 0.5))

    def bump(self, ok: bool) -> None:
        """自愈的本质就是权重的在线学习，不需要玄学。"""
        if ok:
            self.hits += 1
            self.score = min(1.0, self.score + 0.05)
        else:
            self.misses += 1
            self.score = max(0.01, self.score - 0.08)


class Postcondition(BaseModel):
    kind: Literal[
        "none",
        "assert_visible",
        "assert_text",
        "assert_absent",
        "assert_url",       # Web / Electron 专用：只看地址栏，零成本且极稳
        "assert_motion",    # 视频/动画类：两帧差异占比，证明"真的在播"而不是黑屏
        "assert_screen",
        "extract",
    ] = "none"
    claim: str = ""
    # 断言证据（witness）：首跑时模型判定通过后，从界面上抄下来的一小段确定存在的文本。
    # 回放时先做零成本的「这段文本在不在」检查，命中就不必再调模型——
    # 语义断言一次 30~90 秒，回放还调它等于把成本指标写成了谎言。
    # 万一改版导致证据失效，仍会自动回落到语义判定：降本不降级。
    witness: str = ""
    method: Literal["dsl", "llm"] = "dsl"
    threshold: float = 0.85
    # assert_motion 专用：相邻两帧的"变化面积占比"下限。
    # 正片播放通常 >8%，静止页面接近 0%，只有弹幕/时间戳在动时约 1%~2%。
    min_ratio: float = 0.02


class Evidence(BaseModel):
    before: str = ""
    after: str = ""
    tree_hash: str = ""


class Step(BaseModel):
    step_id: str
    intent: str
    action: Action
    strategies: list[LocatorStrategy] = Field(default_factory=list)
    args: dict[str, Any] = Field(default_factory=dict)
    postcondition: Postcondition = Field(default_factory=Postcondition)
    evidence: Evidence = Field(default_factory=Evidence)
    source: Literal["ai", "human", "healed"] = "ai"
    timeout_ms: int = 15000
    optional: bool = False

    @property
    def needs_target(self) -> bool:
        return self.action.needs_target

    @property
    def visual(self) -> dict[str, Any] | None:
        """图像识别兜底定位器（如果有）。"""
        for s in self.strategies:
            if s.type == "visual" and isinstance(s.value, dict):
                return s.value
        return None


class CaseIR(BaseModel):
    case_id: str
    title: str
    platform: str = "android"
    nl: str = ""
    app_id: str = ""
    app_version: str = ""
    ui_fingerprint: str = ""
    tags: list[str] = Field(default_factory=list)
    variables: dict[str, str] = Field(default_factory=dict)
    steps: list[Step] = Field(default_factory=list)
    # 基准画布：所有归一化坐标都以它为准，换分辨率回放时据此算缩放比
    screen: ScreenProfile = Field(default_factory=ScreenProfile)
    # 本条用例拥有的视觉模板（cases/assets/ 下的文件清单），入库时同步写 case_assets
    visual_assets: list[dict[str, Any]] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    created_by: str = "ai"

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return p

    @classmethod
    def load(cls, path: str | Path) -> "CaseIR":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def touch_versions(self, app_version: str, ui_fingerprint: str) -> None:
        self.app_version = app_version
        self.ui_fingerprint = ui_fingerprint

    def is_stale(self, ui_fingerprint: str) -> bool:
        return bool(self.ui_fingerprint) and self.ui_fingerprint != ui_fingerprint

    def step(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.step_id == step_id), None)


class NLCase(BaseModel):
    """用例源文件：人写自然语言，AI 负责翻译成 StepIR。"""

    case_id: str = Field(default_factory=lambda: f"case-{uuid.uuid4().hex[:8]}")
    title: str
    platform: str = "android"
    app_id: str = ""
    url: str = ""              # web 用例的起始地址；desktop 可为空
    endpoint: str = ""         # CDP 端点，如 http://127.0.0.1:9333
    nl: str = ""
    asserts: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    variables: dict[str, str] = Field(default_factory=dict)
    max_steps: int = 40

    @classmethod
    def load(cls, path: str | Path) -> "NLCase":
        import yaml

        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(**raw)


class StepResult(BaseModel):
    step_id: str
    intent: str
    action: str
    status: Literal["passed", "failed", "skipped", "error"] = "passed"
    strategy_used: str = ""
    node_label: str = ""
    judge_method: str = ""
    judge_score: float = 1.0
    latency_ms: int = 0
    ai_used: bool = False
    detail: str = ""
    error: str = ""
    screenshot: str = ""
    # 视觉兜底与跨分辨率信息：报告里要能说清"这张图是按几倍缩放匹配到的"
    match_score: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0

    @property
    def ok(self) -> bool:
        return self.status in ("passed", "skipped")


class RunResult(BaseModel):
    run_id: str
    case_id: str
    case_title: str
    platform: str
    device_id: str
    mode: Literal["ai", "replay"] = "replay"
    started_at: float = Field(default_factory=time.time)
    finished_at: float = 0.0
    steps: list[StepResult] = Field(default_factory=list)
    # 本次执行所用画布（含相对沉淀基准的缩放比），报告与数据库都读它
    screen: ScreenProfile = Field(default_factory=ScreenProfile)
    artifacts_dir: str = ""
    report_path: str = ""
    # 被用户中断：已跑完的步骤可能全是通过，但"整条用例跑完了"这件事不成立。
    # 单独一个标记，比在 steps 里塞一条假步骤干净，也让 passed 无论如何都为 False。
    aborted: bool = False

    @property
    def passed(self) -> bool:
        return (not self.aborted) and all(s.ok for s in self.steps) and bool(self.steps)

    @property
    def ai_steps(self) -> int:
        return sum(1 for s in self.steps if s.ai_used)

    @property
    def duration_ms(self) -> int:
        return int(((self.finished_at or time.time()) - self.started_at) * 1000)

    def summary(self) -> dict:
        total = len(self.steps)
        failed = sum(1 for s in self.steps if s.status != "passed")
        return {
            "run_id": self.run_id,
            "case": self.case_title,
            "platform": self.platform,
            "device": self.device_id,
            "mode": self.mode,
            "passed": self.passed,
            "total_steps": total,
            "failed_steps": failed,
            "ai_steps": self.ai_steps,
            "ai_ratio": round(self.ai_steps / total, 3) if total else 0.0,
            "duration_ms": self.duration_ms,
        }
