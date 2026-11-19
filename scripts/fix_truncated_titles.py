"""修历史上被「22 字硬切」的用例标题。

旧版控制台发请求时用 `text.slice(0, 22)` 当标题，落库/落盘的标题都是半句话：
    '打开【示例聊天应用】，消息首页点击右上角的【+】，在'
标题本身是用户在卡片、用例库、执行记录里认出"这是我发的那句"的唯一线索，
半句话等于没有。代码已改成原句全文，这个脚本把历史数据补齐（title ← nl）。

判定条件刻意收窄，避免误伤手工起的短标题：
  1. 标题恰好 22 字（旧代码的切点）；
  2. 是 nl 的严格前缀（说明就是从 nl 切出来的）；
  3. 不以句末标点结尾；
  4. 用例是 AI 首跑沉淀的（created_by == 'ai' 或 case_id 形如 case-xxxxxx）。

用法：
    python scripts/fix_truncated_titles.py --dry-run
    python scripts/fix_truncated_titles.py
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.config import Config  # noqa: E402
from atp.scriptgen.compiler import compile_case  # noqa: E402
from atp.common.stepir import CaseIR  # noqa: E402

CUT = 22                      # 旧代码 text.slice(0, 22)
SENTENCE_END = "。！？!?…"
AUTO_ID = re.compile(r"^case-[0-9a-f]{6}$", re.I)


def looks_cut(title: str, nl: str) -> bool:
    if not title or not nl or len(title) != CUT:
        return False
    if not nl.startswith(title) or nl == title:
        return False
    return title[-1] not in SENTENCE_END


def fix_files(cfg: Config, dry: bool) -> list[tuple[str, str, str]]:
    changed: list[tuple[str, str, str]] = []
    for path in sorted(cfg.cases.glob("*.stepir.json")):
        case = CaseIR.load(str(path))
        if not looks_cut(case.title, case.nl):
            continue
        if not (case.created_by == "ai" or AUTO_ID.match(case.case_id)):
            continue
        changed.append((case.case_id, case.title, case.nl))
        if dry:
            continue
        case.title = case.nl
        case.save(str(path))
        py = path.with_suffix("").with_suffix(".py")
        try:
            compile_case(str(path), str(py))
        except Exception as e:  # noqa: BLE001 —— 脚本失败不该拦住标题修复
            print(f"  ! 重新编译失败 {py.name}: {e}")
    return changed


def fix_db(cfg: Config, changed: list[tuple[str, str, str]], dry: bool) -> int:
    if dry or not changed:
        return 0
    conn = sqlite3.connect(str(cfg.db_file))
    try:
        n = 0
        for case_id, _old, nl in changed:
            cur = conn.execute("update cases set title=? where case_id=?", (nl, case_id))
            n += cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = Config.load()
    changed = fix_files(cfg, args.dry_run)
    rows = fix_db(cfg, changed, args.dry_run)

    if not changed:
        print("没有需要修复的标题。")
        return 0
    print(f"{'[dry-run] ' if args.dry_run else ''}修复 {len(changed)} 条：")
    for case_id, old, new in changed:
        print(f"  {case_id}")
        print(f"    旧 {old!r}")
        print(f"    新 {new!r}")
    if not args.dry_run:
        print(f"库里更新 {rows} 行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
