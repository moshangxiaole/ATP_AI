"""页面栈契约：同页不新增帧 + 可选落盘（AC-4.2 的 P1 部分）。

这里回答三个问题：
1. 「同一页」判得住吗？（包名 + 粗签名，绝不用含 bounds 的 fine_fp）
2. 同页输入 / 开关会不会把栈顶顶成重复帧，导致 ``stack[-2]`` 拿不到上一页？
3. ``persist_stack=true`` 时产物可读、字段齐全、**不含绝对坐标**，且写失败不炸。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from mobile_qa_helpers import (  # noqa: E402
    APP, OTHER_APP, TreeDriver, built_stack, events_of, make_case, make_config,
    make_executor, page, shifted,
)
from atp.agents.perception.page_stabilizer import (  # noqa: E402
    SAME_PAGE_THRESHOLD, PageFrame, PageStack, jaccard, page_signature,
)


def other_page(count: int = 45):
    """与首页粗签名差异很大的另一个页面。"""
    from atp.common.types import UINode

    rows = [UINode(cls="Text", id=f"other_{i}", text=f"陌生页条目{i}",
                   bounds=(40, 200 + i * 30, 1040, 228 + i * 30))
            for i in range(count)]
    return UINode(cls="Page", bounds=(0, 0, 1080, 2400), children=[
        UINode(cls="Column", id="other_root", bounds=(0, 100, 1080, 2300),
               children=rows),
    ])


class SamePageTest(unittest.TestCase):
    """``PageStack.same_page``：包名 + 粗签名，缺一不可。"""

    def setUp(self):
        self.root = page(count=40)
        self.sig = page_signature(self.root)

    def _stack_with(self, root=None, package=APP) -> PageStack:
        stack = PageStack()
        stack.push(PageFrame(step_id="s00", fine_fp="fp-a",
                             coarse_sig=page_signature(root or self.root),
                             package=package))
        return stack

    def test_same_tree_is_same_page(self):
        stack = self._stack_with()
        frame = PageFrame(step_id="s01", fine_fp="fp-b", coarse_sig=self.sig,
                          package=APP)
        self.assertTrue(stack.same_page(frame))

    def test_bounds_shift_does_not_make_a_new_page(self):
        """整屏平移：fine_fp 一定变，粗签名不变 → 还是同一页（AC-1.10）。"""
        stack = self._stack_with()
        moved = page_signature(shifted(self.root, dy=7))
        self.assertGreaterEqual(jaccard(self.sig, moved), SAME_PAGE_THRESHOLD)
        frame = PageFrame(step_id="s01", fine_fp="fp-shifted",
                          coarse_sig=moved, package=APP)
        self.assertTrue(stack.same_page(frame), "滑动 / 键盘顶起不该算跳页")

    def test_different_tree_is_new_page(self):
        stack = self._stack_with()
        frame = PageFrame(step_id="s01", fine_fp="fp-c",
                          coarse_sig=page_signature(other_page()), package=APP)
        self.assertFalse(stack.same_page(frame))

    def test_package_change_is_never_same_page(self):
        """签名碰巧相似也不能算同一页：切到别的 App 是偏离，不是同页操作。"""
        stack = self._stack_with()
        frame = PageFrame(step_id="s01", fine_fp="fp-b", coarse_sig=self.sig,
                          package=OTHER_APP)
        self.assertFalse(stack.same_page(frame))

    def test_empty_signature_is_not_trusted(self):
        stack = self._stack_with()
        frame = PageFrame(step_id="s01", fine_fp="fp-b", coarse_sig=frozenset(),
                          package=APP)
        self.assertFalse(stack.same_page(frame), "签名缺失时不许判同一页")


class UpdateCurrentTest(unittest.TestCase):
    """``update_current`` 只刷新栈顶，绝不改写「这一页是第几步进来的」。"""

    def test_keeps_identity_and_refreshes_seen_time(self):
        stack = built_stack(page(count=30), package=APP, steps=2)
        top = stack.current()
        frame_id, step_id, at = top.frame_id, top.step_id, top.at
        self.assertEqual(len(stack), 2)

        merged = stack.update_current(fine_fp="fp-new", coarse_sig=frozenset({("n", "x", "", "")}),
                                      package=APP, before_shot="04_s01_after.png")

        self.assertEqual(len(stack), 2, "同页更新不得新增帧")
        self.assertIs(merged, top)
        self.assertEqual(merged.frame_id, frame_id)
        self.assertEqual(merged.step_id, step_id, "入栈步号不能被同页操作覆盖")
        self.assertEqual(merged.at, at)
        self.assertGreaterEqual(merged.last_seen, at)
        self.assertEqual(merged.fine_fp, "fp-new")
        self.assertEqual(merged.before_shot, "04_s01_after.png")

    def test_empty_stack_returns_none(self):
        self.assertIsNone(PageStack().update_current(fine_fp="fp"))

    def test_frames_returns_a_copy(self):
        stack = built_stack(steps=2)
        frames = stack.frames()
        frames.clear()
        self.assertEqual(len(stack), 2, "frames() 必须返回浅拷贝")


class StackDictTest(unittest.TestCase):
    """``as_dict``：字段齐全、可读，且**不含绝对坐标**。"""

    def test_shape_and_no_absolute_coordinates(self):
        stack = built_stack(page(count=30), package=APP, steps=2)
        stack.set_run_id("run-42")
        data = stack.as_dict()

        self.assertEqual(data["version"], 1)
        self.assertEqual(data["run_id"], "run-42")
        self.assertEqual(data["depth"], 2)
        self.assertEqual(len(data["frames"]), 2)

        frame = data["frames"][-1]
        for key in ("frame_id", "step_id", "fine_fp", "coarse_sig", "package",
                    "before_shot", "at", "last_seen", "stable"):
            self.assertIn(key, frame, f"帧字段缺失：{key}")
        self.assertEqual(frame["coarse_sig"]["nodes"], len(page_signature(page(count=30))))
        self.assertEqual(len(frame["coarse_sig"]["hash"]), 8)

        dumped = json.dumps(data, ensure_ascii=False)
        for leaked in ("bounds", "[0, 0, 1080", "children"):
            self.assertNotIn(leaked, dumped, f"页面栈产物不得含节点细节：{leaked}")

    def test_hash_is_stable_across_processes(self):
        """同一棵树两次求摘要必须一致（内置 hash() 有随机盐，不能用）。"""
        a = PageFrame(coarse_sig=page_signature(page(count=20))).sig_summary()
        b = PageFrame(coarse_sig=page_signature(page(count=20))).sig_summary()
        self.assertEqual(a, b)


class PersistTest(unittest.TestCase):
    """``PageStack.persist``：原子写、可读、失败只降级。"""

    def test_writes_valid_json_and_leaves_no_tmp(self):
        stack = built_stack(steps=1)
        stack.set_run_id("run-1")
        with tempfile.TemporaryDirectory(prefix="atp_stack_") as tmp:
            target = Path(tmp) / "artifacts" / "run-1" / "page_stack.json"
            self.assertTrue(stack.persist(target))
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["run_id"], "run-1")
            self.assertEqual(data["depth"], 1)
            self.assertEqual(list(target.parent.glob("*.tmp")), [], "临时文件必须被替换掉")

    def test_fail_safe_on_bad_target(self):
        stack = built_stack(steps=1)
        with tempfile.TemporaryDirectory(prefix="atp_stack_") as tmp:
            self.assertFalse(stack.persist(Path(tmp)), "目标是目录时只能返回 False，不能抛")


class ExecutorFrameTest(unittest.TestCase):
    """端到端：同页点击不新增帧；跳页才新增帧（AC-4.2）。"""

    def test_same_page_steps_do_not_grow_the_stack(self):
        cfg = make_config()
        drv = TreeDriver(page(count=40))
        exe = make_executor(drv, cfg)
        exe.run(make_case(3), mode="replay", run_id="qa-stack-1")

        pushes = events_of(drv, "page_stack_push")
        touches = events_of(drv, "page_stack_touch")
        self.assertEqual(len(pushes), 1, "3 步同页只该有首帧入栈")
        self.assertEqual(pushes[0]["step_id"], "s00")
        self.assertEqual(pushes[0]["depth"], 1)
        self.assertEqual(len(touches), 3, "每步同页操作只刷新栈顶")

    def test_navigation_adds_exactly_one_frame(self):
        cfg = make_config()
        drv = TreeDriver(page(count=40))
        hook = lambda d: setattr(d, "tree", other_page())  # noqa: E731
        drv.after_tap = hook

        exe = make_executor(drv, cfg)
        exe.run(make_case(1), mode="replay", run_id="qa-stack-2")

        pushes = events_of(drv, "page_stack_push")
        self.assertEqual(len(pushes), 2, "首帧 + 跳页帧 = 2")
        self.assertEqual([e["depth"] for e in pushes], [1, 2])

    def test_dedupe_can_be_switched_off(self):
        cfg = make_config()
        cfg.deviation.frame_dedupe = False
        drv = TreeDriver(page(count=40))
        exe = make_executor(drv, cfg)
        exe.run(make_case(2), mode="replay", run_id="qa-stack-3")

        self.assertEqual(len(events_of(drv, "page_stack_push")), 3,
                         "关掉去重后回到老行为：每步一帧")


class PersistStackEndToEndTest(unittest.TestCase):
    """``deviation.persist_stack``：默认不写，开启后可读（设计 Q11）。"""

    def test_default_is_off(self):
        cfg = make_config()
        drv = TreeDriver(page(count=40))
        result = make_executor(drv, cfg).run(make_case(2), mode="replay",
                                             run_id="qa-persist-off")
        self.assertFalse((Path(result.artifacts_dir) / "page_stack.json").exists(),
                         "默认不该落盘：页面文本可能敏感")

    def test_enabled_writes_readable_artifact(self):
        cfg = make_config()
        cfg.deviation.persist_stack = True
        drv = TreeDriver(page(count=40))
        result = make_executor(drv, cfg).run(make_case(2), mode="replay",
                                             run_id="qa-persist-on")

        target = Path(result.artifacts_dir) / "page_stack.json"
        self.assertTrue(target.exists(), "开启后必须产出 page_stack.json")
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(data["run_id"], "qa-persist-on")
        self.assertEqual(data["depth"], 1, "同页两步不应长栈")
        self.assertEqual(data["frames"][0]["step_id"], "s00")
        self.assertNotIn("bounds", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
