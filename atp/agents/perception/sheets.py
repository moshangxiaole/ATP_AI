"""选择类弹层（底部选择面板）的识别与自动确认。

典型形态就是这次踩到的那个：点【请假类别】→ 底部升起一列选项（事假/病假/年假…），
最下面并排两个按钮「取消 | 确定」。**点选项只是高亮，不点确定就不生效。**

AI 在这里特别容易打转：选项的长相在节点树里几乎不变（H5 只是改个颜色），
于是"点年假"看起来毫无效果，重复检测判定它在原地打转，最后整条用例判失败。
人工操作是两拍：选值 → 确定。这里就是把第二拍补成确定动作。

判定条件全部是结构性的、可解释的，宁可漏也不能误点——误点「确定」等于替用户提交表单。
"""

from __future__ import annotations

from dataclasses import dataclass

from atp.common.types import Observation, UINode

# 只认"确认选择"这类按钮。刻意不含「提交 / 发送 / 保存并提交」：
# 表单页底部的「取消 | 提交」不是弹层，点了就是真提交，绝不能自动触发。
CONFIRM_WORDS = {"确定", "确认", "完成", "确定选择", "确认选择", "应用", "ok", "done", "confirm"}
CANCEL_WORDS = {"取消", "关闭", "cancel", "放弃", "返回"}


def _label(node: UINode) -> str:
    return (node.text or node.desc or "").strip().lower()


@dataclass
class ConfirmBar:
    """弹层底部那一条「取消 | 确定」。"""

    confirm: UINode
    cancel: UINode

    @property
    def top(self) -> int:
        return min(self.confirm.bounds[1], self.cancel.bounds[1])

    @property
    def left(self) -> int:
        return min(self.confirm.bounds[0], self.cancel.bounds[0])

    @property
    def right(self) -> int:
        return max(self.confirm.bounds[2], self.cancel.bounds[2])

    @property
    def label(self) -> str:
        return (self.confirm.text or self.confirm.desc or "确定").strip()

    def as_dict(self) -> dict:
        return {"label": self.label, "top": self.top,
                "confirm": list(self.confirm.bounds), "cancel": list(self.cancel.bounds)}


def find_confirm_bar(obs: Observation) -> ConfirmBar | None:
    """在节点树里找并排的「取消 | 确定」。

    五个条件同时成立才算：都在可视区、都在屏幕下半部、同一水平带、
    确定在取消右边、两个按钮宽度量级相当。少一条就返回 None——
    漏判的代价是多花一步模型调用，误判的代价是替用户提交了表单。
    """
    h = obs.height or 0
    if not h:
        return None
    bottom_half = h * 0.45

    confirms = [n for n in obs.root.walk()
                if n.visible and _label(n) in CONFIRM_WORDS]
    cancels = [n for n in obs.root.walk()
               if n.visible and _label(n) in CANCEL_WORDS]
    if not confirms or not cancels:
        return None

    best: tuple[float, ConfirmBar] | None = None
    for c in confirms:
        for k in cancels:
            if c.center[1] < bottom_half or k.center[1] < bottom_half:
                continue
            if abs(c.center[1] - k.center[1]) > max(48, h * 0.05):
                continue
            if c.center[0] <= k.center[0]:
                continue
            ratio = c.area / max(1, k.area)
            if not (0.4 <= ratio <= 2.5):
                continue
            # 间距不能太远：两个按钮应当挨在一起（中间最多隔半个屏宽）
            if c.bounds[0] - k.bounds[2] > obs.width * 0.5:
                continue
            gap = abs(c.center[1] - k.center[1])
            if best is None or gap < best[0]:
                best = (gap, ConfirmBar(confirm=c, cancel=k))
    return best[1] if best else None


def _parents(root: UINode) -> dict[int, UINode]:
    """子节点 → 父节点。UINode 是可变 dataclass（不可哈希），只能用 id 做键。"""
    parent: dict[int, UINode] = {}
    stack = [root]
    while stack:
        n = stack.pop()
        for c in n.children:
            parent[id(c)] = n
            stack.append(c)
    return parent


