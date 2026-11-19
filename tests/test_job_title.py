"""执行卡片/用例标题不能截断 —— 界面上那半句话的真机 bug。

回归的是这一条：用户输入
  「打开【示例聊天应用】，消息首页点击右上角的【+】，在下拉框中点击【钱包】，页面进入【我的钱包】则执行成功」
卡片标题只显示 22 个字，断在「…点击右上角的【+】，在」，用户认不出自己发了什么。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.webapp.jobs import job_title  # noqa: E402

SENTENCE = ("打开【示例聊天应用】，消息首页点击右上角的【+】，在下拉框中点击【钱包】，"
            "页面进入【我的钱包】则执行成功")


class TestJobTitle(unittest.TestCase):
    def test_long_sentence_is_not_cut(self):
        title = job_title({"title": SENTENCE}, SENTENCE)
        self.assertEqual(title, SENTENCE)
        self.assertGreater(len(title), 22)
        self.assertTrue(title.endswith("则执行成功"), "标题必须到句尾，不能停在半句")

    def test_falls_back_to_text(self):
        self.assertEqual(job_title({}, SENTENCE), SENTENCE)
        self.assertEqual(job_title({"title": "  "}, SENTENCE), SENTENCE)

    def test_explicit_title_wins(self):
        self.assertEqual(job_title({"title": "回放：我的钱包入口"}, SENTENCE), "回放：我的钱包入口")

    def test_whitespace_is_collapsed(self):
        # 多行粘贴进来的指令压成一行，否则卡片标题会把头部撑成多段
        self.assertEqual(job_title({"title": "打开【示例聊天应用】\n  点击右上角\t【+】"}, "x"),
                         "打开【示例聊天应用】 点击右上角 【+】")

    def test_no_trailing_space(self):
        self.assertEqual(job_title({"title": "  打开示例视频应用  "}, "x"), "打开示例视频应用")


if __name__ == "__main__":
    unittest.main()
