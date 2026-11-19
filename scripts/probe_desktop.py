"""探测桌面应用能否被 UIA 接管：输出窗口与控件树摘要。

用法：python scripts/probe_desktop.py "示例聊天应用-测试版"
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

import uiautomation as auto


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    auto.SetGlobalSearchTimeout(3)

    print("=== 顶层窗口 ===")
    wins = []
    root = auto.GetRootControl()
    for w in root.GetChildren():
        try:
            n = w.Name or ""
            c = w.ClassName or ""
        except Exception:
            continue
        if n or c:
            wins.append((n, c, w))
    for n, c, _ in wins:
        if name and name not in n:
            continue
        print(f"  name={n!r} class={c!r}")

    target = None
    for n, c, w in wins:
        if name and name in n:
            target = w
            break
    if target is None:
        print(f"\n未找到包含 {name!r} 的窗口")
        return 1

    print(f"\n=== 控件树（{target.Name!r}）===")
    count = [0]

    def walk(ctrl, depth):
        if depth > 6 or count[0] > 200:
            return
        try:
            r = ctrl.BoundingRectangle
            b = f"({int(r.left)},{int(r.top)},{int(r.right)},{int(r.bottom)})"
        except Exception:
            b = "?"
        try:
            nm = (ctrl.Name or "")[:40]
            ct = ctrl.ControlTypeName
            aid = ctrl.AutomationId or ""
        except Exception:
            nm, ct, aid = "", "?", ""
        if nm or aid:
            count[0] += 1
            print(f"  {'  ' * depth}{ct} name={nm!r} autoId={aid!r} {b}")
        try:
            for ch in ctrl.GetChildren():
                walk(ch, depth + 1)
        except Exception:
            pass

    walk(target, 0)
    print(f"\n有名字/ID 的控件数：{count[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
