"""真机验证：应用台账（apps.json）——找应用 → 写台账 → 只查台账 + 严格冷启动。

三件事各验一遍：
  A 台账里没有「示例聊天应用」→ 设备上找（翻桌面）→ 写进 apps.json
  B 再执行一次 → 只读台账，**不允许**再碰设备（把 find_app 换成会炸的桩）
  C 冷启动是硬的：先 am force-stop 再 am start，进程号必须变

跑法：python scripts/verify_app_registry.py [--device DEMO_ANDROID_SERIAL]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.config import Config  # noqa: E402
from atp.agents.executor import Executor  # noqa: E402
from atp.agents.perception.judge import Judge  # noqa: E402
from atp.common.stepir import CaseIR, Step  # noqa: E402
from atp.common.types import Action  # noqa: E402
from atp.agents.platforms.mobile.android import AndroidAdbDriver  # noqa: E402

APP_NAME = "示例聊天应用"
PKG = "com.example.chatapp.desktop"
OTHER = "com.android.settings"


def pid_of(device: str, pkg: str) -> str:
    out = subprocess.run(["adb", "-s", device, "shell", "pidof", pkg],
                         capture_output=True, text=True).stdout.split()
    return out[0] if out else ""


def one_launch_case(app: str) -> CaseIR:
    case = CaseIR(case_id="verify-app", title=f"打开 {app}", platform="android",
                  app_id="", created_by="script")
    case.steps = [Step(step_id="s01", intent=f"打开应用 {app}",
                       action=Action.LAUNCH_APP, args={"app": app}, source="human")]
    return case


def run_case(driver, cfg, app: str, events: list[dict]):
    case = one_launch_case(app)
    ex = Executor(driver, cfg, Judge(), None,
                  on_event=lambda e: events.append(e))
    return ex.run(case, mode="replay")


def check_discover(cfg: Config, device: str) -> tuple[bool, str]:
    """A：台账里没有「示例聊天应用」，设备上找出来并写进 apps.json。"""
    reg_file = cfg.apps_file
    if reg_file.exists():
        backup = reg_file.with_suffix(".bak")
        shutil.copy(reg_file, backup)
    reg_file.write_text(json.dumps({"version": 1, "android": {}, "harmony": {}},
                                   ensure_ascii=False), encoding="utf-8")

    drv = AndroidAdbDriver(device, cfg)
    drv.connect()
    events: list[dict] = []
    t0 = time.time()
    try:
        case = one_launch_case(APP_NAME)
        ex = Executor(drv, cfg, Judge(), None, on_event=events.append)
        res = ex.run(case, mode="replay")
    finally:
        drv.close()
    cost = time.time() - t0

    data = json.loads(reg_file.read_text(encoding="utf-8"))
    entry = (data.get("android") or {}).get(PKG) or {}
    learned = [e for e in events if e.get("type") == "app_resolved"]
    ok = (res.passed and entry.get("name") == APP_NAME
          and any(e.get("source") == "device" for e in learned))
    detail = (f"{'✓' if ok else '✗'} A 发现并登记：{entry.get('name')} → {PKG}"
              f"（{entry.get('activity') or '-'}）· {cost:.1f}s"
              f" · 事件 {[e.get('source') for e in learned]}")
    return ok, detail


def check_reuse(cfg: Config, device: str) -> tuple[bool, str]:
    """B：台账命中时不许再碰设备（find_app 桩一调用就炸）。"""
    drv = AndroidAdbDriver(device, cfg)
    drv.connect()
    events: list[dict] = []

    def boom(name: str):
        raise AssertionError(f"台账已命中却还去设备上找 {name}: 说明查表逻辑没生效")

    drv.find_app = boom          # 实例属性覆盖，命中时不该被调用
    try:
        t0 = time.time()
        res = run_case(drv, cfg, APP_NAME, events)
        cost = time.time() - t0
    finally:
        drv.close()

    sources = [e.get("source") for e in events if e.get("type") == "app_resolved"]
    detail_step = res.steps[0].detail if res.steps else ""
    ok = res.passed and sources[:1] == ["config"] and "应用台账" in detail_step
    return ok, (f"{'✓' if ok else '✗'} B 只查台账：{cost:.1f}s · source={sources}"
                f" · detail={detail_step[:60]}")


def check_cold_start(cfg: Config, device: str) -> tuple[bool, str]:
    """C：先杀再启 —— 应用本来在跑，执行完进程号必须换掉。"""
    drv = AndroidAdbDriver(device, cfg)
    drv.connect()
    events: list[dict] = []
    try:
        drv.launch_app(PKG)
        time.sleep(5)
        pid1 = pid_of(device, PKG)
        res = run_case(drv, cfg, APP_NAME, events)
        time.sleep(3)
        pid2 = pid_of(device, PKG)
        fg = drv.foreground_package()
    finally:
        drv.close()

    cold = [e for e in events if e.get("type") == "cold_start"]
    ok = (res.passed and bool(pid1) and bool(pid2) and pid1 != pid2
          and fg == PKG and bool(cold))
    detail = (f"{'✓' if ok else '✗'} C 严格冷启动：pid {pid1 or '—'} → {pid2 or '—'}"
              f" · 前台 {fg or '—'} · force-stop 事件 {len(cold)} 条")
    return ok, detail


def check_ai_entry(cfg: Config, device: str) -> tuple[bool, str]:
    """D：首跑入口 —— 控制台没给 app_id，从「打开【示例聊天应用】」这句里认出包名。"""
    from atp.agents.runner import AiRunner
    from atp.common.stepir import CaseIR, NLCase

    # 先把台账里这条删掉，逼它真的走"设备上找"
    reg_file = cfg.apps_file
    data = json.loads(reg_file.read_text(encoding="utf-8")) if reg_file.exists() else {}
    (data.get("android") or {}).pop(PKG, None)
    reg_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    drv = AndroidAdbDriver(device, cfg)
    drv.connect()
    events: list[dict] = []
    nl = NLCase(case_id="verify-ai", title="打开示例聊天应用",
                platform="android", app_id="",
                nl="打开【示例聊天应用】，消息首页点击右上角的【+】，在下拉框中点击【钱包】，"
                   "页面进入【我的钱包】则执行成功")
    case = CaseIR(case_id="verify-ai", title=nl.title, platform="android")
    try:
        AiRunner(drv, cfg, on_event=events.append)._resolve_app_name(nl, case)
    finally:
        drv.close()

    gone = (json.loads(reg_file.read_text(encoding="utf-8")).get("android") or {}).get(PKG)
    ok = nl.app_id == PKG and case.app_id == PKG and gone is not None
    return ok, (f"{'✓' if ok else '✗'} D 首跑认包名：nl.app_id={nl.app_id or '—'}"
                f" · 已写回台账 {bool(gone)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="DEMO_ANDROID_SERIAL")
    args = ap.parse_args()

    cfg = Config.load(ROOT)
    # 验证用临时台账，别把工程里的 apps.json 弄乱
    tmp = Path(tempfile.mkdtemp(prefix="atp-apps-")) / "apps.json"
    cfg.apps_path = str(tmp)
    print(f"设备 {args.device} · 临时台账 {tmp}")

    results: list[tuple[bool, str]] = []
    for fn in (check_discover, check_reuse, check_cold_start, check_ai_entry):
        try:
            results.append(fn(cfg, args.device))
        except Exception as e:      # noqa: BLE001
            results.append((False, f"✗ {fn.__name__} 异常：{type(e).__name__}: {e}"))

    print("\n===== 汇总 =====")
    for ok, line in results:
        print(" " + line)
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
