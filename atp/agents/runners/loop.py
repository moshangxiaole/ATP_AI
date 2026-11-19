"""首跑主循环（唯一保留大方法的文件，独占一层）。
共享实现。方法体与原 ``agents/runner.py`` 一致，只换归属文件。
"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path
from typing import Callable
from atp.common.profiles import PlatformProfile, profile_for
from atp.runs.artifacts import REPORT_FILE, STEPIR_FILE, new_run_id, run_dir
from atp.runs.events import EventLog
from atp.config import Config
from atp.agents.perception.judge import Judge
from atp.agents.perception.frames import (DEFAULT_MIN_RATIO, PROGRESS_MIN_RATIO,
                                          frame_delta_ratio)
from atp.agents.perception.locator import derive_strategies, resolve
from atp.agents.perception.page_stabilizer import PageStabilizer, page_signature
from atp.memory.popups import PopupGuard, dismiss_popups
from atp.memory.popup_memory import PopupContext, PopupMemory
from atp.common.stepir import (CaseIR, LocatorStrategy, NLCase, Postcondition,
                               RunResult, Step, StepResult)
from atp.common.types import Action, Observation, RunCancelled, UINode
from atp.agents.perception.vision import capture_template, visual_strategy
from atp.agents.platforms.base import BaseDriver
from atp.agents.llm.client import LLMClient
from atp.agents.llm.vision_ground import VisionGrounder
from atp.agents.planner import (LlmHealer, LlmPlanner, Planner,  # noqa: F401
                                focus_keywords, indexed_nodes, rectify_target)
_STRONG_LOCATORS = {"a11y_id", "text", "desc", "xpath"}
_VISUAL_CLASSES = {"canvas", "svg", "img", "image", "map", "video", "webgl"}
_INTERACTION_ACTIONS = {
    Action.TAP, Action.LONG_PRESS, Action.DOUBLE_TAP, Action.INPUT_TEXT,
    Action.CLEAR_TEXT, Action.SWIPE, Action.SCROLL_TO, Action.BACK, Action.HOME,
    Action.PRESS_KEY, Action.OPEN_URL,
}
class LoopMixin:
    """共享实现。"""
    def run(self, nl: NLCase, out_dir: Path | None = None,
            run_id: str = "") -> tuple[CaseIR, RunResult]:
        from atp.agents import runner as _entry
        time = _entry.time
        run_id = run_id or new_run_id(nl.case_id, "ai", unique=False)
        art = run_dir(out_dir or self.config.artifacts, run_id)
        case = CaseIR(
            case_id=nl.case_id, title=nl.title, platform=nl.platform,
            nl=nl.nl, app_id=nl.app_id, tags=nl.tags, variables=nl.variables,
            created_by="ai",
        )
        if nl.url:
            case.variables.setdefault("url", nl.url)
        if nl.endpoint:
            case.variables.setdefault("endpoint", nl.endpoint)
        result = RunResult(
            run_id=run_id, case_id=case.case_id, case_title=case.title,
            platform=nl.platform, device_id=self.driver.device_id,
            mode="ai", artifacts_dir=str(art),
        )
        # PC 端 / Web 端执行录屏：与回放执行器同一套 RunRecorder。
        # stop() 幂等，finally 兜住取消/异常路径；正常结束已在渲染前显式停录。
        recorder = self._start_recorder(nl.platform, art)
        # 执行过程日志（每次决策 + 每条跳过原因）：排查"为什么又回到上一步"必需
        self._events = EventLog.open(art)
        try:
            obs = self.driver.observe()
            case.touch_versions(self._app_version(nl), obs.fingerprint)
            self._case_id = case.case_id
            self._assets = []
            # 基准画布：先记下来，后面所有归一化坐标都以它为准
            case.screen = self._profile(obs)
            if nl.url:
                sid = f"s{len(case.steps) + 1:02d}"
                idx = len(case.steps) + 1
                self._emit("step_started", index=idx, total=0, step_id=sid,
                           intent=f"打开 {nl.url}", action=Action.OPEN_URL.value)
                sr = self._simple(Action.OPEN_URL, {"url": nl.url}, f"打开 {nl.url}", art,
                                  idx - 1, step_id=sid)
                case.steps.append(Step(
                    step_id=sid, intent=f"打开 {nl.url}", action=Action.OPEN_URL,
                    args={"url": nl.url}, source="ai",
                ))
                result.steps.append(sr)
                self._emit("step_finished", index=idx, total=0,
                           result=sr.model_dump(), evidence={})
                if sr.status == "error":
                    result.finished_at = time.time()
                    return case, result
                time.sleep(0.8)
                # 打开 URL 之后立刻最大化：屏幕尺寸是所有坐标映射的分母，必须先生效。
                # 最大化是「打开 URL」这一步的固有语义（写进第一步，不单列一步）；
                # 画布档案必须按最大化后的窗口重采，否则沉淀的基准坐标整体错位。
                if self.driver.supports(Action.MAXIMIZE):
                    self.driver.maximize()
                    sr.detail = (sr.detail + " · 已最大化窗口").strip(" ·")
                obs = self.driver.observe()
                prof = self._profile(obs)
                case.screen = prof
                self._emit("screen", width=prof.width, height=prof.height,
                           dpr=prof.dpr, scale_x=1.0, scale_y=1.0,
                           base_width=prof.width, base_height=prof.height,
                           label=prof.label)
            # 「打开 xx」里的 xx 要先翻成包名：应用台账优先，没登记就去设备上找一次。
            # 控制台没有 app_id 输入框，全靠这一句。翻不出来就没有 launch 步骤，
            # AI 会拿"当前这一屏"当起点瞎点——真机上真出过 1 步就宣布成功、
            # 还沉淀成用例的事故。
            self._resolve_app_name(nl, case)
            self._case_app_id = str(case.app_id or "")
            # 驱动层 perform 的兜底也依赖 driver.app_id（`args.app or self.app_id`）：
            # 解析出用例级 app 之后同步一份，任何遗漏 app 参数的动作都能接住。
            if self._case_app_id and not getattr(self.driver, "app_id", ""):
                self.driver.app_id = self._case_app_id
            if nl.app_id:
                # 冷启动：先结束进程，再启动。移动端不做这一步，首跑的第一屏就不确定，
                # 录下来的定位器全是"碰巧对上"的坐标。
                self._terminate_first(nl, case, result, art)
                sid = f"s{len(case.steps) + 1:02d}"
                idx = len(case.steps) + 1
                self._emit("step_started", index=idx, total=0, step_id=sid,
                           intent=f"启动应用 {nl.app_id}", action=Action.LAUNCH_APP.value)
                sr = self._simple(Action.LAUNCH_APP, {"app": nl.app_id}, "启动应用", art,
                                  idx - 1, step_id=sid)
                case.steps.append(Step(
                    step_id=sid, intent=f"启动应用 {nl.app_id}", action=Action.LAUNCH_APP,
                    args={"app": nl.app_id}, source="ai",
                ))
                result.steps.append(sr)
                self._emit("step_finished", index=idx, total=0,
                           result=sr.model_dump(), evidence={})
                if sr.status == "error":
                    result.finished_at = time.time()
                    return case, result
                # 启动指令是异步的：等首帧落定再规划，否则模型看到的是转场白屏
                settle = 1.0
                cs = getattr(self.config, "cold_start", None)
                if cs is not None and self.driver.platform in (cs.platforms or []):
                    settle = max(settle, (cs.launch_settle_ms or 0) / 1000.0)
                time.sleep(settle)
                # 桌面应用启动后同样先定画布：窗口大小不同，坐标全错
                if self.driver.supports(Action.MAXIMIZE):
                    self._maximize_step(case, result, art)
                obs = self.driver.observe()
                case.screen = self._profile(obs)
            history: list[str] = []
            # 循环外做的准备工作（打开 URL / 启动应用 / 最大化）必须写进历史：
            # 不然模型不知道"应用已经起来了"，第一拍又发一次 launch —— 冗余动作
            # 还会带着空 app 直接失败（run-20261113-163446 的 s03）。
            for st in case.steps:
                if st.action is Action.LAUNCH_APP:
                    history.append(f"{st.step_id} launch_app {st.intent} -> OK（已启动，无需再启动）")
                elif st.action is Action.MAXIMIZE:
                    history.append(f"{st.step_id} maximize {st.intent} -> OK（窗口已铺满屏幕）")
                elif st.action is Action.OPEN_URL:
                    history.append(f"{st.step_id} open_url {st.intent} -> OK（页面已打开）")
                elif st.action is Action.TERMINATE_APP:
                    history.append(f"{st.step_id} terminate_app {st.intent} -> OK（进程已结束）")
            n = len(case.steps)
            repeat_sig = ""
            repeat_rect: tuple[float, float, float, float] | None = None
            repeat_intent = ""
            # 上一步是不是**真的完成了**（界面变了 / 文字上屏）。
            # 与 last_step_changed 的区别：后者只问"有没有实质推进"，
            # 这个还包含"输入的文字确实出现在界面上"这种小像素但确定的完成。
            last_step_done = False
            repeat_count = 0
            # 已经做完的事（step_id, intent）。模型最容易犯的错不是"点错"，
            # 而是"忘了自己已经做完了"，于是看到搜索框被清空就往回走
            # （run-20261113-182603：已进入会话，却回头点了两次搜索框）。
            done_steps: list[tuple[str, str]] = []
            # 已经搜索过的关键词：搜完就不要再回到搜索框。
            searched: list[str] = []
            # 模型声称完成但验收不通过，被打回继续执行的次数
            done_retries = 0
            tap_input_blocks = 0   # 落点压在输入框上的 tap 被拦截的次数
            # 界面树连续为空的拍数（驱动层已自愈仍读不到时，要给模型指路）
            hollow_rounds = 0
            # 熔断三件套（run-20261113-154411 教训：模型换着花样原地转，31 步还不收手）：
            # 1. 墙钟上限 ai_max_seconds —— 超时直接判失败；
            # 2. 页面停滞 ai_stall_steps —— 每步动作签名都不同但页面没变过，防打转
            #    计数器认不出来，页面签名一眼看穿；
            # 3. 同一动作重复（下面的 repeat ladder）。
            t_start = time.perf_counter()
            max_seconds = max(0, int(getattr(self.config, "ai_max_seconds", 600) or 0))
            stall_limit = max(2, int(getattr(self.config, "ai_stall_steps", 6) or 6))
            prev_page_sig = None
            stall_count = 0
            blocked_swipes = 0
            # 上一步有没有真的改变页面：变了就说明"动作虽然一样但有效"
            # （连按数字键、连续翻页），重复计数必须归零，不能误判成打转。
            last_step_changed = True
            # 上一拍界面树有没有变（页面跳转 / 搜索框被应用自动清空）。
            # 像素差看不出来的结构变化也在这里，用来放行"清空后的合法重输"。
            last_tree_changed = False
            skipped_repeats = 0
            last_input_sig = ""
            input_repeat = 0
            # 已确认发出/提交的内容：拦住"发送后又把同一段文字重输一遍"
            sent_words: list[tuple[str, str]] = []
            # 规划是怎么结束的，直接决定这次算不算成功。
            # 早先只有"break 跳出循环"一种结果，于是模型报错、步数耗尽、原地打转
            # 全都长得像"正常完成"——这正是假成功的来源。
            plan_state = "running"
            plan_reason = ""
            for _ in range(nl.max_steps):
                self._check_cancel()
                if max_seconds and time.perf_counter() - t_start > max_seconds:
                    plan_state = "timeout"
                    plan_reason = f"执行时间超过 {max_seconds} 秒上限，自动终止"
                    self._emit("log", level="error", message=f"{plan_reason}")
                    break
                self._emit("thinking", done=len(history))
                # 规划前先把挡路的弹窗清掉：模型看到广告页/更新提示只会瞎点，
                # 而且每多看一眼就是一次 token 钱。
                obs = self._clear_popups(obs)
                obs = self._settle_ready(obs)
                self._check_cancel()
                decision = self.planner.next(nl, obs, history)
                act = str(decision.get("action", "done")).lower()
                # 把模型的原始决策落盘：跳过/回退类问题只有看得到决策才能定位
                self._log_event("decision", {
                    "action": act, "intent": decision.get("intent"),
                    "target": decision.get("target"),
                    "node_index": decision.get("node_index"),
                    "text": decision.get("text"),
                    "direction": decision.get("direction"),
                    "reason": decision.get("done_reason") or decision.get("abort_reason"),
                })
                if act == "abort":
                    plan_state = "abort"
                    plan_reason = str(decision.get("abort_reason")
                                      or decision.get("done_reason") or "规划中止")
                    self._emit("log", level="error", message=f"规划中止：{plan_reason}")
                    break
                if act == "done":
                    plan_reason = str(decision.get("done_reason", ""))
                    has_interaction = any(s.action in _INTERACTION_ACTIONS
                                          for s in case.steps)
                    # 一道硬闸：只做了启动/最大化这类准备动作就 done，一律打回。
                    # 之前这里要求"做过交互才验收打回"，于是 node 树空壳、模型一上来就 done
                    # 时（run-20261117-124001/124037），反而因为没有交互直接放行，
                    # 收尾验收才判失败——整条用例报废。现在这种"零交互 done"必须继续做。
                    if not has_interaction and done_retries < 2:
                        done_retries += 1
                        hint = (
                            "【系统提示】你还没有执行任何实际操作（搜索/点击/输入/发送），"
                            "只有启动和最大化是**不算完成**的。"
                        )
                        if self._hollow_obs(obs):
                            hint += ("当前节点树读不出内容，但**界面截图是正常渲染的**——"
                                     "这是自绘应用无障碍树没暴露。请用 action=point，"
                                     "在 target 里描述要操作的元素（如「顶部写有搜索的输入框」），"
                                     "系统会按截图精确定位。绝不能用 done。")
                        else:
                            hint += ("请在当前界面继续做场景要求的下一步，"
                                     "找不到节点就用 action=point 按截图定位。")
                        history.append(hint)
                        self._emit("log", level="warn",
                                   message="模型在零交互时就 done，已打回继续执行"
                                           f"（第 {done_retries} 次）")
                        continue
                    # 二道硬闸：场景要求「进入播放页/开始播放」，界面上却还只有
                    # 一个【播放】按钮（停在详情页）——这不算到过播放页
                    # （run-20261117-184531：停在详情页就被判成功）。必须真点下去。
                    if (done_retries < 2 and self._scene_expects_play(nl)
                            and self._play_button_on_screen(obs)):
                        done_retries += 1
                        history.append(
                            "【系统提示】当前界面还停在**详情页**，只有【播放】按钮——"
                            "场景要求的「进入播放页面」还没有完成。请点击这个【播放】按钮"
                            "开始播放；界面上出现播放器画面/暂停按钮/进度条才算到过播放页。")
                        self._emit("log", level="warn",
                                   message="场景要求进入播放页但界面只有播放按钮，"
                                           f"打回继续执行（第 {done_retries} 次）")
                        continue
                    # 模型说"做完了"不等于做完了（run-20261113-182603：消息根本没
                    # 发出去它就 done 了）。这里先问一次验收，不通过就把原因甩回去
                    # 让它接着做。只打回一次：验收用的也是模型，可能看错
                    # （run-20261113-202528 就判过"界面上没有示例聊天应用"，其实界面好端端在
                    # 会话页），打回太多次会把模型搞得推翻重来。
                    if (self.client.available and not nl.asserts and done_retries < 2
                            and has_interaction):
                        verdict = self._probe_accept(nl, history)
                        if verdict is not None and not verdict.passed:
                            done_retries += 1
                            miss = (verdict.detail or "场景还没做完").strip()
                            history.append(
                                f"【系统提示】你说已经完成，但验收**不通过**：{miss}。"
                                "任务可能还没做完，请在**当前界面**继续补上剩下的操作。"
                                "注意：不要回桌面、不要重启应用、不要从头再做一遍；"
                                "如果验收说的和你在截图上看到的明显不符，以界面为准，"
                                "并在 done_reason 里说明你实际完成了什么。")
                            self._emit("log", level="warn",
                                       message=f"模型声称完成但验收不通过，已打回继续执行：{miss[:60]}")
                            continue
                    plan_state = "done"
                    history.append(f"完成：{plan_reason}")
                    self._emit("plan_done", reason=plan_reason)
                    break
                if n >= nl.max_steps:
                    plan_state = "max_steps"
                    plan_reason = f"达到最大步数 {nl.max_steps}，动作序列还没走完"
                    break
                node = None
                node_index = decision.get("node_index")
                nodes = decision.get("_nodes") or []
                if isinstance(node_index, int) and 0 <= node_index < len(nodes):
                    node = nodes[node_index]
                # 意图 ↔ 节点一致性闸门：模型偶尔会「嘴上说点【搜索】、手上选了
                # 分页数字 1」。点错的元素照样 perform 成功，旧逻辑会把它记成 passed
                # 并沉淀成错定位器，回放必然跑偏。先按文案自动纠正，纠正不了就
                # 走图像定位/跳过，绝不再拿一个明显不相干的节点去点。
                if act == "tap" and node is not None:
                    node, visual_target = rectify_target(
                        node, nodes, str(decision.get("intent") or ""), self._emit)
                    if visual_target:
                        history.append(
                            f"【系统提示】节点列表里没有「{visual_target}」，"
                            "不要点其它不相关节点；用 point 按图定位或 swipe 找一屏")
                        decision = {"action": "point", "target": visual_target,
                                    "intent": str(decision.get("intent")
                                                  or f"点击{visual_target}")}
                        act = "point"
                step = self._to_step(n + 1, act, node, decision, obs)
                if step is None:
                    history.append(f"跳过非法动作 {act}")
                    self._emit("log", level="warn", message=f"跳过非法动作 {act}")
                    continue
                # 「搜索已经做过了」闸门。
                # 选中搜索结果后，搜索框会被应用自动清空——模型看到空搜索框就以为
                # 没搜成功，于是回头再点搜索框（run-20261113-182603 连着点了两次，
                # 最后直接 done，消息压根没发）。只要关键词已经输进去过、也在搜索框
                # 相关的意图里再次出现，就拦住它，并明确告诉它"清空是正常的"。
                probe = f"{step.intent} {step.args.get('text', '')}"
                if act in ("tap", "input") and searched and any(
                        k in probe for k in ("搜索框", "搜索栏", "搜索框输入", "搜索输入")):
                    hit = next((w for w, _sid in searched if w in probe), None)
                    if hit is not None:
                        sid_done = next(s for w, s in searched if w == hit)
                        skipped_repeats += 1
                        if skipped_repeats >= 3:
                            plan_state = "stalled"
                            plan_reason = f"「{hit}」已经搜索过，模型仍反复回到搜索框"
                            self._emit("log", level="error",
                                       message=f"{plan_reason}，中止本次执行")
                            break
                        history.append(
                            f"【系统提示】「{hit}」在第 {sid_done} 步就已经输入/搜索过了，"
                            "**不要回到搜索框重输**。搜索结果里没有对应节点时（自绘界面），"
                            "用 action=point + target 描述结果条目按截图定位并点击，"
                            "或按回车触发搜索；**点开结果后搜索框被自动清空是正常的**。"
                            "已进入目标会话/页面就直接做下一步该做的事。")
                        self._emit("log", level="warn",
                                   message=f"「{hit}」已搜索过（{sid_done}），"
                                           "跳过回到搜索框（搜索框被清空是正常的）")
                        continue
                # 「不要往容器里打字」闸门。
                # run-20261113-202528 的 s06：模型选中的节点 xpath 指向**大半个窗口**
                # 的容器（bounds 247,108,1000,950），却要往里输入消息。这种节点既不是
                # 输入框、也不接受键盘，按键最后落到"上一次有焦点的搜索框"里，
                # 消息根本没输入进去，界面还一动不动——后面所有步骤就全乱了。
                if (act == "input" and node is not None
                        and not self._looks_editable(node)
                        and node.area > 0.35 * obs.width * obs.height):
                    skipped_repeats += 1
                    if skipped_repeats >= 3:
                        plan_state = "stalled"
                        plan_reason = "反复把文字输入到整块容器里，没有可编辑控件"
                        self._emit("log", level="error",
                                   message=f"{plan_reason}，中止本次执行")
                        break
                    history.append(
                        f"【系统提示】「{node.label}」是一块覆盖大半屏的**容器**，"
                        "不是输入框，往它里面打字不会生效。请输入到真正的可编辑控件上"
                        "（节点列表里带 Edit/EditText/输入 的项），"
                        "没有这样的节点就用 action=point + target 指定输入框的位置。")
                    self._emit("log", level="warn",
                               message=f"「{node.label}」是整块容器，拒绝向其输入文本")
                    continue
                # 「不要把结果条目点成输入框」硬闸：无论模型直接选错，还是
                # rectify_target 纠正时挑中文案匹配的 TextInput，落点压在
                # 可见输入框上的 tap 一律不执行——点它只会唤起键盘打转。
                # 输入框的正确用法是 input；结果条目选列表可点击行（ctx= 里有
                # 文案）或 action=point 按截图定位。
                # 只在「输入过 + 意图确实要点结果」时拦截：普通用例里点输入框
                # 聚焦/改光标是合法动作（test_acceptance 的登录场景就是），
                # 不能一刀切全禁。搜索结果场景才适用这条硬闸。
                result_words = ("结果", "条目", "建议", "列表", "第一个", "第一条")
                has_input = any(s.action is Action.INPUT_TEXT for s in case.steps)
                if (act == "tap" and node is not None and has_input
                        and any(k in step.intent for k in result_words)
                        and self._taps_editable(node, obs)):
                    tap_input_blocks += 1
                    if tap_input_blocks >= 3:
                        plan_state = "stalled"
                        plan_reason = "反复尝试点击输入框，始终没有点到搜索结果条目"
                        self._emit("log", level="error",
                                   message=f"{plan_reason}，中止本次执行")
                        break
                    history.append(
                        "【系统提示】你这次 tap 的落点压在**输入框**上——点它只会"
                        "唤起/收起键盘，不会进入结果页。关键词已经输入过，"
                        "别再碰输入框：列表里的可点击行（ListItem，文案见 ctx=）"
                        "才是搜索结果；没有合适节点就用 action=point + target "
                        "描述结果条目，让系统按截图定位。")
                    self._emit("log", level="warn",
                               message="目标落点在输入框上，已拦截本次 tap")
                    continue
                # 「不要重启已经启动的应用」闸门。
                # 模型一遇到"找不到界面"就 home + 重新 launch（run-20261113-202528 的
                # s08/s09），把已经进入的会话、已经填好的内容全丢掉，然后从搜索重来。
                relaunch = any(s.action is Action.LAUNCH_APP for s in case.steps)
                if act == "launch" and relaunch and not any(
                        k in step.intent for k in ("重启", "重新启动")):
                    skipped_repeats += 1
                    if skipped_repeats >= 3:
                        plan_state = "stalled"
                        plan_reason = "应用已在运行，模型仍要反复重启应用"
                        self._emit("log", level="error",
                                   message=f"{plan_reason}，中止本次执行")
                        break
                    history.append(
                        "【系统提示】应用在本次执行里已经启动过了，**不要重复启动**"
                        "（重启会丢掉当前页面和已经填好的内容）。看不到想要的界面时，"
                        "先用 back 退一层或直接在当前界面找目标；确实需要重启，"
                        "请在动作说明里写明「重启」。")
                    self._emit("log", level="warn",
                               message="应用已在运行，跳过重复启动（避免丢掉当前进度）")
                    continue
                # 「不要一慌就回桌面」闸门：模型把 home 当"重启按钮"用，
                # 只要它说的是"界面异常/找不到/重新进入"，就把它拦在原地。
                panic = any(k in step.intent for k in
                            ("界面异常", "无法定位", "找不到", "重新进入", "重新打开", "看不清"))
                if act == "home" and panic:
                    skipped_repeats += 1
                    if skipped_repeats >= 3:
                        plan_state = "stalled"
                        plan_reason = "反复退回桌面重来，没有在当前界面继续"
                        self._emit("log", level="error",
                                   message=f"{plan_reason}，中止本次执行")
                        break
                    history.append(
                        "【系统提示】不要退回桌面重来——界面可能只是还没刷新或树过期了。"
                        "当前界面已经重新采集，请直接在这张截图上找下一步该点的位置；"
                        "真要回上一级就用 action=back。")
                    self._emit("log", level="warn",
                               message="拦截「回桌面重来」：请先以当前界面为准继续")
                    continue
                # 滑动的前提是页面滑得动（run-20261113-154411：搜索结果浮层不是
                # 滚动容器，模型连发了多次 swipe 全落在原地，还把界面滑乱）。
                # 不可滑动时直接把 swipe 退回给模型，不给设备硬滑。
                if act == "swipe" and not self._page_scrollable(obs):
                    blocked_swipes += 1
                    if blocked_swipes >= 3:
                        plan_state = "stalled"
                        plan_reason = "连续 3 次 swipe 都因页面不可滑动被忽略"
                        self._emit("log", level="error",
                                   message=f"{plan_reason}，中止本次执行")
                        break
                    history.append(
                        "【系统提示】当前页面没有可滚动容器，swipe 已被忽略；"
                        "目标不在这一屏就说明这屏没有，换思路（检查筛选/搜索条件或返回上一级）")
                    self._emit("log", level="warn",
                               message="页面不可滑动，已忽略模型的 swipe 请求")
                    continue
                # 防打转：模型最容易犯的错是"目标已完成还在重复点入口"，靠提示词压不住，得计数。
                #
                # 三条硬约束（run-20261113-165946 的教训：搜索框已填入「联系人乙」，
                # 模型又输了一遍，第 3 次被旧规则直接判死——太早、太狠）：
                # 1. **上一拍真的改变了页面就不算重复**。连按同一个数字键、连续翻页
                #    这类动作坐标完全一样，但页面在动，属于正常操作；只有"做了等于没做"
                #    才叫重复。旧实现只比动作签名，会把这类合法重复也判死。
                # 2. **输入类动作天然不该重复**：同一个输入框、同一段文字再输一次不会
                #    产生任何新结果（无论是搜索还是聊天输入），从第 2 次起就不再执行。
                # 3. 重复不再"强制滑动"救场——那个滑动会改变动作签名、把计数清零，
                #    正是先前无限打转的来源。改为**跳过执行**并明确提示模型换招，
                #    连续跳过 3 次或同一动作僵到第 5 次才判 stalled。
                # 输入类动作单独去重：同一个输入框 + 同一段文字，中间页面变没变都一样，
                # 再输一次不可能有新结果（run-20261113-165946：搜索框已填入「联系人乙」，
                # 模型反复输入，旧规则第 3 次直接判死）。这里从第 2 次起就跳过，
                # 并把"去点搜索结果/执行下一步"明确回给模型。
                if act == "input":
                    text_now = str(step.args.get("text", ""))
                    # 已发送过的内容绝不再输一遍：发送后 Electron 树里看不到气泡，
                    # 模型会误判失败并重输（run-20261117-110950 的 s12），
                    # 结果消息重复、甚至输进顶部搜索框。
                    if text_now.strip() and any(
                            w == text_now.strip() for w, _ in sent_words):
                        skipped_repeats += 1
                        if skipped_repeats >= 3:
                            plan_state = "stalled"
                            plan_reason = "发送后仍反复重输同一段内容，判定原地打转"
                            self._emit("log", level="error", message=plan_reason)
                            break
                        history.append(
                            "【系统提示】这段文字**已经发送过了**（发送动作已成功执行）。"
                            "界面树看不到气泡是自绘控件的正常现象，不是发送失败。"
                            "不要重复输入、也不要再点发送；直接 done。")
                        self._emit("log", level="warn",
                                   message="已发送内容再次输入，已拦下")
                        continue
                    # 只按文字去重，不把 node.bounds 算进签名：point 视觉定位每拍
                    # 给的矩形都略有抖动（run-20261117-110950 的 s06/s07/s12），
                    # 带上 bounds 会让同一段文字被当成三次不同输入、全放行，
                    # 于是输入框里塞了两遍消息、还回退到顶部搜索框重输。
                    # "清空后合法重输"由下面的 still_there + 页面推进判断兜住。
                    in_sig = text_now
                    input_repeat = input_repeat + 1 if in_sig == last_input_sig else 1
                    last_input_sig = in_sig
                    if text_now and input_repeat >= 2:
                        # 前提是文字**现在还在界面上**：进入会话/跳转后搜索框会被
                        # 自动清空（run-20261113-211702），这时重新输入是合法操作，
                        # 不能按"输过同样的词"拦下来。
                        node_text = (node.text or "") if node is not None else ""
                        still_there = (text_now in node_text if node_text.strip()
                                       else self._text_landed(obs.root, text_now))
                        # 旧实现在"树里读不到文字"时无条件把计数清零，于是桌面
                        # Electron 这种不暴露输入值的界面上，同一段文字会被无限重输
                        # （run-20261116-093006：连输 4 次「彭婉」后熔断）。
                        # 现在只有"上一拍页面确有实质推进"（搜索框可能被清空/页面已跳转）
                        # 才放行重输；页面没动而树又读不到值，只能判定为**树盲**，
                        # 按"已经输入过"处理，并把模型往前推。
                        if not still_there and (last_step_changed or last_tree_changed):
                            input_repeat = 1
                            last_input_sig = ""
                        else:
                            skipped_repeats += 1
                            if skipped_repeats >= 3:
                                plan_state = "stalled"
                                plan_reason = ("输入框内容已经填好，模型仍连续重复输入，判定原地打转"
                                               if still_there else
                                               "界面树读不到输入值，模型仍反复重输同一段文字")
                                self._emit("log", level="error",
                                           message=f"{plan_reason}，中止本次执行")
                                break
                            if still_there:
                                hint = ("【系统提示】这个输入框已经输入过同样的内容，"
                                        "**不要重复输入**。请直接点击下方的搜索结果/会话，"
                                        "或执行下一步。")
                            else:
                                hint = ("【系统提示】这段文字刚才已经发往同一个输入框；"
                                        "界面树可能不暴露输入框的当前值（Electron/自绘控件），"
                                        "**读不到不等于没输入**。不要重复 input：请直接点击"
                                        "搜索结果里的目标条目（结果项不在节点列表里就用 "
                                        "action=point + target 按截图定位），或按回车触发搜索，"
                                        "然后继续下一步。")
                            history.append(hint)
                            # 记进"已搜索过"清单：接下来再往搜索框走会被闸门拦住
                            sid_prev = case.steps[-1].step_id if case.steps else "上一步"
                            searched.append((text_now, sid_prev))
                            del searched[:-4]
                            self._emit("log", level="warn",
                                       message="已输入过同样的文字，跳过重复输入并把模型推向结果/下一步")
                            continue
                sig = f"{act}|{step.args.get('text', '')}"
                rect = self._step_rect(step, node, obs)
                intent_sig = self._intent_sig(step.intent)
                # 同一目标 = 中心距离近、或大框套小框、或大体重合（见 _same_target）。
                # 只看中心距离会把"同一个输入框，一次框整行、一次只框文本框内部"
                # 当成两个目标，于是来回点（run-20261113-180724 的 s07/s08）。
                same_target = self._same_target(rect, repeat_rect, obs)
                same_action = sig == repeat_sig
                same_intent = bool(intent_sig) and intent_sig == repeat_intent
                # 压在输入框上的重复点击要单独算：键盘一开一合会让画面签名变化，
                # 把"界面没变"骗过去（run-20261117-170154：s05~s07 连点搜索框三次，
                # 每次键盘开合都被当成新动作，重复闸门数不到）。点输入框永远
                # 进不了结果页，同一意图压着输入框点两次就是打转。
                # 已聚焦的输入框除外——它有专属提示与熔断口径（见下方 focused 分支）。
                tap_editable = (act == "tap" and self._taps_editable(node, obs)
                                and not (node is not None and node.focused
                                         and self._looks_editable(node)))
                if same_action and same_target and (
                        not last_step_changed
                        or (same_intent and tap_editable)):
                    repeat_count += 1       # 上一步做了等于没做
                elif (same_action and same_target and same_intent
                      and last_step_done and self._commit_like(step)):
                    # 上一步**已经成功**，模型下一步还是同一个"一次性提交"按钮、
                    # 同一句话：这就是纯重复（run-20261113-180724 连点两次【发送】）。
                    # 只对提交类按钮生效——翻页/连续点击这类合法重复必须放行。
                    repeat_count += 1
                else:
                    repeat_count = 1
                    repeat_sig = sig
                    repeat_rect = rect
                    repeat_intent = intent_sig
                if repeat_count >= 5:
                    plan_state = "stalled"
                    plan_reason = f"同一动作重复 {repeat_count} 次且界面无变化，判定原地打转"
                    self._emit("log", level="error", message=f"{plan_reason}，中止本次执行")
                    break
                if repeat_count >= 2:
                    # 上一步做了等于没做 / 上一步已经成功，再发一次不会有任何新结果：
                    # 直接跳过执行，并给出一条"该怎么改"的具体指令。
                    # 跳过 3 次仍不改 → 判打转，别让它把步数和额度耗在同一个点上。
                    skipped_repeats += 1
                    if skipped_repeats >= 3:
                        plan_state = "stalled"
                        plan_reason = "连续多次重复同一动作且界面无变化，判定原地打转"
                        self._emit("log", level="error",
                                   message=f"{plan_reason}，中止本次执行")
                        break
                    history.append(self._repeat_hint(step, act,
                                                     done=last_step_done))
                    if tap_editable:
                        history.append(
                            "【系统提示】你反复点击的目标是**输入框/搜索框**（或压在它上面）——"
                            "点输入框只会唤起键盘，不会进入结果页。关键词已经输入过了；"
                            "搜索结果在下方列表里的**可点击行**（ListItem/卡片行，文案在它的 "
                            "ctx= 里），或用 action=point + target 描述结果条目按截图定位。")
                    recap = self._recap(done_steps)
                    if recap:
                        history.append(recap)
                    self._emit("log", level="warn",
                               message=f"上一步已{'生效' if last_step_done else '无效果'}，"
                                       f"已跳过第 {repeat_count} 次重复动作")
                    continue
                # 目标"已经做完了"的显式信号：这个输入框已经拿到焦点。
                # 模型的标准套路是"先点一下输入框，再点一下，再输入"，而点已聚焦的
                # 输入框不会有任何界面变化（run-20261113-173646 连点三次搜索栏，
                # 每步都记 passed）。这一步没必要执行，直接告诉模型去输入。
                if (act == "tap" and node is not None and node.focused
                        and self._looks_editable(node)):
                    skipped_repeats += 1
                    if skipped_repeats >= 3:
                        plan_state = "stalled"
                        plan_reason = "输入框早已聚焦，模型仍反复点击同一位置"
                        self._emit("log", level="error",
                                   message=f"{plan_reason}，中止本次执行")
                        break
                    history.append(
                        f"【系统提示】「{node.label}」已经处于可输入状态（已聚焦），"
                        "这一步其实早就完成了，无需再点；请直接用 action=input 输入文本。")
                    self._emit("log", level="warn",
                               message=f"「{node.label}」已聚焦，跳过重复点击")
                    continue
                n += 1
                blocked_swipes = 0     # 模型听了劝、换了动作：拦截计数归零
                skipped_repeats = 0
                case.steps.append(step)
                self._emit(
                    "step_started", index=n, total=0,
                    step_id=step.step_id, intent=step.intent, action=step.action.value,
                    node=node.label if node else "",
                )
                sr = self._execute(step, node, obs, art, n)
                result.steps.append(sr)
                if (step.action is Action.OPEN_URL
                        and self.profile.maximize_after_open):
                    # WEB 用例的「打开 URL」步骤：打开即立刻最大化（driver.open_url
                    # 内部已做一次，这里兜底再拉一次），并把画布基准重采成最大化后
                    # 的尺寸 —— 沉淀坐标的分母必须与回放时的窗口一致，否则整体错位。
                    if self.driver.supports(Action.MAXIMIZE):
                        try:
                            self.driver.maximize()
                            sr.detail = (sr.detail + " · 已最大化窗口").strip(" ·")
                        except Exception:  # noqa: BLE001
                            pass
                    obs = self.driver.observe()
                    case.screen = self._profile(obs)
                self._emit(
                    "step_finished", index=n, total=0, result=sr.model_dump(),
                    evidence={"before": step.evidence.before, "after": step.evidence.after},
                )
                if sr.status == "error":
                    history.append(
                        f"{step.step_id} {act} {step.intent} -> 失败:{sr.error}")
                    break
                pre_obs = obs
                obs = self.driver.observe()
                self._log_event("observation", self._observation_snapshot(obs))
                # 空树兜底：驱动层会自愈（等 a11y 树长出来/重新认窗口），但万一
                # 还是空的，必须明说"是读不到、不是界面坏了"，并把模型引向
                # point 按坐标点——否则它会直接 done 收场
                # （run-20261113-205329：进会话后"节点列表为空"就放弃了）。
                if self._hollow_obs(obs):
                    hollow_rounds += 1
                    if hollow_rounds <= 2:
                        history.append(
                            "【系统提示】当前界面树读不出任何内容（应用可能正在加载页面），"
                            "**这不代表界面坏了**。不要 done、不要重启："
                            "目标在画面上可见时，用 action=point 描述它的位置直接点；"
                            "否则下一步等页面加载完再操作。")
                        self._emit("log", level="warn",
                                   message="界面树暂时为空，已提示模型改用 point 按坐标操作")
                else:
                    hollow_rounds = 0
                # 步骤完成校验 + 页面停滞熔断。
                #
                # 只看结构签名不够：桌面上"点搜索框"只是拿到焦点，界面树原封不动
                # （run-20261113-173646 连点三次搜索栏、每步都记 passed，模型却
                # 以为没点到，一直在重试）。反过来 canvas 类界面结构本来就不变，
                # 只有画面在动。所以两个信号取"或"：结构变了 or 画面变了，都算这一步
                # 真的推进了；两者都没变才算"这一步没有产生任何效果"。
                try:
                    cur_sig = page_signature(obs.root)
                except Exception:  # noqa: BLE001 —— 签不出来就不参与熔断
                    cur_sig = None
                tree_changed = cur_sig is not None and (
                    prev_page_sig is None or cur_sig != prev_page_sig)
                ratio = frame_delta_ratio(
                    pre_obs.screenshot if pre_obs else None, obs.screenshot)
                if ratio is None:
                    # 内存里没图（某些驱动只在落盘后才给图）→ 退回读 evidence 文件
                    ratio = frame_delta_ratio(
                        art / step.evidence.before if step.evidence.before else None,
                        art / step.evidence.after if step.evidence.after else None,
                    )
                if ratio is not None:
                    # 有前后截图就以画面为准：它才是"用户看到的结果"。树签名在
                    # 第一步必然算"变了"（没有上一拍可比），拿它当推进会被骗。
                    # 两个口径分开：
                    #   changed    = 画面动了（哪怕只亮了个焦点框）→ 用于停滞熔断；
                    #   progressed = 有实质推进（翻页/弹层/换页面）→ 用于判重。
                    changed = ratio >= DEFAULT_MIN_RATIO
                    progressed = ratio >= PROGRESS_MIN_RATIO
                else:
                    changed = progressed = bool(tree_changed)
                # 语义完成校验：输入的文字真实出现在界面上，比"画面变了"更硬。
                # 打两个字只改几十个像素，按像素判会误判成"没效果"，
                # 于是模型又输一遍/又点一遍。
                text_now = str(step.args.get("text") or "")
                landed = False
                if step.action is Action.INPUT_TEXT:
                    landed = self._text_landed(obs.root, text_now)
                    if landed:
                        changed = progressed = True
                # 驱动确实把按键送进了目标：树里读不到 value 不等于"打了空炮"。
                # 桌面 Electron / Chromium 经常只在 ValuePattern 暴露输入值；自绘输入框
                # 甚至根本不在树里，只能靠 point 视觉定位（node.cls == "Point"）。
                # 必须复用 guard 的 _input_delivered —— 它认 Point；之前这里用
                # _looks_editable(node) 重算一遍，Point 被判不可编辑 → input_delivered=False，
                # 于是"landed or input_delivered"分支不进，模型收不到"输入已完成"的提示，
                # 反复重输 s06/s08/s09（run-20261117-130207 的直接原因）。
                input_delivered = self._input_delivered(step, sr, text_now, node)
                # 输入"打了空炮"：既没送进可编辑控件、界面也没有这段文字/任何变化。
                # run-20261113-202528 就是这样——消息没进输入框，界面纹丝不动，
                # 却记了 passed，模型以为已经输好了，接着点发送、然后怀疑界面坏了。
                input_void = (step.action is Action.INPUT_TEXT and sr.ok
                              and not landed and not changed and not input_delivered)
                # 判重只看"有没有实质推进"：点一下搜索框焦点框亮一下不算进展，
                # 否则模型会一直点下去。
                last_step_changed = progressed
                last_tree_changed = bool(tree_changed)
                last_step_done = bool(changed or landed or input_delivered)
                skipped_repeats = 0     # 真的执行了一步：跳过计数归零
                if input_void:
                    sr.status = "failed"
                    sr.error = f"输入没有生效：界面上看不到「{text_now}」"
                    sr.detail = (sr.detail + " · 输入未生效").strip(" ·")
                    history.append(
                        f"{step.step_id} input 「{text_now}」 -> 失败：输入没有生效，"
                        "界面上看不到这段文字，说明没点中输入框（或这个位置不接受键盘输入）。"
                        "请重新精确定位输入框：优先选节点列表里可编辑的控件；"
                        "没有的话用 action=point + target 指到输入框的正文区域，再输入。")
                    self._emit("log", level="error",
                               message=f"{step.step_id} 输入未生效：界面上看不到"
                                       f"「{text_now[:20]}」，请重新定位输入框")
                else:
                    history.append(self._history_line(step, act, changed, progressed, ratio))
                if last_step_done:
                    # 记进"已完成清单"：模型忘了自己做过什么时，靠它把人拽回来
                    done_steps.append((step.step_id, step.intent))
                    del done_steps[:-8]
                # 发送/提交动作成功：明确告诉模型"已发出，不要再输/再发"。
                # Electron 自绘界面点完发送后树里看不到气泡，模型会以为没成功，
                # 于是重输消息、再点发送、甚至回退到搜索框（run-20261117-110950
                # 的 s09~s13 就是这么多跑 4 步的）。验收由最终 v01 统一判定。
                if (sr.ok and self._commit_like(step)
                        and step.action in (Action.TAP, Action.PRESS_KEY)):
                    # 发送动作本身没有 text：取最近一次输入的内容，
                    # 才能拦住"发完之后又输同一段"的回退。
                    sent_text = text_now.strip() or next(
                        (w for w, _ in reversed(searched) if w), "")
                    if sent_text:
                        sent_words.append((sent_text, step.step_id))
                        del sent_words[:-3]
                    history.append(
                        f"【系统提示】{step.step_id} 已执行「{step.intent}」。"
                        "发送/提交类操作**一次就够**：即使界面树暂时看不到消息气泡，"
                        "也说明是 Electron 自绘控件不暴露状态，不是没发出。"
                        "**不要再把同样的文字输入一遍，也不要再点一次发送**。"
                        "若场景已全部完成就直接 done，由最终验收统一判定。")
                if landed or input_delivered:
                    word = text_now.strip()
                    if word:
                        searched.append((word, step.step_id))
                        del searched[:-4]
                    # 给模型的"往下走"指令。模型只看得到节点列表和这段历史，
                    # 不主动告诉它"输入已完成、结果可能在节点列表之外"，
                    # 它就会一直用同一个 input 试探（本次事故的直接原因）。
                    evidence = ("界面树已确认文字上屏" if landed
                                else "驱动已把文字送入可编辑控件，界面树未暴露该值")
                    history.append(
                        f"【系统提示】{step.step_id} 已把「{word}」输入完成（{evidence}），"
                        "这一步到此为止，**不要重复 input 同样的文字**。"
                        "接下来点击结果里的目标条目；结果项不在节点列表里就用 "
                        "action=point + target 按截图定位，或按回车触发搜索，"
                        "然后继续后续步骤。")
                if not changed and sr.ok:
                    # 让报告/详情页也能看出来"这步点了但没有任何反应"
                    sr.detail = (sr.detail + " · 界面无变化").strip(" ·")
                    # 连续没效果时，把"已经做完的事"重新摆给模型看：
                    # 它这时最容易忘记进度、往回到前面的步骤去。
                    recap = self._recap(done_steps)
                    if recap:
                        history.append(recap)
                elif not progressed and sr.ok:
                    sr.detail = (sr.detail + " · 画面仅有细微变化").strip(" ·")
                elif landed:
                    sr.detail = (sr.detail + " · 已确认文字上屏").strip(" ·")
                if cur_sig is not None:
                    prev_page_sig = cur_sig
                if not changed:
                    stall_count += 1
                    if stall_count >= stall_limit:
                        plan_state = "stalled"
                        plan_reason = (f"连续 {stall_count} 步页面无任何变化，"
                                       "疑似原地打转")
                        self._emit("log", level="error",
                                   message=f"{plan_reason}，中止本次执行")
                        break
                else:
                    stall_count = 0
                # 选择类弹层：点完选项界面没变化、确认栏还在 → 补一拍【确定】。
                # 这一拍会被记成真实步骤，所以回放时同样会点，不会出现"首跑过了、回放卡住"。
                # 注意**不能**再用"指纹完全没变"当闸：弹层有展开动画，点开那一屏和
                # 稳定下来的那一屏本来就不一样，真机上就是因为这条闸整段自动确认被跳过。
                if sr.ok and step.action is Action.TAP and node is not None:
                    after = self._auto_confirm(n + 1, node, pre_obs, obs, case, result, art, history)
                    if after is not None:
                        n += 1
                        obs = after
            # 规划没收好尾 = 这次没跑完。必须留一条显式失败步骤，
            # 否则"零失败"会被下游当成"全通过"，直接沉淀出一条假用例。
            if plan_state != "done":
                n += 1
                result.steps.append(StepResult(
                    step_id=f"x{n:02d}", intent=f"规划未正常结束：{plan_reason}", action="noop",
                    status="error", ai_used=True,
                    error=f"{plan_reason}——没有正常收尾，不能判定为通过",
                ))
            else:
                # 场景断言语义化：自然语言 → assert_screen，由判定层处理
                for i, claim in enumerate(nl.asserts, start=1):
                    n += 1
                    before_obs = self.driver.observe()
                    st = Step(
                        step_id=f"a{i:02d}", intent=f"断言：{claim}", action=Action.WAIT_FOR,
                        postcondition=Postcondition(kind="assert_screen", claim=claim,
                                                    method="llm" if self.client.available else "dsl"),
                        source="ai",
                    )
                    case.steps.append(st)
                    self._emit(
                        "step_started", index=n, total=0, step_id=st.step_id,
                        intent=st.intent, action=st.action.value,
                    )
                    sres = self._execute(st, None, before_obs, art, n)
                    result.steps.append(sres)
                    # 断言通过之后，把"这条断言是靠界面上哪句话成立的"记成证据。
                    # 下一次回放就不必再问模型同一句话，AI 步数才能真正压到 3% 以下。
                    if sres.ok:
                        witness = self._capture_witness(before_obs)
                        if witness:
                            st.postcondition.witness = witness
                            sres.detail = (sres.detail + f" · 证据「{witness}」").strip()
                    self._emit(
                        "step_finished", index=n, total=0, result=sres.model_dump(),
                        evidence={"before": st.evidence.before, "after": st.evidence.after},
                    )
                # 前面全过也不代表"这事办成了"——必须再单独验一次整体场景。
                # 已有失败步骤时不用验：结论已经是不通过，别再花钱问一次。
                if all(sr.ok for sr in result.steps):
                    self._accept(nl, case, result, art, history)
            result.finished_at = time.time()
            result.screen = case.screen
            case.visual_assets = list(self._assets)
            case.save(art / STEPIR_FILE)
            # 停录必须在渲染报告之前：报告页要能内嵌这段 MP4。
            if recorder is not None:
                recorder.stop()
            from atp.report.html import render_report
            render_report(result, art)
            result.report_path = str(art / REPORT_FILE)
            return case, result
        finally:
            if recorder is not None:
                recorder.stop()
            if self._events is not None:
                self._events.close()
                self._events = None
