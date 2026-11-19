"""StepIR → 可执行脚本。

生成的 .py 是给人看、给人改的：步骤顺序、选择器、意图注释一目了然。
它内部复用 ``atp.scriptgen.dsl.Script``，因此脚本与用例库共享同一份执行逻辑。
"""

from __future__ import annotations

import time
from pathlib import Path

from atp.common.stepir import CaseIR, Step
from atp.common.types import Action


def _best(step: Step):
    if not step.strategies:
        return None
    return max(step.strategies, key=lambda x: x.score)


def _visual_call(kind: str, step: Step, best) -> str:
    """视觉定位渲染成一行可读代码：asset + 归一化坐标 + 基准画布，一目了然。"""
    v = best.value if isinstance(best.value, dict) else {}
    base = tuple(v.get("base") or [])
    common = (f"asset={v.get('asset', '')!r}, rect={v.get('rect', [])!r}, "
              f"base={base!r}, label={v.get('label', '')!r}")
    if kind == "tap":
        return f"S.tap_image({common})"
    if kind == "input":
        return f"S.input({step.args.get('text', '')!r}, by='visual', value={v!r})"
    return f"S.tap_image({common})"


def _post_kw(post) -> str:
    """断言参数：证据优先，回放靠它跳过模型。"""
    kw = f"method={post.method!r}"
    if getattr(post, "witness", ""):
        kw += f", witness={post.witness!r}"
    ratio = float(getattr(post, "min_ratio", 0.02) or 0.02)
    if post.kind == "assert_motion" and abs(ratio - 0.02) > 1e-6:
        kw += f", min_ratio={ratio!r}"
    return kw


def _render_step(idx: int, step: Step) -> str:
    best = _best(step)
    by = best.type if best else ""
    value = str(best.value) if best else ""
    cls_kw = f", node_cls={best.node_cls!r}" if best and best.node_cls else ""
    comment = f"# {step.step_id} {step.intent}"
    a = step.action
    post = step.postcondition

    if a is Action.LAUNCH_APP:
        app = step.args.get("app", "")
        body = f'S.launch({app!r})'
    elif a is Action.TERMINATE_APP:
        app = step.args.get("app", "")
        body = f'S.terminate({app!r})' if app else "S.terminate()"
    elif a is Action.MAXIMIZE:
        body = "S.maximize()"
    elif a is Action.OPEN_URL:
        body = f"S.open_url({step.args.get('url', '')!r})"
    elif a is Action.TAP:
        if by == "visual":
            body = _visual_call("tap", step, best)
        elif by == "bounds":
            body = f'S.tap(by="bounds", value={value})'
        else:
            body = f"S.tap(by={by!r}, value={value!r}{cls_kw})"
    elif a is Action.INPUT_TEXT:
        text = step.args.get("text", "")
        if by == "visual":
            body = _visual_call("input", step, best)
        elif by:
            body = f"S.input({text!r}, by={by!r}, value={value!r}{cls_kw})"
        else:
            body = f"S.input({text!r})"
    elif a is Action.CLEAR_TEXT:
        body = f"S.clear(by={by!r}, value={value!r}{cls_kw})" if by else "S.clear()"
    elif a is Action.SWIPE:
        body = f"S.swipe({step.args.get('direction', 'up')!r})"
    elif a is Action.BACK:
        body = "S.back()"
    elif a is Action.HOME:
        body = "S.home()"
    elif a is Action.PRESS_KEY:
        body = f"S.key({step.args.get('key', 'back')!r})"
    elif a is Action.SCREENSHOT:
        body = "S.shot()"
    elif a is Action.WAIT_FOR and post.kind != "none":
        body = f"S.check({post.kind!r}, {post.claim!r}, {_post_kw(post)})"
    else:
        body = f"S.raw({a.value!r}, **{step.args!r})"

    lines = [comment, body]
    # 动作带断言时补充一行显式 check，脚本更可读也可单独注释掉
    if post.kind != "none" and a not in (Action.WAIT_FOR,):
        lines.append(f"S.check({post.kind!r}, {post.claim!r}, {_post_kw(post)})")
    return "\n".join(lines)


def to_python(case: CaseIR, source_file: str = "") -> str:
    body = "\n\n".join(_render_step(i, s) for i, s in enumerate(case.steps, 1))
    header = f'''# -*- coding: utf-8 -*-
"""由 ATP 从 StepIR 生成的回归脚本。

用例：{case.title}
用例 ID：{case.case_id}
平台：{case.platform}
基准画布：{case.screen.width}x{case.screen.height}（dpr {case.screen.dpr:g}，{case.screen.label or "-"}）
来源：{source_file or case.case_id + ".stepir.json"}
生成时间：{time.strftime("%Y-%m-%d %H:%M:%S")}

分辨率无关：所有坐标（bounds / 视觉模板矩形）都按 0~1 归一化存储，
跑在别的分辨率终端上时按「当前画布 / 基准画布」实时换算，不需要重新录制。

移动端的两条默认行为（由执行器保证，脚本里不必重复写）：
1. launch 之前先结束应用进程，保证每次都是冷启动、都从首页起步；
2. 某一步定位不到目标时，先上下滑动查找（目标可能只是被滚出了视野），
   滑动仍找不到才允许 AI 修复定位器。

不要手工改定位器后忘了同步 StepIR——两边不一致时以 StepIR 为准。
需要重新生成：python -m atp.cli compile <stepir.json> --out <this_file>
"""

import sys
from pathlib import Path

for _p in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
    if (_p / "atp" / "__init__.py").exists():
        sys.path.insert(0, str(_p))
        break

from atp.scriptgen.dsl import Script

S = Script(
    platform={case.platform!r},
    app={case.app_id!r},
    title={case.title!r},
    case_id={case.case_id!r},
)
'''
    footer = '''

if __name__ == "__main__":
    result = S.run()
    print(result.summary())
'''
    return header + "\n" + body + footer


def compile_case(stepir_path: str | Path, out_path: str | Path | None = None) -> Path:
    src = Path(stepir_path)
    case = CaseIR.load(src)
    if out_path:
        out = Path(out_path)
    else:
        base = src.name[: -len(".stepir.json")] if src.name.endswith(".stepir.json") else src.stem
        out = src.with_name(base + ".py")
    out.write_text(to_python(case, src.name), encoding="utf-8")
    return out


def to_yaml(case: CaseIR) -> str:
    """人读版步骤清单（不作为执行入口，避免出现第二份真相）。"""
    lines = [
        f"# {case.title}",
        f"# platform: {case.platform}  app: {case.app_id or '-'}",
        f"# app_version: {case.app_version or '-'}  ui_fingerprint: {case.ui_fingerprint or '-'}",
        "steps:",
    ]
    for s in case.steps:
        best = _best(s)
        lines.append(f"  - id: {s.step_id}")
        lines.append(f"    intent: {s.intent}")
        lines.append(f"    action: {s.action.value}")
        if best:
            cls = f" [{best.node_cls}]" if best.node_cls else ""
            lines.append(f"    locator: {best.type}:{best.value}{cls}")
        if s.args:
            lines.append(f"    args: {s.args}")
        if s.postcondition.kind != "none":
            lines.append(f"    assert: {s.postcondition.kind} {s.postcondition.claim}")
    return "\n".join(lines) + "\n"
