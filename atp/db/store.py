"""用例库落库层：SQLite。

为什么是 SQLite 而不是「一堆 JSON 文件」或者 MySQL：

1. **必须能查**。用例库的核心动作是「这句话以前跑过吗」。靠 glob + JSON 反序列化
   在几十条时看不出问题，上千条以后每次检索都要扫全盘，控制台一保存就卡。
   索引字段（platform / nl / title）进 SQL 之后，检索是 O(log n)。
2. **必须能统计**。AI 参与步数占比、命中率、回放耗时这些指标是这套系统的立项依据，
   散落在 artifacts 目录里就没法聚合。runs / run_steps 两张表专门干这件事。
3. **必须零部署**。客户内网、CI 容器、开发本机，三处都要能直接跑。
   一个 .db 文件 copy 走就能带走整库，比装 MySQL 现实得多。
   （SQLite 单写并发足够：控制台是单机串行调度，没有多写场景。）

文件：`<home>/data/atp.db`（可被 atp.config.json 的 db_path 覆盖）

表清单：
    cases            用例主表（一条用例一行，含全量 StepIR 快照）
    case_steps       步骤表（结构化，用于按步骤检索/统计）
    case_assets      视觉模板资产（图 + 归一化坐标 + 基准分辨率）
    runs             执行主表（每次跑一行，含当次分辨率与缩放比）
    run_steps        执行明细（每步一行，含命中的定位方式与匹配分数）
    library_queries  检索日志（用于算命中率、调阈值）
    schema_meta      结构版本

设计原则：**JSON 文件仍是用例的"可执行产物"，数据库是"可查询索引 + 全量快照"**。
写入时两边同时更新（cases.stepir_json 存全量，磁盘 .stepir.json 给 CI 直接跑），
读取时优先走库，库不可用时自动降级回文件扫描——不能因为一个 sqlite 锁就让控制台停摆。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

DB_FILE = "data/atp.db"
SCHEMA_VERSION = 1

# 给前端/CLI 展示用：表名 + 中文说明 + 用途
TABLES: list[tuple[str, str, str]] = [
    ("cases", "用例主表", "一条用例一行：标题/平台/自然语言/脚本路径/基准分辨率/命中次数"),
    ("case_steps", "用例步骤表", "每个步骤一行：动作、意图、定位策略 JSON、后置断言"),
    ("case_assets", "视觉模板表", "图像识别用的小图：路径 + 归一化矩形 + 采集时分辨率"),
    ("runs", "执行记录表", "每次执行一行：模式、耗时、AI 步数、当次分辨率与缩放比"),
    ("run_steps", "执行明细表", "每步一行：命中策略、匹配分数、判定方式、耗时"),
    ("library_queries", "检索日志表", "每次「这句话有没有用例」的相似度与阈值，用来调参"),
    ("schema_meta", "结构版本表", "内部使用"),
]

_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS cases (
    case_id        TEXT PRIMARY KEY,
    title          TEXT NOT NULL DEFAULT '',
    platform       TEXT DEFAULT '',
    app_id         TEXT DEFAULT '',
    app_version    TEXT DEFAULT '',
    nl             TEXT DEFAULT '',
    url            TEXT DEFAULT '',
    endpoint       TEXT DEFAULT '',
    tags           TEXT DEFAULT '[]',
    variables      TEXT DEFAULT '{}',
    ui_fingerprint TEXT DEFAULT '',
    stepir_path    TEXT DEFAULT '',
    script_path    TEXT DEFAULT '',
    stepir_json    TEXT DEFAULT '',
    script_sha     TEXT DEFAULT '',
    step_count     INTEGER DEFAULT 0,
    image_steps    INTEGER DEFAULT 0,
    source         TEXT DEFAULT 'ai',
    version        INTEGER DEFAULT 1,
    base_width     INTEGER DEFAULT 0,
    base_height    INTEGER DEFAULT 0,
    dpr            REAL DEFAULT 1.0,
    screen_label   TEXT DEFAULT '',
    created_at     REAL DEFAULT 0,
    updated_at     REAL DEFAULT 0,
    last_run_at    REAL DEFAULT 0,
    last_status    TEXT DEFAULT '',
    hit_count      INTEGER DEFAULT 0,
    run_count      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cases_platform ON cases(platform);
CREATE INDEX IF NOT EXISTS idx_cases_updated  ON cases(updated_at DESC);

CREATE TABLE IF NOT EXISTS case_steps (
    case_id       TEXT NOT NULL,
    idx           INTEGER NOT NULL,
    step_id       TEXT DEFAULT '',
    intent        TEXT DEFAULT '',
    action        TEXT DEFAULT '',
    strategies    TEXT DEFAULT '[]',
    args          TEXT DEFAULT '{}',
    expect_kind   TEXT DEFAULT 'none',
    expect_claim  TEXT DEFAULT '',
    expect_method TEXT DEFAULT 'dsl',
    image_asset   TEXT DEFAULT '',
    image_rect    TEXT DEFAULT '',
    source        TEXT DEFAULT 'ai',
    PRIMARY KEY (case_id, idx)
);

CREATE TABLE IF NOT EXISTS case_assets (
    asset_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL,
    step_id     TEXT DEFAULT '',
    name        TEXT DEFAULT '',
    rel_path    TEXT DEFAULT '',
    sha1        TEXT DEFAULT '',
    base_width  INTEGER DEFAULT 0,
    base_height INTEGER DEFAULT 0,
    dpr         REAL DEFAULT 1.0,
    norm_rect   TEXT DEFAULT '',
    created_at  REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_assets_case ON case_assets(case_id);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    case_id      TEXT DEFAULT '',
    title        TEXT DEFAULT '',
    platform     TEXT DEFAULT '',
    device_id    TEXT DEFAULT '',
    mode         TEXT DEFAULT '',
    status       TEXT DEFAULT '',
    total_steps  INTEGER DEFAULT 0,
    ai_steps     INTEGER DEFAULT 0,
    ai_ratio     REAL DEFAULT 0,
    duration_ms  INTEGER DEFAULT 0,
    screen_w     INTEGER DEFAULT 0,
    screen_h     INTEGER DEFAULT 0,
    scale_x      REAL DEFAULT 1.0,
    scale_y      REAL DEFAULT 1.0,
    summary      TEXT DEFAULT '{}',
    artifacts_dir TEXT DEFAULT '',
    report_path  TEXT DEFAULT '',
    started_at   REAL DEFAULT 0,
    finished_at  REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_case ON runs(case_id, started_at DESC);

CREATE TABLE IF NOT EXISTS run_steps (
    run_id        TEXT NOT NULL,
    idx           INTEGER NOT NULL,
    step_id       TEXT DEFAULT '',
    action        TEXT DEFAULT '',
    intent        TEXT DEFAULT '',
    status        TEXT DEFAULT '',
    strategy_used TEXT DEFAULT '',
    match_score   REAL DEFAULT 0,
    scale_x       REAL DEFAULT 1.0,
    scale_y       REAL DEFAULT 1.0,
    judge_method  TEXT DEFAULT '',
    judge_score   REAL DEFAULT 0,
    latency_ms    INTEGER DEFAULT 0,
    ai_used       INTEGER DEFAULT 0,
    node_label    TEXT DEFAULT '',
    error         TEXT DEFAULT '',
    screenshot    TEXT DEFAULT '',
    PRIMARY KEY (run_id, idx)
);

CREATE TABLE IF NOT EXISTS library_queries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL DEFAULT 0,
    text        TEXT DEFAULT '',
    platform    TEXT DEFAULT '',
    hit_case_id TEXT DEFAULT '',
    score       REAL DEFAULT 0,
    threshold   REAL DEFAULT 0,
    hit         INTEGER DEFAULT 0
);
"""


