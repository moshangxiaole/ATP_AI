"""ATP 命令行入口。

常驻用法：
  python -m atp.cli doctor
  python -m atp.cli devices
  python -m atp.cli ai   cases/oa_approve.case.yaml --platform android
  python -m atp.cli run  cases/oa_approve.stepir.json
  python -m atp.cli compile cases/oa_approve.stepir.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from atp.common.profiles import profile_for
from atp.config import Config, tool_path
from atp.scriptgen.compiler import compile_case, to_yaml
from atp.agents.executor import Executor
from atp.agents.perception.judge import Judge
from atp.common.stepir import CaseIR, NLCase
from atp.agents.devices.manager import DeviceManager


# ---------------------------------------------------------------- doctor
def cmd_doctor(cfg: Config) -> int:
    """环境自检：检查依赖、模型连通性与设备工具是否可用。"""
    from atp.agents.llm.client import LLMClient

    rows: list[tuple[str, str, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        rows.append((name, "OK" if ok else "MISSING", detail))

    for tool in ("adb", "hdc"):
        path = tool_path(tool) or (cfg.adb if tool == "adb" else cfg.hdc)
        add(tool, bool(path), path or "未找到")

    for mod, hint in (
        ("playwright", "Web / Electron(CDP) 需要"),
        ("uiautomation", "Windows 原生 UIA 需要"),
        ("pyautogui", "桌面视觉兜底需要"),
        ("PIL", "截图渲染需要"),
    ):
        ok = shutil.which("python") is not None
        try:
            __import__(mod)
        except Exception:
            ok = False
        add(mod, ok, hint if not ok else "")

    client = LLMClient(cfg.model)
    add("model", client.available,
        f"{cfg.model.model} @ {cfg.model.base_url}" if client.available else
        "未配置（设置 ATP_MODEL_BASE_URL / API_KEY / NAME）")

    from atp.db.store import CaseDB

    db = CaseDB(cfg.db_file)
    info = db.info()
    db.close()
    add("database", bool(info.get("ok")),
        f"{info.get('path')} · {info.get('counts', {}).get('cases', 0)} 用例")
    try:
        __import__("numpy")
        add("numpy", True, "视觉模板匹配（可选，缺失时视觉兜底不可用）")
    except Exception:
        add("numpy", False, "pip install numpy 后可启用截图+图像识别兜底")

    print("环境自检")
    print("-" * 62)
    for name, status, detail in rows:
        flag = "✓" if status == "OK" else "✗"
        print(f" {flag} {name:<14} {status:<9} {detail}")
    print("-" * 62)
    print(f" 工程目录: {cfg.home}")
    print(f" 产物目录: {cfg.artifacts}")
    return 0 if all(s == "OK" for _, s, _ in rows[:1] + rows[2:3]) else 0


# ---------------------------------------------------------------- db
def cmd_db(cfg: Config, as_json: bool = False, import_files: bool = False) -> int:
    """用例库管理：列出、导入、删除本地用例。"""
    from atp.db.store import CaseDB

    db = CaseDB(cfg.db_file)
    if import_files:
        # 显式 --import 是「按文件重建索引」：文件才是唯一真相，
        # 手工改了 stepir.json 就该覆盖库里的旧快照，不能因为是"已存在的用例"跳过。
        n = db.import_cases(cfg.cases, only_missing=False)
        print(f"已从 cases/ 同步 {n} 条用例（文件为准，覆盖库中旧快照）")
    info = db.info()
    cases = db.list_cases(limit=20)
    db.close()

    if as_json:
        print(json.dumps({**info, "recent": cases}, ensure_ascii=False, indent=2))
        return 0

    print("用例库（SQLite）")
    print("-" * 74)
    print(f" 文件   {info['path']}")
    print(f" 大小   {info['size_kb']} KB · 结构版本 v{info['schema_version']} · "
          f"{'可用' if info['ok'] else '不可用 ' + str(info.get('error', ''))}")
    print("-" * 74)
    print(f"{'表名':<18}{'行数':>7}  说明")
    for t in info["tables"]:
        print(f"{t['name']:<18}{t['rows']:>7}  {t['label']} · {t['desc']}")
    print("-" * 74)
    h = info["hit"]
    print(f" 检索 {h['total']} 次 · 命中 {h['hits']} 次（{h['hit_rate'] * 100:.1f}%）")
    if cases:
        print("\n最近更新的用例：")
        for c in cases[:8]:
            print(f"  {c['case_id']:<34} {c['step_count']:>2} 步 "
                  f"图 {c['image_steps']} · {c['platform']:<7} {c['title'][:28]}")
    return 0


# ---------------------------------------------------------------- devices
def cmd_devices(cfg: Config, as_json: bool = False) -> int:
    """列出本地与远程设备。"""
    mgr = DeviceManager(cfg)
    devices = mgr.discover()
    if as_json:
        print(json.dumps([d.as_dict() for d in devices], ensure_ascii=False, indent=2))
        return 0

    print(f"发现 {len(devices)} 个终端")
    print("-" * 78)
    print(f"{'平台':<10}{'类型':<9}{'标识':<34}{'名称'}")
    print("-" * 78)
    for d in devices:
        print(f"{d.platform:<10}{d.kind:<9}{d.device_id:<34}{d.name}")
    remote = [d for d in devices if d.kind == "remote"]
    if remote:
        print(f"\n其中远程设备 {len(remote)} 台，可用 --prefer-remote 指定优先调度")
    if not any(d.platform in ("android", "harmony") and d.kind != "virtual" for d in devices):
        print("\n提示：没有探测到真机。插上设备后执行 `adb devices` / `hdc list targets` 再试。")
    return 0


# ---------------------------------------------------------------- ai 首跑
def cmd_ai(cfg: Config, args) -> int:
    """AI 首跑：把自然语言用例规划成 StepIR 并执行。"""
    from atp.agents.runner import AiRunner
    from atp.agents.llm.client import LLMClient

    src = Path(args.case)
    if not src.exists():
        print(f"用例文件不存在: {src}", file=sys.stderr)
        return 2

    nl = NLCase.load(src)
    if args.platform:
        nl.platform = args.platform
    if args.app:
        nl.app_id = args.app
    if args.url:
        nl.url = args.url
    if args.endpoint:
        nl.endpoint = args.endpoint

    mgr = DeviceManager(cfg)
    client = LLMClient(cfg.model)
    out_dir = cfg.cases

    with mgr.lease(nl.platform, args.device or "", args.prefer_remote,
                   endpoint=nl.endpoint, url="") as driver:
        runner = AiRunner(driver, cfg, client=client)
        case, result = runner.run(nl, out_dir=out_dir)

    base = out_dir / nl.case_id
    from atp.memory.sediment import sediment_blocker
    from atp.db.store import CaseDB, record_run

    print(f"\n首跑完成：{'通过' if result.passed else '未通过'}")
    _print_summary(result)
    print(f"报告   : {result.report_path}")
    blocked = sediment_blocker(result)
    if blocked:
        # 失败的首跑不沉淀：录下来的步骤本身就没走通，入库只会让下次回放继续失败
        print(f"未沉淀：{blocked}")
        print(f"产物   : {result.artifacts_dir}")
        record_run(cfg, result, screen=case.screen, artifacts_dir=result.artifacts_dir,
                   report_path=result.report_path, case=case, ensure_case=False)
        return 1

    stepir = case.save(str(base) + ".stepir.json")
    script = compile_case(stepir, str(base) + ".py")

    db = CaseDB(cfg.db_file)
    ok = db.upsert_case(case, stepir_path=stepir.name, script_path=script.name,
                        source=case.created_by)
    db.close()
    record_run(cfg, result, screen=case.screen, artifacts_dir=result.artifacts_dir,
               report_path=result.report_path, case=case)

    print(f"StepIR : {stepir}")
    print(f"脚本   : {script}")
    print(f"用例库 : {cfg.db_file} {'已入库' if ok else '入库失败（仅文件）'}")
    return 0


# ---------------------------------------------------------------- run
def cmd_run(cfg: Config, args) -> int:
    """执行用例：按模式选择 AI 首跑或零 LLM 脚本回放。"""
    src = Path(args.case)
    if not src.exists():
        print(f"用例不存在: {src}", file=sys.stderr)
        return 2

    if src.suffix in (".yaml", ".yml"):
        return cmd_ai(cfg, args)

    case = CaseIR.load(src)
    healer = None
    judge_llm = None
    from atp.agents.llm.client import LLMClient

    client = LLMClient(cfg.model)
    if client.available:
        judge_llm = client          # 语义断言需要模型；纯 DSL 断言不会产生调用
    if args.heal:
        if client.available:
            from atp.agents.runner import LlmHealer

            healer = LlmHealer(client)
        else:
            print("警告：未配置模型，--heal 不生效", file=sys.stderr)

    mgr = DeviceManager(cfg)
    # 回放也要能接回原来的浏览器/Electron 实例，否则 web/桌面用例不可复现
    with mgr.lease(case.platform, args.device or "", args.prefer_remote,
                   endpoint=case.variables.get("endpoint", ""),
                   url=case.variables.get("url", "")) as driver:
        result = Executor(driver, cfg, Judge(judge_llm), healer).run(
            case, mode="replay", persist_path=src
        )

    _print_summary(result)
    print(f"\n报告: {result.report_path}")
    return 0 if result.passed else 1


# ---------------------------------------------------------------- apps 应用台账
def cmd_apps(cfg: Config, args) -> int:
    """应用台账（apps.json）维护：查看 / 登记 / 忘掉 / 去设备上认一次。

    移动端「打开 xx」必须有包名。台账命中就永远用同一个包名，
    回放和首跑才走同一条路；没命中才去设备上找，找到自动写回来。
    """
    from atp.memory.apps import AppRegistry, guess_app_names, resolve_app

    reg = AppRegistry.from_config(cfg)
    plat = args.platform or ""

    if args.add:
        name, _, app_id = args.add.partition("=")
        if not app_id.strip():
            print("格式：--add 应用名=包名，例如 --add 示例聊天应用=com.example.chatapp.desktop", file=sys.stderr)
            return 2
        entry = reg.remember(plat or "android", name.strip(), app_id.strip(),
                             activity=args.activity or "", source="manual")
        print(f"已登记 {entry.name} → {entry.app_id}"
              + (f"（{entry.activity}）" if entry.activity else ""))
        return 0

    if args.forget:
        target = plat or "android"
        entry = reg.get(target, args.forget)
        if entry is None:
            print(f"台账里没有「{args.forget}」", file=sys.stderr)
            return 1
        reg.forget(target, args.forget)
        print(f"已删除 {entry.name} → {entry.app_id}")
        return 0

    if args.sync:
        # 用例里本来就有 app_id 和那句自然语言，能把「名字→包名」白捡一遍：
        # 老工程迁移过来时不用再一台台设备去翻桌面。
        from atp.memory.apps import guess_app_names

        n = 0
        for path in sorted(cfg.cases.glob("*.stepir.json")):
            try:
                case = CaseIR.load(str(path))
            except Exception:      # noqa: BLE001 —— 坏文件跳过，不影响其它用例
                continue
            app_id = (case.app_id or "").strip()
            if "." not in app_id or not profile_for(case.platform).discover_apps:
                continue
            names = guess_app_names(case.nl or case.title)
            reg.remember(case.platform, names[0] if names else case.title[:12],
                         app_id, source="case")
            n += 1
        print(f"已从用例库补齐 {n} 条应用登记 → {cfg.apps_file}")
        return 0

    if args.resolve and not args.find:
        # 只查台账：不连设备，纯文件查询——排查"为什么没用台账"时最常用
        targets = [plat] if plat else ["android", "harmony"]
        for p in targets:
            entry = reg.get(p, args.resolve)
            if entry is not None:
                print(f"{entry.name} → {entry.app_id}"
                      + (f"（{entry.activity}）" if entry.activity else "")
                      + f"  [{entry.source}] · {p}")
                return 0
        print(f"台账里没有「{args.resolve}」。想让它去设备上找一次："
              f"python -m atp.cli apps --find {args.resolve} --device <id>", file=sys.stderr)
        return 1

    if args.find:
        name = args.find.strip()
        if not name:
            print("--find 需要一个应用名", file=sys.stderr)
            return 2
        mgr = DeviceManager(cfg)
        with mgr.lease(plat or "android", args.device or "", False) as driver:
            entry = resolve_app(name, registry=reg, driver=driver,
                                platform=driver.platform, discover=True)
            print(f"（设备 {driver.device_id} · 画布 {driver.screen_size()[0]}x"
                  f"{driver.screen_size()[1]}）")
        if entry is None:
            print(f"没找到「{name}」：台账里没有，设备上也没认出来。"
                  f"\n可以手工登记：python -m atp.cli apps --add {name}=<包名>", file=sys.stderr)
            return 1
        print(f"{entry.name} → {entry.app_id}"
              + (f"（{entry.activity}）" if entry.activity else "")
              + f"  [{entry.source}]")
        return 0

    if args.text:
        names = guess_app_names(args.text)
        print("猜到的应用名：" + ("、".join(names) if names else "（无）"))
        return 0 if names else 1

    # 默认：把台账打出来
    platforms = [plat] if plat else ["android", "harmony"]
    total = 0
    for p in platforms:
        entries = reg.entries(p)
        total += len(entries)
        print(f"{p}（{len(entries)} 个）")
        if not entries:
            print("  — 还没登记。首跑「打开 xx」时会在设备上找一次并自动写入")
            continue
        for e in entries:
            alias = f" · 别名 {'/'.join(e.aliases)}" if e.aliases else ""
            act = f" · {e.activity}" if e.activity else ""
            print(f"  {e.name:<12} {e.app_id}{act}{alias}  [{e.source}]")
    if args.as_json:
        data = {p: {e.app_id: e.as_dict() for e in reg.entries(p)} for p in platforms}
        print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\n台账文件：{cfg.apps_file}（共 {total} 条，可直接手工编辑）")
    return 0


# ---------------------------------------------------------------- compile
def cmd_compile(cfg: Config, args) -> int:
    """把 StepIR 编译成可读、可改、可直接跑的 Python 回归脚本。"""
    src = Path(args.case)
    out = compile_case(src, args.out)
    print(f"已生成脚本: {out}")
    if args.yaml:
        case = CaseIR.load(src)
        ypath = Path(out).with_suffix(".replay.yaml")
        ypath.write_text(to_yaml(case), encoding="utf-8")
        print(f"已生成人读清单: {ypath}")
    return 0


# ---------------------------------------------------------------- 输出
def _print_summary(result) -> None:
    """在终端打印一次执行的结果摘要。"""
    s = result.summary()
    print("-" * 62)
    for st in result.steps:
        mark = "✓" if st.status == "passed" else "✗"
        print(f" {mark} {st.step_id:<5}{st.action:<12}{st.intent[:28]:<30}{st.latency_ms:>5}ms")
        if st.error:
            print(f"      ↳ {st.error[:70]}")
    print("-" * 62)
    print(
        f" 结论 {'通过' if s['passed'] else '失败'} | 步骤 {s['total_steps'] - s['failed_steps']}/"
        f"{s['total_steps']} | AI 参与 {s['ai_steps']} | 耗时 {s['duration_ms'] / 1000:.1f}s"
    )


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器（各子命令的参数集中在这里定义）。"""
    p = argparse.ArgumentParser("atp", description="ATP · Agent 自动化测试平台")
    p.add_argument("--home", default="", help="工程目录，默认当前目录")
    p.add_argument("--config", default="", help="配置文件路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="环境自检")
    d = sub.add_parser("devices", help="列出本地与远程设备")
    d.add_argument("--json", action="store_true")

    q = sub.add_parser("db", help="查看用例库（SQLite）表结构与行数")
    q.add_argument("--json", action="store_true")
    q.add_argument("--import", dest="import_files", action="store_true",
                   help="把 cases/*.stepir.json 回填进库")

    def common(sp):
        sp.add_argument("--device", default="", help="指定设备 id")
        sp.add_argument("--platform", default="", help="android|harmony|desktop|web")
        sp.add_argument("--app", default="", help="包名 / bundleName / 小程序 appId")
        sp.add_argument("--url", default="", help="web/桌面端起始地址")
        sp.add_argument("--endpoint", default="", help="CDP 端点，如 http://127.0.0.1:9333")
        sp.add_argument("--prefer-remote", action="store_true", help="优先调度远程真机")

    a = sub.add_parser("ai", help="用自然语言首跑并沉淀")
    a.add_argument("case")
    common(a)

    r = sub.add_parser("run", help="执行用例（StepIR 回放 / NL 首跑）")
    r.add_argument("case")
    common(r)
    r.add_argument("--heal", action="store_true", help="允许 AI 修复失效定位器")

    ap = sub.add_parser("apps", help="应用台账：应用名 ↔ 包名（apps.json）")
    ap.add_argument("--platform", default="", help="android | harmony，默认两个都列")
    ap.add_argument("--device", default="", help="去设备上找时指定设备 id")
    ap.add_argument("--resolve", default="", help="按台账解析一个应用名（不碰设备）")
    ap.add_argument("--find", default="", help="台账没有就去设备上找，找到写回台账")
    ap.add_argument("--add", default="", help="手工登记：应用名=包名")
    ap.add_argument("--activity", default="", help="配合 --add：入口组件 pkg/.Act")
    ap.add_argument("--forget", default="", help="删除一条登记")
    ap.add_argument("--sync", action="store_true", help="从案例库里补齐名字↔包名")
    ap.add_argument("--text", default="", help="从一句话里猜应用名（调试用）")
    ap.add_argument("--json", dest="as_json", action="store_true")

    c = sub.add_parser("compile", help="StepIR → 可执行脚本")
    c.add_argument("case")
    c.add_argument("--out", default="")
    c.add_argument("--yaml", action="store_true")

    w = sub.add_parser("web", help="启动 Web 控制台（对话式执行）")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    w.add_argument("--no-reuse", action="store_true",
                   help="忽略已在运行的实例，强制再起一个（端口会自动顺延）")
    w.add_argument("--stop", action="store_true", help="停止后台控制台")
    w.add_argument("--status", action="store_true", help="查看控制台是否在运行")
    return p


def cmd_web(cfg: Config, args) -> int:
    """启动 Web 控制台服务，可选端口与是否自动打开浏览器。"""
    from atp.webapp import server

    if args.stop:
        return server.stop(cfg, args.host, args.port)
    if args.status:
        url = server.find_running(cfg, args.host, args.port)
        if url:
            print(f"运行中  {url}")
            return 0
        print("未运行")
        return 1
    return server.serve(cfg, args.host, args.port,
                        open_browser=not args.no_open, reuse=not args.no_reuse)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数后分派到对应子命令。"""
    args = build_parser().parse_args(argv)
    cfg = Config.load(args.home or None, args.config or None)
    handlers = {
        "doctor": lambda: cmd_doctor(cfg),
        "devices": lambda: cmd_devices(cfg, getattr(args, "json", False)),
        "db": lambda: cmd_db(cfg, getattr(args, "json", False),
                             getattr(args, "import_files", False)),
        "ai": lambda: cmd_ai(cfg, args),
        "run": lambda: cmd_run(cfg, args),
        "apps": lambda: cmd_apps(cfg, args),
        "compile": lambda: cmd_compile(cfg, args),
        "web": lambda: cmd_web(cfg, args),
    }
    return handlers[args.cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
