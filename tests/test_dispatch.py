"""三端分发的极简入口与共享内核结构回归。

拆分前的 agents/runner.py(1995 行) / executor.py(1076 行) 现在只剩极简分发；
执行逻辑按职责落到 runners/ 与 executors/ 下的 mixin，三端脚本各自独立。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.agents.executor import Executor  # noqa: E402
from atp.agents.executors.core import ReplayCore  # noqa: E402
from atp.agents.executors.mobile import MobileExecutor  # noqa: E402
from atp.agents.executors.pc import PcExecutor  # noqa: E402
from atp.agents.executors.web import WebExecutor  # noqa: E402
from atp.agents.runner import AiRunner  # noqa: E402
from atp.agents.runners.core import RunnerCore  # noqa: E402
from atp.agents.runners.mobile import MobileRunner  # noqa: E402
from atp.agents.runners.pc import PcRunner  # noqa: E402
from atp.agents.runners.web import WebRunner  # noqa: E402
from atp.config import Config  # noqa: E402


class _Driver:
    def __init__(self, platform):
        self.platform = platform
        self.device_id = "fake"


class DispatchTest(unittest.TestCase):
    """统一入口按平台分发到对应端脚本。"""

    def test_ai_runner_dispatch(self):
        cfg = Config()
        for platform, expected in (
            ("android", MobileRunner), ("harmony", MobileRunner), ("ios", MobileRunner),
            ("desktop", PcRunner), ("web", WebRunner),
        ):
            with self.subTest(platform=platform):
                obj = AiRunner(_Driver(platform), cfg)
                self.assertIs(type(obj), expected)

    def test_executor_dispatch(self):
        cfg = Config()
        for platform, expected in (
            ("android", MobileExecutor), ("harmony", MobileExecutor), ("ios", MobileExecutor),
            ("desktop", PcExecutor), ("web", WebExecutor),
        ):
            with self.subTest(platform=platform):
                obj = Executor(_Driver(platform), cfg)
                self.assertIs(type(obj), expected)

    def test_unknown_platform_falls_back_to_entry(self):
        cfg = Config()
        self.assertIs(type(AiRunner(_Driver("unknown"), cfg)), AiRunner)
        self.assertIs(type(Executor(_Driver("unknown"), cfg)), Executor)


class StructureTest(unittest.TestCase):
    """入口极简、平台脚本独立、共享逻辑分层。"""

    def test_entries_are_thin(self):
        for name in ("runner.py", "executor.py"):
            p = ROOT / "atp" / "agents" / name
            self.assertLess(len(p.read_text(encoding="utf-8").splitlines()), 80,
                            f"{name} 应只做分发，不能重新膨胀")

    def test_platform_scripts_are_separate_files(self):
        for group in ("runners", "executors"):
            for name in ("pc.py", "mobile.py", "web.py"):
                self.assertTrue((ROOT / "atp" / "agents" / group / name).exists(),
                                f"缺少 {group}/{name}")

    def test_shared_mixins_exist(self):
        for group in ("runners", "executors"):
            self.assertTrue((ROOT / "atp" / "agents" / group / "core.py").exists())

    def test_platform_profiles_are_fixed(self):
        cfg = Config()
        self.assertEqual(AiRunner(_Driver("android"), cfg).profile.key, "mobile")
        self.assertEqual(AiRunner(_Driver("desktop"), cfg).profile.key, "pc")
        self.assertEqual(AiRunner(_Driver("web"), cfg).profile.key, "web")

    def test_core_is_shared_base(self):
        self.assertTrue(issubclass(MobileRunner, RunnerCore))
        self.assertTrue(issubclass(PcRunner, RunnerCore))
        self.assertTrue(issubclass(WebRunner, RunnerCore))
        self.assertTrue(issubclass(MobileExecutor, ReplayCore))
        self.assertTrue(issubclass(PcExecutor, ReplayCore))
        self.assertTrue(issubclass(WebExecutor, ReplayCore))


if __name__ == "__main__":
    unittest.main()
