"""Web 控制台的回归测试：任务事件流、用例库命中、HTTP 路由。

每次运行用独立临时目录，不污染真实 cases/ 与 artifacts/。

早先固定用 build_tmp/web：Windows 上服务线程还占着产物文件时 rmtree 会静默失败，
于是每跑一次都在 build_tmp 里留一批截图，攒到上千个文件后清理动作会被安全策略拦下。
独立临时目录既不累积，也不会和上一轮的残留互相当成"已有用例"。
"""

from __future__ import annotations

import atexit
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="atp-web-test-"))
atexit.register(lambda: shutil.rmtree(TMP, ignore_errors=True))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from atp.config import Config  # noqa: E402
from atp.webapp import server  # noqa: E402
from atp.webapp.jobs import RunStore, start_run  # noqa: E402
from atp.runs.library import find_similar_case, similarity  # noqa: E402
from atp.webapp.server import App, Handler  # noqa: E402
from console_fakes import install, restore  # noqa: E402


def _cfg() -> Config:
    cfg = Config.load(ROOT)
    cfg.artifacts_dir = str(TMP / "artifacts")
    cfg.cases_dir = str(TMP / "cases")
    # 用例库也要隔离：否则单测会把 mock 用例写进真实的 data/atp.db
    cfg.db_path = str(TMP / "atp.db")
    # 单测必须离线且确定：清掉模型配置，强制走内置脚本化规划器。
    # 否则谁在 atp.config.json 里配了真模型，测试就会跟着网络抖动红或绿。
    cfg.model.base_url = ""
    cfg.model.api_key = ""
    cfg.model.model = ""
    cfg.model.vision_model = ""
    (TMP / "artifacts").mkdir(parents=True, exist_ok=True)
    (TMP / "cases").mkdir(parents=True, exist_ok=True)
    return cfg


