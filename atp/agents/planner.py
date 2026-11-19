"""规划与修复：决定"下一步做什么"，以及定位器失效时怎么救回来。

两条硬规则（也是本模块存在的原因）：

1. 规划器只能从"编号节点列表"里选目标，不允许自由编造选择器——这是定位器质量的源头；
2. 修复器只在单步失效时被调用，且必须返回可校验的定位策略，不接管整条用例。

节点编号（``indexed_nodes``）把界面树压成一份"带编号的清单"：按用例里的实体名
（【】「」等括号词）加权排序，让模型在几百个节点里先看到真正可能的目标。
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from atp.agents.llm.client import LLMClient, LLMError
from atp.common.stepir import CaseIR, LocatorStrategy, NLCase, Step
from atp.common.types import Observation, UINode
from atp.agents.perception.locator import is_editable


# ---------------------------------------------------------------- 节点编号
# 中文用例里实体名的括法五花八门：【搜索】「搜索」『搜索』“搜索”"搜索"《搜索》。
# 早先只认「」和引号，用户全用【】时 focus 直接返回空 → 目标优先级整体失效。
_FOCUS_BRACKET = re.compile(
    r"【([^】]{1,20})】|「([^」]{1,20})」|『([^』]{1,20})』|"
    r"“([^”]{1,20})”|\"([^\"]{1,20})\"|《([^》]{1,20})》"
)


def focus_keywords(nl: NLCase) -> list[str]:
    """从自然语言里抽括号括起来的实体名，作为节点排序的重点目标。

    聊天列表、长表单这类界面节点动辄几百个，固定截断前 110 个很容易把真正的目标挤掉，
    导致模型"看不见"要点的按钮，只能随手挑一个能点的节点（真机上就是把分页
    「1」当成【搜索】按钮点）。按关键词优先能根治这个问题。
    """
    text = f"{nl.nl} {' '.join(nl.asserts)}"
    out: list[str] = []
    for m in _FOCUS_BRACKET.finditer(text):
        kw = next((g for g in m.groups() if g is not None), "").strip()
        if kw and kw not in out:
            out.append(kw)
    return out


def _intent_targets(intent: str) -> list[str]:
    """从一步的意图里抽出「要点什么」：【搜索】按钮 → ['搜索']。"""
    out: list[str] = []
    for m in _FOCUS_BRACKET.finditer(intent):
        w = next((g for g in m.groups() if g is not None), "").strip()
        if w and w not in out:
            out.append(w)
    return out


def _descendant_label(n: UINode, depth: int = 3) -> str:
    """可点击容器的自述文案：取最近的后代文本（ListItem 的字文案在子 Text 上）。"""
    if depth <= 0:
        return ""
    for c in n.children or []:
        t = (c.text or c.desc or "").strip()
        if t and len(t) <= 30 and not any(0xE000 <= ord(ch) <= 0xF8FF for ch in t):
            return t
        t = _descendant_label(c, depth - 1)
        if t:
            return t
    return ""


def _node_matches_target(node: UINode, words: list[str]) -> bool:
    """节点文案/描述/id 里是否出现目标词。去空格再比，«登 录» 也算命中«登录»。

    点击容器（ListItem/按钮壳子）自己往往 text 为空，文案挂在子 Text 上；
    不算子孙文案的话，一致性闸门会把模型**选对的结果行**误判成"节点文案
    和意图不符"，再"纠正"到文案刚好匹配的可点击 TextInput 上——
    run-20261118-091012：节点 18 是正确的 ListItem，却被纠正去点搜索框。
    """
    text = _descendant_label(node) if (node.clickable and not node.text) else ""
    hay = re.sub(r"\s+", "", f"{node.text} {node.desc} {node.id} {text}").lower()
    return any(re.sub(r"\s+", "", w).lower() in hay for w in words if w.strip())


def rectify_target(node: UINode | None, nodes: list[UINode], intent: str,
                   emit=None) -> tuple[UINode | None, str]:
    """把「模型嘴上说的目标」和「手上选的节点」对齐。

    返回 (节点, 图像定位目标词)：
    - 节点非 None：可以直接点（原节点，或已纠正到文案匹配的节点）；
    - 节点 None 且目标词非空：节点列表里压根没有目标，调用方应改走 point，
      绝不能拿一个明显不相干的节点凑数（真机上就是点【搜索】却点了分页「1」）。
    """
    if node is None:
        return None, ""
    words = _intent_targets(intent)
    label = f"{node.text} {node.desc} {node.id}".strip()
    if not words or not label or _node_matches_target(node, words):
        return node, ""
    # 不强制 clickable：<button><span>搜索</span></button> 这种结构里，
    # 文案落在静态 span 上，点它一样会冒泡到按钮；只认可点击节点会白丢一次纠正。
    # 但**输入框必须排除**：它文本里带着刚输入的关键词、又常被标成可点击，
    # 一致性闸门一旦选它，就会把"点结果条目"变成"点搜索框打转"。
    matches = [c for c in nodes
               if c.visible and not is_editable(c.cls)
               and _node_matches_target(c, words)]
    better = next((c for c in matches if c.clickable), None)
    if better is None and matches:
        better = matches[0]
    if better is not None:
        if emit:
            emit("log", level="warn",
                 message=f"目标「{'/'.join(words)}」与选中节点「{label}」不符，"
                         f"已改定位到「{better.text or better.desc}」")
        return better, ""
    if emit:
        emit("log", level="warn",
             message=f"目标「{'/'.join(words)}」在节点列表里没有对应项，"
                     f"不点「{label}」，改用图像定位")
    return None, words[0]


def _descendant_label(n: UINode, depth: int = 3) -> str:
    """可点击容器的自述文案：取最近的后代文本（ListItem 的字文案在子 Text 上）。"""
    if depth <= 0:
        return ""
    for c in n.children or []:
        t = (c.text or c.desc or "").strip()
        if t and len(t) <= 30 and not any(0xE000 <= ord(ch) <= 0xF8FF for ch in t):
            return t
        t = _descendant_label(c, depth - 1)
        if t:
            return t
    return ""


def _field_context(n: UINode, candidates: list[UINode]) -> str:
    """找这个占位值上方最近的字段名：H5 表单标题在上一行、值在下一行。"""
    x1, y1, _, _ = n.bounds
    label = (n.text or n.desc or "").strip()
    best: tuple[int, str] | None = None
    for m in candidates:
        t = (m.text or "").strip()
        if not t or t == label or len(t) > 14:
            continue
        if any(0xE000 <= ord(c) <= 0xF8FF for c in t):   # 图标字体，跳过
            continue
        dy = y1 - m.bounds[3]                              # 字段名在上方
        if dy < 0 or dy > 170:
            continue
        dx = abs(m.bounds[0] - x1)
        if dx > 140:
            continue
        score = dy + dx
        if best is None or score < best[0]:
            best = (score, t)
    return best[1] if best else ""


def _is_hollow_listing(listing: str) -> bool:
    """节点清单是不是"空壳"：条数极少，且没有一处像样的业务文字。

    判据与驱动层 ``DesktopUiaDriver._hollow`` 同源：只有容器类名 / 窗口标题 /
    纯数字这类噪声时，说明 a11y 树没暴露，界面本身没坏（截图仍在）。
    """
    lines = [l for l in listing.splitlines() if l.startswith("[")]
    if not lines:
        return True
    if len(lines) > 12:
        return False
    # 真正有业务含义的文字：排除 cls=/id= 里带出来的 class 名、纯数字、单字标题
    meaningful = 0
    for line in lines:
        m = re.search(r"\btext=(['\"])(.*?)\1", line)
        if not m:
            continue
        txt = m.group(2).strip()
        if not txt or txt.isdigit() or len(txt) <= 1:
            continue
        if txt in {"SampleDesktop", "示例聊天应用", "示例聊天应用-测试版", "Desktop", "Screen"}:
            continue
        meaningful += 1
    return meaningful == 0


def indexed_nodes(root: UINode, max_nodes: int = 110,
                  focus: list[str] | None = None) -> tuple[list[UINode], str]:
    """与 UINode.to_prompt 同一套筛选口径，保证编号与提示词一致。

    排序策略：关键词命中 > 表单控件 > 可点击 > 其余，同分保持 DOM 顺序。
    这样"目标永远在列表第 0 位附近"，但整体仍然可读。
    """
    focus = [f for f in (focus or []) if f]
    # 只给模型看**真的能点到**的节点：uiautomator 会把滚出视口的节点也留在树里，
    # 只是 bounds 报成 (0,0,0,0)。把它们列进提示词，模型就会照着点，等于点屏幕左上角，
    # 界面一动不动——线上真机上就是这么"打转"的。留在树里、但不给模型看，
    # 模型唯一的路就是按提示词先滑一屏。
    candidates = [n for n in root.walk()
                  if n.visible and (n.clickable or n.text or n.desc or n.id or n.value)]

    def hit_focus(n: UINode) -> bool:
        hay = f"{n.text} {n.value} {n.desc} {n.id}"
        return any(len(kw) >= 2 and kw in hay for kw in focus)

    def score(n: UINode) -> float:
        hay = f"{n.text} {n.value} {n.desc} {n.id}"
        s = 0.0
        for kw in focus:
            if kw and kw in hay:
                s += 10 + min(len(kw), 8)
        if n.desc == "可编辑输入区" or n.cls in ("input", "textarea"):
            s += 6
        elif n.desc:
            s += 3
        if n.clickable:
            s += 1
        return s

    pinned: list[UINode] = []
    pinned_ids: set[int] = set()
    for n in sorted(candidates, key=lambda x: -score(x)):
        if len(pinned) >= 45 or score(n) < 3:
            break
        pinned.append(n)
        pinned_ids.add(id(n))
    rest = [n for n in candidates if id(n) not in pinned_ids]
    picked = (pinned + rest)[:max_nodes]

    # 双保险：场景里点名要找的控件，即使排在 DOM 末尾也绝不能被截断掉。
    # 编号与 listing 用同一份 picked，模型看到的编号和 nodes[node_index] 始终一致。
    if focus:
        wanted = [n for n in candidates if n.visible and hit_focus(n)][:max_nodes]
        wanted_ids = {id(n) for n in wanted}
        have = {id(n) for n in picked}
        if any(id(n) not in have for n in wanted):
            picked = (wanted + [n for n in picked if id(n) not in wanted_ids])[:max_nodes]

    # 长表单里同文案的占位值特别多（三个【请选择】、多个【请输入】）。
    # 光给 text，模型分不清哪一行属于【请假类别】、哪一行属于【时间范围】，
    # 只好随便挑一个——真机上就是这样点错了行、跑进附件页。
    # 重复文案额外带上"上方最近的字段名"，模型就能按字段名选对行。
    from collections import Counter

    counts = Counter((n.text or n.desc or "").strip() for n in picked)
    contexts: dict[int, str] = {}
    for n in picked:
        label = (n.text or n.desc or "").strip()
        if not label:
            # 可点击但文本为空的容器（ListItem/卡片行）：文案往往挂在子节点上，
            # 不补出来模型就只能看到「可点击 + 空文本」，不知道这一行是什么——
            # 于是转头去点文本正好匹配的输入框（run-20261117-170154：
            # TextInput '示例综艺节目' 标着可点击，真正的结果行 ListItem 却是空文本）。
            if n.clickable:
                child = _descendant_label(n)
                if child:
                    contexts[id(n)] = child
            continue
        if counts[label] >= 2:
            contexts[id(n)] = _field_context(n, picked)

    lines: list[str] = []
    for i, n in enumerate(picked):
        x, y = n.center
        ctx = contexts.get(id(n))
        hint = f" ctx={ctx!r}" if ctx else ""
        # value 是输入框当前已填的内容。不展示它，模型只看到占位文案「搜索」，
        # 会以为自己没输过，于是反复 input 同一个关键词（run-20261116-093006）。
        val = f" value={(n.value or '')!r}" if n.value else ""
        # 输入框必须单独标「输入框」，不能标「可点击」：它文本里带着刚输入的
        # 关键词时，模型会把它当成"文案正好匹配的可点击结果条目"点进去
        # （run-20261117-170154：点击搜索结果第一条 → 实际命中搜索框）。
        tag = "输入框" if is_editable(n.cls) else ("可点击" if n.clickable else "静态")
        lines.append(
            f"[{i}] {tag} "
            f"cls={n.cls} id={n.id or '-'} text={(n.text or n.desc or '')!r}{val} "
            f"center=({x},{y}){hint}"
        )
    if len(candidates) > max_nodes:
        lines.append(f"... 共 {len(candidates)} 个节点，已按相关度截取前 {max_nodes} 个")
    return picked, "\n".join(lines)


_PLAN_SYSTEM = """你是移动端/桌面端 UI 自动化测试的执行大脑。根据用户给出的测试场景和当前界面，决定"下一步唯一动作"。

