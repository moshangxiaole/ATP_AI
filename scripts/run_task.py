"""通过 HTTP 跑一条用例并实时打印事件流。

用途：验收控制台链路（不只是 CLI）——起任务、看事件、看落库、看产物。
用法：
  python scripts/run_task.py --file payload.json        # 从 JSON 读参数
  python scripts/run_task.py --text "..." --platform web --url ...
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765"

# 本机回环必须绕开代理：这台机器的 HTTP_PROXY 会劫持 127.0.0.1，返回 502。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with _OPENER.open(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def get(path: str) -> dict:
    with _OPENER.open(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="")
    ap.add_argument("--text", default="")
    ap.add_argument("--platform", default="web")
    ap.add_argument("--url", default="")
    ap.add_argument("--mode", default="auto")
    ap.add_argument("--title", default="")
    ap.add_argument("--max-steps", type=int, default=14)
    args = ap.parse_args()

    if args.file:
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        payload = {"text": args.text, "platform": args.platform, "mode": args.mode,
                   "title": args.title, "max_steps": args.max_steps}
        if args.url:
            payload["url"] = args.url
    if not payload.get("text"):
        print("缺少用例描述", file=sys.stderr)
        return 2

    job = post("/api/run", payload)
    run_id = job["run_id"]
    print(f"run_id = {run_id}\n" + "-" * 70)

    since, done, t0 = 0, False, time.time()
    while not done:
        data = get(f"/api/runs/{run_id}/events?since={since}&wait=20")
        for ev in data.get("events", []):
            t = ev.get("type")
            if t == "run_started":
                print(f"▶ 开始 {ev.get('platform')} · {ev.get('mode')} · {ev.get('title')}")
            elif t == "library_hit":
                print(f"★ 用例库命中 {ev.get('case_id')} （{ev.get('score')}，{ev.get('steps')} 步）")
            elif t == "screen":
                print(f"▭ 画布 {ev.get('width')}x{ev.get('height')} dpr={ev.get('dpr')} "
                      f"缩放 {ev.get('scale_x')}x")
            elif t == "log":
                print(f"  · {ev.get('message')}")
            elif t == "step_started":
                print(f"[{ev.get('index'):02d}] ▶ {ev.get('action'):<12} {ev.get('intent')}")
            elif t == "step_finished":
                r = ev.get("result") or {}
                mark = "✓" if r.get("status") == "passed" else "✗"
                extra = f" | {r.get('strategy_used')}" if r.get("strategy_used") else ""
                if r.get("match_score"):
                    extra += f" 图像{r.get('match_score')}"
                print(f"     {mark} {r.get('status')} {r.get('latency_ms')}ms{extra}"
                      f"{(' | ' + r.get('error')) if r.get('error') else ''}")
            elif t == "sedimented":
                print(f"✎ 已沉淀 {ev.get('steps')} 步 → {ev.get('stepir')} / {ev.get('script')}")
            elif t == "db_saved":
                print(f"⛁ {ev.get('message')} · saved={ev.get('saved')}")
            elif t == "heal":
                print(f"⚒ 定位器已修复 {ev.get('strategies')}")
            elif t == "error":
                print(f"✗ 错误 {ev.get('message')}")
            elif t == "run_finished":
                done = True
                s = ev.get("summary") or {}
                print("-" * 70)
                print(f"结果 {ev.get('status')} · 步骤 {s.get('total_steps')} "
                      f"· AI {s.get('ai_steps')} · {round((s.get('duration_ms') or 0) / 1000, 1)}s "
                      f"· 画布 {ev.get('screen_label') or '-'}")
                if ev.get("report_url"):
                    print(f"报告 {BASE}{ev['report_url']}")
        since = data.get("next", since)
        if data.get("status") in ("passed", "failed", "error", "cancelled") and not done:
            print(f"（任务已结束：{data.get('status')}）")
            done = True
        if time.time() - t0 > 900:
            print("超时退出")
            break
    print(f"用时 {round(time.time() - t0, 1)}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