def ancestors_of(root: UINode, node: UINode) -> list[UINode]:
    """从近到远的祖先链；node 不在树里就返回空。"""
    parent = _parents(root)
    chain: list[UINode] = []
    cur = parent.get(id(node))
    while cur is not None and len(chain) < 64:
        chain.append(cur)
        cur = parent.get(id(cur))
    return chain


def shares_scroll_with(root: UINode, node: UINode, bar: ConfirmBar) -> bool:
    """选项和确认栏是不是**同一个滚动容器**里的两块内容。

    这是"表单页"和"弹层"最关键的区别：
    - 弹层：选项在自己的可滚列表里，按钮在列表**外面**；
    - 表单页：字段和底部「取消 | 确定」都在同一个 WebView 里，滚一个另一个跟着动。

    真机上的请假表单正是后者——底部固定栏里同时有「编辑附言 / 附件 / 提交」，
    如果把它当成弹层，点一下字段就会替用户把表单提交掉。宁可漏判。
    """
    cx, cy = bar.confirm.center
    for a in ancestors_of(root, node):
        if a.scrollable and a.contains(cx, cy):
            return True
    return False


def same_bar(a: ConfirmBar | None, b: ConfirmBar | None, obs: Observation,
             tol_ratio: float = 0.12) -> bool:
    """两条「取消 | 确定」是不是同一个（允许动画位移）。

    早先这里要求 bounds 完全相等，结果弹层还在展开动画里时（顶边从 428 升到 370），
    确认栏一移动就判成"不是同一个"，自动确认整条被跳过——真机上就是这么打转的。
    """
    if a is None or b is None:
        return False
    if a.label.strip() != b.label.strip():
        return False
    h = obs.height or 1
    w = obs.width or 1
    ax, ay = a.confirm.center
    bx, by = b.confirm.center
    if abs(ay - by) > h * tol_ratio or abs(ax - bx) > w * tol_ratio:
        return False
    aw = a.confirm.bounds[2] - a.confirm.bounds[0]
    bw = b.confirm.bounds[2] - b.confirm.bounds[0]
    return abs(aw - bw) <= max(w * 0.15, 20)


def refind_option(obs: Observation, node: UINode, bar: ConfirmBar) -> UINode | None:
    """在**点击之后**的树里重新找到刚才那个选项。

    点之前拿到的坐标不能直接用：弹层展开动画会把整列选项上移一百多像素。
    这里按文案找，再按距离挑最近的，最后用 is_option 复核。
    """
    label = _label(node)
    if not label:
        return None
    old = node.center
    cands = [n for n in obs.root.walk()
             if n.visible and _label(n) == label and n is not bar.confirm
             and is_option(n, bar, obs)]
    if not cands:
        return None
    best = min(cands, key=lambda n: abs(n.center[0] - old[0]) + abs(n.center[1] - old[1]))
    # 位移超过 1/3 屏就算换了对象，不能拿它当"刚点的那个选项"
    limit = max(obs.height, 1) * 0.33
    if abs(best.center[1] - old[1]) > limit:
        return None
    return best


def is_option(node: UINode | None, bar: ConfirmBar, obs: Observation) -> bool:
    """刚点的这个节点像不像"弹层里的一个选项"。

    选项的形态：单行短文案、高度不超过屏幕 12%、位于确认栏上方、
    水平方向落在确认栏的跨度内。
    """
    if node is None or not node.visible:
        return False
    label = _label(node)
    if not label or label in CONFIRM_WORDS or label in CANCEL_WORDS:
        return False
    h = obs.height or 1
    x1, y1, x2, y2 = node.bounds
    if (y2 - y1) > h * 0.12:
        return False
    if node.center[1] >= bar.top - 4:
        return False
    if not (bar.left - 8 <= node.center[0] <= bar.right + 8):
        return False
    # 必须在弹层区域里：确认栏以上最多一屏高的范围
    if node.center[1] < bar.top - h * 0.75:
        return False
    return True


__all__ = ["CANCEL_WORDS", "CONFIRM_WORDS", "ConfirmBar", "ancestors_of",
           "find_confirm_bar", "is_option", "refind_option", "same_bar",
           "shares_scroll_with"]
