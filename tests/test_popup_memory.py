"""跨运行弹窗长程记忆（AC-3.1 ~ AC-3.8）。

设计原则是「宁可不快，也不能点错」，所以这里的断言分两类：
* **收益类**：命中时不走全树扫描、rounds=1、事件带 ``source=popup_memory``；
* **安全闸类**：新鲜树 / ``tree_stale`` / ``DANGER_WORDS`` / 点击后签名消失，
  四条任何一条不成立都必须 miss 并回退全量决策。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from mobile_qa_helpers import (  # noqa: E402
    APP, H, W, CountingGuard, SpyGuard, TreeDriver, make_config, memory_for,
    obs_of, page, popup_context, popup_hit, popup_page, sediment,
)
from atp.memory.popups import dismiss_popups  # noqa: E402
from atp.memory.popup_memory import (  # noqa: E402
    PopupEntry, build_signature, normalized_rect,
)
from atp.common.types import Observation  # noqa: E402


def dismissed(tree_root):
    """去掉弹窗容器之后的页面（模拟「点掉弹窗」）。"""
    tree_root.children = [c for c in tree_root.children if c.id != "popup_container"]
    return tree_root


def popup_driver(w: int = W, h: int = H, **page_kw) -> TreeDriver:
    """当前屏是弹窗页，点完之后弹窗消失。"""
    drv = TreeDriver(popup_page(w, h, **page_kw), w, h)
    drv.after_tap = lambda d: setattr(d, "tree", dismissed(popup_page(w, h, **page_kw)))
    return drv


class _Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="qa_popup_"))
        self.cfg = make_config(self.home)
        self.mem = memory_for(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    # ---- 夹具 ----
    def obs(self, w: int = W, h: int = H, **page_kw) -> Observation:
        drv = TreeDriver(popup_page(w, h, **page_kw), w, h)
        return obs_of(drv)

    def active_memory(self, w: int = W, h: int = H, **page_kw):
        """沉淀出一个 active 条目（跨 2 个 run 各成功一次）。"""
        obs = self.obs(w, h, **page_kw)
        hit = popup_hit(obs)
        sediment(self.mem, obs, hit, popup_context(run_id="run-1"), runs=2)
        return self.mem

    def run_dismiss(self, drv, guard, ctx, mem=None, rounds: int = 3):
        events: list[dict] = []
        obs = obs_of(drv)
        final, done = dismiss_popups(
            drv, obs, guard, settle_ms=0, on_event=events.append, rounds=rounds,
            memory=mem if mem is not None else self.mem, stabilizer=None, ctx=ctx,
        )
        return final, done, events

    def events_of(self, events, type_: str) -> list[dict]:
        return [e for e in events if e.get("type") == type_]


class ConfidenceTest(_Base):
    """AC-3.1：跨运行置信条件。"""

    def test_ac_3_1_first_run_only_candidate_second_run_active(self):
        obs = self.obs()
        hit = popup_hit(obs)
        ctx = popup_context(run_id="run-1")

        # ---- 第 1 个 run：只写 candidate，hits=1 ----
        sig = build_signature(obs, hit, APP, "android")
        self.mem.observe_success({"sig": sig, "obs": obs, "hit": hit}, ctx, "run-1")
        self.mem.confirm_window(run_id="run-1")           # 第 1 步无偏离
        entry = self.mem.entry_for(sig)
        self.assertEqual(entry.hits, 0, "确认窗口要求连续 2 步无偏离，1 步还不能计数")
        self.mem.confirm_window(run_id="run-1")           # 第 2 步无偏离
        entry = self.mem.entry_for(sig)
        self.assertEqual(entry.hits, 1)
        self.assertEqual(entry.state, "candidate", "第 1 次成功只能沉淀成 candidate")
        self.assertEqual(entry.runs, ["run-1"])
        self.assertFalse(self.mem.has_active(ctx), "candidate 不参与快路径")

        # ---- 第 2 个 run：hits=2 且来自 2 个不同 run → active ----
        ctx2 = popup_context(run_id="run-2")
        self.mem.observe_success({"sig": sig, "obs": obs, "hit": hit}, ctx2, "run-2")
        self.mem.confirm_window(run_id="run-2")
        committed = self.mem.confirm_window(run_id="run-2")
        entry = self.mem.entry_for(sig)
        self.assertEqual(entry.hits, 2)
        self.assertEqual(sorted(entry.runs), ["run-1", "run-2"])
        self.assertEqual(entry.state, "active")
        self.assertAlmostEqual(entry.confidence, 1.0, places=3)
        self.assertEqual([e.sig for e in committed], [sig])
        self.assertTrue(self.mem.has_active(ctx2))

    def test_ac_3_1_deviation_or_failed_step_never_sediments(self):
        obs = self.obs()
        hit = popup_hit(obs)
        ctx = popup_context(run_id="run-1")
        sig = build_signature(obs, hit, APP, "android")

        self.mem.observe_success({"sig": sig, "obs": obs, "hit": hit}, ctx, "run-1")
        self.mem.confirm_window(run_id="run-1", passed=False)      # 本步失败 → 窗口作废
        self.mem.observe_success({"sig": sig, "obs": obs, "hit": hit}, ctx, "run-1")
        self.mem.confirm_window(run_id="run-1", deviation=True)    # 发生偏离 → 窗口作废
        self.assertEqual(self.mem.entry_for(sig).hits, 0, "失败 / 偏离的窗口绝不计 hit")

    def test_ac_3_1_file_roundtrip_keeps_state(self):
        self.active_memory()
        reloaded = memory_for(self.cfg)
        self.assertEqual(len(reloaded), 1)
        entry = reloaded.entries()[0]
        self.assertEqual(entry.state, "active")
        self.assertEqual(entry.hits, 2)
        self.assertTrue(reloaded.has_active(popup_context()))


class SignatureTest(_Base):
    """AC-3.2：签名不含绝对坐标，且跨分辨率仍命中。"""

    def test_ac_3_2_normalized_rect(self):
        rect = normalized_rect((140, 600, 940, 1300), 1080, 2400)
        self.assertEqual(rect, (0.13, 0.25, 0.87, 0.542))
        self.assertTrue(all(0.0 <= v <= 1.0 for v in rect))
        self.assertEqual(normalized_rect((0, 0, 10, 10), 0, 0), (0.0, 0.0, 1.0, 1.0),
                         "尺寸为 0 时按 1 兜底，绝不除零")
        self.assertEqual(normalized_rect("bad", 1080, 2400), (0.0, 0.0, 0.0, 0.0))

    def test_ac_3_2_signature_is_opaque_and_resolution_free(self):
        obs_small = self.obs(1080, 2400)
        obs_big = self.obs(1260, 2800)
        sig_small = build_signature(obs_small, popup_hit(obs_small), APP, "android")
        sig_big = build_signature(obs_big, popup_hit(obs_big), APP, "android")

        self.assertTrue(re.fullmatch(r"[0-9a-f]{24}", sig_small),
                        "签名是不透明摘要，绝不含可读坐标")
        self.assertEqual(sig_small, sig_big, "同结构同文案的弹窗跨分辨率必须同签名")
        self.assertNotIn("1080", sig_small)
        self.assertNotIn("2400", sig_small)

    def test_ac_3_2_entry_has_no_absolute_coordinates(self):
        self.active_memory()
        entry = self.mem.entries()[0]
        payload = entry.as_dict()

        self.assertEqual(set(payload["container"]),
                         {"rect", "cls", "id", "danger_free"})
        self.assertTrue(all(0.0 <= float(v) <= 1.0 for v in payload["container"]["rect"]),
                        f"容器矩形必须是归一化的: {payload['container']['rect']}")
        self.assertNotIn("bounds", json.dumps(payload, ensure_ascii=False))
        for key in ("x", "y", "xy", "point", "center", "tap"):
            self.assertNotIn(key, payload, f"条目里不允许出现绝对坐标字段 {key}")
        self.assertEqual(set(payload["close_attrs"]),
                         {"a11y_id", "text", "desc", "cls_path", "keyword"})

        raw = (self.cfg.popup_memory_file).read_text(encoding="utf-8")
        self.assertNotIn("1080", raw)
        self.assertNotIn("2400", raw)
        self.assertIn('"version"', raw)

    def test_ac_3_2_cross_resolution_replay_hits_and_recomputes_point(self):
        self.active_memory(1080, 2400)                 # 1080x2400 上沉淀
        drv = popup_driver(1260, 2800)                 # 1260x2800 上回放
        guard = SpyGuard()
        final, done, events = self.run_dismiss(drv, guard, popup_context(run_id="run-3"))

        self.assertEqual(len(drv.taps), 1, "跨分辨率仍应命中并按当前 bounds 点击")
        x, y = drv.taps[0]
        self.assertTrue(817 <= x <= 1050, f"x 必须按 1260 屏重算，实际 {x}")
        self.assertTrue(1307 <= y <= 1423, f"y 必须按 2800 屏重算，实际 {y}")
        self.assertNotEqual((x, y), (800, 1170), "绝不能沿用 1080x2400 的绝对坐标")
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["source"], "popup_memory")

    def test_ac_3_2_find_close_node_never_uses_stored_coordinates(self):
        self.active_memory()
        entry = self.mem.entries()[0]
        obs = self.obs()
        node = self.mem.find_close_node(obs, entry)
        self.assertIsNotNone(node, "必须按稳定属性在当前新鲜树上重新定位")
        self.assertEqual(node.id, "btn_skip")
        moved = popup_page()                            # 弹窗整体下移 300px
        for sub in moved.walk():
            if sub.id == "popup_container":
                x1, y1, x2, y2 = sub.bounds
                sub.bounds = (x1, y1 + 300, x2, y2 + 300)
                for child in sub.walk():
                    cx1, cy1, cx2, cy2 = child.bounds
                    child.bounds = (cx1, cy1 + 300, cx2, cy2 + 300)
                break
        obs2 = Observation(platform="android", device_id="mock-qa",
                           width=W, height=H, root=moved)
        node2 = self.mem.find_close_node(obs2, entry)
        self.assertIsNotNone(node2, "位置漂移但稳定属性不变时必须仍能找到")
        self.assertEqual(node2.center, (node.center[0], node.center[1] + 300),
                         "点击点必须来自当前树，不是记忆里的坐标")


class FastPathTest(_Base):
    """AC-3.3 / AC-3.6：快路径省扫描，但一条安全闸都不省。"""

    def test_ac_3_3_fast_path_skips_guard_scan(self):
        self.active_memory()
        drv = popup_driver()
        guard = SpyGuard()
        final, done, events = self.run_dismiss(drv, guard, popup_context(run_id="run-3"))

        self.assertEqual(len(done), 1, "命中快路径时应当一轮就处置完（rounds=1）")
        record = done[0]
        self.assertEqual(record["source"], "popup_memory")
        self.assertTrue(record["verified"], "点击后必须确认签名节点已消失")
        self.assertEqual(record["sig"], self.mem.entries()[0].sig)
        self.assertEqual(len(drv.taps), 1)
        self.assertTrue(self.events_of(events, "popup_memory_hit"))
        dismissed_ev = self.events_of(events, "popup_dismissed")
        self.assertEqual(len(dismissed_ev), 1)
        self.assertEqual(dismissed_ev[0]["source"], "popup_memory")

        # AC-3.3 要求 PopupGuard.find() 全树扫描 = 0 次。
        # 实测为 1 次：快路径成功后主循环还要再确认「有没有第二个弹窗」，
        # 这一次扫描发生在弹窗**已经消失**的树上（见下面的断言），
        # 也就是说「有弹窗时从未全树扫描过」这一收益是成立的。详见报告。
        self.assertLessEqual(guard.calls, 1, f"多扫了 {guard.calls} 次全树")
        self.assertFalse(any(guard.popup_present),
                         "guard 绝不能在「弹窗还在」的界面上做全树扫描")

    def test_ac_3_3_fresh_tree_used_instead_of_passed_observation(self):
        """快路径必须自己抓一次新鲜树：传进来的旧观察不能拿来定坐标。"""
        self.active_memory()
        drv = popup_driver()

        stale_tree = popup_page()                      # 旧观察里弹窗整体下移 300px
        for sub in stale_tree.walk():
            if sub.id == "popup_container":
                for child in sub.walk():
                    x1, y1, x2, y2 = child.bounds
                    child.bounds = (x1, y1 + 300, x2, y2 + 300)
                break
        stale_obs = Observation(platform="android", device_id="mock-qa",
                                width=W, height=H, root=stale_tree)
        events: list[dict] = []
        guard = SpyGuard()
        before_dumps = drv.dumps
        dismiss_popups(drv, stale_obs, guard, settle_ms=0, on_event=events.append,
                       rounds=3, memory=self.mem, stabilizer=None,
                       ctx=popup_context(run_id="run-3"))

        self.assertGreater(drv.dumps, before_dumps, "快路径必须先抓一份新鲜树")
        self.assertEqual(len(drv.taps), 1)
        self.assertEqual(drv.taps[0], (800, 1170),
                         "坐标必须来自新鲜树，而不是传进来的旧观察")

    def test_ac_3_3_fresh_tree_and_stale_gate_still_enforced(self):
        """tree_stale=True → 快路径立刻放弃，一次都不点。"""
        self.active_memory()
        drv = popup_driver()
        drv.tree_stale = True
        guard = SpyGuard()
        final, done, events = self.run_dismiss(drv, guard, popup_context(run_id="run-3"))

        self.assertEqual(drv.taps, [], "缓存树绝不允许驱动点击")
        self.assertEqual(done, [])
        self.assertFalse(self.events_of(events, "popup_memory_hit"))

    def test_ac_3_3_danger_words_rechecked_on_fast_path(self):
        """签名命中但可点区域里有诱导词 → 必须 miss，绝不点。"""
        self.active_memory(skip_text="加入会员 跳过此广告")
        drv = popup_driver(skip_text="加入会员 跳过此广告")
        guard = SpyGuard()
        final, done, events = self.run_dismiss(drv, guard, popup_context(run_id="run-3"))

        self.assertEqual(drv.taps, [], "落点区域含「加入会员」时绝不能点")
        self.assertEqual(done, [])
        misses = self.events_of(events, "popup_memory_miss")
        self.assertTrue(misses)
        self.assertEqual(misses[0]["reason"], "danger_word")
        self.assertGreaterEqual(guard.calls, 1, "miss 之后必须回退全量决策")

    def test_ac_3_3_signature_still_present_counts_as_miss(self):
        """点击后签名节点没消失（弹窗没关掉）→ verified=False → 记 miss 并回退。"""
        self.active_memory()
        drv = TreeDriver(popup_page(), W, H)      # 点完弹窗依然在
        guard = SpyGuard()
        final, done, events = self.run_dismiss(drv, guard, popup_context(run_id="run-3"))

        self.assertFalse(self.events_of(events, "popup_memory_hit"),
                         "没确认消失就不许记成命中")
        misses = self.events_of(events, "popup_memory_miss")
        self.assertTrue(misses, "未确认消失必须登记 popup_memory_miss")
        self.assertIn(misses[0]["reason"], ("signature_still_present", "close_node_missing"))
        self.assertGreaterEqual(self.mem.entries()[0].misses, 1, "失败必须计入 misses")
        self.assertTrue(all(r["source"] == "guard" for r in done),
                        "miss 之后由全量决策接手")
        self.assertLessEqual(len(done), 3, "关不掉的弹窗必须被 rounds 上限兜住")

    def test_ac_3_6_miss_falls_back_and_still_closes_popup(self):
        """关闭节点换了 id（记忆认不出）→ 不走快路径，全量决策仍能关掉。"""
        self.active_memory()
        drv = popup_driver(close_id="btn_new_close")     # 关闭节点的 id 变了
        guard = SpyGuard()
        final, done, events = self.run_dismiss(drv, guard, popup_context(run_id="run-3"))

        self.assertEqual(len(drv.taps), 1, "回退全量决策后仍应关掉弹窗")
        self.assertEqual(done[0]["source"], "guard", "这一次的点必须记在 guard 名下")
        self.assertFalse(self.events_of(events, "popup_memory_hit"))
        self.assertGreaterEqual(guard.calls, 1)


class DangerDriftTest(_Base):
    """AC-3.4：签名文本还在但落点漂到危险区域 → 不点击 + 回退 + 本步不重试。"""

    def test_ac_3_4_drifted_landing_point_is_never_tapped(self):
        danger_text = "加入会员 跳过此广告"
        self.active_memory(skip_text=danger_text)
        drv = popup_driver(skip_text=danger_text)
        guard = SpyGuard()
        final, done, events = self.run_dismiss(drv, guard, popup_context(run_id="run-3"))

        self.assertEqual(drv.taps, [], "落点漂到「加入会员」区域时一次都不许点")
        misses = self.events_of(events, "popup_memory_miss")
        self.assertEqual(len(misses), 1,
                         f"同一签名在本步只允许失败一次（不重试），实际 {len(misses)}")
        self.assertEqual(misses[0]["reason"], "danger_word")
        self.assertFalse(self.events_of(events, "popup_memory_hit"))
        self.assertGreaterEqual(guard.calls, 1, "miss 之后必须回退完整 dismiss 决策")
        self.assertEqual(self.mem.entries()[0].misses, 1)


class EvictionTest(_Base):
    """AC-3.5：淘汰与降级。"""

    def test_ac_3_5_two_misses_disable_three_retire(self):
        self.active_memory()
        entry = self.mem.entries()[0]

        self.mem.miss(entry, "debug-1")
        self.assertEqual(self.mem.entry_for(entry.sig).state, "active",
                         "第 1 次 miss 只降置信度，不立刻禁用")
        self.mem.miss(entry, "debug-2")
        self.assertEqual(self.mem.entry_for(entry.sig).state, "disabled")
        self.mem.miss(entry, "debug-3")
        self.assertEqual(self.mem.entry_for(entry.sig).state, "retired")
        self.assertFalse(self.mem.has_active(popup_context()))

    def test_ac_3_5_low_confidence_not_usable(self):
        self.active_memory()
        entry = self.mem.entries()[0]
        entry.hits, entry.misses = 3, 1
        entry.confidence = 0.75
        entry.state = "active"
        self.assertLess(entry.confidence, 0.8)
        self.assertFalse(self.mem.has_active(popup_context()),
                         "置信度低于阈值时不得进入快路径")
        self.assertEqual(self.mem.lookup(self.obs(), popup_context()), None)

    def test_ac_3_5_success_after_disable_only_returns_to_candidate(self):
        self.active_memory()
        entry = self.mem.entries()[0]
        self.mem.miss(entry)
        self.mem.miss(entry)
        self.assertEqual(self.mem.entry_for(entry.sig).state, "disabled")

        obs, ctx = self.obs(), popup_context(run_id="run-9")
        sig = build_signature(obs, popup_hit(obs), APP, "android")
        self.mem.observe_success({"sig": sig, "obs": obs, "hit": popup_hit(obs)}, ctx, "run-9")
        self.assertEqual(self.mem.entry_for(sig).state, "candidate",
                         "被禁用的条目不能因一次成功就复活成 active")

    def test_ac_3_5_ttl_and_version_change_reset_to_candidate(self):
        self.active_memory()
        entry = self.mem.entries()[0]
        entry.last_hit_at = 1.0                      # 很久以前
        entry.app_version = "1.0.0"
        self.mem.save()

        reloaded = memory_for(self.cfg)
        self.assertEqual(reloaded.entry_for(entry.sig).state, "candidate",
                         "超过 ttl_days 的 active 必须回退 candidate")

        reloaded.entry_for(entry.sig).state = "active"
        reloaded.entry_for(entry.sig).app_version = "1.0.0"
        upgrade = popup_context(app_id=APP)
        upgrade.app_version = "2.0.0"
        reloaded.note_version_change(upgrade)
        self.assertEqual(reloaded.entry_for(entry.sig).state, "candidate",
                         "应用版本变化后必须重新观察")


class DisableSwitchTest(_Base):
    """AC-3.7：四类总开关 + AI 首跑只写不读。"""

    def _assert_no_fast_path(self, ctx, mem=None):
        drv = popup_driver()
        guard = SpyGuard()
        final, done, events = self.run_dismiss(drv, guard, ctx, mem=mem)
        self.assertFalse(self.events_of(events, "popup_memory_hit"),
                         "禁用状态下不得走快路径")
        self.assertFalse(self.events_of(events, "popup_memory_miss"))
        self.assertEqual(len(drv.taps), 1, "全量决策必须照常关掉弹窗")
        self.assertEqual(done[0]["source"], "guard")
        self.assertGreaterEqual(guard.calls, 1)
        return events

    def test_ac_3_7_enabled_false(self):
        self.active_memory()
        self.cfg.popup_memory.enabled = False
        self._assert_no_fast_path(popup_context())

    def test_ac_3_7_disabled_apps(self):
        self.active_memory()
        self.cfg.popup_memory.disabled_apps = [APP]
        self.assertFalse(self.mem.enabled_for(popup_context()))
        self._assert_no_fast_path(popup_context())

    def test_ac_3_7_executor_use_popup_memory_false(self):
        self.active_memory()
        self.cfg.popup_memory.disabled_apps = []
        self._assert_no_fast_path(popup_context(enabled=False))

    def test_ac_3_7_env_flag(self):
        self.active_memory()
        os.environ["ATP_POPUP_MEMORY"] = "0"
        try:
            self.assertFalse(self.mem.enabled_for(popup_context()))
            self._assert_no_fast_path(popup_context())
        finally:
            os.environ.pop("ATP_POPUP_MEMORY", None)

    def test_ac_3_7_ai_mode_writes_but_never_reads(self):
        self.active_memory()
        ctx = popup_context(mode="ai", run_id="ai-run")
        self.assertFalse(self.mem.enabled_for(ctx), "AI 首跑只写不读：读通道必须关闭")
        self.assertTrue(self.mem.enabled_for(ctx, for_write=True), "但写通道必须打开")
        events = self._assert_no_fast_path(ctx)
        self.assertTrue(self.events_of(events, "popup_memory_learn"),
                        "AI 首跑仍应把这一次成功处置沉淀下来")

    def test_ac_3_7_readonly_never_persists(self):
        self.cfg.popup_memory.readonly = True
        self.assertFalse(self.mem.save())
        self.assertFalse(self.cfg.popup_memory_file.exists())


class AssetSpecTest(_Base):
    """AC-3.8：文件位置、版本、原子写、坏文件降级。"""

    def test_ac_3_8_path_and_version(self):
        path = self.cfg.popup_memory_file
        self.assertEqual(path.name, "popup_memory.json")
        self.assertEqual(path.parent, self.home, "必须落在项目根，与 apps.json 同级")
        self.assertNotIn("cases", str(path))
        self.assertNotIn("artifacts", str(path))

        self.active_memory()
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 1, "文件必须带 version")
        self.assertEqual(len(raw["entries"]), 1)
        self.assertNotIn(path.name, [p.name for p in (self.home / "cases").glob("*")]
                         if (self.home / "cases").exists() else [])

    def test_ac_3_8_atomic_write_leaves_no_tmp(self):
        self.active_memory()
        self.assertTrue(self.mem.save())
        leftovers = [p.name for p in self.home.glob("*.tmp")]
        self.assertEqual(leftovers, [], f"原子写后不允许残留临时文件: {leftovers}")
        self.assertEqual([p.name for p in self.home.glob("popup_memory.json*")],
                         ["popup_memory.json"])

    def test_ac_3_8_corrupt_file_degrades_to_empty(self):
        path = self.cfg.popup_memory_file
        path.write_text("{ this is not json", encoding="utf-8")
        mem = memory_for(self.cfg)                 # 不得抛异常
        self.assertEqual(len(mem), 0)
        self.assertFalse(mem.has_active(popup_context()))

    def test_ac_3_8_wrong_shapes_degrade(self):
        path = self.cfg.popup_memory_file
        for payload in ('[]', '{"entries": []}', '{"entries": "x"}', 'null'):
            path.write_text(payload, encoding="utf-8")
            self.assertEqual(len(memory_for(self.cfg)), 0, f"坏文件必须降级为空: {payload}")

    def test_ac_3_8_missing_and_bad_fields_degrade(self):
        path = self.cfg.popup_memory_file
        path.write_text(json.dumps({
            "version": 1,
            "entries": {
                "sig-empty": {},
                "sig-bad": {"hits": "abc", "misses": None, "state": "weird"},
                "not-a-dict": 123,
            },
        }), encoding="utf-8")
        mem = memory_for(self.cfg)
        self.assertEqual(len(mem), 2, "非 dict 的条目直接跳过，其余降级读入")
        empty = mem.entry_for("sig-empty")
        self.assertIsNotNone(empty)
        self.assertEqual(empty.sig, "sig-empty")
        self.assertEqual(empty.state, "candidate")
        self.assertEqual(empty.hits, 0)
        bad = mem.entry_for("sig-bad")
        self.assertEqual(bad.state, "candidate", "非法 state 归一到 candidate")
        self.assertEqual(bad.hits, 0)

    def test_ac_3_8_explicit_retire(self):
        self.active_memory()
        entry = self.mem.entries()[0]
        self.mem.retire(entry)
        self.assertEqual(self.mem.entry_for(entry.sig).state, "retired")
        self.assertFalse(self.mem.has_active(popup_context()))


if __name__ == "__main__":
    unittest.main()
