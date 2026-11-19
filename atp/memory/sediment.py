"""用例沉淀的准入判定。

首跑（AI 接管）跑通了才值得沉成模板。跑不通的那种，录下来的步骤是"没走通的过程"：
定位器是猜的、页面是错的、断言本来就没满足。它一旦入库，下次检索命中它就必然再失败
一次——既浪费执行时间，又让"用例库到底有没有用"这个指标彻底失真。

所以准入只有一条硬规则：**每一步都通过**。产物（截图、报告、stepir 草稿）仍然留在
本次 run 的 artifacts 目录里，排障时照样能看。

判定放在这里而不是各调用点各写一份：控制台和 CLI 曾经各写各的，早晚会有一边忘了拦。
"""

from __future__ import annotations


def sediment_blocker(result) -> str:
    """不能沉淀时返回原因（给人看的一句话），可以沉淀时返回空串。"""
    steps = getattr(result, "steps", None) or []
    if not steps:
        return "没有任何执行步骤（启动/取界面就失败了），不沉淀"
    bad = [s for s in steps if not s.ok]
    if not bad:
        return ""
    first = bad[0]
    err = (first.error or first.status or "").strip()
    detail = f"{first.step_id} {first.intent}" + (f"（{err[:60]}）" if err else "")
    head = f"{len(bad)} 步失败，首个失败 {detail}"
    if len(bad) > 1:
        head += f" 等 {len(bad)} 处"
    return f"本次执行未通过（{head}），不写入用例库"


__all__ = ["sediment_blocker"]
