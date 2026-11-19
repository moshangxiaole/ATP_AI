"""定位器：六级降级 + 权重在线学习。

回放阶段的稳定性完全取决于这一层。原则是：
1. 永不用单一策略；2. 每次命中都回写权重；3. 全 miss 才允许 AI 介入。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from atp.common.stepir import LOCATOR_PRIORITY, LocatorStrategy
from atp.common.types import UINode

_PRED = re.compile(r"\[@(\w+)=['\"]([^'\"]*)['\"]\]")
_INDEX = re.compile(r"\[(\d+)\]")
_DYNAMIC = re.compile(
    r"^[\d\s:：\-/.]+$|"            # 纯数字 / 日期 / 时间
    r"^[\d.,]+$|"                   # 纯金额数字（1,234.00）
    r"^\d{4}-\d{2}-\d{2}|"
    r"\d{4}\s*年\d{1,2}\s*月|"
    r"^[¥$￥]\s?[\d,.]+$|"          # 带币种
    r"\d{2}:\d{2}"                  # 含时刻
)


def is_stable_text(text: str) -> bool:
    """文案能不能当定位依据：日期、金额、长句、纯数字都不行。"""
    if not text or len(text) > 24:
        return False
    if _DYNAMIC.search(text):
        return False
    return True


def is_stable_id(value: str) -> bool:
    """resource-id 天然很长（含包名），不能套用文案的长度限制。
    只排除明显动态的 id：带 4 位以上连续数字、uuid、纯序号。"""
    if not value:
        return False
    if re.search(r"\d{4,}", value):
        return False
    if re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", value):
        return False
    if re.search(r"/\d+$", value):
        return False
    return True


@dataclass
class LocateResult:
    """一次定位的结果：命中的节点、置信度与失败原因。"""
    node: UINode | None = None
    strategy: LocatorStrategy | None = None
    attempts: list[tuple[str, bool]] = field(default_factory=list)
    reason: str = ""
    # 视觉兜底命中时的匹配分数与缩放比（其余策略恒为 0 / 1）
    match_score: float = 0.0
    scale: float = 1.0
    # 策略在树里匹配到了、但目标在屏幕外（bounds 全 0 或中心点在画布之外）。
    # 这个字段的唯一用途是让人看懂"为什么这次不算命中"——它必须触发滑动查找。
    offscreen: str = ""

    @property
    def ok(self) -> bool:
        return self.node is not None


def _xpath_match(root: UINode, expr: str) -> list[UINode]:
    """极简 xpath 子集：//Cls、[@text=]、[@id=]、[@desc=]、[n]、以及 / 链式。

    不追求完整规范，只覆盖自动化里真正用得上的写法，避免引入 lxml 依赖。
    """
    expr = expr.strip()
    descendant = expr.startswith("//")
    body = expr[2:] if descendant else expr.lstrip("/")
    segments = [s for s in body.split("/") if s]

    current: list[UINode] = [root]
    for i, seg in enumerate(segments):
        idx = None
        m = _INDEX.search(seg)
        if m:
            idx = int(m.group(1))
        preds = dict(_PRED.findall(seg))
        tag = _INDEX.sub("", _PRED.sub("", seg)) or "*"

        pool: list[UINode] = []
        for node in current:
            pool.extend(node.walk() if (i == 0 and descendant) else node.children)

        hits = []
        for n in pool:
            if tag not in ("*", "") and n.cls != tag:
                continue
            if "text" in preds and n.text != preds["text"]:
                continue
            if "id" in preds and n.id != preds["id"]:
                continue
            if "desc" in preds and n.desc != preds["desc"]:
                continue
            hits.append(n)

        if idx is not None:
            hits = [hits[idx - 1]] if 0 < idx <= len(hits) else []
        current = hits
        if not current:
            return []
    return current


def _narrow(hits: list[UINode], node_cls: str) -> list[UINode]:
    """同文案命中多个节点时，用控件类型收窄——这是"同意"按钮和"同意"输入框的关键区别。"""
    if not node_cls or not hits:
        return hits
    same = [n for n in hits if n.cls == node_cls]
    return same or hits


def _on_screen(node: UINode | None, screen: tuple[int, int] = (0, 0)) -> bool:
    """这个节点现在真的能点吗？

    三道检查，少一道都会让"找不到就滑动"失效：
    1. 面积 > 0 —— uiautomator 把滚出视口的节点报成 (0,0,0,0)；
    2. 中心点在画布内 —— 半出屏的节点点中心会点到屏幕外，等于空点；
    3. 中心点没有被别的节点盖住 —— 长表单底部常有固定栏（附件/提交），
       页面内容滑到它下面时，坐标是"对"的、点下去却落在固定栏上。
       真机实测：点「请假类别」的「请选择」直接跳进了「附件」页。
    """
    if node is None or not node.visible:
        return False
    if screen and screen[0] > 0 and screen[1] > 0:
        cx, cy = node.center
        if not (0 <= cx <= screen[0] and 0 <= cy <= screen[1]):
            return False
    return True


def _visible(hits: list[UINode], screen: tuple[int, int] = (0, 0)) -> list[UINode]:
    """节点是否可见（尺寸非零且未被标记为隐藏）。"""
    return [n for n in hits if _on_screen(n, screen)]


def _contains(node: UINode, x: int, y: int) -> bool:
    """矩形 a 是否包含矩形 b。"""
    x1, y1, x2, y2 = node.bounds
    return x1 <= x <= x2 and y1 <= y <= y2


def _related(a: UINode, b: UINode) -> bool:
    """两个节点是否属于同一区域，用于合并相邻候选。"""
    return (any(n is b for n in a.walk()) or any(n is a for n in b.walk()))


def _overlap_ratio(a: UINode, b: UINode) -> float:
    """两个矩形对 a 的覆盖比例（0~1）。"""
    ax1, ay1, ax2, ay2 = a.bounds
    bx1, by1, bx2, by2 = b.bounds
    w = min(ax2, bx2) - max(ax1, bx1)
    h = min(ay2, by2) - max(ay1, by1)
    if w <= 0 or h <= 0 or a.area <= 0:
        return 0.0
    return (w * h) / a.area


def describe(node: UINode | None) -> str:
    """给人看的一句话标签：优先自身文案，其次子树里第一段文案，最后退到控件类型。

    遮挡物往往是个没有文案的容器（底部固定栏），直接用 label 会得到 "LinearLayout"，
    对排查毫无帮助。
    """
    if node is None:
        return ""
    for n in node.walk():
        t = (n.text or n.desc or "").strip()
        if t:
            return t[:20]
    return node.cls or "遮挡层"


def occluded_by(root: UINode, node: UINode) -> UINode | None:
    """谁盖住了这个节点的中心？

    dump 顺序就是绘制顺序：越靠后的兄弟越晚画、盖在上面。所以取"包含中心点的
    最后一个节点"；它和目标是祖先/后代关系时不算遮挡（父子本来就会重叠）。
    """
    if not node.visible:
        return None
    cx, cy = node.center
    covers = [n for n in root.walk()
              if n is not node and n.visible and _contains(n, cx, cy)]
    if not covers:
        return None
    top = covers[-1]
    if _related(top, node):
        return None
    return top if _overlap_ratio(node, top) >= 0.5 else None


def _prefer_near(hits: list[UINode], ref: tuple[int, int] | None,
                 screen: tuple[int, int]) -> list[UINode]:
    """同文案命中多行时，优先挑离"沉淀时记下的位置"最近的那一行。

    H5 长表单里【请假类别】【假期子类别】【时间范围】的值全是「请选择」，
    文本策略一视同仁地取 DOM 第一个，于是"选请假类别"经常点到"时间范围"。
    只有位置差在一屏的 1/4 以内才认这个参考点——页面滚过之后，旧坐标反而是误导。
    """
    if not ref or len(hits) < 2 or not screen[1]:
        return hits

    def dist(n: UINode) -> int:
        cx, cy = n.center
        return abs(cx - ref[0]) + abs(cy - ref[1])

    if min(dist(n) for n in hits) > screen[1] * 0.25:
        return hits
    return sorted(hits, key=dist)


def _match_one(root: UINode, s: LocatorStrategy, screen: tuple[int, int],
               avoid_occluded: bool = False,
               ref: tuple[int, int] | None = None,
               expected: set[str] | None = None) -> UINode | None:
    """按单条策略找**可点击的**目标。屏幕外、被遮挡的节点一律不算命中。

    `avoid_occluded` 由调用方决定：执行器第一轮要求"真能点到"（被盖住就去滑动），
    滑动也没能救回来时才退回宽松模式，避免把原本点得到的目标判成失败。
    `ref` 是其它策略记下的坐标（同文案消歧），`expected` 是其它策略的期望文案
    （用来识破"坐标策略点到了遮挡物"）。
    """

    def ok(hits: list[UINode]) -> list[UINode]:
        out = _visible(hits, screen)
        if avoid_occluded:
            out = [n for n in out if occluded_by(root, n) is None]
        return _prefer_near(out, ref, screen)

    v = s.value
    if s.type == "a11y_id":
        hits = root.find_by_id(str(v))
        if not hits and "/" in str(v):
            # Android resource-id 常带 package 前缀，允许后缀匹配
            suffix = str(v).split("/")[-1]
            hits = [n for n in root.walk() if n.id.split("/")[-1] == suffix]
        hits = _narrow(ok(hits), s.node_cls)
        return hits[0] if hits else None

    if s.type == "text":
        hits = ok(root.find_by_text(str(v)))
        if not hits:
            hits = ok(root.find_by_text(str(v), contains=True))
        hits = _narrow(hits, s.node_cls)
        return hits[0] if hits else None

    if s.type == "desc":
        hits = _narrow(ok([n for n in root.walk() if n.desc == str(v)]), s.node_cls)
        return hits[0] if hits else None

    if s.type == "xpath":
        hits = ok(_xpath_match(root, str(v)))
        return hits[0] if hits else None

    if s.type == "bounds":
        if not isinstance(v, list) or len(v) != 4 or not screen[0]:
            return None
        w, h = screen
        x1, y1, x2, y2 = (int(v[0] * w / 1000), int(v[1] * h / 1000),
                          int(v[2] * w / 1000), int(v[3] * h / 1000))
        if (x2 - x1) <= 0 or (y2 - y1) <= 0:
            return None
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        hit = root.find_smallest_at(cx, cy)
        if hit is root:
            hit = None
        if hit is not None:
            # 坐标落点被固定栏盖住时，find_smallest_at 交回来的**就是遮挡物本身**。
            # 真机事故：目标「请选择」的旧坐标落在底部栏里，这里返回了「附件」节点，
            # 于是"点请假类别"名正言顺地跳进附件页，后面全在错误的页面上打转。
            # 判据：其它策略明明说目标有文案，落点却没有（或文案完全不同）。
            if avoid_occluded and expected:
                lab = (hit.text or hit.desc or "").strip().lower()
                if not lab or not any(e in lab or lab in e for e in expected):
                    return None
            hx1, hy1, hx2, hy2 = hit.bounds
            # 命中的节点比目标框大得多（canvas / 整块面板 / body）→ 不能点它的中心，
            # 那会把"点登录按钮"变成"点页面正中"。此时坐标才是真实意图。
            if (hx2 - hx1) > 2.5 * max(1, x2 - x1) or (hy2 - hy1) > 2.5 * max(1, y2 - y1):
                return UINode(cls="Point", bounds=(x1, y1, x2, y2), clickable=True)
            # Electron 自绘输入框在 UIA 树里只是一块不可点击的 Group 容器，
            # find_smallest_at 把它交回来；照着它 tap/input 会点容器中心、
            # 输入也送不进真正的输入框（run-20261117-113350 s06~s10 反复绕路）。
            # 只针对容器类且面积明显大于落点框的情况合成 Point，
            # 避免误伤正常的静态文本/列表项节点。
            container_cls = {"group", "panel", "window", "container", "div", "section"}
            box_area = max(1, (x2 - x1) * (y2 - y1))
            hit_area = max(1, (hx2 - hx1) * (hy2 - hy1))
            # 覆盖两种实际形态：整体大得多（整块面板），或只是某一维明显大
            # （Electron 输入区所在的容器：宽度接近、高度远大于落点框）。
            big_container = (hit_area > 2 * box_area
                             or (hx2 - hx1) > 1.4 * max(1, x2 - x1)
                             or (hy2 - hy1) > 1.5 * max(1, y2 - y1))
            if not hit.clickable and (hit.cls or "").lower() in container_cls \
                    and not is_editable(hit.cls) and big_container:
                return UINode(cls="Point", bounds=(x1, y1, x2, y2), clickable=True)
            return hit
        if not root.children:
            # 无节点树（纯视觉端 / canvas）：按坐标直接合成一个目标，让动作仍可执行
            return UINode(cls="Point", bounds=(x1, y1, x2, y2), clickable=True)
        return None

    # visual：需要模板图，交给驱动的视觉兜底实现
    return None


def _match_hidden(root: UINode, s: LocatorStrategy, screen: tuple[int, int]) -> UINode | None:
    """同样的策略，但只找**不可见**的匹配——用来解释"为什么没命中"。

    只做诊断，不参与定位：找到它说明"目标在这一页、只是滚出视野了"，
    执行器据此触发滑动查找，并在报告里写清原因。
    """
    v = s.value
    if s.type in ("text", "desc"):
        field = "text" if s.type == "text" else "desc"
        hits = [n for n in root.walk() if getattr(n, field) == str(v)]
        if not hits and s.type == "text":
            hits = [n for n in root.walk() if str(v) in (n.text or "")]
    elif s.type == "a11y_id":
        hits = [n for n in root.walk()
                if n.id == str(v) or n.id.split("/")[-1] == str(v).split("/")[-1]]
    elif s.type == "xpath":
        hits = _xpath_match(root, str(v))
    else:
        return None
    for n in hits:
        if not _on_screen(n, screen):
            return n
    return None


def resolve(
    root: UINode,
    strategies: list[LocatorStrategy],
    screen: tuple[int, int] = (0, 0),
    tol: int = 12,
    visual_matcher: "Callable[[LocatorStrategy], tuple[UINode | None, float, float]] | None" = None,
    avoid_occluded: bool = False,
) -> LocateResult:
    """按权重从高到低尝试，命中即停，并回写所有策略的权重。

    `visual_matcher` 由执行器注入（它手上才有截图 + 模板目录），
    这样定位模块不必反向依赖驱动层，单测里也能直接塞一个假匹配器。
    """
    if not strategies:
        return LocateResult(reason="没有可用定位策略")

    ordered = sorted(strategies, key=lambda s: s.score, reverse=True)
    attempts: list[tuple[str, bool]] = []
    hidden = ""

    # 同一条用例的多条策略描述的是同一个目标：把坐标和文案合起来当"身份证据"，
    # 既能在同文案多行时消歧，也能识破坐标策略点到了遮挡物。
    ref: tuple[int, int] | None = None
    expected: set[str] = set()
    for s in strategies:
        v = s.value
        if s.type == "bounds" and isinstance(v, list) and len(v) == 4 and screen[0]:
            w, h = screen
            try:
                x1, y1, x2, y2 = (int(v[0] * w / 1000), int(v[1] * h / 1000),
                                  int(v[2] * w / 1000), int(v[3] * h / 1000))
                ref = ((x1 + x2) // 2, (y1 + y2) // 2)
            except (TypeError, ValueError):
                pass
        elif s.type in ("text", "desc") and isinstance(v, str) and len(v.strip()) >= 2:
            expected.add(v.strip().lower())

    for s in ordered:
        score, scale = 0.0, 1.0
        if s.type == "visual":
            if visual_matcher is None:
                s.bump(False)
                attempts.append(("visual:无匹配器", False))
                continue
            node, score, scale = visual_matcher(s)
        else:
            node = _match_one(root, s, screen, avoid_occluded, ref, expected)
            if node is None and tol:
                # 坐标类策略允许容差再试一次
                node = _match_one(root, s, screen, avoid_occluded, ref, expected)
        if node is not None:
            s.bump(True)
            attempts.append((f"{s.type}:{s.value if s.type != 'visual' else s.value.get('label', '')}", True))
            return LocateResult(node=node, strategy=s, attempts=attempts,
                                match_score=round(score, 4), scale=round(scale, 4))
        s.bump(False)
        if not hidden:
            ghost = _match_hidden(root, s, screen)
            if ghost is not None:
                hidden = (ghost.text or ghost.desc or ghost.id or s.type)[:24]
        attempts.append((f"{s.type}:{s.value if s.type != 'visual' else s.value.get('asset', '')}", False))

    reason = "所有定位策略均未命中"
    if hidden:
        reason = f"「{hidden}」在界面树里，但不在可视区域（bounds 为空或被滚出屏幕）"
    return LocateResult(attempts=attempts, reason=reason, offscreen=hidden)


# "edit" 必须整词匹配的坑：Windows UIA 的输入框 ControlTypeName 就是 "Edit"，
# 早先没有这个词导致桌面端输入框全部不被当成可编辑 → 输入前从不清空，
# 残留内容直接拼在关键词后面（run-20261113-154411：搜索词拼成一串查无结果）。
_EDITABLE_HINTS = ("edittext", "textinput", "textfield", "searchfield", "input",
                   "textarea", "edit")


def is_editable(cls: str) -> bool:
    """节点是否是可编辑输入框；与 text 分离，value 只表示运行时内容。"""
    c = (cls or "").lower()
    return any(h in c for h in _EDITABLE_HINTS)


def derive_strategies(node: UINode, root: UINode | None = None,
                      screen: tuple[int, int] = (0, 0)) -> list[LocatorStrategy]:
    """从一次观察到的节点，生成多策略定位器集合。这是"沉淀"的关键一步。

    输入框是个例外：它的 text/desc 是**当前值和占位提示**，不是稳定标签。
    示例视频应用 的搜索框占位语每次进来都不一样（"后会友期""你好，星期六"轮播），
    拿它当定位器等于埋一颗定时炸弹——下次回放第一句就失配。
    所以可编辑控件只保留 id / xpath / 坐标。
    """
    out: list[LocatorStrategy] = []
    editable = is_editable(node.cls)

    if node.id and is_stable_id(node.id):
        out.append(LocatorStrategy.make("a11y_id", node.id))

    if not editable and is_stable_text(node.text):
        out.append(LocatorStrategy.make("text", node.text, node.cls))

    if not editable and is_stable_text(node.desc):
        out.append(LocatorStrategy.make("desc", node.desc, node.cls))

    if root is not None:
        path = _build_xpath(root, node)
        if path:
            out.append(LocatorStrategy.make("xpath", path))

    if screen[0] and node.area > 0:
        w, h = screen
        x1, y1, x2, y2 = node.bounds
        out.append(
            LocatorStrategy.make(
                "bounds",
                [int(x1 * 1000 / w), int(y1 * 1000 / h),
                 int(x2 * 1000 / w), int(y2 * 1000 / h)],
            )
        )

    if not out:
        # 极端情况：无 id 无文本无坐标，只能靠位置
        out.append(LocatorStrategy.make("bounds", list(node.bounds)))
    return out


def _build_xpath(root: UINode, target: UINode, max_depth: int = 8) -> str:
    """为节点构造稳定的 XPath 路径，作为定位策略之一。"""
    chain: list[UINode] = []

    def dfs(node: UINode, trail: list[UINode]) -> bool:
        if node is target:
            chain.extend(trail + [node])
            return True
        for c in node.children:
            if dfs(c, trail + [node]):
                return True
        return False

    if not dfs(root, []):
        return ""
    chain = chain[-max_depth:]
    segs = []
    for n in chain[1:]:
        parent = next((p for p in chain if n in p.children), None)
        idx = parent.children.index(n) + 1 if parent else 1
        segs.append(f"{n.cls or '*'}[{idx}]")
    return "//" + "/".join(segs) if segs else ""


def heal_order(strategies: list[LocatorStrategy]) -> list[LocatorStrategy]:
    """给 AI 修复器用：返回需要被替换的弱策略。"""
    return [s for s in strategies if s.score < 0.5]


__all__ = [
    "LOCATOR_PRIORITY",
    "LocateResult",
    "derive_strategies",
    "heal_order",
    "is_stable_text",
    "resolve",
]
