"""失败的用例不沉淀。

失败的首跑录下来的是一串"没走通"的过程：定位器是猜的、页面是错的、断言本来就没满足。
它一旦入库，下次检索命中它就必然再失败一次——白跑一遍，还把"用例库有没有用"这个指标搅乱。
准入只有一条：**每一步都通过**。产物仍在 artifacts 里，排障不受影响。
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.config import Config  # noqa: E402
from atp.memory.sediment import sediment_blocker  # noqa: E402
from atp.common.stepir import CaseIR, RunResult, Step, StepResult  # noqa: E402
from atp.common.types import Action  # noqa: E402
from atp.db.store import CaseDB  # noqa: E402
import atp.webapp.jobs as jobs  # noqa: E402


def run(*statuses: str) -> RunResult:
    r = RunResult(run_id="r1", case_id="c1", case_title="例子", platform="android",
                  device_id="fake", mode="ai")
    for i, st in enumerate(statuses, start=1):
        r.steps.append(StepResult(step_id=f"s{i:02d}", intent=f"第{i}步",
                                  action="tap", status=st,
                                  error="" if st == "passed" else "没找到目标元素"))
    return r


class TestSedimentGuard(unittest.TestCase):
    def test_all_passed_is_sedimentable(self):
        self.assertEqual(sediment_blocker(run("passed", "passed")), "")

    def test_skipped_counts_as_passed(self):
        # skipped 是"驱动不支持所以没执行"，不是失败，不该拦住沉淀
        self.assertEqual(sediment_blocker(run("passed", "skipped")), "")

    def test_one_failed_blocks(self):
        msg = sediment_blocker(run("passed", "failed", "passed"))
        self.assertTrue(msg)
        self.assertIn("未通过", msg)
        self.assertIn("s02", msg)
        self.assertIn("不写入用例库", msg)

    def test_error_blocks(self):
        self.assertTrue(sediment_blocker(run("passed", "error")))

    def test_empty_run_blocks(self):
        # 启动/取界面就失败：一条步骤都没有，更没有沉淀的价值
        msg = sediment_blocker(RunResult(run_id="r", case_id="c", case_title="t",
                                         platform="android", device_id="fake"))
        self.assertTrue(msg)
        self.assertIn("没有任何执行步骤", msg)

    def test_message_mentions_first_failure_only(self):
        msg = sediment_blocker(run("failed", "failed", "failed"))
        self.assertIn("s01", msg)
        self.assertNotIn("s02", msg)


class _FakeDriver:
    device_id = "fake-device"
    platform = "android"


class TestFirstRunDoesNotSedimentFailures(unittest.TestCase):
    """打在执行器出口上：控制台首跑失败后，磁盘和库里都不该多出一条用例。

    只测 `sediment_blocker` 是不够的——真正的坑是"函数写对了，调用点忘了拦"。
    这里把 AiRunner / 设备租借都替换掉，直接走 `_run_ai` 的收尾。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_sediment_"))
        self.cfg = Config(home=self.tmp)
        self.cfg.artifacts_dir = "art"
        self.cfg.cases_dir = "cases"
        self.cfg.db_path = "data/t.db"
        self.cfg.persist_runs = True

    def _run(self, status: str):
        result = RunResult(run_id="r1", case_id="c1", case_title="例子",
                           platform="android", device_id="fake", mode="ai")
        result.steps = [StepResult(step_id="s01", intent="打开应用", action="launch_app",
                                   status=status,
                                   error="" if status == "passed" else "启动超时")]
        result.artifacts_dir = str(self.cfg.artifacts / "r1")

        class FakeRunner:
            def __init__(self, *a, **kw):
                pass

            def run(self, nl, out_dir=None, run_id=""):
                # 真实首跑按自然语言生成 case_id，这里照做，避免测试自己造出一致性假象
                case = CaseIR(case_id=nl.case_id, title=nl.title, platform=nl.platform,
                              created_by="ai")
                case.steps = [Step(step_id="s01", intent="打开应用",
                                   action=Action.LAUNCH_APP, args={"app": "com.demo"})]
                result.case_id = nl.case_id
                return case, result

        store = jobs.RunStore()
        job = store.create(run_id="r1", case_id="", title="例子", platform="android",
                           device_id="fake", mode="ai", text="打开应用")
        events: list[dict] = []

        @contextlib.contextmanager
        def fake_lease(*a, **kw):
            yield _FakeDriver()

        with mock.patch.object(jobs, "AiRunner", FakeRunner), \
             mock.patch.object(jobs, "LLMClient", lambda *a, **kw: mock.Mock(available=True)), \
             mock.patch.object(jobs, "LlmPlanner", lambda *a, **kw: mock.Mock()), \
             mock.patch.object(jobs, "DeviceManager") as mgr:
            mgr.return_value.lease = fake_lease
            jobs._run_ai(job, self.cfg, store, {}, events.append, "android", "fake",
                         False, "", 8, [])
        return job, events

    def test_failed_run_writes_no_case(self):
        job, events = self._run("failed")
        self.assertEqual(job.status, "failed")
        self.assertTrue(job.case_id)
        self.assertFalse(list(self.cfg.cases.glob(f"{job.case_id}*")),
                         "失败用例不该留下 stepir/脚本")
        db = CaseDB(self.cfg.db_file)
        try:
            self.assertIsNone(db.get_case(job.case_id), "失败用例不该进用例库")
        finally:
            db.close()
        kinds = [e.get("type") for e in events]
        self.assertIn("not_sedimented", kinds)
        self.assertNotIn("sedimented", kinds)
        self.assertNotIn("db_saved", kinds)
        # 执行记录要留：失败统计本身就是用例库的价值之一
        self.assertIn("run_finished", [e.get("type") for e in job.events])

    def test_passed_run_is_sedimented(self):
        job, events = self._run("passed")
        self.assertEqual(job.status, "passed")
        self.assertTrue(list(self.cfg.cases.glob(f"{job.case_id}*")))
        db = CaseDB(self.cfg.db_file)
        try:
            self.assertIsNotNone(db.get_case(job.case_id))
        finally:
            db.close()
        kinds = [e.get("type") for e in events]
        self.assertIn("sedimented", kinds)
        self.assertNotIn("not_sedimented", kinds)


if __name__ == "__main__":
    unittest.main()