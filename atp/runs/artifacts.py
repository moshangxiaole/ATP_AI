"""运行产物约定：目录怎么起名、文件叫什么。

这套命名是**对外契约**：报告页按相对路径内嵌截图与录像，控制台按文件名
区分产物，导出的 zip 也是这一套。散在各个模块里字符串拼一遍，改一处就漏一处。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

# 一次执行目录里固定出现的产物
EVENTS_FILE = "events.jsonl"     # 执行事件日志（每次决策 / 每条跳过原因）
LLM_FILE = "llm.jsonl"           # 模型调用旁路：完整提示词 + 原始返回
ACTIONS_FILE = "actions.jsonl"   # 驱动操作旁路：动作 / 目标节点 / 返回结果
REPORT_FILE = "report.html"      # 自包含报告
RECORDING_FILE = "recording.mp4"  # 执行录像（PC / WEB）
STEPIR_FILE = "stepir.json"      # 当次沉淀出的用例 IR


def run_dir(root: str | Path, run_id: str) -> Path:
    """拿到（并创建）一次执行的产物目录。"""
    path = Path(root) / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_run_id(case_id: str, tag: str = "", *, unique: bool = True) -> str:
    """生成 run_id。

    - 回放：``<case>-20261113-214208-b1d3``（带随机后缀，同一秒多次执行也不撞）
    - 首跑：``<case>-ai-211310``（不带后缀，控制台按这个前缀找产物）
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = "-".join(part for part in (case_id or "run", tag, stamp) if part)
    return f"{base}-{uuid.uuid4().hex[:4]}" if unique else base


def evidence_name(index: int, step_id: str, phase: str) -> str:
    """单步证据图：``01_s01_before.png`` / ``01_s01_after.png``。"""
    return f"{index:02d}_{step_id}_{phase}.png"


__all__ = ["EVENTS_FILE", "LLM_FILE", "ACTIONS_FILE", "REPORT_FILE",
           "RECORDING_FILE", "STEPIR_FILE",
           "run_dir", "new_run_id", "evidence_name"]