严格只输出 JSON，不要任何解释文字：
{"action": "tap|input|point|swipe|back|home|key|launch|open_url|done",
 "node_index": 整数或 null,
 "target": "action=point 时填：要点的东西长什么样（文案/颜色/相对位置）",
 "text": "输入内容，仅 action=input 时填；action=key 时填按键名（如 Enter）",
 "key": "按键名，仅 action=key 时填：enter(回车/发送/确认搜索)、back、tab、esc。触发搜索和发送消息都用 enter，绝不要用 back",
 "url": "要打开的地址，仅 action=open_url 时填",
 "direction": "up|down|left|right，仅 action=swipe 时填",
 "intent": "这一句话描述你在做什么",
 "done_reason": "action=done 时说明为何完成"}

规则：
- 一次只做一个动作，绝不允许"顺便把其他也做了"。
- node_index 必须来自界面节点列表里的编号；**列表里没有合适节点时绝不能用 done**，
  要用 action=point 让系统按截图定位（见下一条）。done 只表示"场景全部做完"，
  绝不表示"我找不到目标"——把"找不到"当成"做完了"是头号失败原因。
- **节点列表为空/几乎为空（只有 Pane、TitleBar、Group 这类无文字空壳），或目标
  （canvas 画的界面、自绘输入框、纯图片、图表里的按钮、会话列表项）不在列表里时，
  必须用 action=point**，在 target 里清楚描述目标长什么样（文案+颜色+相对位置），
  系统会截图+图像识别去定位并沉淀模板。界面截图是真实渲染的，节点树读不到不代表界面是空的。