def _wait(job, timeout: float = 40.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if job.status in ("passed", "failed", "error", "cancelled"):
            return
        time.sleep(0.05)
    raise AssertionError(f"任务超时未结束: {job.status}")


NL = "登录阳光 OA，进入待办，打开第一条请假申请，填写同意并提交"


class SimilarityTest(unittest.TestCase):
    def test_same_text_scores_one(self):
        self.assertEqual(similarity(NL, NL), 1.0)

    def test_punctuation_insensitive(self):
        self.assertGreater(similarity(NL, "登录阳光OA进入待办，打开第一条请假申请填写同意并提交"), 0.9)

    def test_unrelated_is_low(self):
        self.assertLess(similarity(NL, "切换深色主题并修改字体大小"), 0.25)


class RunFlowTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        install()                      # 假设备/假模型只在测试进程里注入
        self.cfg = _cfg()
        self.store = RunStore()

    def tearDown(self):
        restore()

    def test_ai_first_run_persists_and_emits(self):
        job = start_run(self.cfg, self.store, {
            "text": NL, "platform": "android", "mode": "ai", "title": "Web 首跑",
        })
        _wait(job)

        self.assertEqual(job.status, "passed")
        types = [e["type"] for e in job.events]
        self.assertEqual(types[0], "run_started")
        self.assertEqual(types[-1], "run_finished")
        self.assertIn("sedimented", types)

        finished = [e for e in job.events if e["type"] == "step_finished"]
        self.assertGreaterEqual(len(finished), 7)
        self.assertTrue(all(e["result"]["status"] == "passed" for e in finished))
        # 截图 URL 指向真实文件，这是前端缩略图能不能显示的关键
        first = finished[0]["images"][0]
        name = first["url"].rsplit("/", 1)[-1]
        self.assertTrue((Path(job.artifacts_dir) / name).exists())
        self.assertTrue(first["url"].startswith(f"/api/runs/{job.run_id}/artifacts/"))

        self.assertTrue(Path(job.stepir_path).exists())
        self.assertTrue(Path(job.script_path).exists())
        self.assertTrue(Path(job.report_path).exists())

    def test_second_run_hits_library_and_uses_no_ai(self):
        first = start_run(self.cfg, self.store, {"text": NL, "platform": "android", "mode": "ai"})
        _wait(first)
        self.assertEqual(first.status, "passed")

        hit = find_similar_case(self.cfg, NL, "android")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.score, 1.0)

        second = start_run(self.cfg, self.store, {"text": NL, "platform": "android", "mode": "auto"})
        _wait(second)
        self.assertEqual(second.status, "passed")
        self.assertIn("library_hit", [e["type"] for e in second.events])
        self.assertEqual(second.summary["ai_steps"], 0)

    def test_replay_without_library_reports_clear_error(self):
        job = start_run(self.cfg, self.store, {
            "text": "完全不存在的一条用例", "platform": "android", "mode": "replay",
        })
        _wait(job)
        self.assertEqual(job.status, "error")
        self.assertIn("用例库中没有匹配", job.error)

    def test_cancel_requested_before_execution(self):
        job = start_run(self.cfg, self.store, {"text": NL, "platform": "android", "mode": "ai"})
        job.cancel_requested = True
        _wait(job)
        self.assertIn(job.status, ("cancelled", "passed", "failed"))

    def test_empty_text_rejected(self):
        with self.assertRaises(ValueError):
            start_run(self.cfg, self.store, {"text": "  ", "platform": "android"})


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install()
        cls.cfg = _cfg()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.httpd.daemon_threads = True
        cls.httpd.app = App(cls.cfg)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        restore()
        shutil.rmtree(TMP, ignore_errors=True)

    def _get(self, path: str):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=40) as r:
            return r.status, r.read()

    def _post(self, path: str, body: dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read())

    def _delete(self, path: str):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method="DELETE")
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read())

    def _run_and_wait(self, text: str) -> dict:
        run_id = self._post("/api/run", {"text": text, "platform": "android",
                                         "mode": "ai"})["run_id"]
        since, events = 0, []
        for _ in range(40):
            data = json.loads(self._get(f"/api/runs/{run_id}/events?since={since}&wait=10")[1])
            events += data["events"]
            since = data["next"]
            if any(e["type"] == "run_finished" for e in events):
                break
        return [e for e in events if e["type"] == "run_finished"][0]

    def test_static_and_state(self):
        code, body = self._get("/")
        self.assertEqual(code, 200)
        self.assertIn("ATP", body.decode("utf-8"))

        code, body = self._get("/app.css")
        self.assertEqual(code, 200)

        code, body = self._get("/api/state")
        state = json.loads(body)
        self.assertIn("model", state)
        self.assertIn("devices", state)
        self.assertTrue(any(d["platform"] == "desktop" for d in state["devices"]))
        self.assertFalse(any(d["platform"] == "mock" for d in state["devices"]),
                        "设备列表里还留着已下线的 Mock 终端")

    def test_run_over_http_and_fetch_artifacts(self):
        started = self._post("/api/run", {"text": NL, "platform": "android", "mode": "ai"})
        run_id = started["run_id"]

        since, events = 0, []
        for _ in range(40):
            _, body = self._get(f"/api/runs/{run_id}/events?since={since}&wait=10")
            data = json.loads(body)
            events += data["events"]
            since = data["next"]
            if any(e["type"] == "run_finished" for e in events):
                break
        fin = [e for e in events if e["type"] == "run_finished"][0]
        self.assertEqual(fin["status"], "passed")

        img = [i for e in events if e["type"] == "step_finished" for i in e["images"]][0]
        code, blob = self._get(img["url"])
        self.assertEqual(code, 200)
        self.assertTrue(blob.startswith(b"\x89PNG"))

        code, html = self._get(fin["report_url"])
        self.assertEqual(code, 200)
        self.assertIn(b"data:image/png", html)

        code, script = self._get(fin["script_url"])
        self.assertEqual(code, 200)
        self.assertIn(b"Script", script)

        # AI 首跑也必须进 runs / run_steps：控制台的统计口径与回放共用一套表
        from atp.db.store import CaseDB

        db = CaseDB(self.cfg.db_file)
        try:
            runs = db.runs(limit=50, case_id=fin["case_id"])
            self.assertTrue(runs, "AI 首跑没有落库到 runs")
            self.assertEqual(runs[0]["run_id"], run_id)
            self.assertEqual(runs[0]["mode"], "ai")
            self.assertEqual(runs[0]["status"], "passed")
            self.assertTrue(db.run_steps(run_id), "run_steps 为空")
            self.assertEqual(db.counts()["cases"] >= 1, True)
        finally:
            db.close()

    def test_unknown_run_returns_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/runs/run-nope/events?since=0&wait=0")
        self.assertEqual(ctx.exception.code, 404)

    def test_case_detail_history_and_delete(self):
        """左侧用例点开的"执行过程"，以及删除时库与文件必须一起清干净。"""
        fin = self._run_and_wait("在真机上点登录，检查工作台")
        case_id = fin["case_id"]

        detail = json.loads(self._get(f"/api/cases/{case_id}")[1])
        self.assertEqual(detail["case"]["case_id"], case_id)
        self.assertTrue(detail["case"]["title"])
        self.assertTrue(detail["steps"], "用例详情没有步骤")
        self.assertTrue(detail["runs"], "用例详情没有历史执行")
        run_id = detail["runs"][0]["run_id"]

        hist = json.loads(self._get(f"/api/history/{run_id}")[1])
        self.assertTrue(hist["steps"], "执行明细为空")
        self.assertTrue(any(s["image"] for s in hist["steps"]))
        shot = next(s["image"] for s in hist["steps"] if s["image"])
        # 历史执行的截图也要能打开：内存任务表里早就没有这个 job 了
        self.assertEqual(self._get(shot)[0], 200)
        self.assertTrue(hist["run"]["has_report"])
        self.assertEqual(self._get(f"/api/runs/{run_id}/report")[0], 200)

        art_dir = Path(hist["run"]["artifacts_dir"])
        self.assertTrue(art_dir.is_dir())

        info = self._delete(f"/api/cases/{case_id}")
        self.assertTrue(info["ok"])
        self.assertGreaterEqual(info["db"]["runs"], 1)
        self.assertFalse(art_dir.exists(), "删除用例要连执行产物一起删")
        self.assertFalse(list(self.cfg.cases.glob(f"{case_id}*")))

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(f"/api/cases/{case_id}")
        self.assertEqual(ctx.exception.code, 404)

        from atp.db.store import CaseDB

        db = CaseDB(self.cfg.db_file)
        try:
            self.assertIsNone(db.get_case(case_id))
            self.assertEqual(db.runs(limit=50, case_id=case_id), [])
            self.assertEqual(db.run_steps(run_id), [])
        finally:
            db.close()

    def test_delete_unknown_case_returns_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._delete("/api/cases/not-exist-case")
        self.assertEqual(ctx.exception.code, 404)

    def test_traversal_blocked(self):
        self._post("/api/run", {"text": NL, "platform": "android", "mode": "ai"})
        runs = json.loads(self._get("/api/runs")[1])["runs"]
        run_id = runs[0]["run_id"]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(f"/api/runs/{run_id}/artifacts/../../atp.config.json")
        self.assertEqual(ctx.exception.code, 404)


