"""弹窗自动处置。

真机上跑用例，十步里能撞三次弹窗：开屏广告、版本更新、青少年模式、权限申请、
"邀你评价"、"签到领奖"、营销浮层。这些都不是用例目标，但每一个都能把手动回放卡死。

设计上的取舍很明确：**宁可不关，也不能点错**。
一次误点可能签到、下单、开通会员，比用例失败严重得多。所以：

1. 关键词白名单，只认「跳过 / 关闭 / 我知道了 / 以后再说 / 允许」这类明确的否定或放行语义；
2. 同一个可点区域里只要出现「加入会员 / 开通 / 立即购买 / 确认支付」等诱导词，
   整块都不点——真实案例：示例视频应用 广告条的「加入会员 跳过此广告」是**同一个点击目标**，
   点"跳过"的位置实际会跳到会员购买页；
3. 单步处置次数有上限，关不掉的弹窗不会把一步拖成死循环；
4. 每处置一个都发事件，报告里能看见"这步关了哪几个弹窗"，而不是悄悄消失。

判定只看节点树，不调模型；处置动作用坐标点击，所以对四端驱动都成立。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from atp.common.types import Observation, RunCancelled, UINode

# 正向关键词：命中即认为"这个弹窗可以安全地关掉"。
# 刻意不含单独的「取消」「放弃」「同意」——它们在正常表单里是会丢数据/下单的动作，
# 不属于"弹窗处置"。宁可不关，也不能替用户做决定。
SKIP_WORDS = ("跳过此广告", "跳过广告", "跳过片头", "跳过", "关闭广告", "关闭弹窗")
CLOSE_WORDS = ("关闭", "我知道了", "知道了", "以后再说", "下次再说", "暂不", "稍后再说",
               "稍后", "残忍拒绝", "不再提示", "下次一定", "忽略")
ALLOW_WORDS = ("始终允许", "仅在使用时允许", "仅本次允许", "允许", "同意并继续")

# 诱导/危险词：出现在同一个可点区域里，整块放弃
DANGER_WORDS = ("加入会员", "开通会员", "立即开通", "开通", "续费", "立即购买", "立刻购买",
                "立即订购", "确认支付", "立即使用", "领取", "查看详情", "下载安装",
                "立即更新", "立即升级", "去购买")

# 这些控件类型本身就是"按钮/文字"，才值得作为弹窗处置目标
_TEXTY = {"text", "button", "genericcontainer", "row", "column", "stack", "flex",
          "navigator", "relativecontainer", "image", "span", "a", "div"}

# 无文字关闭按钮的尺寸区间（占屏幕的比例）。
# 真机实测：示例视频应用 安卓首页活动弹窗的 X 是 `<View id=.../btnClose>`，
# 1260x2800 屏上 105x105 → 宽 8.3% / 高 3.8%。区间给足余量，
# 但上限必须卡死，否则「我知道了」这种 1134x154 的大按钮（宽 90%）也会被当成 X。
_CLOSE_W_MIN, _CLOSE_W_MAX = 0.02, 0.16
_CLOSE_H_MIN, _CLOSE_H_MAX = 0.008, 0.10
# 列表类容器：出现在里面的 close 更像"删掉这一条"，不属于弹窗处置
_LISTY = re.compile(r"recyclerview|listview|gridview|viewpager|scrollview|waterfall", re.I)
# 状态描述里的「关闭」不是关闭按钮。真机血案：设置列表的蓝牙行 desc 是
# 「蓝牙, 已关闭」，白名单里的「关闭」直接命中，一点就跳进蓝牙设置页，
# 后续所有步骤的界面全错位——比不关弹窗严重得多。
_STATE_CLOSE = re.compile(r"(已|未|正在|被|待)\s*关闭|关闭\s*(状态|中|后)")
# 带顿号/逗号的标签是"一句描述"（蓝牙, 已关闭 / WLAN, KLYG），不是按钮
_COMPOSED = re.compile(r"[,，、;；]")
# 这些短词最容易在描述里误命中，需要额外把关
_AMBIGUOUS = ("关闭", "跳过", "忽略", "暂不", "稍后")
# "关闭"前面挂这些词时，说的是状态/行为，不是按钮文案（已关闭 / 自动关闭 / 无法关闭）
_STATE_PREFIX = ("已", "未", "被", "待", "不", "自动", "手动", "正在", "无法", "禁止", "将要")


@dataclass
class PopupHit:
    """一条可直接点掉的弹窗动作。"""

    keyword: str
    text: str
    x: int
    y: int
    kind: str = "close"          # skip / close / allow
    node_cls: str = ""

    def as_dict(self) -> dict:
        return {"keyword": self.keyword, "text": self.text, "x": self.x, "y": self.y,
                "kind": self.kind, "node_cls": self.node_cls}


@dataclass
class PopupGuard:
    """从一次界面观察里找出可安全处置的弹窗。"""

    extra_close_words: list[str] = field(default_factory=list)
    # 单次最多返回几条，防止一口气点掉页面上所有"取消"
    limit: int = 3

    def _words(self) -> list[tuple[str, str]]:
        """(关键词, 类别)，按优先级排序：跳过 > 关闭 > 允许。"""
        out: list[tuple[str, str]] = []
        out += [(w, "skip") for w in SKIP_WORDS]
        out += [(w, "close") for w in CLOSE_WORDS]
        out += [(w, "allow") for w in ALLOW_WORDS]
        out += [(w, "close") for w in self.extra_close_words if w]
        return out

    @staticmethod
    def _ranges(text: str, words: tuple[str, ...] | list[str],
                start: int = 0) -> list[tuple[int, int]]:
        spans = []
        for w in words:
            i = text.find(w, start)
            while i >= 0:
                spans.append((i, i + len(w)))
                i = text.find(w, i + 1)
        return spans

    def find(self, obs: Observation) -> list[PopupHit]:
        """扫描整棵树，返回可点击的弹窗处置动作（不执行）。

        两条通道，顺序有讲究：
        1. **文字通道**：认「跳过 / 关闭 / 我知道了」这类明确语义。
        2. **图标通道**：只在文字通道一无所获时才启用——认 `id` 里带 close 的
           无文字小按钮（安卓上大量弹窗的 X 就是这么做的）。

        为什么图标通道必须排在后面、且加尺寸闸：任何页面上都可能有个 X，
        猜错一次就可能把用户带出当前页面。有文字可依时绝不猜。
        """
        if not obs.root.children:
            return []

        hits: list[PopupHit] = []
        seen: set[tuple[int, int]] = set()
        w, h = obs.width or 0, obs.height or 0

        for node in obs.root.walk():
            label = _label(node)
            if not label:
                continue
            if not _onscreen(node, w, h):
                continue
            # 命中直接放弃：可点区域里一旦混进诱导词，这块无论点哪儿都可能走偏
            if any(w_ in label for w_ in DANGER_WORDS):
                continue

            for word, kind in self._words():
                idx = label.find(word)
                if idx < 0:
                    continue
                if _is_description(label, word, idx):
                    continue
                x, y = _point_for(node, label, word, idx)
                key = (x // 8, y // 8)
                if key in seen:
                    continue
                seen.add(key)
                hits.append(PopupHit(keyword=word, text=label[:40], x=x, y=y,
                                     kind=kind, node_cls=node.cls))
                break

        if not hits:
            return self._find_icon_close(obs)

        # 跳过优先：广告条上同时有"跳过"和"关闭"时先试跳过
        order = {"skip": 0, "close": 1, "allow": 2, "close_id": 3}
        hits.sort(key=lambda h: order.get(h.kind, 9))
        return hits[: self.limit]

    def _find_icon_close(self, obs: Observation) -> list[PopupHit]:
        """无文字关闭按钮兜底：`id` 带 close、尺寸像角标、不在列表容器里。"""
        w, h = obs.width or 0, obs.height or 0
        if not w or not h:
            return []

        out: list[PopupHit] = []

        def walk(node: UINode, in_list: bool) -> None:
            if len(out) >= self.limit:
                return
            in_list = in_list or bool(_LISTY.search(node.cls))
            if (not in_list and not _label(node) and node.clickable
                    and _onscreen(node, w, h)):
                name = _resource_name(node.id)
                low = name.lower()
                if "close" in low and not low.endswith("closed"):
                    x1, y1, x2, y2 = node.bounds
                    nw, nh = x2 - x1, y2 - y1
                    if (_CLOSE_W_MIN * w <= nw <= _CLOSE_W_MAX * w
                            and _CLOSE_H_MIN * h <= nh <= _CLOSE_H_MAX * h):
                        out.append(PopupHit(keyword=name, text="", x=(x1 + x2) // 2,
                                            y=(y1 + y2) // 2, kind="close_id",
                                            node_cls=node.cls))
            for c in node.children:
                walk(c, in_list)

        walk(obs.root, False)
        return out


def _label(node: UINode) -> str:
    return f"{node.text or ''} {node.desc or ''}".strip()


def _is_description(label: str, word: str, idx: int) -> bool:
    """这个「关闭/跳过」是状态描述，还是真的可点的关闭按钮？

    判据只看措辞本身，不猜意图：
    - 「已关闭 / 未关闭 / 正在关闭」是状态；
    - 带逗号顿号的标签是一句描述（`蓝牙, 已关闭`、`WLAN, KLYG`），按钮不长这样；
    - 判定不了的一律**当作描述**——漏关一个弹窗只是难受，误点一行的代价是整条用例错位。
    """
    if word not in _AMBIGUOUS:
        return False
    if _COMPOSED.search(label):
        return True
    if label[max(0, idx - 2):idx].endswith(_STATE_PREFIX):
        return True
    return bool(_STATE_CLOSE.search(label))


def _onscreen(node: UINode, w: int, h: int) -> bool:
    """节点是不是"真的在屏幕上"。

    这一步不是洁癖。真机实测：示例视频应用 详情页里藏着一个 bounds=(-2,0,...) 的
    「跳过前贴片广告」节点，白名单一命中就去点 (-2,0)——那根本不是跳过按钮，
    而是屏幕左上角外侧，点下去会拉到系统状态栏，把整个用例带到别处。
    任何隐藏（0 尺寸 / 完全在屏幕外）的节点一律不参与处置。
    """
    x1, y1, x2, y2 = node.bounds
    if x2 - x1 <= 2 or y2 - y1 <= 2:
        return False
    if x2 <= 0 or y2 <= 0:
        return False
    if w and h and (x1 >= w or y1 >= h):
        return False
    return True


def _resource_name(res_id: str) -> str:
    """`com.example.videoapp:id/btnClose` → `btnClose`。

    鸿蒙/HarmonyOS 的 id 也常见 `btn_close`、`close_button` 这类写法，
    最后一段取出来再判断，比在整串里找子串稳（包名里出现 close 的概率不为零）。
    """
    if not res_id:
        return ""
    tail = re.split(r"[/:.]", res_id)
    return tail[-1] if tail else res_id


def dismiss_popups(driver, obs: Observation, guard: PopupGuard | None,
                   settle_ms: int = 600, on_event=None,
                   rounds: int = 3,
                   memory=None, stabilizer=None, ctx=None) -> tuple[Observation, list[dict]]:
    """扫描 → 点掉 → 重新观察，最多 `rounds` 轮。

    执行器和 AI 首跑共用这一份实现：两边各写一份，早晚会有一边忘了带黑名单。
    返回（最新观察，已处置记录）。

    每一轮点击前都会**重新抓一次树**再定坐标。这不是洁癖，是真机上撞出来的：
    开屏广告的「跳过」在 (1084,225)，抓树时它还在，点下去时广告已经自己播完，
    这一点正好落在首页右上角的搜索入口上——用例于是从"等首页"变成"进了搜索页"，
    后面所有步骤全错位。抓树和点击之间差了 2 秒，界面完全可能已经换了。

    ``memory`` / ``stabilizer`` / ``ctx`` 三个参数默认 None；全为 None 时
    行为与原实现逐字一致。有记忆且上下文允许读取时，每轮在 ``guard.find``
    之前先试一次零决策快路径；快路径任何一条安全闸不过，立刻登记 miss、
    发 ``popup_memory_miss``，并**回退完整全量决策**（本步不再重试该条目）。
    """
    done: list[dict] = []
    if guard is None:
        return obs, done

    memory_read = False
    memory_write = False
    blocked_sigs: set[str] = set()
    if memory is not None and ctx is not None:
        try:
            memory_read = bool(memory.enabled_for(ctx))
            memory_write = bool(memory.enabled_for(ctx, for_write=True))
        except Exception:  # noqa: BLE001
            memory_read = False
            memory_write = False

    def _emit_event(ev: dict) -> None:
        if on_event is None:
            return
        try:
            on_event(ev)
        except Exception:  # noqa: BLE001
            pass

    for _ in range(max(1, rounds)):
        # ---- 快路径：命中长程记忆时不经 guard 决策，但安全闸一条不少 ----
        if memory_read and _memory_has_active(memory, ctx):
            # 零抓树的 O(n) 检查：库里没条目时绝不额外 dump 一次树
            fast = _memory_fast_path(driver, obs, guard, memory, ctx, settle_ms,
                                     stabilizer, blocked_sigs, _emit_event)
            if fast is not None:
                record, new_obs = fast
                done.append(record)
                obs = new_obs
                # 快路径命中且效果已确认（签名节点消失）→ 直接结束本轮：
                # 不再回到 guard.find() 做全树扫描（AC-3.3：记忆命中时扫描次数为 0）。
                # 如果这一屏还有别的弹窗，交给下一步的 _dismiss_popups 处理，
                # 绝不会因为省一次扫描而漏掉安全闸。
                break

        # ---- 全量决策路径：原实现一行未删 ----
        hits = guard.find(obs)
        if not hits:
            break
        # 点击前再看一眼：弹窗自己消失是最常见的情况，这时一个点都不该点。
        # 而且这份树必须是"新的"——界面还在动时 uiautomator 抓不到树、只能退回缓存，
        # 拿两三秒前的坐标去点，落到哪儿全凭运气（真机撞过：跳过 → 首页搜索入口）。
        fresh = _fresh_obs(driver)
        for _ in range(2):
            if fresh is not None and not getattr(driver, "tree_stale", False):
                break
            time.sleep(0.5)
            fresh = _fresh_obs(driver)
        if fresh is None or getattr(driver, "tree_stale", False):
            # 界面一直在动（广告、转场、加载动画）。这一轮不点：
            # 晚一步关弹窗只是慢，点错一步是整条用例报废。
            break
        obs = fresh
        fresh_hits = guard.find(fresh)
        if not fresh_hits:
            break
        hits = fresh_hits
        for h in hits:
            try:
                driver.tap_xy(h.x, h.y)
            except Exception:  # noqa: BLE001
                return obs, done
            record = h.as_dict()
            record["source"] = "guard"
            record["verified"] = False
            record["sig"] = ""
            if memory_write:
                sig = _memory_signature(fresh, h, ctx)
                if sig:
                    record["sig"] = sig
                    try:
                        memory.observe_success(
                            {"sig": sig, "obs": fresh, "hit": h}, ctx,
                            str(getattr(ctx, "run_id", "") or ""),
                        )
                        _emit_event({"type": "popup_memory_learn", "sig": sig,
                                     "keyword": h.keyword, "kind": h.kind})
                    except Exception:  # noqa: BLE001
                        pass
            if stabilizer is not None or memory_write:
                verified, post = _post_tap_verify(driver, settle_ms, stabilizer,
                                                  memory, ctx, record.get("sig", ""))
                record["verified"] = bool(verified)
                if post is not None:
                    obs = post
            elif settle_ms:
                time.sleep(settle_ms / 1000)
            done.append(record)
            _emit_event({"type": "popup_dismissed", **record})
        try:
            obs = driver.observe()
        except Exception:  # noqa: BLE001
            break
    return obs, done


def _memory_has_active(memory, ctx) -> bool:
    """记忆库是否已有可复用的 active 条目；任何异常都只降级为「不用快路径」。"""
    try:
        return bool(memory.has_active(ctx))
    except Exception:  # noqa: BLE001
        return False


def _memory_signature(obs: Observation, hit: PopupHit, ctx) -> str:
    """惰性导入，避免 popup.py ↔ popup_memory.py 模块级循环依赖。"""
    try:
        from atp.memory.popup_memory import build_signature

        return build_signature(obs, hit, str(getattr(ctx, "app_id", "") or ""),
                               str(getattr(ctx, "platform", "") or ""))
    except Exception:  # noqa: BLE001
        return ""


def _fail_memory(memory, entry, ctx, sig: str, reason: str, blocked: set[str],
                 emit) -> None:
    """快路径任一安全闸不过：登记 miss + 发事件，本步不再重试该条目。"""
    if sig:
        blocked.add(sig)
    if memory is not None and entry is not None:
        try:
            memory.miss(entry, reason)
        except Exception:  # noqa: BLE001
            pass
    emit({"type": "popup_memory_miss", "sig": sig or "", "reason": reason,
          "keyword": str(getattr(entry, "keyword", "") or ""),
          "state": str(getattr(entry, "state", "") or "")})


def _post_tap_verify(driver, settle_ms: int, stabilizer, memory, ctx,
                     sig: str) -> tuple[bool, Observation | None]:
    """点击后确认签名节点消失；有门禁时用树-only 稳定采样替代固定 sleep，最多补 1 次。

    返回 ``(是否确认消失, 最新观察)``；无记忆时只做门禁采样，返回 (True, None)。
    """
    verified = True
    post: Observation | None = None
    if stabilizer is not None and settle_ms > 0:
        try:
            stabilizer.invalidate()
            res = stabilizer.wait_stable("popup_settle", ceiling_ms=settle_ms)
            if not res.stable:
                # 门禁最多补 1 次采样，绝不串联叠加多轮等待
                res = stabilizer.wait_stable("popup_settle_recheck", ceiling_ms=settle_ms)
            if res.obs is not None:
                post = res.obs
        except RunCancelled:
            raise
        except Exception:  # noqa: BLE001
            post = None
    elif settle_ms:
        time.sleep(settle_ms / 1000)

    if sig and memory is not None and ctx is not None:
        try:
            entry = memory.entry_for(sig)
            fresh = post if post is not None else _fresh_obs(driver)
            if entry is not None and fresh is not None:
                verified = not bool(memory.signature_present(fresh, entry, ctx))
            else:
                verified = False
        except Exception:  # noqa: BLE001
            verified = False
    return verified, post


def _memory_fast_path(driver, obs: Observation, guard: PopupGuard, memory, ctx,
                      settle_ms: int, stabilizer, blocked: set[str], emit):
    """零决策快路径：结构化记忆命中 → 新鲜树重定位 → 全部安全闸 → 点击 → 确认消失。

    成功返回 ``(record, new_obs)``；任何一步不成立返回 None，由调用方回退全量决策。
    """
    entry = None
    sig = ""
    try:
        # 1. 当前新鲜树（树-only）+ tree_stale 必须是 False
        fresh = _fresh_obs(driver)
        if fresh is None or getattr(driver, "tree_stale", False):
            return None
        entry = memory.lookup(fresh, ctx)
        if entry is None or entry.sig in blocked:
            return None
        sig = entry.sig

        # 2. 按条目里的稳定属性在当前树上重新定位，并按当前 bounds 算点击点
        node = memory.find_close_node(fresh, entry)
        if node is None:
            # 防御性兜底、当前不可达：正常链路 lookup() 内部已用 find_close_node
            # 过滤过，同一棵 fresh 树不会两次返回不同结果。保留是因为 memory 是
            # 鸭子类型（单测替身 / 未来实现可能不保证该不变量），这里必须拦住 None，
            # 否则会在下面 node.center 处抛 AttributeError 变成无记录的异常降级。
            _fail_memory(memory, entry, ctx, sig, "close_node_missing", blocked, emit)
            return None
        if getattr(driver, "tree_stale", False):
            _fail_memory(memory, entry, ctx, sig, "tree_stale", blocked, emit)
            return None
        if not _onscreen(node, fresh.width, fresh.height):
            _fail_memory(memory, entry, ctx, sig, "offscreen", blocked, emit)
            return None
        label = _label(node)
        idx = label.find(entry.keyword) if entry.keyword else -1
        if idx >= 0:
            x, y = _point_for(node, label, entry.keyword, idx)
        else:
            x, y = node.center

        # 3. DANGER_WORDS 复核整个可点区域（含弹窗容器与落点最小节点）
        container = memory.container_of(fresh, entry)
        for probe in (node, container):
            if probe is None:
                continue
            for sub in probe.walk():
                if any(word in _label(sub) for word in DANGER_WORDS):
                    _fail_memory(memory, entry, ctx, sig, "danger_word", blocked, emit)
                    return None
        smallest = fresh.root.find_smallest_at(x, y, 2)
        if smallest is not None and any(word in _label(smallest) for word in DANGER_WORDS):
            _fail_memory(memory, entry, ctx, sig, "danger_word_at_point", blocked, emit)
            return None
        if not _onscreen(node, fresh.width, fresh.height):
            _fail_memory(memory, entry, ctx, sig, "offscreen_recheck", blocked, emit)
            return None

        # 4. 点击 → 设备动作后立刻让会话级 reuse 失效
        try:
            driver.tap_xy(x, y)
        except Exception:  # noqa: BLE001
            _fail_memory(memory, entry, ctx, sig, "tap_failed", blocked, emit)
            return None
        if stabilizer is not None:
            try:
                stabilizer.invalidate()
            except Exception:  # noqa: BLE001
                pass

        # 5. 点击后确认签名节点消失（门禁最多补 1 次采样）
        verified, post = _post_tap_verify(driver, settle_ms, stabilizer,
                                          memory, ctx, sig)
        if not verified:
            _fail_memory(memory, entry, ctx, sig, "signature_still_present",
                         blocked, emit)
            return None

        hit = PopupHit(keyword=entry.keyword, text=label[:40], x=x, y=y,
                       kind=entry.kind, node_cls=node.cls)
        record = hit.as_dict()
        record["source"] = "popup_memory"
        record["verified"] = True
        record["sig"] = sig
        emit({"type": "popup_memory_hit", "sig": sig, "keyword": entry.keyword,
              "kind": entry.kind, "confidence": entry.confidence})
        emit({"type": "popup_dismissed", **record})
        try:
            new_obs = driver.observe()
        except Exception:  # noqa: BLE001
            new_obs = post if post is not None else fresh
        return record, new_obs
    except RunCancelled:
        raise
    except Exception:  # noqa: BLE001
        # 记忆链路的任何异常都只降级，绝不影响既有弹窗处置主流程
        if sig:
            _fail_memory(memory, entry, ctx, sig, "fast_path_exception", blocked, emit)
        return None


def _fresh_obs(driver) -> Observation | None:
    """只为"确认按钮还在原地"抓一次树，不必截图。"""
    try:
        from atp.agents.perception.scroller import observe_tree

        return observe_tree(driver)
    except Exception:  # noqa: BLE001
        try:
            return driver.observe()
        except Exception:  # noqa: BLE001
            return None


def _point_for(node: UINode, label: str, word: str, idx: int) -> tuple[int, int]:
    """算出关键词在节点上的点击点。

    一个节点常常塞多段文案（`加入会员 跳过此广告`、`取消  确定`）。直接点节点中心
    很容易点到隔壁按钮，所以按"字符占比"把关键词的位置换算成像素偏移。
    """
    x1, y1, x2, y2 = node.bounds
    width = max(1, x2 - x1)
    cy = (y1 + y2) // 2
    text = node.text or ""

    if idx >= len(text):
        # 关键词落在 desc 里，拿不到它在本行的位置，只能退回节点中心
        return (x1 + x2) // 2, cy

    total = max(1, len(text))
    start = min(idx, total)
    end = min(total, start + len(word))
    cx = x1 + int(width * (start + end) / 2.0 / total)
    # 夹回节点内部，避免算到边界外
    cx = min(max(cx, x1 + 2), x2 - 2)
    return cx, cy