- 输入框类型节点要用 action=input，不要先 tap 再 input（input 会自己聚焦）。
  描述为「可编辑输入区」的节点就是消息输入框/富文本输入框。
- **标记为「输入框」的节点（TextInput/TextField/EditText 等）绝不能当 tap 目标**：
  输入框里显示的搜索词/已填内容**不是搜索结果**，点它只会唤起键盘、永远进不了
  结果页。要点击结果条目时，选列表里的**可点击行**（ListItem/卡片行，文案在它的
  ctx= 里），文案相同也不是输入框；结果行不在列表里就用 action=point 按截图定位。
- **目标不在当前节点列表里，先怀疑它被滚出了视野**：用 action=swipe 滚一屏再看新列表
  （direction=up 看更下面的内容，down 往回看）。会话列表、商品列表、设置页这类界面
  至少要上下各滑 2~3 屏，才能下"找不到"的结论；直接 done 是最常见的失败原因。
- **长表单 / H5 页面：目标大概率在屏幕下方**。看不到【事由】【共计天数】这类字段时，
  唯一正确的动作是 swipe（direction=up），不要点列表里任何一个"看起来像"的节点。
- 同文案的占位值（多个【请选择】【请输入】）后面带 `ctx=所属字段名`：
  必须按 ctx 选对应那一行，别挑错字段；也不要点底部固定栏里的【附件】【提交】。
