"""真机验证移动端两条默认行为：冷启动 + 滑动查找。

不是"再跑一遍用例"，而是把两个能力各自造一个反例场景，看执行器能不能自己救回来：

A. 冷启动：先把示例视频应用 弄到"搜索页"（非首页），再执行一条**只有 launch 一步**的用例。
   没有冷启动的话，启动只是把搜索页拉回前台，用例第一步就找不到首页入口。

B. 滑动查找：目标「关于手机」在设置首页的最底部，首屏根本没有。用例里只写
   "点【关于手机】"，不写任何滑动步骤，看执行器会不会自己滑下去把它找出来。

C. 回归：完整回放已入库的 `videoapp_search_play_android`（10 步），确认这两条默认行为
   没有把原来零 AI 的回放弄坏。

用法：
    python scripts/verify_mobile_defaults.py            # 自动挑第一个安卓设备
    python scripts/verify_mobile_defaults.py DEMO_ANDROID_SERIAL
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.config import Config  # noqa: E402
from atp.agents.executor import Executor  # noqa: E402
from atp.agents.perception.judge import Judge  # noqa: E402
from atp.agents.perception.locator import LocatorStrategy  # noqa: E402
from atp.common.stepir import CaseIR, Step  # noqa: E402
from atp.common.types import Action, UINode  # noqa: E402
from atp.agents.platforms.mobile.android import AndroidAdbDriver  # noqa: E402

VIDEO_APP = "com.example.videoapp"
SETTINGS = "com.android.settings"


def find(root: UINode, pred) -> UINode | None:
    return next((n for n in root.walk() if pred(n)), None)


def texts(root: UINode) -> list[str]:
    return [t for t in ((n.text or n.desc).strip() for n in root.walk()) if t]


def run(drv, cfg: Config, case_id: str, steps: list[Step], app: str = ""):
    """跑一条临时用例（不落库，只留报告）。"""
    case = CaseIR(case_id=case_id, title=case_id, platform="android", app_id=app)
    case.steps = list(steps)
    return Executor(drv, cfg, Judge(), None).run(case, mode="replay")


def launch_step(sid: str = "s01", app: str = "") -> Step:
    return Step(step_id=sid, intent=f"打开应用 {app}", action=Action.LAUNCH_APP,
                args={"app": app})


def check_cold_start(drv, cfg) -> bool:
    print("\n=== A. 冷启动：先把 App 停在搜索页，再执行「只有 launch 一步」的用例 ===")
    drv.terminate_app(VIDEO_APP)
    time.sleep(0.8)
    drv.launch_app(VIDEO_APP)
    time.sleep(4.0)
    # 手动点到搜索页：这时 App 处于"非首页"状态，正好用来验证冷启动
    obs = drv.observe()
    entry = find(obs.root, lambda n: n.id.endswith("clSearchContainer"))
    if entry is None:
        print(" ! 首页搜索入口没出现，跳过 A（可能被弹窗挡住）")
        return False
    drv.tap_xy(*entry.center)
    time.sleep(2.0)
    pid_before = _pid(drv, VIDEO_APP)
    on_search = find(drv.dump_tree(), lambda n: n.cls == "EditText") is not None
    print(f" 准备状态：搜索页输入框在不在 = {on_search}，pid = {pid_before}")

    r = run(drv, cfg, "probe-cold-start", [launch_step(app=VIDEO_APP)], app=VIDEO_APP)
    pid_after = _pid(drv, VIDEO_APP)
    tree = drv.dump_tree()
    home = (find(tree, lambda n: n.id.endswith("clSearchContainer")) is not None
            and find(tree, lambda n: n.cls == "EditText") is None)
    step = r.steps[0]
    print(f" 步骤结果：{step.status} · {step.detail}")
    print(f" 结束状态：落在首页 = {home}，pid {pid_before} → {pid_after}")
    ok = r.passed and home and pid_after and pid_before != pid_after
    print(" ✓ 冷启动生效：进程被换掉，界面回到首页" if ok else " ✗ 冷启动没生效")
    return bool(ok)


def check_scroll_find(drv, cfg) -> bool:
    print("\n=== B. 滑动查找：首屏看不到「关于手机」，用例只写一步点击 ===")
    steps = [
        launch_step("s01", app=SETTINGS),
        Step(step_id="s02", intent="点击【关于手机】", action=Action.TAP,
             strategies=[LocatorStrategy.make("text", "关于手机")]),
    ]
    t0 = time.time()
    r = run(drv, cfg, "probe-scroll-find", steps, app=SETTINGS)
    cost = time.time() - t0
    s2 = r.steps[1]
    print(f" s02：{s2.status} · {s2.detail} · {s2.latency_ms}ms")
    err = s2.error
    if err:
        print(f" 失败原因：{err}")
    opened = any(t in ("关于手机", "版本号") for t in texts(drv.dump_tree()))
    ok = r.passed and opened and "滑动" in s2.detail
    print(f" 进到「关于手机」页 = {opened}，整例耗时 {cost:.1f}s")
    print(" ✓ 滑动查找生效：零 AI 且自己滑到了首屏之外的目标" if ok else " ✗ 滑动查找没生效")
    return bool(ok)


def check_replay(drv, cfg) -> bool:
    print("\n=== C. 回归：完整回放已入库的 10 步安卓用例 ===")
    path = ROOT / "cases" / "videoapp_search_play_android.stepir.json"
    case = CaseIR.load(path)
    r = Executor(drv, cfg, Judge(), None).run(case, mode="replay")
    print(f" 结果 {'passed' if r.passed else 'failed'} · {len(r.steps)} 步 · "
          f"AI {r.ai_steps} · {r.duration_ms / 1000:.1f}s")
    for s in r.steps:
        if not s.ok or "冷启动" in s.detail:
            print(f"  {s.step_id} {s.status} {s.intent} · {s.detail[:60]}{s.error[:60]}")
    print(f" 报告：{r.report_path}")
    return r.passed and r.ai_steps == 0


def _pid(drv, pkg: str) -> str:
    try:
        out = drv._shell("pidof", pkg.split("/")[0])  # noqa: SLF001
        return (out or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    cfg = Config.load()
    cfg.persist_runs = False        # 探针用例不进用例库
    device = sys.argv[1] if len(sys.argv) > 1 else ""
    if not device:
        from atp.agents.devices.manager import DeviceManager
        found = [d for d in DeviceManager(cfg).discover() if d.platform == "android"]
        device = found[0].device_id if found else ""
    if not device:
        print("没发现安卓设备，先 adb devices 确认")
        return 2

    drv = AndroidAdbDriver(device, cfg)
    drv.connect()
    print(f"设备 {device} · 画布 {drv.screen_size()}")
    results: dict[str, bool] = {}
    try:
        for name, fn in (("A 冷启动", check_cold_start),
                         ("B 滑动查找", check_scroll_find),
                         ("C 回归", check_replay)):
            try:
                results[name] = fn(drv, cfg)
            except Exception as e:  # noqa: BLE001
                print(f" ! {name} 异常：{type(e).__name__}: {e}")
                results[name] = False
    finally:
        # 收尾：把设备还回桌面，别把测试机停在设置页
        try:
            drv.terminate_app(SETTINGS)
            drv.terminate_app(VIDEO_APP)
            drv.press_key("home")
        except Exception:  # noqa: BLE001
            pass
        drv.close()        # Android 会把输入法还给用户

    print("\n===== 汇总 =====")
    for k, v in results.items():
        print(f" {'✓' if v else '✗'} {k}")
    return 0 if len(results) == 3 and all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
