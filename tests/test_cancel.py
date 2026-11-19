"""取消必须真的停下，而不是"下一个事件到了再说"。

早先的实现只在事件回调里抛异常：一步动作动辄十几秒（等界面稳定、滑动查找、
断言轮询），这段时间一个事件都不发，用户点了取消只能干等，看起来就是"点了没反应"。
现在执行循环自己轮询取消标志——每步之间、每次等待里、每次滑动前都查一次。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from atp.config import Config  # noqa: E402
from atp.agents.executor import Executor  # noqa: E402
from atp.common.stepir import CaseIR, Step  # noqa: E402
from atp.common.types import Action, RunCancelled  # noqa: E402
from console_fakes import FakeOaDriver  # noqa: E402
from atp.webapp.jobs import RunStore, _execute  # noqa: E402


def _cfg(tmp: Path) -> Config:
    cfg = Config(home=tmp)
    cfg.artifacts_dir = str(tmp / "art")
    cfg.cases_dir = str(tmp / "cases")
    cfg.db_path = str(tmp / "data" / "t.db")
    cfg.persist_runs = False          # 取消用例不测落库，别把库掺进这一层
    cfg.cold_start.enabled = False
    return cfg


class ExecutorCancelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_cancel_"))
        self.cfg = _cfg(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _case(n: int = 4) -> CaseIR:
        case = CaseIR(case_id="cancel-case", title="连续返回", platform="android")
        case.steps = [Step(step_id=f"s{i:02d}", intent=f"第 {i} 次返回", action=Action.BACK)
                      for i in range(1, n + 1)]
        return case

    def test_executor_stops_mid_run_and_keeps_partial(self):
        checks = {"n": 0}

        def should_cancel() -> bool:
            # 第 1 步放行（循环一次 + 步内两次检查），第 2 步的检查点就取消
            checks["n"] += 1
            return checks["n"] > 3

        ex = Executor(FakeOaDriver(), self.cfg, should_cancel=should_cancel)
        started = time.time()
        with self.assertRaises(RunCancelled) as ctx:
            ex.run(self._case(), run_id="r-cancel")

        self.assertLess(time.time() - started, 10, "取消后不该继续把剩余步骤跑完")
        partial = getattr(ctx.exception, "partial", None)
        self.assertIsNotNone(partial, "中断时要把已跑完的部分带出来")
        self.assertEqual(len(partial.steps), 1)
        # 已跑的步骤是通过的，但"跑完了"这件事不成立，绝不能被当成通过
        self.assertTrue(partial.steps[0].ok)
        self.assertTrue(partial.aborted)
        self.assertFalse(partial.passed)
        self.assertTrue(partial.report_path and Path(partial.report_path).exists(),
                        "中断也要留一份报告，说明停在哪一步")

    def test_no_cancel_runs_to_the_end(self):
        ex = Executor(FakeOaDriver(), self.cfg)
        result = ex.run(self._case(3), run_id="r-ok")
        self.assertEqual(len(result.steps), 3)
        self.assertTrue(result.passed)

    def test_job_cancel_flag_is_an_event(self):
        store = RunStore()
        job = store.create(run_id="r1", case_id="", title="t", platform="android",
                           device_id="", mode="ai", text="随便")
        self.assertFalse(job.cancel_requested)
        job.request_cancel()
        self.assertTrue(job.cancel_requested)
        self.assertTrue(job.cancel_event.is_set())
        # 老写法（直接赋值）也要继续有效，测试与外部调用都在用
        job.cancel_requested = False
        self.assertFalse(job.cancel_event.is_set())
        self.assertIn("cancel_requested", job.to_dict())


class JobCancelTest(unittest.TestCase):
    """任务层：开跑前就取消的运行，必须落到 cancelled 且不沉淀。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_jobcancel_"))
        self.cfg = _cfg(self.tmp)
        self.cfg.persist_runs = True

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cancel_before_start_finishes_as_cancelled(self):
        store = RunStore()
        job = store.create(run_id="r-c", case_id="", title="t", platform="android",
                           device_id="", mode="ai", text="随便")
        job.request_cancel()
        _execute(job, self.cfg, store, {"text": "随便", "platform": "android", "mode": "ai"})
        self.assertEqual(job.status, "cancelled")
        kinds = [e["type"] for e in job.events]
        self.assertIn("run_finished", kinds)
        self.assertNotIn("sedimented", kinds)
        self.assertFalse(list(self.cfg.cases.glob("*.stepir.json")))


if __name__ == "__main__":
    unittest.main()
