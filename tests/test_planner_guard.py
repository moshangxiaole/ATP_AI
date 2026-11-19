"""规划器「看得见目标、点得中目标」回归。

事故 run-20261113-110342-3ef6：用例写明「点击【搜索】按钮」，AI 第 9 步却
用 xpath 点了分页数字「1」（status=passed，画面毫无变化），随后模型连接被重置，
整场执行连报告都没留下。三个独立缺陷：

1. focus_keywords 只认「」引号，用例全用【】→ 焦点词为空 → 目标优先排序失效；
2. 纯文案按钮（<button>搜索</button>，无 id/placeholder）只有 1 分，掉进
   rest 后被 110 节点上限截断 → 模型根本看不到【搜索】，只能随手挑一个节点；
3. 模型断连抛的是 http.client.RemoteDisconnected，不是 LLMError，穿透
   LlmPlanner.next 的 except，把整场执行打断。

外加一道防线：意图里点名的目标与选中节点文案不符时自动纠正，不再「点错也算过」。
"""

from __future__ import annotations

import http.client
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from atp.agents.runner import AiRunner, LlmPlanner, focus_keywords, indexed_nodes, rectify_target  # noqa: E402
from atp.agents.llm.client import LLMClient, LLMError, RESPONSE_CACHE  # noqa: E402
from atp.config import Config, ModelConfig  # noqa: E402
from atp.common.stepir import NLCase  # noqa: E402
from atp.common.types import UINode  # noqa: E402
from console_fakes import FakeLLMClient, FakeOaDriver, ScriptedPlanner  # noqa: E402

CASE_NL = (
    "打开https://demo.example.com/Admin/Public/login，【用户名】输入：05115，"
    "【密码】输入：zAj8VL^<@!bp，【安全码】输入：1。点击【登录】按钮，"
    "点击顶部左侧【协同】，再点击【新建事项】，在中间输入框输入【请假申请单】，"
    "点击【搜索】按钮，页面出现多条包含请假申请单的数据表示用例执行成功"
)


def _node(text: str, cls: str = "button", bounds=(10, 10, 60, 40),
          clickable: bool = True, desc: str = "", ident: str = "") -> UINode:
    return UINode(cls=cls, id=ident, text=text, desc=desc,
                  bounds=bounds, clickable=clickable)


def _busy_page() -> tuple[UINode, UINode]:
    """仿 OA 新建事项页：大量静态表格节点 + 输入框 + 分页 + 末尾的纯文案搜索按钮。"""
    root = UINode(cls="Dom", bounds=(0, 0, 1920, 1033))
    for i in range(160):
        root.children.append(_node(f"模板条目{i}", cls="span",
                                   bounds=(300, 200 + i * 4, 500, 220 + i * 4),
                                   clickable=False))
    for i, ph in enumerate(["请输入关键字进行过滤", "请输入模板标题关键字搜索", "到第"]):
        root.children.append(_node("", cls="input", desc=ph, ident=f"f{i}",
                                   bounds=(550, 180 + i * 40, 900, 215 + i * 40)))
    for i, t in enumerate(["1", "2", "3", "46", "确定"]):
        root.children.append(_node(t, cls="a", bounds=(630 + i * 22, 960,
                                                       650 + i * 22, 985)))
    search = _node("搜索", bounds=(900, 180, 980, 215))
    root.children.append(search)
    return root, search


class FocusKeywordTest(unittest.TestCase):
    def test_all_chinese_bracket_styles(self):
        nl = NLCase(title="t", nl="点【搜索】，再点「确定」、『返回』、“提交”和《帮助》")
        self.assertEqual(focus_keywords(nl), ["搜索", "确定", "返回", "提交", "帮助"])

    def test_real_case_extracts_search(self):
        words = focus_keywords(NLCase(title="t", nl=CASE_NL, platform="web"))
        self.assertIn("搜索", words)
        self.assertIn("协同", words)
        self.assertIn("新建事项", words)


