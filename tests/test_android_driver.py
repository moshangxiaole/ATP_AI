"""Android 驱动的回归测试。

这一层管的是"通道本身靠不靠谱"——真机上调通一次很容易，下次换台机器还能不能
自动成立，就靠这些用例兜着：

1. 中文输入：`adb shell input text` 只吃 ASCII，必须自动切到 ADBKeyBoard 广播注入，
   而且用完要把输入法还给用户（不然测试机会被改成"测试专用机"）。
2. 冷启动：App 会记住上次页面（示例视频应用 回到 SearchActivity），用例起点必须确定。
3. 抓树：视频播放时界面永不 idle，uiautomator 会死等 10 秒再报错，不能重试三遍。

全部走假 adb，不连真机：
    python -m unittest tests.test_android_driver -v
"""

from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.config import Config  # noqa: E402
from atp.agents.platforms.mobile.android import AndroidAdbDriver  # noqa: E402
from atp.agents.platforms.base import NotSupported  # noqa: E402

SOGOU = "com.sohu.inputmethod.sogou.vivo/.SogouIME"
ADB_IME = "com.android.adbkeyboard/.AdbIME"
PKG = "com.example.videoapp"
LAUNCHER = "com.example.videoapp/.MainActivity"


class FakeAdb(AndroidAdbDriver):
    """把 adb 调用截下来，按命令行子串返回假输出。"""

    def __init__(self, responses: dict[str, str] | None = None, ime: str = SOGOU):
        super().__init__("fake-device", Config())
        self.calls: list[str] = []
        self.responses = {"settings get secure default_input_method": ime + "\n"}
        self.responses.update(responses or {})
        self.fail_calls: set[str] = set()

    def _run(self, args, binary=False, timeout=30):  # noqa: ANN001, ANN201
        key = " ".join(args)
        self.calls.append(key)
        for pat, val in self.responses.items():
            if pat in key:
                return val
        return ""

    def count(self, needle: str) -> int:
        return sum(1 for c in self.calls if needle in c)


class TestChineseInput(unittest.TestCase):
    def test_ascii_does_not_touch_ime(self):
        d = FakeAdb()
        d.input_text("hello")
        self.assertEqual(d.count("ime set"), 0)
        self.assertEqual(d.count("input text hello"), 1)

    def test_chinese_switches_ime_and_broadcasts_base64(self):
        d = FakeAdb()
        d.input_text("示例演员")
        self.assertEqual(d.count(f"ime set {ADB_IME}"), 1)
        b64 = base64.b64encode("示例演员".encode()).decode()
        self.assertEqual(d.count(f"ADB_INPUT_B64 --es msg {b64}"), 1)
        self.assertEqual(d.count("input text"), 0)

    def test_second_chinese_input_does_not_switch_again(self):
        d = FakeAdb()
        d.input_text("示例演员")
        d.input_text("示例电视剧")
        self.assertEqual(d.count(f"ime set {ADB_IME}"), 1)

    def test_close_restores_user_ime(self):
        d = FakeAdb()
        d.input_text("示例演员")
        d.close()
        self.assertEqual(d.count(f"ime set {SOGOU}"), 1)

    def test_no_restore_when_never_switched(self):
        d = FakeAdb()
        d.input_text("abc")
        d.close()
        self.assertEqual(d.count("ime set"), 0)

    def test_missing_text_ime_raises(self):
        # 三台候选都切不过去 → 明确报错，而不是静默输入失败
        d = FakeAdb()
        d._switch_ime = lambda ime: False  # type: ignore[method-assign]
        with self.assertRaises(NotSupported):
            d.input_text("示例演员")

    def test_clear_text_uses_broadcast_on_text_ime(self):
        d = FakeAdb(ime=ADB_IME)
        d.clear_text()
        self.assertEqual(d.count("ADB_CLEAR_TEXT"), 1)
        self.assertEqual(d.count("KEYCODE_DEL"), 0)

    def test_clear_text_batches_backspaces_otherwise(self):
        # 非 ADBKeyBoard 时按聚焦控件长度退格，且要成批（一次 keyevent 多个 keycode）
        d = FakeAdb(ime=SOGOU)
        from atp.common.types import UINode

        d._focused = UINode(cls="EditText", text="爱的修学旅行2")
        d.clear_text()
        self.assertEqual(d.count("KEYCODE_MOVE_END"), 1)
        del_calls = [c for c in d.calls if "KEYCODE_DEL" in c]
        self.assertEqual(len(del_calls), 1)          # 7 字 → 一批就够
        self.assertEqual(del_calls[0].count("KEYCODE_DEL"), 11)   # max(8, 7+4)