class LauncherTest(unittest.TestCase):
    """一键启动的支撑逻辑：认得出自己人、不误认别人、停得掉。"""

    def setUp(self):
        self.home = TMP / "launcher"
        (self.home / "cases").mkdir(parents=True, exist_ok=True)
        self.cfg = _cfg()
        self.cfg.home = self.home
        self.httpd = None

    def tearDown(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        server.runtime_file(self.cfg).unlink(missing_ok=True)

    def _serve(self) -> int:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(self.cfg)
        port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return port

    def test_no_instance_returns_empty(self):
        self.assertEqual(server.find_running(self.cfg, "127.0.0.1", 1), "")

    def test_detects_own_instance(self):
        port = self._serve()
        self.assertEqual(server.find_running(self.cfg, "127.0.0.1", port),
                         f"http://127.0.0.1:{port}/")

    def test_ignores_instance_of_another_project(self):
        # 同一端口上跑的是别人家的工程，不该被当成自己的复用
        port = self._serve()
        other = _cfg()
        other.home = TMP / "another-project"
        self.assertEqual(server.find_running(other, "127.0.0.1", port), "")

    def test_runtime_file_points_at_live_port(self):
        port = self._serve()
        server.runtime_file(self.cfg).write_text(
            json.dumps({"app": "atp-console", "pid": 999999, "port": port,
                        "url": f"http://127.0.0.1:{port}/"}), encoding="utf-8")
        self.assertEqual(server.find_running(self.cfg, "127.0.0.1", 1),
                         f"http://127.0.0.1:{port}/")

    def test_stop_is_idempotent_when_nothing_runs(self):
        self.assertEqual(server.stop(self.cfg, "127.0.0.1", 1), 0)
        self.assertFalse(server.runtime_file(self.cfg).exists())


if __name__ == "__main__":
    unittest.main()