class NodeTruncationTest(unittest.TestCase):
    def test_without_focus_the_search_button_is_dropped(self):
        """复现事故：焦点词为空时，纯文案按钮会被 110 节点上限挤掉。"""
        root, search = _busy_page()
        nodes, listing = indexed_nodes(root)
        self.assertFalse(any(n is search for n in nodes))
        self.assertNotIn("text='搜索'", listing)

    def test_focus_keeps_search_button_and_numbering_stays_consistent(self):
        root, search = _busy_page()
        nodes, listing = indexed_nodes(
            root, focus=focus_keywords(NLCase(title="t", nl=CASE_NL, platform="web")))
        hit = [i for i, n in enumerate(nodes) if n is search]
        self.assertTrue(hit, "【搜索】按钮必须出现在候选列表里")
        self.assertLess(hit[0], 10, "目标应该排在列表前部，别让模型去翻页找")
        # listing 里编号必须和 nodes 下标一致（末尾可能有截断提示行）
        lines = [l for l in listing.splitlines() if l.startswith("[")]
        self.assertGreaterEqual(len(lines), len(nodes))
        self.assertIn(f"[{hit[0]}] ", lines[hit[0]])


class RectifyTargetTest(unittest.TestCase):
    def test_wrong_node_is_corrected_to_matching_button(self):
        wrong, right = _node("1"), _node("搜索")
        got, visual = rectify_target(wrong, [wrong, right], "点击【搜索】按钮触发搜索")
        self.assertIs(got, right)
        self.assertEqual(visual, "")

    def test_button_label_inside_static_span_still_corrects(self):
        wrong = _node("1")
        span = _node("搜索", cls="span", clickable=False,
                     bounds=(900, 180, 980, 215))
        got, visual = rectify_target(wrong, [wrong, span], "点击【搜索】按钮触发搜索")
        self.assertIs(got, span)
        self.assertEqual(visual, "")

    def test_matching_node_kept_with_spaces(self):
        n = _node("登 录")
        got, visual = rectify_target(n, [n], "点击【登录】按钮提交表单")
        self.assertIs(got, n)
        self.assertEqual(visual, "")

    def test_no_matching_node_falls_back_to_visual(self):
        wrong = _node("1")
        got, visual = rectify_target(wrong, [wrong], "点击【搜索】按钮触发搜索")
        self.assertIsNone(got, "绝不能拿不相干的节点凑数")
        self.assertEqual(visual, "搜索")

    def test_intent_without_brackets_does_not_interfere(self):
        n = _node("1")
        got, visual = rectify_target(n, [n], "点击第一页按钮")
        self.assertIs(got, n)
        self.assertEqual(visual, "")

    def test_list_item_child_label_is_not_rectified_to_input_box(self):
        """run-20261118-091012：模型选对结果行，却被纠正去点搜索框。"""
        input_box = _node("示例关键词", cls="TextInput", clickable=True)
        result_text = _node("示例关键词")   # 文案挂在子 Text 上
        row = _node("", cls="ListItem", clickable=True)
        row.children = [result_text]
        intent = "点击搜索建议中的第一个条目【示例关键词】进入播放页"
        got, visual = rectify_target(row, [input_box, result_text, row], intent)
        self.assertIs(got, row, "文案匹配应看后代，正确的 ListItem 不能被纠正掉")
        self.assertEqual(visual, "")

    def test_rectify_never_picks_input_box_when_row_has_no_child_text(self):
        """结果行确实没有文案可选时，宁可走 point，也不能选输入框凑数。"""
        input_box = _node("示例关键词", cls="TextInput", clickable=True)
        wrong = _node("1")
        got, visual = rectify_target(
            wrong, [wrong, input_box],
            "点击搜索结果【示例关键词】")
        self.assertIsNone(got, "输入框文案再匹配也不能作为 tap 的纠正目标")
        self.assertEqual(visual, "示例关键词")

    def test_icon_only_node_is_not_blocked(self):
        icon = _node("", ident="", desc="")
        got, visual = rectify_target(icon, [icon], "点击【搜索】按钮")
        self.assertIs(got, icon, "图标按钮没有文案，不能凭空判定它选错了")
        self.assertEqual(visual, "")


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _ok_body() -> bytes:
    import json
    content = json.dumps({"ok": True})
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


def _always_failing(exc: Exception):
    """build_opener 的替身：无论哪条网络路径都抛同一个连接层异常。"""
    def build(*args):
        op = mock.Mock()
        op.open.side_effect = exc
        return op
    return build