- **选择类弹层要两拍**：点开【请假类别】这类选择框后，先点选项（如【年假】），
  **再点底部的【确定】/【确认】才算生效**；点完选项弹层还开着，说明还没提交。
  点完选项后界面回到表单、弹层已关，就说明生效了，不要再点一次同一个选项。
- **走错页面要用 back 退回去**：点了不该点的入口（例如误进「附件」上传页、详情页），
  唯一正确的动作是 action=back，靠滑动/点击是回不来的。
- 只有滚到底/到顶、或 point 视觉定位也确认截图里确实没有目标时，才用 done 并说明卡在哪里。
- **启动/最大化/进入页面之后绝不允许 done**：这些只是准备动作，场景要求的实际操作
  （搜索、点结果、输入、发送等）一个都没做。此时就算节点列表是空的，也要用 point 按截图继续，
  而不是 done。只做了准备动作的 done 会被验收判失败，整条用例报废。
- **已经到达过的地方不要重复进入**：如果当前界面已经是目标的聊天窗口、目标表单或目标页面，
  就不要再点它的入口（会话列表项、导航项、标签页），直接做下一步该做的事。
- **禁止重复同一个动作**：同一个动作连着做 2 次而界面没变化，说明它无效，换节点或换方式。
- **已经打开过的地址不要再次 open_url**：重复导航会把好不容易填好的表单清空。
- **搜索框只输入一次**：输入关键词后下方会出现搜索历史/建议或正在加载的结果。
  节点列表里的 `value='...'` 是输入框**当前实际填的内容**（不是占位文案）；
  value 已经是目标关键词时**绝不要再 input 同样的内容**——那不会产生新结果，
  只会白白消耗步骤。此时应当等待结果、直接点击结果里的联系人/条目，
  或按回车触发搜索；结果项没出现在节点列表里（自绘/canvas 界面）就用
  action=point + target 描述结果条目，让系统按截图定位，不要退回搜索框重输。
