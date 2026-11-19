"""用例文件库：``cases/`` 下的用例文件读写与「这句话以前跑过吗」的检索。

为什么要单独一层：

- **文件是唯一真相**：``cases/<id>.stepir.json`` 能被 CI 直接跑、被 git 管版本，
  数据库只是索引。所以文件路径规则必须是显式、可复用的（``case_path``）。
- **检索要走索引**：库里把 platform / nl / title 入了表，几千条也不会退化成全盘扫
  JSON；库打不开时再降级扫文件，控制台不能因为一个 sqlite 锁就停摆。
- 相似度用字符二元组 Jaccard：中文短句上比编辑距离稳，也不需要向量库。

放在 runs 层（而不是 webapp），是因为「用例文件」是执行产物的一部分，
CLI 与 Web 都要用同一套规则。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from atp.common.stepir import CaseIR
from atp.config import Config
from atp.db.store import CaseDB


def norm(text: str) -> str:
    return re.sub(r"[\s，。、；：！？,.;:!?·「」【】\"'（）()]+", "", text.lower())


def bigrams(text: str) -> set[str]:
    s = norm(text)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i: i + 2] for i in range(len(s) - 1)}


def similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard：比编辑距离更适合中文短句，且不需要向量库。"""
    A, B = bigrams(a), bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


@dataclass
class LibraryHit:
    path: Path
    case: CaseIR
    score: float


def case_path(cfg: Config, case_id: str) -> Path | None:
    """用例的可执行产物路径：优先 .stepir.json，退而求其次用生成的 .py。"""
    p = cfg.cases / f"{case_id}.stepir.json"
    if p.exists():
        return p
    py = cfg.cases / f"{case_id}.py"
    return py if py.exists() else None


def slug_id(text: str, title: str = "") -> str:
    """用例 ID：可读的英文前缀 + 随机后缀，避免中文标题全变成一串横线。"""
    seed = title or text
    ascii_part = re.sub(r"[^a-zA-Z0-9]+", "-", seed).strip("-").lower()
    if len(ascii_part) < 3:
        ascii_part = "case"
    return f"{ascii_part[:32]}-{uuid.uuid4().hex[:6]}"


def find_similar_case(cfg: Config, text: str, platform: str = "",
                      threshold: float = 0.55) -> LibraryHit | None:
    """用例库优先：先查有没有语义相近的已沉淀用例。

    检索走数据库（索引字段已入表，几千条也不会退化成全盘扫 JSON）；
    库不可用时自动降级为文件扫描，保证控制台永远能跑。
    """
    best: LibraryHit | None = None
    db = CaseDB(cfg.db_file)
    try:
        rows = db.candidates(platform) if db.conn else []
    except Exception:  # noqa: BLE001
        rows = []

    if rows:
        for case_id, title, nl_text in rows:
            score = similarity(text, nl_text or title)
            if score >= threshold and (best is None or score > best.score):
                case = db.get_case(case_id)
                path = case_path(cfg, case_id)
                if case is not None and path is not None:
                    best = LibraryHit(path=path, case=case, score=score)
        db.log_query(text, platform, best.case.case_id if best else "",
                     best.score if best else 0.0, threshold)
        db.close()
        return best

    # 降级：扫文件。库里没有数据（或库打不开）时的兜底路径。
    db.close()
    for path in sorted(cfg.cases.glob("*.stepir.json")):
        try:
            case = CaseIR.load(path)
        except Exception:  # noqa: BLE001 —— 坏文件跳过，不影响其它用例
            continue
        if not case.steps:
            continue
        if platform and case.platform and case.platform != platform:
            continue
        score = similarity(text, case.nl or case.title)
        if score >= threshold and (best is None or score > best.score):
            best = LibraryHit(path=path, case=case, score=score)
    return best


__all__ = ["LibraryHit", "norm", "bigrams", "similarity",
           "case_path", "slug_id", "find_similar_case"]