class LLMNetworkTest(unittest.TestCase):
    def setUp(self):
        # 这几个用例都打同一段 payload（"hi"）。进程级响应去重缓存会让后两个
        # 用例命中前一个的缓存、根本不走 build_opener，测不到网络路径切换。
        # 这里每个用例前清空，保证每条都真实走一遍网络分支。
        RESPONSE_CACHE._data.clear()

    def _client(self) -> LLMClient:
        cfg = ModelConfig()
        cfg.base_url = "https://api.example.com/v1"
        cfg.api_key = "k"
        cfg.model = "m"
        return LLMClient(cfg, timeout=2)

    def test_remote_disconnected_becomes_llm_error(self):
        with mock.patch("atp.agents.llm.client.urllib.request.build_opener",
                        side_effect=_always_failing(
                            http.client.RemoteDisconnected("closed"))):
            with mock.patch("atp.agents.llm.client.time.sleep"):
                with self.assertRaises(LLMError) as ctx:
                    self._client().chat_json([{"role": "user", "content": "hi"}])
        self.assertIn("RemoteDisconnected", str(ctx.exception))
        self.assertIn("api.example.com", str(ctx.exception))

    def test_connection_failure_switches_network_path_and_recovers(self):
        """代理挂了就自动直连：本机代理临时不可用不该让整条用例失败。"""
        modes = []

        def build(*args):
            mode = "auto" if not args else "direct"
            modes.append(mode)
            op = mock.Mock()
            if mode == "auto":
                op.open.side_effect = OSError("WinError 10061 目标计算机积极拒绝")
            else:
                op.open.return_value = _Resp(_ok_body())
            return op

        with mock.patch("atp.agents.llm.client.urllib.request.build_opener",
                        side_effect=build):
            with mock.patch("atp.agents.llm.client.time.sleep"):
                d = self._client().chat_json([{"role": "user", "content": "hi"}])
        self.assertEqual(d, {"ok": True})
        self.assertEqual(modes, ["auto", "direct"])

    def test_no_proxy_env_forces_direct_from_first_try(self):
        with mock.patch.dict(os.environ, {"ATP_MODEL_NO_PROXY": "1"}):
            modes = []

            def build(*args):
                modes.append("auto" if not args else "direct")
                op = mock.Mock()
                op.open.return_value = _Resp(_ok_body())
                return op

            with mock.patch("atp.agents.llm.client.urllib.request.build_opener",
                            side_effect=build):
                self._client().chat_json([{"role": "user", "content": "hi"}])
        self.assertEqual(modes, ["direct"])

    def test_planner_aborts_instead_of_crashing_the_run(self):
        root, _ = _busy_page()
        obs = types.SimpleNamespace(root=root)
        with mock.patch("atp.agents.llm.client.urllib.request.build_opener",
                        side_effect=_always_failing(
                            http.client.RemoteDisconnected("closed"))):
            with mock.patch("atp.agents.llm.client.time.sleep"):
                d = LlmPlanner(self._client()).next(
                    NLCase(title="t", nl=CASE_NL), obs, [])
        self.assertEqual(d["action"], "abort")
        self.assertIn("模型不可用", d["abort_reason"])


class AbortStillProducesReportTest(unittest.TestCase):
    def test_abort_path_leaves_report_and_stepir(self):
        """模型断连走 abort 收尾：已跑过的步骤、报告、stepir 都要留下。"""
        tmp = Path(tempfile.mkdtemp(prefix="atp_abort_"))
        try:
            cfg = Config(home=tmp)
            cfg.artifacts_dir = str(tmp / "art")
            cfg.cases_dir = str(tmp / "cases")
            cfg.db_path = str(tmp / "data" / "t.db")
            nl = NLCase(case_id="abort-1", title="点登录", nl=CASE_NL, platform="android")
            runner = AiRunner(FakeOaDriver(), cfg,
                              planner=ScriptedPlanner(
                                  [{"action": "abort", "abort_reason": "模型连接中断"}]),
                              client=FakeLLMClient())
            case, result = runner.run(nl, out_dir=cfg.artifacts, run_id="abort-1")
            art = Path(cfg.artifacts) / "abort-1"
            self.assertTrue((art / "report.html").exists(), "异常路径也必须出报告")
            self.assertTrue((art / "stepir.json").exists(), "异常路径也必须留 stepir")
            self.assertTrue(result.report_path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
