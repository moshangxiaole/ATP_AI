"""执行事件日志（``events.jsonl`` + 旁路日志）。

「为什么这一步没做 / 模型为什么又让我输一遍」这类问题，光看步骤列表答不了：
被跳过的动作根本不产生步骤记录，模型当时看到的提示词和它原话说了什么也不在报告里。
所以一次执行会落三类日志：

    events.jsonl    全链路主日志：决策、步骤、模型调用、驱动操作，按时间混排
    llm.jsonl       模型调用旁路：完整 system/user 提示词 + 原始返回 + 解析结果
    actions.jsonl   执行操作旁路：动作、目标节点、参数、驱动返回结果

写日志永远不能影响执行：磁盘满、句柄关掉、编码异常，一律吞掉。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, TextIO

from atp.runs.artifacts import ACTIONS_FILE, EVENTS_FILE, LLM_FILE

# 按事件前缀分流的旁路文件（同一条事件在 events.jsonl 里始终保留一份）
MIRRORS = {
    "llm_": LLM_FILE,
    "action_": ACTIONS_FILE,
}


class EventLog:
    def __init__(self, path: Path, handle: TextIO | None = None,
                 mirrors: dict[str, TextIO] | None = None):
        self.path = path
        self._fh = handle
        self._mirrors = mirrors or {}

    @classmethod
    def open(cls, art: str | Path) -> "EventLog":
        """在产物目录里新建一份。打不开就退化成"只丢日志"，不抛异常。"""
        root = Path(art)
        path = root / EVENTS_FILE
        try:
            fh = path.open("w", encoding="utf-8")
        except Exception:  # noqa: BLE001
            return cls(path, None)
        mirrors: dict[str, TextIO] = {}
        for filename in set(MIRRORS.values()):
            try:
                mirrors[filename] = (root / filename).open("w", encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        return cls(path, fh, mirrors)

    def write(self, type_: str, payload: dict[str, Any]) -> None:
        if self._fh is None:
            return
        try:
            # _nodes 是整棵候选节点树，写进去只会把文件撑爆
            safe = {k: v for k, v in payload.items() if not k.startswith("_")}
            line = json.dumps({"ts": round(time.time(), 3), "type": type_, **safe},
                              ensure_ascii=False, default=str) + "\n"
            self._fh.write(line)
            self._fh.flush()
            for prefix, filename in MIRRORS.items():
                fh = self._mirrors.get(filename)
                if fh is not None and type_.startswith(prefix):
                    fh.write(line)
                    fh.flush()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        handles = [self._fh] + list(self._mirrors.values())
        self._fh = None
        self._mirrors = {}
        for fh in handles:
            if fh is None:
                continue
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = ["EventLog", "MIRRORS"]
