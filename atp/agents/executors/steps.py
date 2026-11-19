"""主流程与单步执行。

共享实现。方法体与原 ``agents/executor.py`` 一致，只换归属文件。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Protocol

from atp.common.profiles import PlatformProfile, profile_for
from atp.runs.artifacts import REPORT_FILE, STEPIR_FILE, new_run_id, run_dir
from atp.config import Config
from atp.agents.platforms.base import BaseDriver
from atp.db.store import CaseDB
from atp.memory.apps import looks_like_package
from atp.agents.perception.deviation import BackController, DeviationDetector
from atp.agents.perception.judge import Judge, JudgeResult, text_corpus
from atp.agents.perception.locator import LocateResult, derive_strategies, describe, occluded_by, resolve
from atp.agents.perception.page_stabilizer import PageFrame, PageStack, PageStabilizer, page_signature
from atp.memory.popups import PopupGuard, dismiss_popups
from atp.memory.popup_memory import PopupContext, PopupMemory
from atp.agents.perception.scroller import find_by_scrolling, trail_text
from atp.common.stepir import (CaseIR, LocatorStrategy, Postcondition, RunResult,
                               ScreenProfile, Step, StepResult)
from atp.common.types import Action, Observation, RunCancelled
from atp.agents.perception.vision import VisualLocator, denorm_rect, frame_diff_ratio, make_point_node
from atp.agents.executors.base import discriminative_post

def recompile_strategies(step: Step, obs: Observation, node) -> None:
    """AI 首跑时用它把当时看到的节点固化成多策略定位器。"""
    step.strategies = derive_strategies(node, obs.root, (obs.width, obs.height))

class StepsMixin:
    """共享实现。"""

    def run(self, case: CaseIR, mode: str = "replay", persist_path: Path | None = None,
            run_id: str = "") -> RunResult:
        run_id = run_id or new_run_id(case.case_id)
        art = run_dir(self.run_root, run_id)
        self._run_id = run_id
        self._mode = mode
        self._case_app_id = str(case.app_id or "")
        self._stack.clear()
        self._stack.set_run_id(run_id)
        self._retry_requested = False
        self._retry_detail = ""
        self._cancel_seen = False
        self._back.reset_run()

        result = RunResult(
            run_id=run_id, case_id=case.case_id, case_title=case.title,
            platform=case.platform, device_id=self.driver.device_id,
            mode=mode, artifacts_dir=str(art),
        )

        stale_note = ""
        try:
            # 首帧也要等页面完全加载完：树-only 门禁通过后只取一次截图。
            first = self._stabilizer.stable_observe("run_start")
            self._push_frame(first, "s00")
        except RunCancelled:
            raise
        except Exception as e:
            result.finished_at = time.time()
            result.steps.append(
                StepResult(step_id="s00", intent="连接设备", action="connect",
                           status="error", error=f"{type(e).__name__}: {e}")
            )
            self._persist_stack(art)
            return result

        # 分辨率档案：这一步决定了所有归一化坐标怎么落回当前画布
        self._screen = self._profile(first)
        if case.screen.ok:
            self._screen.scaled_from(case.screen)
        else:
            case.screen = ScreenProfile(**self._screen.as_dict())
        result.screen = self._screen
        result.screen = self._screen
        self._emit(
            "screen", width=self._screen.width, height=self._screen.height,
            dpr=self._screen.dpr, scale_x=self._screen.scale_x,
            scale_y=self._screen.scale_y,
            base_width=case.screen.width, base_height=case.screen.height,
            label=self._screen.label,
        )

        if case.ui_fingerprint and case.is_stale(first.fingerprint):
            stale_note = "界面指纹已变化，脚本可能失效（建议重跑 AI 沉淀）"

        # PC 端 / Web 端执行录屏：整场跑完后在报告页以 MP4 回放（用户要求）。
        # stop() 幂等：正常结束 / 取消 / 异常三条路都会走到，编码只发生一次。
        self._recorder = None           # 同一 Executor 连跑多条用例时不残留
        recorder = self._start_recorder(art)
        try:
            for idx, step in enumerate(case.steps, start=1):
                self._check_cancel()
                self._emit(
                    "step_started", index=idx, total=len(case.steps),
                    step_id=step.step_id, intent=step.intent, action=step.action.value,
                )
                self._retry_requested = False
                self._retry_detail = ""
                self._retry_mode = False
                self._last_deviation = False
                sr = self._run_step(idx, step, art, case)
                if self._retry_requested and not self._retry_mode:
                    # 偏离纠正成功：重跑当前步骤恰好 1 次。
                    # 不新增 StepResult 条目、不新增 _heal、不污染步骤计数。
                    detail = self._retry_detail
                    self._retry_mode = True
                    self._retry_requested = False
                    self._check_cancel()
                    sr = self._run_step(idx, step, art, case)
                    sr.detail = (sr.detail + " | " + detail).strip(" |")
                    self._retry_mode = False
                if stale_note and sr.status == "failed":
                    sr.detail = (sr.detail + " | " + stale_note).strip(" |")
                result.steps.append(sr)
                self._confirm_memory(sr, deviation=bool(self._last_deviation))
                self._emit(
                    "step_finished", index=idx, total=len(case.steps),
                    result=sr.model_dump(),
                    evidence={"before": step.evidence.before, "after": step.evidence.after},
                )
                if sr.status == "error" and not step.optional:
                    break
        except RunCancelled as exc:
            # 取消也要留痕：已经跑完的步骤、截图、报告都在，方便判断"跑到哪一步被停的"。
            # 异常继续往上抛，由任务层把状态改成 cancelled——绝不能在这里吞掉。
            result.aborted = True
            result.finished_at = time.time()
            if recorder is not None:
                recorder.stop()          # 取消报告里也要有 MP4，必须在渲染前编码完
            self._persist_stack(art)
            case.save(art / STEPIR_FILE)
            try:
                from atp.report.html import render_report

                render_report(result, art)
                result.report_path = str(art / REPORT_FILE)
            except Exception:  # noqa: BLE001
                pass
            setattr(exc, "partial", result)
            raise
        finally:
            if recorder is not None:
                recorder.stop()          # 幂等：正常路径已停过，这里只兜异常/取消

        result.finished_at = time.time()
        self._persist_stack(art)
        # 权重是跑出来的，不是配出来的：回写后下次优先级自动收敛
        if persist_path:
            case.save(persist_path)
        case.save(art / STEPIR_FILE)

        from atp.report.html import render_report

        report = art / REPORT_FILE
        render_report(result, art)
        result.report_path = str(report)
        self._record(result, art, case)
        return result

    def _run_step(self, idx: int, step: Step, art: Path, case: CaseIR) -> StepResult:
        self._check_cancel()
        t0 = time.perf_counter()
        sr = StepResult(step_id=step.step_id, intent=step.intent, action=step.action.value)

        try:
            # 树-only 稳定轮询 → 只取一次 screenshot → 才允许写 before 证据 / 定位。
            obs = self._stabilizer.stable_observe("step_before")
            gate = self._stabilizer.last_result
        except RunCancelled:
            raise
        except Exception as e:
            sr.status = "error"
            sr.error = f"无法获取界面: {e}"
            sr.latency_ms = int((time.perf_counter() - t0) * 1000)
            return sr

        # fail-closed：门禁不稳且这一步要点元素时，绝不点击。
        # 例外（双树校验）：轮播/信息流这类**常驻动画页**的细指纹永不稳定，
        # 但页面内容实际没变。门禁超时后再取一棵树，目标在两棵树里都解析到
        # 同一位置 → 只是动画在抖，降级执行；目标变了/找不到 → 内容真在变
        # （AC-1.4 口径）→ 维持 fail-closed。没有这道豁免，回放在动画页
        # 必然卡死，永远复现不了首跑（run-20261117-184735：示例视频应用搜索页 s04）。
        if step.needs_target and getattr(obs, "page_ready", True) is False:
            reason = str(getattr(obs, "page_ready_reason", "") or "page_not_ready")
            # 门禁失败也要留证据：早先直接 return，报告里该步前后图全是空引用，
            # 控制台缩略图显示成破损图（用户截图反馈）。
            before_name = f"{idx:02d}_{step.step_id}_before.png"
            try:
                if obs.screenshot:
                    self._dump(art, before_name, obs.screenshot)
                    step.evidence.before = before_name
            except OSError:
                pass
            degrade = False
            # 双树校验只用于「超时类」门禁失败：unsupported_page / observe_failed
            # 是确定性结论，补一棵树纯属浪费（AC-1.6 的抓树次数上限）。
            # 树过期（tree_stale）绝不豁免——缓存树不允许作为点击依据（AC-1.5）。
            if reason in ("timeout_unstable", "cached_unstable") \
                    and not getattr(self.driver, "tree_stale", False):
                try:
                    obs2 = self.driver.observe()
                    if obs2 is not None and obs2.root is not None:
                        # 回放执行器不组合 GuardMixin（那是 AI runner 侧的），
                        # 显式引用静态方法，别走 self.——否则 AttributeError
                        # 会被 except 吞掉，降级永远不生效。
                        from atp.agents.runners.guard import GuardMixin
                        w1, h1 = obs.width or 0, obs.height or 0
                        w2 = obs2.width or w1
                        h2 = obs2.height or h1
                        loc1 = resolve(obs.root, step.strategies, (w1, h1),
                                       self.config.bbox_tolerance, None)
                        loc2 = resolve(obs2.root, step.strategies, (w2, h2),
                                       self.config.bbox_tolerance, None)
                        if loc1.ok and loc2.ok:
                            # 目标必须是**同一个元素**：文案一致 + 位置一致。
                            # 文案在变（目标按钮-3 → 目标按钮-4，AC-1.4）说明
                            # 内容真在翻新，只是坐标碰巧没动，不能降级。
                            t1 = (loc1.node.text or "", loc1.node.desc or "")
                            t2 = (loc2.node.text or "", loc2.node.desc or "")
                            ra = GuardMixin._step_rect(step, loc1.node, obs)
                            rb = GuardMixin._step_rect(step, loc2.node, obs2)
                            degrade = (t1 == t2
                                       and GuardMixin._same_target(ra, rb, obs))
                except Exception:  # noqa: BLE001  校验失败按不可信处理
                    degrade = False
            if degrade:
                sr.detail = ("页面门禁超时，但目标在两拍树里位置一致"
                             "（常驻动画页），降级执行")
                sr.latency_ms = int((time.perf_counter() - t0) * 1000)
            else:
                sr.status = "failed"
                sr.error = "page_unstable"
                sr.detail = f"页面未完全加载完成（{reason}），放弃点击"
                sr.latency_ms = int((time.perf_counter() - t0) * 1000)
                return sr
        if gate is not None and (gate.reused or gate.reason == "thin_stable"):
            sr.detail = (sr.detail + f" 页面门禁：{gate.reason}（{gate.wait_ms}ms）").strip()

        # 动作之前先清掉挡路的弹窗。用例只关心目标动作，
        # 开屏广告/更新提示/青少年模式这些不该让整条用例失败。
        obs = self._dismiss_popups(obs, idx, step, case)
        self._carry_gate(obs)

        before_name = f"{idx:02d}_{step.step_id}_before.png"
        self._dump(art, before_name, obs.screenshot)
        step.evidence.before = before_name
        step.evidence.tree_hash = obs.fingerprint

        loc = LocateResult()
        # 移动端要额外排除"被固定栏盖住"的目标：坐标是对的，点下去却落在别人身上
        # （真机实测：点「请假类别」的「请选择」直接跳进了「附件」页）。
        # Web/桌面的 DOM 树本来就大量重叠（fixed 头、浮层），这套判定在那边只会误伤。
        avoid_occluded = self.profile.occluded_aware
        if step.needs_target:
            loc = resolve(obs.root, step.strategies, (obs.width, obs.height),
                          self.config.bbox_tolerance, self._visual_matcher(obs),
                          avoid_occluded=avoid_occluded)

        # 目标不在首屏 → 先滑一滑。这一步必须排在 AI 修复之前：
        # 目标只是被滚出了视野时，AI 在当前这屏也看不见它，修出来的定位器必然错；
        # 而滑动是零成本的，几十次滑动也不如一次模型调用贵。
        scroll_note = ""
        if step.needs_target and not loc.ok:
            # 目标在树里但 bounds 为空 = 只是被滚出了视野，几乎一定滑一下就能找到。
            # 这种情况在 H5 长表单上极其常见，报告里要写清楚，别让人以为定位器坏了。
            if loc.offscreen:
                sr.detail = f"「{loc.offscreen}」不在可视区域，开始滑动查找"
            else:
                legacy = resolve(obs.root, step.strategies, (obs.width, obs.height),
                                 self.config.bbox_tolerance, self._visual_matcher(obs))
                blocker = occluded_by(obs.root, legacy.node) if legacy.ok else None
                if blocker is not None:
                    label = (legacy.node.text or legacy.node.desc or "")[:16]
                    sr.detail = f"「{label}」被「{describe(blocker)}」盖住，先滑动让它露出来"
            s_loc, s_obs, scroll_note = self._scroll_find(step, obs, idx)
            if s_loc is not None and s_obs is not None:
                loc, obs = s_loc, s_obs
                sr.detail = scroll_note
                if obs.screenshot:
                    # 证据图换成"找到它的那一屏"，报告里能看见它当时在哪儿
                    self._dump(art, before_name, obs.screenshot)

        # 只有"需要目标元素"的步骤才值得修定位器。
        # open_url / maximize / back 这类无目标动作也要去调一次模型修定位，
        # 白等 40 秒还改不动任何东西——线上实测踩过。
        if (step.needs_target and not loc.ok and self.healer
                and self.config.allow_healing and self.ai_budget > 0
                and not self._retry_mode):
            healed = self._heal(step, obs, case, art)
            if healed is not None:
                step.strategies = healed
                step.source = "healed"
                sr.ai_used = True
                obs = self.driver.observe()
                loc = resolve(obs.root, step.strategies, (obs.width, obs.height),
                              self.config.bbox_tolerance, self._visual_matcher(obs),
                              avoid_occluded=avoid_occluded)
                if loc.ok:
                    sr.detail = (sr.detail + " 定位器已由 AI 修复").strip()
                    self._emit(
                        "heal", step_id=step.step_id, index=idx,
                        strategies=[f"{s.type}:{s.value}" for s in step.strategies[:4]],
                    )

        sr.match_score = loc.match_score
        sr.scale_x = self._screen.scale_x
        sr.scale_y = self._screen.scale_y

        if step.needs_target and not loc.ok:
            # 最后一道诚实性检查：目标确实在树里、只是被固定栏压住时，
            # 宁可在这一步报"被遮挡"，也不要照着坐标点下去——那会点到遮挡物，
            # 把整条用例带到另一个页面（真机：点「请选择」跳进了「附件」页）。
            legacy = resolve(obs.root, step.strategies, (obs.width, obs.height),
                             self.config.bbox_tolerance, self._visual_matcher(obs))
            blocker = occluded_by(obs.root, legacy.node) if legacy.ok else None
            if blocker is not None:
                label = (legacy.node.text or legacy.node.desc or "")[:16]
                sr.status = "failed"
                sr.error = f"「{label}」被「{describe(blocker)}」遮挡，滑动后仍未脱离"
                sr.detail = (scroll_note or "未滑动") + "；按坐标点会点到遮挡物，已放弃"
                sr.latency_ms = int((time.perf_counter() - t0) * 1000)
                sr.screenshot = before_name
                return sr
            sr.status = "failed"
            sr.error = loc.reason or "未定位到目标元素"
            sr.detail = ((scroll_note + " ") if scroll_note else "") + "尝试过: " + ", ".join(
                f"{k}{'✓' if v else '✗'}" for k, v in loc.attempts[:5])
            sr.latency_ms = int((time.perf_counter() - t0) * 1000)
            sr.screenshot = before_name
            return sr

        # 移动端"打开应用"= 先杀进程再启动。App 还活在后台时，启动往往只是把
        # 上次停留的页面拉回前台，用例的起点就不确定了。
        # 顺序不能反：先把「应用名」翻成包名（台账/设备），冷启动才有东西可杀。
        app_note = ""
        if step.action in (Action.LAUNCH_APP, Action.TERMINATE_APP):
            app_note = self._resolve_app_step(step, idx, case)

        cold_note = ""
        if step.action is Action.LAUNCH_APP:
            cold_note = self._cold_start(step, idx, case)
            if cold_note:
                sr.detail = " ".join(x for x in (app_note, cold_note) if x)
        elif app_note:
            sr.detail = app_note

        before_action = obs
        self._check_cancel()   # 动作发出前最后一次检查：刚点取消就别再动设备了
        action_res = self.driver.perform(step.action, loc.node, step.args)
        # 每个设备动作后立刻让会话级 reuse 失效（下一次必须重新采树）
        self._stabilizer.invalidate()
        if action_res.ok and step.action is Action.LAUNCH_APP:
            self._post_launch_settle()
        sr.strategy_used = f"{loc.strategy.type}:{loc.strategy.value}" if loc.strategy else ""
        if loc.strategy is not None and loc.strategy.type == "visual":
            sr.strategy_used = f"visual:{loc.strategy.value.get('label', '')}"
            sr.detail = (sr.detail + " " + f"图像识别命中（相似度 {loc.match_score:.3f}，"
                         f"缩放 {loc.scale:.2f}x）").strip()
        sr.node_label = loc.node.label if loc.node else ""

        if not action_res.ok:
            sr.status = "error"
            sr.error = action_res.error or action_res.detail
            sr.latency_ms = int((time.perf_counter() - t0) * 1000)
            sr.screenshot = before_name
            return sr

        # 后置断言（dsl 才轮询，语义断言只判一次，避免烧钱）
        post = step.postcondition
        verdict = JudgeResult(passed=True)
        if post.kind != "none":
            post, dropped = discriminative_post(post, obs)
            # 纯校验步骤（WAIT_FOR，无动作，如 AI 首跑沉淀的整体验收）：
            # 验收证据在「执行前」就已在屏上 = 场景终态没有变化，这正是验收
            # 要证明的事，直接通过。动作后断言不能这么判——那里的「证据已在」
            # 意味着动作没生效，必须走下面的失效回落逻辑。
            verify_already_true = (dropped and step.action is Action.WAIT_FOR
                                   and post.kind == "assert_screen")
            if verify_already_true:
                verdict = JudgeResult(passed=True, method="witness", score=1.0,
                                      detail=f"验收证据「{dropped}」已在屏上（场景终态未变化）")
                obs2 = obs          # 快捷路径不进判定链，但后续证据截图要用它
            if dropped and not verify_already_true:
                sr.detail = (sr.detail + f" 证据「{dropped}」前后状态都有，已忽略").strip()
                self._emit("witness_dropped", step_id=step.step_id, witness=dropped)
            if not verify_already_true:
                deadline = time.time() + max(1.0, step.timeout_ms / 1000)
                semantic = post.method == "llm" or post.kind == "assert_screen"
                if post.kind == "assert_motion":
                    # 视频类断言：只比像素，不看节点树，也不问模型
                    verdict = self._motion_verdict(post, deadline)
                    try:
                        obs2 = self.driver.observe()
                    except Exception:  # noqa: BLE001
                        obs2 = obs
                elif semantic and not post.witness:
                    # 没有任何廉价证据可用：只能等界面稳定后判一次。
                    # 语义断言动辄几十秒，只能判一次，但判之前必须等界面稳定：
                    # 刚点完登录、跳转还没回来就截图，模型看到的还是登录页，
                    # 于是把"登录成功"判成"失败"——这不是模型的问题，是采样时刻的问题。
                    obs2 = self._settle(deadline)
                    verdict = self.judge.check(post, obs2)
                else:
                    # 有断言证据就走廉价轮询：等界面自己长出来，命中即通过，
                    # 顺带把"等跳转"和"判通过"合成一个动作，不必靠固定 sleep。
                    while True:
                        self._check_cancel()
                        obs2 = self.driver.observe()
                        verdict = self.judge.check(post, obs2)
                        if verdict.passed or time.time() > deadline:
                            break
                        time.sleep(0.3)
                    # 证据失效（改版、换账号、文案变了）→ 回落到语义判定，
                    # 宁可多花一次模型钱，也不能把真通过判成失败。
                    if not verdict.passed and semantic:
                        obs2 = self._settle(deadline + 20)
                        verdict = self.judge.check(post, obs2)
                if verdict.method.startswith("llm"):
                    sr.ai_used = True
        else:
            # 这一步没有断言，after 观察只用来留一张证据图。
            # 再抓一遍节点树纯属浪费——鸿蒙上 dumpLayout 一次 1.6 秒，
            # 十步用例白扔十几秒。截图本身是必须的，树不是。
            obs2 = obs
            try:
                shot = self.driver.screenshot()
                if shot:
                    obs2 = Observation(platform=obs.platform, device_id=obs.device_id,
                                       width=obs.width, height=obs.height,
                                       root=obs.root, screenshot=shot, url=obs.url)
            except Exception:  # noqa: BLE001
                pass

        sr.judge_method = verdict.method if post.kind != "none" else ""
        sr.judge_score = verdict.score
        after_name = f"{idx:02d}_{step.step_id}_after.png"
        if self.config.screenshot_on_success:
            self._dump(art, after_name, obs2.screenshot)
            step.evidence.after = after_name
            sr.screenshot = after_name

        if verdict.passed:
            sr.status = "passed"
            sr.detail = (sr.detail + " " + verdict.detail).strip() or "通过"
        else:
            sr.status = "failed"
            sr.error = verdict.detail or "断言未通过"

        # ---- 感知 → 决策 → 执行 → 验证：偏离检测与自动回退 ----
        try:
            self._verify_deviation(step, case, before_action, obs2, verdict, sr)
        except RunCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            # 偏离链路的任何异常都只降级，绝不影响既有步骤结论
            self._emit("log", level="warn",
                       message=f"{step.step_id} 偏离检测降级：{exc}")

        sr.latency_ms = int((time.perf_counter() - t0) * 1000)
        return sr

    def _heal(self, step: Step, obs: Observation, case: CaseIR, art: Path):
        assert self.healer is not None
        try:
            new = self.healer.heal(step, obs, case)
        except Exception:
            return None
        self.ai_budget -= 1
        if not new:
            return None
        log = art / "healing.log"
        with log.open("a", encoding="utf-8") as f:
            f.write(
                f"[{time.strftime('%H:%M:%S')}] {step.step_id} {step.intent}\n"
                f"  old={[f'{s.type}:{s.value}' for s in step.strategies]}\n"
                f"  new={[f'{s.type}:{s.value}' for s in new]}\n"
            )
        return new

    def _start_recorder(self, art: Path):
        """PC 端 / Web 端开执行录屏；其余平台或 ATP_RECORD=0 时返回 None。"""
        if not self.profile.record_video:
            return None
        from atp.runs.recorder import RunRecorder, _is_disabled

        if _is_disabled():
            return None
        rec = RunRecorder(self.driver, art)
        self._recorder = rec
        try:
            rec.start()
        except Exception:  # noqa: BLE001  录屏起不来不影响执行
            self._recorder = None
            return None
        return rec