class CaseDB:
    """用例库读写。

    线程安全策略：控制台是多线程的（每个 run 一个线程），sqlite3 连接不能跨线程共享，
    所以这里用 check_same_thread=False + 一把进程内锁，把写操作串行化。
    SQLite 的写锁本来就是全局的，串行化不会损失任何并发。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self.error = ""

    # ---------- 连接 ----------
    @property
    def available(self) -> bool:
        try:
            return self.conn is not None
        except Exception:  # noqa: BLE001
            return False

    @property
    def conn(self) -> sqlite3.Connection | None:
        if self._conn is None:
            self._connect()
        return self._conn

    def _connect(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                c = sqlite3.connect(str(self.path), timeout=15, check_same_thread=False)
                c.row_factory = sqlite3.Row
                # WAL：读不阻塞写，控制台列表和执行记录能真正并行
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=NORMAL")
                c.execute("PRAGMA foreign_keys=ON")
                self._conn = c
                self._migrate(c)
            except Exception as e:  # noqa: BLE001
                # 落库失败不能让执行链路挂掉：退化成「只写文件」
                self.error = f"{type(e).__name__}: {e}"
                self._conn = None

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        c.executescript(_DDL)
        row = c.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
        if row is None:
            c.execute("INSERT INTO schema_meta(key, value) VALUES('version', ?)",
                      (str(SCHEMA_VERSION),))
        c.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._conn = None

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor | None:
        c = self.conn
        if c is None:
            return None
        with self._lock:
            try:
                cur = c.execute(sql, tuple(params))
                c.commit()
                return cur
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"
                return None

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        c = self.conn
        if c is None:
            return []
        with self._lock:
            try:
                return list(c.execute(sql, tuple(params)).fetchall())
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"
                return []

    # ---------- 用例 ----------
    def upsert_case(self, case, *, stepir_path: str = "", script_path: str = "",
                    source: str = "", script_sha: str = "",
                    assets: list[dict] | None = None) -> bool:
        """用例入库：cases + case_steps 同步覆盖写，资产表增量补。

        覆盖而不是新增：同 case_id 的用例代表"同一个场景的最新沉淀"，
        历史执行结果在 runs 表里，不需要在 cases 表里堆版本。
        """
        from atp.common.stepir import CaseIR  # 避免循环导入

        if not isinstance(case, CaseIR):
            raise TypeError("upsert_case 需要 CaseIR")
        now = time.time()
        screen = case.screen
        row = (
            case.case_id, case.title, case.platform, case.app_id, case.app_version,
            case.nl, case.variables.get("url", ""), case.variables.get("endpoint", ""),
            json.dumps(case.tags, ensure_ascii=False),
            json.dumps(case.variables, ensure_ascii=False),
            case.ui_fingerprint, str(stepir_path), str(script_path),
            case.model_dump_json(), script_sha,
            len(case.steps), sum(1 for s in case.steps if any(x.type == "visual" for x in s.strategies)),
            source or case.created_by,
            int(screen.width), int(screen.height), float(screen.dpr), screen.label,
            case.created_at, now,
        )
        cur = self._exec(
            """INSERT INTO cases (
                   case_id, title, platform, app_id, app_version, nl, url, endpoint,
                   tags, variables, ui_fingerprint, stepir_path, script_path,
                   stepir_json, script_sha, step_count, image_steps, source,
                   base_width, base_height, dpr, screen_label, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(case_id) DO UPDATE SET
                   title=excluded.title, platform=excluded.platform, app_id=excluded.app_id,
                   app_version=excluded.app_version, nl=excluded.nl, url=excluded.url,
                   endpoint=excluded.endpoint, tags=excluded.tags, variables=excluded.variables,
                   ui_fingerprint=excluded.ui_fingerprint, stepir_path=excluded.stepir_path,
                   script_path=excluded.script_path, stepir_json=excluded.stepir_json,
                   script_sha=excluded.script_sha,
                   step_count=excluded.step_count, image_steps=excluded.image_steps,
                   base_width=excluded.base_width, base_height=excluded.base_height,
                   dpr=excluded.dpr, screen_label=excluded.screen_label,
                   updated_at=excluded.updated_at,
                   version=cases.version + 1""",
            row,
        )
        if cur is None:
            return False

        self._exec("DELETE FROM case_steps WHERE case_id=?", (case.case_id,))
        for i, s in enumerate(case.steps, 1):
            visual = next((x for x in s.strategies if x.type == "visual"), None)
            vval = visual.value if visual is not None else {}
            if not isinstance(vval, dict):
                vval = {}
            rect = vval.get("rect", "")
            self._exec(
                """INSERT INTO case_steps (
                       case_id, idx, step_id, intent, action, strategies, args,
                       expect_kind, expect_claim, expect_method,
                       image_asset, image_rect, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (case.case_id, i, s.step_id, s.intent, s.action.value,
                 json.dumps([x.model_dump(mode="json") for x in s.strategies], ensure_ascii=False),
                 json.dumps(s.args, ensure_ascii=False),
                 s.postcondition.kind, s.postcondition.claim, s.postcondition.method,
                 str(vval.get("asset", "")),
                 json.dumps(rect, ensure_ascii=False) if rect != "" else "",
                 s.source),
            )
        for a in (assets if assets is not None else case.visual_assets) or []:
            try:
                self.add_asset(**a)
            except TypeError:
                continue
        return True

    def get_case(self, case_id: str):
        from atp.common.stepir import CaseIR

        for row in self._query("SELECT stepir_json FROM cases WHERE case_id=?", (case_id,)):
            try:
                return CaseIR.model_validate_json(row["stepir_json"])
            except Exception:  # noqa: BLE001
                return None
        return None

    def list_cases(self, platform: str = "", limit: int = 200) -> list[dict]:
        sql = ("SELECT case_id, title, platform, nl, step_count, image_steps, source, "
               "created_at, updated_at, last_run_at, last_status, hit_count, run_count, "
               "script_path, stepir_path, base_width, base_height, dpr, screen_label "
               "FROM cases")
        args: list[Any] = []
        if platform:
            sql += " WHERE platform=?"
            args.append(platform)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self._query(sql, args)]

    def candidates(self, platform: str = "") -> list[tuple[str, str, str]]:
        """给相似度检索用：只取 case_id / title / nl 三个字段，别把大 JSON 拉进内存。"""
        sql = "SELECT case_id, title, nl FROM cases"
        args: list[Any] = []
        if platform:
            sql += " WHERE platform=? OR platform=''"
            args.append(platform)
        return [(r["case_id"], r["title"], r["nl"]) for r in self._query(sql, args)]

    def bump_hit(self, case_id: str, hit: bool = True) -> None:
        if hit:
            self._exec("UPDATE cases SET hit_count=hit_count+1 WHERE case_id=?", (case_id,))

    def import_cases(self, cases_dir: Path, only_missing: bool = True) -> int:
        """把磁盘上已有的 .stepir.json 回填进库。

        老工程升级上来时库里是空的，但文件积了一堆。若不回填，检索就会一直走
        文件降级路径——功能没错，只是永远用不上索引。启动时补一次，之后两边同步。
        """
        from atp.common.stepir import CaseIR

        n = 0
        known = {r["case_id"] for r in self._query("SELECT case_id FROM cases")} \
            if only_missing else set()
        for path in sorted(Path(cases_dir).glob("*.stepir.json")):
            try:
                case = CaseIR.load(path)
            except Exception:  # noqa: BLE001
                continue
            if case.case_id in known:
                continue
            script = path.with_suffix(".py")
            if self.upsert_case(case, stepir_path=str(path),
                                script_path=str(script) if script.exists() else "",
                                source="import"):
                n += 1
        return n

    def case_meta(self, case_id: str) -> dict | None:
        """用例主表里的一行（不含 stepir_json 大字段），详情页用。"""
        rows = self._query(
            "SELECT case_id, title, platform, app_id, nl, tags, source, step_count, "
            "image_steps, base_width, base_height, dpr, screen_label, created_at, "
            "updated_at, last_run_at, last_status, hit_count, run_count, "
            "stepir_path, script_path, version "
            "FROM cases WHERE case_id=?", (case_id,))
        return dict(rows[0]) if rows else None

    def case_steps(self, case_id: str) -> list[dict]:
        """用例的步骤清单（结构化字段，不解析 StepIR 大 JSON）。"""
        rows = self._query(
            "SELECT idx, step_id, intent, action, strategies, args, expect_kind, "
            "expect_claim, expect_method, image_asset, source "
            "FROM case_steps WHERE case_id=? ORDER BY idx", (case_id,))
        out = []
        for r in rows:
            d = dict(r)
            # 定位策略只给摘要：详情页要的是"这步点了什么"，不是完整 JSON
            try:
                strategies = json.loads(d.pop("strategies") or "[]")
                d["locators"] = [f"{s.get('type')}:{s.get('value')}" for s in strategies][:3]
            except Exception:  # noqa: BLE001
                d["locators"] = []
            try:
                d["args"] = json.loads(d.get("args") or "{}")
            except Exception:  # noqa: BLE001
                d["args"] = {}
            out.append(d)
        return out

    def delete_case(self, case_id: str) -> dict:
        """删掉一条用例在库里的全部痕迹。

        用例不是孤立的一行：步骤、视觉模板、跑过的每一次执行（runs / run_steps）
        都挂在它下面。只删 cases 会留下一堆孤儿记录，统计口径立刻失真，
        而且"删了还能查到"是最容易被当成 bug 的行为。
        """
        run_ids = [r["run_id"] for r in
                   self._query("SELECT run_id FROM runs WHERE case_id=?", (case_id,))]
        counts: dict[str, int] = {"runs": len(run_ids), "run_steps": 0}
        for rid in run_ids:
            cur = self._exec("DELETE FROM run_steps WHERE run_id=?", (rid,))
            counts["run_steps"] += (cur.rowcount or 0) if cur is not None else 0
        for table in ("case_assets", "case_steps"):
            cur = self._exec(f"DELETE FROM {table} WHERE case_id=?", (case_id,))
            counts[table] = (cur.rowcount or 0) if cur is not None else 0
        cur = self._exec("DELETE FROM runs WHERE case_id=?", (case_id,))
        if cur is not None:
            counts["runs"] = max(counts["runs"], cur.rowcount or 0)
        cur = self._exec("DELETE FROM cases WHERE case_id=?", (case_id,))
        counts["cases"] = (cur.rowcount or 0) if cur is not None else 0
        return counts

    # ---------- 资产 ----------
    def add_asset(self, case_id: str, step_id: str = "", name: str = "", rel_path: str = "",
                  sha1: str = "", base_width: int = 0, base_height: int = 0,
                  dpr: float = 1.0, norm_rect: Any = "") -> None:
        self._exec(
            """INSERT INTO case_assets
                   (case_id, step_id, name, rel_path, sha1, base_width, base_height,
                    dpr, norm_rect, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (case_id, step_id, name, rel_path, sha1, int(base_width), int(base_height),
             float(dpr), json.dumps(norm_rect, ensure_ascii=False), time.time()),
        )

    def assets(self, case_id: str = "") -> list[dict]:
        if case_id:
            rows = self._query("SELECT * FROM case_assets WHERE case_id=? ORDER BY asset_id",
                               (case_id,))
        else:
            rows = self._query("SELECT * FROM case_assets ORDER BY asset_id DESC LIMIT 500")
        return [dict(r) for r in rows]

    # ---------- 执行记录 ----------
    def record_run(self, result, *, screen: Any = None,
                   artifacts_dir: str = "", report_path: str = "") -> None:
        """RunResult 落库：一次执行写 runs 一行 + run_steps N 行。"""
        s = result.summary()
        screen_w = int(getattr(screen, "width", 0) or 0)
        screen_h = int(getattr(screen, "height", 0) or 0)
        sx = getattr(screen, "scale_x", 1.0) or 1.0
        sy = getattr(screen, "scale_y", 1.0) or 1.0
        cur = self._exec(
            """INSERT OR REPLACE INTO runs (
                   run_id, case_id, title, platform, device_id, mode, status,
                   total_steps, ai_steps, ai_ratio, duration_ms, screen_w, screen_h,
                   scale_x, scale_y, summary, artifacts_dir, report_path,
                   started_at, finished_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (result.run_id, result.case_id, result.case_title, result.platform,
             result.device_id, result.mode, "passed" if result.passed else "failed",
             s["total_steps"], s["ai_steps"], s["ai_ratio"], s["duration_ms"],
             screen_w, screen_h, sx, sy,
             json.dumps(s, ensure_ascii=False), artifacts_dir or result.artifacts_dir,
             report_path or result.report_path, result.started_at, result.finished_at),
        )
        if cur is None:
            return
        self._exec("DELETE FROM run_steps WHERE run_id=?", (result.run_id,))
        for i, st in enumerate(result.steps, 1):
            self._exec(
                """INSERT INTO run_steps (
                       run_id, idx, step_id, action, intent, status, strategy_used,
                       match_score, scale_x, scale_y, judge_method, judge_score,
                       latency_ms, ai_used, node_label, error, screenshot)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (result.run_id, i, st.step_id, st.action, st.intent, st.status,
                 st.strategy_used, float(st.match_score or 0), sx, sy,
                 st.judge_method, float(st.judge_score or 0), int(st.latency_ms),
                 1 if st.ai_used else 0, st.node_label, st.error, st.screenshot),
            )
        self._exec(
            """UPDATE cases SET last_run_at=?, last_status=?, run_count=run_count+1
               WHERE case_id=?""",
            (result.finished_at or time.time(), "passed" if result.passed else "failed",
             result.case_id),
        )

    def runs(self, limit: int = 50, case_id: str = "") -> list[dict]:
        if case_id:
            rows = self._query(
                "SELECT * FROM runs WHERE case_id=? ORDER BY started_at DESC LIMIT ?",
                (case_id, int(limit)))
        else:
            rows = self._query("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
                               (int(limit),))
        return [dict(r) for r in rows]

    def run_steps(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM run_steps WHERE run_id=? ORDER BY idx", (run_id,))]

    def get_run(self, run_id: str) -> dict | None:
        """按 run_id 取一条历史执行记录。

        控制台内存里只留最近 60 个任务，刷新页面/隔天再看就查不到了；
        用例详情要能翻历史执行，就必须能从库里把它捞回来（含产物目录与报告路径）。
        """
        rows = self._query("SELECT * FROM runs WHERE run_id=?", (run_id,))
        return dict(rows[0]) if rows else None

    # ---------- 检索日志 ----------
    def log_query(self, text: str, platform: str, hit_case_id: str,
                  score: float, threshold: float) -> None:
        self._exec(
            """INSERT INTO library_queries (at, text, platform, hit_case_id, score, threshold, hit)
               VALUES (?,?,?,?,?,?,?)""",
            (time.time(), text[:500], platform, hit_case_id, float(score),
             float(threshold), 1 if hit_case_id else 0),
        )

    def hit_stats(self) -> dict:
        rows = self._query(
            """SELECT COUNT(*) AS total, SUM(hit) AS hits,
                      AVG(CASE WHEN hit=1 THEN score END) AS avg_hit_score
               FROM library_queries""")
        if not rows:
            return {"total": 0, "hits": 0, "hit_rate": 0.0, "avg_hit_score": 0.0}
        r = rows[0]
        total = int(r["total"] or 0)
        hits = int(r["hits"] or 0)
        return {
            "total": total,
            "hits": hits,
            "hit_rate": round(hits / total, 3) if total else 0.0,
            "avg_hit_score": round(float(r["avg_hit_score"] or 0), 3),
        }

    # ---------- 概览 ----------
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for name, _, _ in TABLES:
            if name == "schema_meta":
                continue
            rows = self._query(f"SELECT COUNT(*) AS n FROM {name}")
            out[name] = int(rows[0]["n"]) if rows else 0
        return out

    def info(self) -> dict:
        counts = self.counts()
        return {
            "engine": "sqlite",
            "path": str(self.path),
            "exists": self.path.exists(),
            "size_kb": round(self.path.stat().st_size / 1024, 1) if self.path.exists() else 0,
            "schema_version": SCHEMA_VERSION,
            "ok": self.conn is not None,
            "error": self.error,
            "tables": [
                {"name": n, "label": label, "desc": desc, "rows": counts.get(n, 0)}
                for n, label, desc in TABLES if n != "schema_meta"
            ],
            "counts": counts,
            "hit": self.hit_stats(),
        }


def db_info(cfg) -> dict:
    """给 CLI / HTTP 用的库概览（失败也要能返回，不能抛）。"""
    db = CaseDB(cfg.db_file)
    try:
        return db.info()
    finally:
        db.close()


def record_run(cfg, result, *, screen=None, artifacts_dir: str = "",
               report_path: str = "", case=None, ensure_case: bool = True) -> bool:
    """把一次执行写进库（runs + run_steps）。

    AI 首跑与回放走的是两个执行器（AiRunner / Executor），但**统计口径必须一致**：
    "这次跑了几步、几步用了 AI、当次什么分辨率" 只能是同一套字段，
    否则控制台和报表会各说各话。所以统一收在这里。

    `ensure_case=False` 用于"这次跑挂了、不沉淀"的场景：执行记录要留（失败统计本身
    有价值），但**不能**顺手把这条用例补进库里——那正好绕过了沉淀准入，
    让失败的用例照样出现在用例库和检索结果里。
    """
    db = CaseDB(cfg.db_file)
    try:
        if ensure_case and case is not None and db.get_case(case.case_id) is None:
            db.upsert_case(case, source=case.created_by or "auto")
        db.record_run(result, screen=screen, artifacts_dir=artifacts_dir,
                      report_path=report_path)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        db.close()
