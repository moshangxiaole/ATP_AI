"""单文件 HTML 报告。

自带截图（base64 内嵌），可以直接发群里、挂 CI 归档，不需要起服务。
每一步都是可审计的：意图、使用的定位策略、判定方式、耗时、前后截图。
"""

from __future__ import annotations

import base64
import html as H
import time
from pathlib import Path

from atp.common.stepir import RunResult, StepResult
from atp.runs.artifacts import RECORDING_FILE, REPORT_FILE

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6ec;--text:#1b1f27;--muted:#6b7280;
--ok:#0f7b45;--okbg:#e8f6ee;--bad:#b42318;--badbg:#fdecea;--warn:#8a5a00;--warnbg:#fff4e5;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:20px;font-weight:600;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{color:var(--muted);font-size:12px}
.card .v{font-size:22px;font-weight:600;margin-top:2px}
.pass .v{color:var(--ok)}.fail .v{color:var(--bad)}
.step{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px 16px;margin-bottom:12px}
.step.ok{border-left:3px solid var(--ok)}
.step.no{border-left:3px solid var(--bad)}
.hd{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.id{font-family:ui-monospace,Consolas,monospace;color:var(--muted);font-size:12px}
.intent{font-weight:500}
.badge{font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.b-ok{background:var(--okbg);color:var(--ok);border-color:#bfe3cd}
.b-bad{background:var(--badbg);color:var(--bad);border-color:#f5c2bd}
.b-ai{background:#eef2ff;color:#3743a8;border-color:#cfd6f7}
.b-warn{background:var(--warnbg);color:var(--warn);border-color:#f0d9a8}
.meta{color:var(--muted);font-size:12px;margin-top:6px;font-family:ui-monospace,Consolas,monospace;
word-break:break-all}
.err{color:var(--bad);font-size:13px;margin-top:6px}
.errbox{background:var(--badbg);border-radius:6px;padding:8px 10px;margin-top:8px}
.shots{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
.shots figure{margin:0;flex:0 0 auto}
.shots img{max-height:320px;border:1px solid var(--line);border-radius:8px;display:block}
.shots figcaption{color:var(--muted);font-size:11px;margin-top:4px}
.rec video{max-width:440px;width:100%;border:1px solid var(--line);border-radius:8px;
margin-top:10px;display:block;background:#000}
.foot{color:var(--muted);font-size:12px;margin-top:28px;text-align:center}
"""


def _img(art: Path, name: str) -> str:
    """把截图内嵌成 data URL：报告要能单文件转发，不能引用外部图片。"""
    if not name:
        return ""
    p = art / name
    if not p.exists():
        return ""
    mime = "image/png"
    raw = p.read_bytes()
    if raw[:2] == b"\xff\xd8":
        mime = "image/jpeg"
    b64 = base64.b64encode(raw).decode()
    return f'<img src="data:{mime};base64,{b64}" alt="{H.escape(name)}">'


def _badge(cls: str, text: str) -> str:
    """生成一个状态徽标（通过 / 失败 / AI 参与等）。"""
    return f'<span class="badge {cls}">{H.escape(text)}</span>'


def _step_html(sr: StepResult, art: Path, idx: int) -> str:
    """渲染单步卡片：状态、意图、判定方式、定位策略、耗时与前后截图。"""
    ok = sr.status == "passed"
    cls = "ok" if ok else "no"
    badges = [
        _badge("b-ok" if ok else "b-bad", {"passed": "通过", "failed": "断言失败",
                                           "error": "执行异常", "skipped": "跳过"}.get(sr.status, sr.status)),
        _badge("", sr.action),
    ]
    if sr.ai_used:
        badges.append(_badge("b-ai", "AI 参与"))
    if sr.judge_method:
        badges.append(_badge("", f"判定 {sr.judge_method} {sr.judge_score:.2f}"))

    shots = ""
    for name, cap in (
        (f"{idx:02d}_{sr.step_id}_before.png", "执行前"),
        (f"{idx:02d}_{sr.step_id}_after.png", "执行后"),
    ):
        tag = _img(art, name)
        if tag:
            shots += f"<figure>{tag}<figcaption>{cap}</figcaption></figure>"

    return f"""
<div class="step {cls}">
  <div class="hd">
    <span class="id">{idx:02d} {H.escape(sr.step_id)}</span>
    <span class="intent">{H.escape(sr.intent)}</span>
    {''.join(badges)}
  </div>
  <div class="meta">
    {H.escape(sr.strategy_used or '无定位')}
    {('· 图像相似度 ' + f'{sr.match_score:.3f}') if sr.match_score else ''} · {sr.latency_ms} ms
    {('· <b>' + H.escape(sr.node_label) + '</b>') if sr.node_label else ''}
  </div>
  {f'<div class="errbox"><div class="err">{H.escape(sr.error)}</div></div>' if sr.error else ''}
  {f'<div class="meta">{H.escape(sr.detail)}</div>' if sr.detail and sr.status == "passed" else ''}
  {f'<div class="shots">{shots}</div>' if shots else ''}
</div>"""


def _canvas_label(result: RunResult) -> str:
    sc = result.screen
    if not sc.ok:
        return "未采集"
    return f"{sc.width}x{sc.height}"


def _screen_note(result: RunResult) -> str:
    """画布与缩放比：跨分辨率回放时，这一行是判读截图的第一依据。"""
    sc = result.screen
    if not sc.ok:
        return "画布尺寸未采集（旧用例或驱动未实现 screen_profile）"
    scaled = abs(sc.scale_x - 1) > 0.001 or abs(sc.scale_y - 1) > 0.001
    note = f"画布 {sc.width}x{sc.height} · dpr {sc.dpr:g} · {H.escape(sc.label or sc.platform)}"
    if scaled:
        note += f" · 相对沉淀基准缩放 {sc.scale_x:.2f}x×{sc.scale_y:.2f}x（坐标已按比例换算）"
    return note


def _video_html(art: Path) -> str:
    """执行录屏（MP4）。src 用相对路径：报告经控制台 /api/runs/<id>/report 服务时
    解析到 artifacts 路由；直接用文件管理器打开时解析到同目录的 recording.mp4。"""
    v = art / RECORDING_FILE
    if not v.exists():
        return ""
    mb = v.stat().st_size / 1e6
    return f"""
<div class="step rec">
  <div class="hd"><span class="intent">执行录屏</span>{_badge("", f"MP4 · {mb:.1f}MB")}</div>
  <video controls preload="metadata" src="artifacts/recording.mp4"></video>
</div>"""


def render_report(result: RunResult, art: Path) -> Path:
    """把一次执行渲染成单文件 HTML；截图内嵌、录屏走相对路径，可离线归档。"""
    total = len(result.steps)
    failed = sum(1 for s in result.steps if s.status != "passed")
    rate = int((total - failed) / total * 100) if total else 0
    kind = "通过" if result.passed else "失败"

    steps_html = [_step_html(sr, art, i) for i, sr in enumerate(result.steps, 1)]

    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(result.started_at))
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{H.escape(result.case_title)} · 执行报告</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
  <h1>{H.escape(result.case_title)}</h1>
  <div class="sub">{H.escape(result.case_id)} · {H.escape(result.platform)} ·
    {H.escape(result.device_id)} · 模式 {result.mode} · 开始于 {started}</div>
  <div class="sub">{_screen_note(result)}</div>

  <div class="cards">
    <div class="card {'pass' if result.passed else 'fail'}"><div class="k">结论</div><div class="v">{kind}</div></div>
    <div class="card"><div class="k">通过率</div><div class="v">{rate}%</div></div>
    <div class="card"><div class="k">步骤</div><div class="v">{total - failed}/{total}</div></div>
    <div class="card"><div class="k">AI 参与步数</div><div class="v">{result.ai_steps}</div></div>
    <div class="card"><div class="k">总耗时</div><div class="v">{result.duration_ms / 1000:.1f}s</div></div>
    <div class="card"><div class="k">画布</div><div class="v" style="font-size:16px">{_canvas_label(result)}</div></div>
  </div>

  {_video_html(art)}
  {''.join(steps_html)}

  <div class="foot">ATP · Agent Test Platform · run {H.escape(result.run_id)}</div>
</div></body></html>"""

    out = art / REPORT_FILE
    out.write_text(body, encoding="utf-8")
    return out
