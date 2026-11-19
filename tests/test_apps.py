"""应用台账（apps.json）的回归测试。

对应的是真机事故：用户说「打开【示例聊天应用】…」，控制台没有 app_id，首跑就在当前
屏幕上戳了一下就宣布成功，还沉淀成 1 步用例。根子是**没人把「示例聊天应用」翻成包名**。

这里固定四条：
1. 从自然语言里能认出应用名，且不把「打开应用」这类套话当名字；
2. 台账按包名存、按名字/别名/包名都能查回来，手工登记的条目不被打架；
3. 解析顺序是「台账 → 设备上找 → 写回台账」，第二次不再碰设备；
4. 执行器/首跑都走这条路径：步骤里的应用名被换成包名后再冷启动。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.agents.runner import AiRunner  # noqa: E402
from atp.config import Config  # noqa: E402
from atp.memory.apps import (AppRegistry, guess_app_names, looks_like_package,  # noqa: E402
                           resolve_app, resolve_from_text)
from atp.agents.executor import Executor  # noqa: E402
from atp.agents.perception.judge import Judge  # noqa: E402
from atp.common.stepir import CaseIR, NLCase, Step  # noqa: E402
from atp.common.types import Action, UINode  # noqa: E402
from atp.agents.platforms.base import BaseDriver  # noqa: E402

W, H = 1080, 1920
PKG = "com.example.chatapp.desktop"


class FakePhone(BaseDriver):
    """只会「打开应用」的假手机：记下 terminate/launch 的调用，find_app 可控。"""

    platform = "android"

    def __init__(self, found: tuple[str, str] | None = (PKG, f"{PKG}/.MainActivity")):
        super().__init__("fake-phone")
        self.found = found
        self.find_calls: list[str] = []
        self.calls: list[tuple[str, str]] = []

    # ---------- 感知 ----------
    def connect(self) -> None:
        self._connected = True

    def screen_size(self) -> tuple[int, int]:
        return W, H

    def screenshot(self) -> bytes:
        return b""

    def dump_tree(self) -> UINode:
        return UINode(cls="Root", bounds=(0, 0, W, H),
                      children=[UINode(cls="TextView", text="首页", bounds=(0, 0, W, 200))])

    # ---------- 输入 ----------
    def launch_app(self, app_id: str) -> None:
        self.calls.append(("launch", app_id))

    def terminate_app(self, app_id: str) -> None:
        self.calls.append(("terminate", app_id))

    # ---------- 找应用 ----------
    def find_app(self, name: str):
        self.find_calls.append(name)
        return self.found


class FakeDesktop(BaseDriver):
    """只会「启动应用」的假桌面驱动：记下 launch 调用，不做设备扫描。"""

    platform = "desktop"

    def __init__(self):
        super().__init__("fake-desktop")
        self.calls: list[tuple[str, str]] = []

    def connect(self) -> None:
        self._connected = True

    def screen_size(self) -> tuple[int, int]:
        return 1920, 1200

    def screenshot(self) -> bytes:
        return b""

    def dump_tree(self) -> UINode:
        return UINode(cls="Root", bounds=(0, 0, 1920, 1200))

    def launch_app(self, app_id: str) -> None:
        self.calls.append(("launch", app_id))


def cfg_for(tmp: Path) -> Config:
    cfg = Config(home=ROOT)
    cfg.artifacts_dir = str(tmp / "art")
    cfg.db_path = str(tmp / "t.db")
    cfg.apps_path = str(tmp / "apps.json")
    cfg.persist_runs = False
    cfg.screenshot_on_success = False
    cfg.cold_start.settle_ms = 0
    cfg.cold_start.launch_settle_ms = 0
    return cfg


class TestGuessNames(unittest.TestCase):
    def test_bracket_name(self):
        self.assertEqual(
            guess_app_names("打开【示例聊天应用】，消息首页点击右上角的【+】，在下拉框中点击【钱包】"),
            ["示例聊天应用"])

    def test_plain_name(self):
        self.assertEqual(guess_app_names("打开示例视频应用，搜索示例演员并播放"), ["示例视频应用"])

    def test_ignores_boilerplate(self):
        # 「打开应用」不是应用名，猜成这个会去启动一个叫"应用"的东西
        self.assertEqual(guess_app_names("打开应用并登录"), [])
        self.assertEqual(guess_app_names("点击登录按钮"), [])

    def test_multiple_candidates(self):
        self.assertEqual(guess_app_names("先打开微信，再打开示例聊天应用"), ["微信", "示例聊天应用"])

    def test_looks_like_package(self):
        self.assertTrue(looks_like_package(PKG))
        self.assertFalse(looks_like_package("示例聊天应用"))


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_apps_"))
        self.reg = AppRegistry(self.tmp / "apps.json")

    def test_remember_and_lookup(self):
        self.reg.remember("android", "示例聊天应用", PKG, activity=f"{PKG}/.MainActivity",
                          aliases=["iMOA"], source="device")
        for key in ("示例聊天应用", "iMOA", PKG, "imoa"):
            entry = self.reg.get("android", key)
            self.assertIsNotNone(entry, key)
            self.assertEqual(entry.app_id, PKG)

    def test_platform_isolation(self):
        self.reg.remember("android", "示例视频应用", "com.example.videoapp", source="case")
        self.reg.remember("harmony", "示例视频应用", "com.example.videoapp.harmony", source="case")
        self.assertEqual(self.reg.get("android", "示例视频应用").app_id, "com.example.videoapp")
        self.assertEqual(self.reg.get("harmony", "示例视频应用").app_id, "com.example.videoapp.harmony")

    def test_alias_merged_not_overwritten(self):
        self.reg.remember("android", "示例聊天应用", PKG, source="device")
        self.reg.remember("android", "iMOA", PKG, source="user")
        entry = self.reg.get("android", PKG)
        self.assertEqual(entry.name, "示例聊天应用")          # 先登记的名字不被顶掉
        self.assertIn("iMOA", entry.aliases)

    def test_manual_entry_keeps_source(self):
        self.reg.remember("android", "示例聊天应用", PKG, source="manual")
        self.reg.remember("android", "芒芒", PKG, source="device")
        entry = self.reg.get("android", PKG)
        self.assertEqual(entry.source, "manual")
        self.assertIn("芒芒", entry.aliases)

    def test_single_char_is_not_a_match(self):
        self.reg.remember("android", "示例视频应用", "com.example.videoapp", source="case")
        self.reg.remember("android", "示例视频应用极速版", "com.example.videoapp.lite", source="case")
        # 单字会同时命中两个，宁可查不到让用户补全
        self.assertIsNone(self.reg.get("android", "芒"))

    def test_file_roundtrip(self):
        self.reg.remember("android", "示例聊天应用", PKG, source="device")
        again = AppRegistry(self.tmp / "apps.json")
        entry = again.get("android", "示例聊天应用")
        self.assertEqual(entry.app_id, PKG)
        raw = json.loads((self.tmp / "apps.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 1)
        self.assertIn(PKG, raw["android"])

    def test_broken_file_does_not_crash(self):
        (self.tmp / "apps.json").write_text("{ 这不是 json", encoding="utf-8")
        self.assertEqual(AppRegistry(self.tmp / "apps.json").entries("android"), [])


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_apps_"))
        self.cfg = cfg_for(self.tmp)
        self.reg = AppRegistry.from_config(self.cfg)
        self.events: list[dict] = []

    def test_config_hit_skips_device(self):
        self.reg.remember("android", "示例聊天应用", PKG, source="device")
        drv = FakePhone()
        entry = resolve_app("示例聊天应用", registry=self.reg, driver=drv,
                            platform="android", on_event=self.events.append)
        self.assertEqual(entry.app_id, PKG)
        self.assertEqual(drv.find_calls, [], "台账命中就不该再问设备")
        self.assertEqual(self.events[0]["source"], "config")

    def test_device_discovery_writes_back(self):
        drv = FakePhone()
        entry = resolve_app("示例聊天应用", registry=self.reg, driver=drv,
                            platform="android", on_event=self.events.append)
        self.assertEqual(entry.app_id, PKG)
        self.assertEqual(drv.find_calls, ["示例聊天应用"])
        # 已经落盘：换一个 registry 实例（模拟下次执行）也能查到
        again = AppRegistry.from_config(self.cfg).get("android", "示例聊天应用")
        self.assertEqual(again.app_id, PKG)
        self.assertEqual(self.events[0]["source"], "device")

    def test_not_found_returns_none(self):
        drv = FakePhone(found=None)
        self.assertIsNone(resolve_app("不存在的应用", registry=self.reg, driver=drv,
                                      platform="android"))
        self.assertEqual(self.reg.entries("android"), [], "找不到不许瞎记")

    def test_from_text(self):
        drv = FakePhone()
        entry = resolve_from_text("打开【示例聊天应用】，消息首页点击右上角的【+】",
                                 registry=self.reg, driver=drv, platform="android")
        self.assertEqual(entry.name, "示例聊天应用")
        self.assertEqual(drv.find_calls, ["示例聊天应用"])


class TestExecutorUsesRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_apps_"))
        self.cfg = cfg_for(self.tmp)

    def _case(self, app: str) -> CaseIR:
        case = CaseIR(case_id="app-case", title="打开应用", platform="android", app_id="")
        case.steps = [Step(step_id="s01", intent=f"打开应用 {app}",
                           action=Action.LAUNCH_APP, args={"app": app})]
        return case

    def test_name_is_resolved_before_cold_start(self):
        drv = FakePhone()
        events: list[dict] = []
        r = Executor(drv, self.cfg, Judge(), None, on_event=events.append).run(
            self._case("示例聊天应用"), mode="replay")

        self.assertTrue(r.passed, [s.error for s in r.steps if s.error])
        # 杀进程和启动都必须用包名，不能拿「示例聊天应用」去 am force-stop
        self.assertEqual(drv.calls, [("terminate", PKG), ("launch", PKG)])
        self.assertIn("应用台账", r.steps[0].detail)
        self.assertEqual(r.steps[0].status, "passed")
        kinds = [e["type"] for e in events]
        self.assertIn("app_resolved", kinds)
        self.assertIn("app_used", kinds)

    def test_second_run_never_touches_device(self):
        drv = FakePhone()
        Executor(drv, self.cfg, Judge(), None).run(self._case("示例聊天应用"), mode="replay")
        self.assertEqual(drv.find_calls, ["示例聊天应用"])

        drv2 = FakePhone()
        Executor(drv2, self.cfg, Judge(), None).run(self._case("示例聊天应用"), mode="replay")
        self.assertEqual(drv2.find_calls, [], "第二次应该直接读台账")

    def test_unknown_app_keeps_original_behaviour(self):
        drv = FakePhone(found=None)
        r = Executor(drv, self.cfg, Judge(), None).run(self._case("没装的应用"), mode="replay")
        self.assertEqual(drv.calls, [("terminate", "没装的应用"), ("launch", "没装的应用")])
        self.assertTrue(r.passed)

    def test_empty_app_arg_inherits_case_app_id(self):
        """真机事故（run-20261112-185027）：AI 规划器产出 args={"app": ""}，
        case.app_id 明明有包名，驱动却收到空串直接报「缺少包名」。
        执行器必须把 case 级包名写回步骤参数，不能只用来做解析。"""
        case = CaseIR(case_id="app-case", title="打开应用", platform="android",
                      app_id=PKG)
        case.steps = [Step(step_id="s01", intent="打开【示例聊天应用】应用",
                           action=Action.LAUNCH_APP, args={"app": ""})]
        drv = FakePhone()
        r = Executor(drv, self.cfg, Judge(), None).run(case, mode="replay")
        self.assertTrue(r.passed, [s.error for s in r.steps if s.error])
        self.assertEqual(drv.calls, [("terminate", PKG), ("launch", PKG)])
        self.assertEqual(case.steps[0].args["app"], PKG, "包名必须写回步骤参数")

    def test_empty_app_and_case_resolves_from_title(self):
        """run-20261113-145734-f20f 事故：AI 在桌面端既没填 args.app 也没填 case.app_id，
        执行器必须能从用例标题「打开【示例聊天应用-测试版】」反查出桌面启动路径，
        否则 launch_app 拿空串去 os.startfile 必然失败、后续步骤全崩。"""
        reg = AppRegistry.from_config(self.cfg)
        reg.remember("desktop", "示例聊天应用-测试版",
                     "C:/Program Files/samplechat-pc/示例聊天应用.exe",
                     aliases=["示例聊天应用"], source="manual")
        case = CaseIR(case_id="app-case",
                      title="打开【示例聊天应用-测试版】，顶部搜索栏输入【联系人乙】",
                      platform="desktop", app_id="")
        case.steps = [Step(step_id="s01", intent="启动目标应用【示例聊天应用-测试版】",
                           action=Action.LAUNCH_APP, args={"app": ""})]
        drv = FakeDesktop()
        r = Executor(drv, self.cfg, Judge(), None).run(case, mode="replay")
        self.assertTrue(r.passed, [s.error for s in r.steps if s.error])
        self.assertEqual(drv.calls, [("launch", "C:/Program Files/samplechat-pc/示例聊天应用.exe")])
        self.assertEqual(case.steps[0].args["app"], "C:/Program Files/samplechat-pc/示例聊天应用.exe")


class TestAiRunnerResolvesAppName(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_apps_"))
        self.cfg = cfg_for(self.tmp)

    def test_console_run_gets_app_id_from_sentence(self):
        drv = FakePhone()
        runner = AiRunner(drv, self.cfg)
        nl = NLCase(case_id="c1", title="打开示例聊天应用", platform="android", app_id="",
                    nl="打开【示例聊天应用】，消息首页点击右上角的【+】，在下拉框中点击【钱包】")
        case = CaseIR(case_id="c1", title=nl.title, platform="android")

        runner._resolve_app_name(nl, case)

        self.assertEqual(nl.app_id, PKG, "没解析出包名，首跑就没有 launch 步骤")
        self.assertEqual(case.app_id, PKG)
        self.assertEqual(drv.find_calls, ["示例聊天应用"])

    def test_package_app_id_is_kept(self):
        drv = FakePhone()
        runner = AiRunner(drv, self.cfg)
        nl = NLCase(case_id="c2", title="打开示例聊天应用", platform="android", app_id=PKG,
                    nl="打开示例聊天应用")
        runner._resolve_app_name(nl, CaseIR(case_id="c2", title=nl.title))
        self.assertEqual(nl.app_id, PKG)
        self.assertEqual(drv.find_calls, [])

    def test_desktop_resolves_from_registry(self):
        # 桌面端现在也要从台账认应用（示例聊天应用-测试版 是 PC 客户端，启动路径登记在 apps.json）。
        # 台账命中就不该再去设备上找——桌面没有包管理器概念。
        reg = AppRegistry.from_config(self.cfg)
        reg.remember("desktop", "示例聊天应用-测试版",
                     "C:/Program Files/samplechat-pc/示例聊天应用.exe",
                     aliases=["示例聊天应用"], source="manual")
        drv = FakePhone()
        drv.platform = "desktop"
        runner = AiRunner(drv, self.cfg)
        nl = NLCase(case_id="c3", title="打开示例聊天应用-测试版", platform="desktop", app_id="",
                    nl="打开【示例聊天应用-测试版】，顶部搜索栏输入【联系人乙】")
        runner._resolve_app_name(nl, CaseIR(case_id="c3", title=nl.title))
        self.assertEqual(nl.app_id, "C:/Program Files/samplechat-pc/示例聊天应用.exe")
        self.assertEqual(drv.find_calls, [], "桌面端不扫设备")

    def test_desktop_unknown_stays_empty(self):
        # 台账里没有、且桌面端不扫设备：保持原样，绝不瞎认一个。
        drv = FakePhone(found=None)
        drv.platform = "desktop"
        runner = AiRunner(drv, self.cfg)
        nl = NLCase(case_id="c4", title="打开随便什么", platform="desktop", app_id="",
                    nl="打开随便什么应用")
        runner._resolve_app_name(nl, CaseIR(case_id="c4", title=nl.title))
        self.assertEqual(nl.app_id, "")
        self.assertEqual(drv.find_calls, [], "桌面端台账没有、也不扫设备，保持原样")


if __name__ == "__main__":
    unittest.main()