class TestLaunch(unittest.TestCase):
    def test_uses_am_start_with_clear_top(self):
        # monkey 走 LAUNCHER intent，App 会恢复到上次页面（SearchActivity），
        # 用例第一步就找不到首页入口。必须显式 am start 入口 Activity。
        d = FakeAdb({"resolve-activity": "priority=0 preferredOrder=0\n" + LAUNCHER})
        d.launch_app(PKG)
        self.assertEqual(d.count(f"am start -n {LAUNCHER} --activity-clear-top"), 1)
        self.assertEqual(d.count("monkey"), 0)

    def test_falls_back_to_monkey_without_launcher(self):
        d = FakeAdb({"resolve-activity": "priority=0 preferredOrder=0\n"})
        d.launch_app(PKG)
        self.assertEqual(d.count(f"monkey -p {PKG}"), 1)

    def test_accepts_pkg_activity_form(self):
        # 应用台账里存了入口组件，就直接用它启动：少一次 resolve-activity（真机 0.4s/次），
        # 也避免"台账写的是 A、启动解析成 B"这种两套答案
        d = FakeAdb({"resolve-activity": LAUNCHER})
        d.launch_app("com.example.videoapp/.MainActivity")
        self.assertEqual(d.count(f"am start -n {PKG}/.MainActivity --activity-clear-top"), 1)
        self.assertEqual(d.count("resolve-activity"), 0)

    def test_terminate_strips_activity_suffix(self):
        d = FakeAdb()
        d.terminate_app("com.example.videoapp/.MainActivity")
        self.assertEqual(d.count(f"am force-stop {PKG}"), 1)

    def test_perform_empty_app_string_falls_back_to_driver_app_id(self):
        # 真机事故：AI 规划器产出 args={"app": ""}——键存在值为空串，
        # args.get("app", self.app_id) 的默认值取不到，驱动收到空包名报「缺少包名」，
        # 而 case 级 app_id 明明有包名。perform 必须显式兜底。
        from atp.common.types import Action

        d = FakeAdb({"resolve-activity": "priority=0 preferredOrder=0\n" + LAUNCHER})
        d.app_id = PKG
        res = d.perform(Action.LAUNCH_APP, None, {"app": ""})
        self.assertTrue(res.ok)
        self.assertEqual(d.count(f"am start -n {LAUNCHER} --activity-clear-top"), 1)

    def test_perform_empty_app_string_terminate_falls_back(self):
        from atp.common.types import Action

        d = FakeAdb()
        d.app_id = PKG
        d.perform(Action.TERMINATE_APP, None, {"app": ""})
        self.assertEqual(d.count(f"am force-stop {PKG}"), 1)


HIERARCHY = ('<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
             '<node class="android.widget.FrameLayout" text="" bounds="[0,0][1260,2800]">'
             '<node class="android.widget.TextView" text="首页" bounds="[0,2600][1260,2700]"/>'
             "</node></hierarchy>")


class TestDumpTree(unittest.TestCase):
    def test_idle_error_is_not_retried(self):
        # 视频播放中：界面永不 idle，重试只是把 12 秒罚三遍
        d = FakeAdb({"uiautomator dump": "ERROR: could not get idle state."})
        with self.assertRaises(RuntimeError) as ctx:
            d.dump_tree()
        self.assertIn("不空闲", str(ctx.exception))
        self.assertEqual(d.count("uiautomator dump"), 1)

    def test_stale_cache_used_while_not_idle(self):
        d = FakeAdb({"uiautomator dump": "UI hierchary dumped to: /sdcard/atp_ui.xml",
                     "cat /sdcard/atp_ui.xml": HIERARCHY})
        first = d.dump_tree()
        self.assertTrue(any(n.text == "首页" for n in first.walk()))

        # 界面开始播视频，树刷不出来：3 秒内的缓存还能顶一下，
        # 免得播放中的一步因为抓不到树直接判失败
        d.responses["uiautomator dump"] = "ERROR: could not get idle state."
        second = d.dump_tree()
        self.assertTrue(d._tree_stale)
        self.assertTrue(any(n.text == "首页" for n in second.walk()))

    def test_cache_expires(self):
        d = FakeAdb({"uiautomator dump": "UI hierchary dumped to: /sdcard/atp_ui.xml",
                     "cat /sdcard/atp_ui.xml": HIERARCHY})
        d.dump_tree()
        d.responses["uiautomator dump"] = "ERROR: could not get idle state."
        d._tree_at -= 10          # 假装缓存已经过期
        with self.assertRaises(RuntimeError):
            d.dump_tree()


if __name__ == "__main__":
    unittest.main()
