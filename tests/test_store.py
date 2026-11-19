"""用例库落库的回归测试：表结构、读写一致性、检索、执行记录。

重点验证"落库"不是事后导出的截图，而是可查询、可复算的真相：
- 沉淀一次 → cases / case_steps / case_assets 同步写好；
- 回放一次 → runs / run_steps 记全（含分辨率与缩放比）；
- 检索走库，能命中历史上沉淀过的用例。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.config import Config  # noqa: E402
from atp.common.stepir import (  # noqa: E402
    CaseIR, LocatorStrategy, Postcondition, RunResult, ScreenProfile, Step, StepResult,
)
from atp.common.types import Action  # noqa: E402
from atp.db.store import CaseDB  # noqa: E402

TMP = ROOT / "build_tmp" / "store"


def make_case(case_id: str = "case_demo") -> CaseIR:
    case = CaseIR(case_id=case_id, title="OA 登录", platform="web",
                  nl="打开 OA 后台并登录", created_by="ai")
    case.variables["url"] = "https://demo.example.com/Admin/Public/login"
    case.screen = ScreenProfile(width=1920, height=1040, dpr=1.0, label="web-1920x1040")
    case.steps = [
        Step(step_id="s01", intent="打开登录页", action=Action.OPEN_URL,
             args={"url": "https://demo.example.com/Admin/Public/login"}, source="ai"),
        Step(step_id="s02", intent="填用户名", action=Action.INPUT_TEXT,
             strategies=[LocatorStrategy.make("a11y_id", "#username")],
             args={"text": "05115"}, source="ai"),
        Step(step_id="s03", intent="点登录", action=Action.TAP,
             strategies=[LocatorStrategy.make("visual", {
                 "asset": "assets/case_demo/s03_登录_a1b2.png",
                 "rect": [0.42, 0.51, 0.55, 0.56], "base": [1920, 1040],
                 "threshold": 0.82, "label": "登录"})],
             postcondition=Postcondition(kind="assert_url", claim="login"), source="ai"),
    ]
    case.visual_assets = [{
        "case_id": case_id, "step_id": "s03", "name": "s03_登录_a1b2.png",
        "rel_path": "assets/case_demo/s03_登录_a1b2.png", "sha1": "a1b2",
        "base_width": 1920, "base_height": 1040, "dpr": 1.0,
        "norm_rect": [0.42, 0.51, 0.55, 0.56],
    }]
    return case


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.db = CaseDB(TMP / "schema.db")
        self.db._exec("DELETE FROM case_steps")
        for t in ("cases", "case_assets", "runs", "run_steps", "library_queries"):
            self.db._exec(f"DELETE FROM {t}")

    def tearDown(self):
        self.db.close()

    def test_tables_exist_with_expected_names(self):
        names = {r[0] for r in self.db._query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("cases", "case_steps", "case_assets", "runs", "run_steps",
                  "library_queries", "schema_meta"):
            self.assertIn(t, names)

    def test_info_reports_real_rows(self):
        self.db.upsert_case(make_case(), stepir_path="case_demo.stepir.json",
                            script_path="case_demo.py", source="ai")
        info = self.db.info()
        self.assertTrue(info["ok"])
        self.assertEqual(info["engine"], "sqlite")
        self.assertEqual(info["counts"]["cases"], 1)
        self.assertEqual(info["counts"]["case_steps"], 3)
        self.assertEqual(info["counts"]["case_assets"], 1)
        labels = {t["name"]: t["rows"] for t in info["tables"]}
        self.assertEqual(labels["cases"], 1)


class TestCaseWriteRead(unittest.TestCase):
    def setUp(self):
        self.db = CaseDB(TMP / "cases.db")
        for t in ("cases", "case_steps", "case_assets", "runs", "run_steps"):
            self.db._exec(f"DELETE FROM {t}")

    def tearDown(self):
        self.db.close()

    def test_upsert_then_read_back_full_stepir(self):
        ok = self.db.upsert_case(make_case(), stepir_path="case_demo.stepir.json",
                                 script_path="case_demo.py", source="ai")
        self.assertTrue(ok)
        back = self.db.get_case("case_demo")
        self.assertIsNotNone(back)
        self.assertEqual(len(back.steps), 3)
        self.assertEqual(back.screen.width, 1920)
        self.assertEqual(back.steps[2].visual["label"], "登录")
        self.assertEqual(back.steps[2].postcondition.kind, "assert_url")

    def test_second_upsert_overwrites_not_duplicates(self):
        self.db.upsert_case(make_case(), stepir_path="a.json")
        case = make_case()
        case.steps.pop()
        case.title = "OA 登录 v2"
        self.db.upsert_case(case, stepir_path="a.json")
        rows = self.db._query("SELECT * FROM cases")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "OA 登录 v2")
        self.assertEqual(rows[0]["step_count"], 2)
        self.assertEqual(rows[0]["version"], 2)
        self.assertEqual(self.db._query("SELECT COUNT(*) c FROM case_steps")[0]["c"], 2)

    def test_step_rows_carry_locator_json_and_image(self):
        self.db.upsert_case(make_case())
        rows = self.db.run_steps("") if False else self.db._query(
            "SELECT * FROM case_steps WHERE case_id='case_demo' ORDER BY idx")
        self.assertEqual([r["action"] for r in rows],
                         ["open_url", "input_text", "tap"])
        self.assertIn("#username", rows[1]["strategies"])
        self.assertEqual(rows[2]["image_asset"], "assets/case_demo/s03_登录_a1b2.png")
        self.assertEqual(rows[2]["expect_kind"], "assert_url")

    def test_assets_recorded_with_base_resolution(self):
        self.db.upsert_case(make_case())
        assets = self.db.assets("case_demo")
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["base_width"], 1920)
        self.assertIn("0.42", assets[0]["norm_rect"])

    def test_list_cases_and_candidates(self):
        self.db.upsert_case(make_case())
        lst = self.db.list_cases(platform="web")
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["image_steps"], 1)
        cands = self.db.candidates("web")
        self.assertEqual(cands[0][0], "case_demo")
        self.assertIn("OA", cands[0][1])


class TestRunRecords(unittest.TestCase):
    def setUp(self):
        self.db = CaseDB(TMP / "runs.db")
        for t in ("cases", "runs", "run_steps"):
            self.db._exec(f"DELETE FROM {t}")
        self.db.upsert_case(make_case())

    def tearDown(self):
        self.db.close()

    def _result(self, run_id: str = "run-1", passed: bool = True) -> RunResult:
        r = RunResult(run_id=run_id, case_id="case_demo", case_title="OA 登录",
                      platform="web", device_id="chromium", mode="replay")
        r.steps = [
            StepResult(step_id="s01", intent="打开", action="open_url", status="passed"),
            StepResult(step_id="s02", intent="填账号", action="input_text", status="passed",
                       strategy_used="a11y_id:#username", latency_ms=120),
            StepResult(step_id="s03", intent="点登录", action="tap",
                       status="passed" if passed else "failed",
                       strategy_used="visual:登录", match_score=0.93,
                       judge_method="dsl", ai_used=False, latency_ms=340),
        ]
        r.finished_at = time.time()
        return r

    def test_run_and_steps_persisted(self):
        screen = ScreenProfile(width=1280, height=720, label="web-maximized")
        screen.scaled_from(ScreenProfile(width=1920, height=1040))
        self.db.record_run(self._result(), screen=screen)
        runs = self.db.runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "passed")
        self.assertEqual(runs[0]["screen_w"], 1280)
        self.assertAlmostEqual(runs[0]["scale_x"], round(1280 / 1920, 4), places=4)
        steps = self.db.run_steps("run-1")
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[2]["strategy_used"], "visual:登录")
        self.assertAlmostEqual(steps[2]["match_score"], 0.93, places=2)
        self.assertEqual(steps[2]["scale_x"], runs[0]["scale_x"])

    def test_case_last_status_updated(self):
        self.db.record_run(self._result(passed=False))
        rows = self.db._query("SELECT last_status, run_count FROM cases WHERE case_id=?",
                              ("case_demo",))
        self.assertEqual(rows[0]["last_status"], "failed")
        self.assertEqual(rows[0]["run_count"], 1)

    def test_hit_counting(self):
        self.db.bump_hit("case_demo")
        self.db.bump_hit("case_demo")
        self.assertEqual(self.db.list_cases()[0]["hit_count"], 2)


class TestQueryLog(unittest.TestCase):
    def setUp(self):
        self.db = CaseDB(TMP / "q.db")
        self.db._exec("DELETE FROM library_queries")

    def tearDown(self):
        self.db.close()

    def test_hit_rate(self):
        self.db.log_query("登录 OA", "web", "case_demo", 0.91, 0.55)
        self.db.log_query("给联系人甲发消息", "desktop", "", 0.21, 0.55)
        stats = self.db.hit_stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["hits"], 1)
        self.assertAlmostEqual(stats["hit_rate"], 0.5)


class TestLibrarySearchAndImport(unittest.TestCase):
    """走 web 层的检索入口，确认"先查库"这条路真的读的是数据库。"""

    def setUp(self):
        self.cfg = Config(home=ROOT)
        self.cfg.artifacts_dir = str(TMP / "art")
        self.cfg.cases_dir = str(TMP / "cases")
        self.cfg.db_path = str(TMP / "search.db")
        (TMP / "cases").mkdir(parents=True, exist_ok=True)
        (TMP / "art").mkdir(parents=True, exist_ok=True)
        self.db = CaseDB(self.cfg.db_file)
        for t in ("cases", "case_steps", "library_queries"):
            self.db._exec(f"DELETE FROM {t}")
        make_case().save(TMP / "cases" / "case_demo.stepir.json")

    def tearDown(self):
        self.db.close()

    def test_import_backfills_files(self):
        n = self.db.import_cases(self.cfg.cases)
        self.assertEqual(n, 1)
        self.assertEqual(self.db.counts()["cases"], 1)
        # 再导一次不重复
        self.assertEqual(self.db.import_cases(self.cfg.cases), 0)

    def test_search_hits_db(self):
        from atp.runs.library import find_similar_case

        self.db.import_cases(self.cfg.cases)
        hit = find_similar_case(self.cfg, "打开 OA 后台并登录", "web")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.case.case_id, "case_demo")
        self.assertTrue(hit.path.exists())
        self.assertGreater(hit.score, 0.55)

    def test_search_miss_returns_none(self):
        from atp.runs.library import find_similar_case

        self.db.import_cases(self.cfg.cases)
        self.assertIsNone(find_similar_case(self.cfg, "把订单导出成 Excel 发给财务", "web"))

    def test_search_logged_for_threshold_tuning(self):
        from atp.runs.library import find_similar_case

        self.db.import_cases(self.cfg.cases)
        find_similar_case(self.cfg, "打开 OA 后台并登录", "web")
        stats = self.db.hit_stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["hits"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
