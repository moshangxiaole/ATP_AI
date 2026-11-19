"""鸿蒙 launch_app：隐式启动失败必须被识别，ability 缺失要自动解析。

事故 run-20261117-164437-238b：`aa start -b X` 不带 `-a` 在鸿蒙上解析失败，
系统弹「暂无可用打开方式」，而 hdc/aa 失败 exit 仍是 0，执行器误报「已启动」，
AI 带着错误假设继续跑导致整场执行跑偏。
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.agents.platforms.mobile.harmony import HarmonyHdcDriver  # noqa: E402
from atp.config import Config  # noqa: E402

_BM_DUMP = '''
{
  "name": "com.example.videoapp.harmony",
  "app": [{"name": "com.example.videoapp.harmony", "label": "示例视频应用"}],
  "module": [{
    "name": "entry",
    "ability": [{
        "name": "EntryAbility",
        "exported": true
    }, {
        "name": "SecondAbility",
        "exported": false
    }]
  }]
}
'''


def _driver() -> HarmonyHdcDriver:
    return HarmonyHdcDriver("H0D6R2", Config())


class EntryAbilityTest(unittest.TestCase):
    def test_parses_first_ability_from_bm_dump(self):
        d = _driver()
        with mock.patch.object(d, "_shell", return_value=_BM_DUMP):
            self.assertEqual(d._entry_ability("com.example.videoapp.harmony"), "EntryAbility")
        # 缓存：第二次不再查设备
        with mock.patch.object(d, "_shell", side_effect=AssertionError("不应再查")):
            self.assertEqual(d._entry_ability("com.example.videoapp.harmony"), "EntryAbility")

    def test_dump_failure_returns_empty(self):
        d = _driver()
        with mock.patch.object(d, "_shell", side_effect=RuntimeError("设备离线")):
            self.assertEqual(d._entry_ability("com.x.y"), "")
        self.assertEqual(d._abilities["com.x.y"], "")


class LaunchAppTest(unittest.TestCase):
    def test_app_id_with_ability_used_directly(self):
        d = _driver()
        with mock.patch.object(d, "_shell", return_value="start ability successfully.") as sh:
            d.launch_app("com.x.y/EntryAbility")
        argv = sh.call_args[0]
        self.assertEqual(argv, ("aa", "start", "-b", "com.x.y", "-a", "EntryAbility"))

    def test_missing_ability_resolved_from_device(self):
        d = _driver()
        with mock.patch.object(d, "_shell", return_value=_BM_DUMP) as sh:
            d.launch_app("com.example.videoapp.harmony")
        argv = sh.call_args[0]
        self.assertEqual(argv, ("aa", "start", "-b", "com.example.videoapp.harmony", "-a", "EntryAbility"))

    def test_failed_launch_raises_not_silently_ok(self):
        d = _driver()
        bad = ("error: failed to start ability.\n"
               "Error Code:10103101 Failed to find a matching application "
               "for implicit launch.")
        with mock.patch.object(d, "_shell", return_value=bad):
            with self.assertRaises(RuntimeError) as ctx:
                d.launch_app("com.example.videoapp.harmony")
        self.assertIn("10103101", str(ctx.exception))

    def test_success_output_does_not_raise(self):
        d = _driver()
        with mock.patch.object(d, "_shell", return_value="start ability successfully."):
            d.launch_app("com.example.videoapp.harmony/EntryAbility")   # 不抛即通过


if __name__ == "__main__":
    unittest.main()