- 发送消息时：先在「可编辑输入区」input 文本，再点「发送」按钮或按回车键，两者选其一即可。
- **点完就不要再来一遍**：发送/确认/登录/提交这类按钮，点一次成功（消息已上屏、输入框已清空、
  页面已跳转）就**立刻进入下一步**。只有在你确认第一次根本没生效时才重试，
  并且重试前先说明理由——重复点【发送】只会白白消耗步骤，还可能被判定为原地打转。
- 界面上出现权限/协议/更新弹窗，优先处理弹窗，再继续原任务。
- **done 的门槛很高**：只有场景要求的所有实际操作都做完（消息已发出、表单已提交）才能 done。
  仅仅"打开了页面/进入了会话"不算完成，必须继续做剩下的事。
- **节点树读不到内容时，界面不是坏的**：这是自绘/Electron 界面无障碍树没暴露，截图仍是正确的。
  这种情况一律用 point 视觉定位继续做，绝不能因为"节点列表里没有"就 done。
- 已完成全部场景且有把握时返回 done。"""

_HEAL_SYSTEM = """你是 UI 定位器修复器。某一步在当前界面上找不到目标元素，请给出新的定位方式。

只输出 JSON：
{"by": "text|desc|a11y_id|xpath|bounds",
 "value": "定位值；bounds 用 [x1,y1,x2,y2] 归一化到 0~1000",
 "confidence": 0~1,
 "reason": "一句话"}

优先用界面上可见的稳定文案，不要用日期/金额/序号等易变内容。"""


class Planner(Protocol):
    def next(self, nl: NLCase, obs: Observation, history: list[str]) -> dict[str, Any]: ...


class LlmPlanner:
    def __init__(self, client: LLMClient):
        self.client = client

    def next(self, nl: NLCase, obs: Observation, history: list[str]) -> dict[str, Any]:
        nodes, listing = indexed_nodes(obs.root, focus=focus_keywords(nl))
        hist = "\n".join(history[-12:]) or "（尚未执行任何动作）"
        # 空壳树（自绘 / Electron a11y 没暴露）时明确告诉模型：界面是好的、可用 point，
        # 别把"节点列表空"当成"场景做完了"。run-20261117-124001/124037 就是栽在这里。
        hollow_hint = ""
        if _is_hollow_listing(listing):
            hollow_hint = (
                "\n\n【重要】当前界面节点树读不出内容（全是 Pane/TitleBar 空壳），"
                "但截图里界面是正常渲染的。这是自绘界面的无障碍树未暴露，不是界面坏了。"
                "要操作的元素请用 action=point + target 描述，系统会按截图精确定位。"
                "现在绝不能用 done。"
            )
        prompt = (
            f"测试场景：{nl.nl or nl.title}\n"
            f"期望断言：{'; '.join(nl.asserts) or '无'}\n\n"
            f"已执行：\n{hist}\n\n"
            f"当前界面节点：\n{listing}"
            f"{hollow_hint}"
        )
        try:
            data = self.client.chat_json([
                {"role": "system", "content": _PLAN_SYSTEM},
                {"role": "user", "content": prompt},
            ])
        except LLMError as e:
            # 这里绝不能返回 done：模型挂了、网络断了，一条动作都没做，
            # 早先按 done 处理，于是整条用例"零失败"地被判定通过并沉淀。
            return {"action": "abort", "abort_reason": f"模型不可用: {e}",
                    "_nodes": nodes}
        data["_nodes"] = nodes
        return data


class LlmHealer:
    def __init__(self, client: LLMClient):
        self.client = client

    def heal(self, step: Step, obs: Observation, case: CaseIR) -> list[LocatorStrategy] | None:
        _, listing = indexed_nodes(obs.root)
        prompt = (
            f"目标动作：{step.action.value} —— {step.intent}\n"
            f"原来的定位策略：{[(s.type, s.value) for s in step.strategies]}\n\n"
            f"当前界面节点：\n{listing}"
        )
        try:
            data = self.client.chat_json([
                {"role": "system", "content": _HEAL_SYSTEM},
                {"role": "user", "content": prompt},
            ])
        except LLMError:
            return None

        by = str(data.get("by", "")).strip()
        value = data.get("value")
        if by not in ("text", "desc", "a11y_id", "xpath", "bounds") or value in (None, ""):
            return None
        if by == "bounds":
            if not isinstance(value, list) or len(value) != 4:
                return None

        fresh = LocatorStrategy.make(by, value)  # type: ignore[arg-type]
        fresh.score = float(data.get("confidence", 0.6))
        # 保留旧策略作为后续兜底，只把新策略提到最前
        merged = [fresh] + [s for s in step.strategies if not (s.type == by and s.value == value)]
        return merged[:6]
