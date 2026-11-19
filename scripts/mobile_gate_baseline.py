"""真机页面稳定门禁基线（只读采样，不改设备状态）。

为什么单独有这个脚本：设计文档 FR-0 要求「可复跑的基线采集脚本」，但完整
A/B 回放要反复启动被测 App、点击、按返回，代价大且会动到真机状态。这里只做
**只读**采样——连上设备后对**当前前台页面**反复过门禁，采集：

* 每拍耗时分位值（P50 / P95 / mean / max）；
* 每次门禁的抓树次数、截图次数（用来验证「树-only 轮询 + 唯一一次截图」）；
* 稳定率、降级率、超时率。

它回答的是「门禁本身快不快、稳不稳」；「整例回放省了多少秒」仍要等完整
A/B 基线（脚本尾部会明确写进产物，避免把两件事混为一谈）。

用法：
    python scripts/mobile_gate_baseline.py                     # 自动挑第一个安卓设备
    python scripts/mobile_gate_baseline.py DEMO_ANDROID_SERIAL
    python scripts/mobile_gate_baseline.py DEMO_ANDROID_SERIAL --samples 15
"""

from __future__ import annotations

import argparse
import re
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.config import Config  # noqa: E402
from atp.agents.perception.page_stabilizer import PageStabilizer  # noqa: E402


def pct(values: list[float], q: float) -> float:
    """线性插值分位数；样本为空时返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return float(ordered[lo] * (1 - frac) + ordered[hi] * frac)


def summarize(values: list[float]) -> dict:
    return {
        "p50": round(pct(values, 0.50), 1),
        "p95": round(pct(values, 0.95), 1),
        "mean": round(statistics.fmean(values), 1) if values else 0.0,
        "min": round(min(values), 1) if values else 0.0,
        "max": round(max(values), 1) if values else 0.0,
    }


def run_probe(device: str, samples: int, legacy_samples: int = 3) -> dict:
    from atp.agents.platforms.mobile.android import AndroidAdbDriver

    class CountingDriver(AndroidAdbDriver):
        """给真机驱动加两个计数器：门禁到底抓了几次树、截了几次图。"""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.dumps = 0
            self.shots = 0

        def dump_tree(self):
            self.dumps += 1
            return super().dump_tree()

        def screenshot(self):
            self.shots += 1
            return super().screenshot()

    cfg = Config.load()
    drv = CountingDriver(device, cfg)
    drv.connect()
    stabilizer = PageStabilizer(drv, cfg)

    waits: list[float] = []
    polls: list[int] = []
    dumps: list[int] = []
    shots: list[int] = []
    reasons: dict[str, int] = {}
    stable = degraded = 0

    # 预热一拍：第一拍常含首次 dumpLayout 的初始化开销，单列出来，不进分位值
    warm = stabilizer.wait_stable("warmup")
    print(f"预热：stable={warm.stable} wait={warm.wait_ms}ms polls={warm.tree_polls}")

    for i in range(samples):
        d0, s0 = drv.dumps, drv.shots
        t0 = time.perf_counter()
        stabilizer.stable_observe("probe")
        cost = (time.perf_counter() - t0) * 1000

        waits.append(float(cost))
        polls.append(int(getattr(stabilizer.last_result, "tree_polls", 0) or 0))
        dumps.append(drv.dumps - d0)
        shots.append(drv.shots - s0)
        st = stabilizer.last_result
        reason = str(getattr(st, "reason", "") or "")
        reasons[reason] = reasons.get(reason, 0) + 1
        if getattr(st, "stable", False):
            stable += 1
        if getattr(st, "degraded", False):
            degraded += 1
        print(f"  #{i + 1:02d} stable={getattr(st, 'stable', False)} "
              f"reason={reason or '-'} wait={cost:.0f}ms polls={polls[-1]} "
              f"dumps={dumps[-1]} shots={shots[-1]}")

    snapshot = {"width": 0, "height": 0, "foreground": ""}
    try:
        size = drv.screen_size()
        snapshot["width"], snapshot["height"] = int(size[0]), int(size[1])
    except Exception:  # noqa: BLE001
        pass
    for key in ("foreground_package", "current_package"):
        value = getattr(drv, key, "")
        if isinstance(value, str) and value:
            snapshot["foreground"] = value
            break
    if not snapshot["foreground"]:
        # 驱动没有这个能力位时，退回 dumpsys 读一次：产物要能说清「采样时停在哪」。
        try:
            out = str(drv._shell("dumpsys", "activity", "activities"))  # noqa: SLF001
            m = re.search(r"(?:mResumedActivity|ResumedActivity|topResumedActivity)[^\n]*?([A-Za-z][\w.]*)/", out)
            if m:
                snapshot["foreground"] = m.group(1)
        except Exception:  # noqa: BLE001
            pass

    # 参照组：老路径直接 observe()（一次抓树 + 一次截图），不含任何稳定判定。
    # 「门禁相对老路径多花了多少」是判定提速是否成立的唯一硬指标。
    legacy: list[float] = []
    for _ in range(legacy_samples):
        t0 = time.perf_counter()
        try:
            drv.observe()
        except Exception:  # noqa: BLE001
            break
        legacy.append((time.perf_counter() - t0) * 1000)

    try:
        drv.close()
    except Exception:  # noqa: BLE001
        pass

    gate = summarize(waits)
    base = summarize(legacy)
    overhead = 0.0
    if gate["p50"] and base["p50"]:
        overhead = round((gate["p50"] - base["p50"]) / base["p50"] * 100, 1)

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "device": device,
        "platform": "android",
        "canvas": [snapshot["width"], snapshot["height"]],
        "foreground_package": snapshot["foreground"],
        "samples": samples,
        "stable_ratio": round(stable / samples, 3) if samples else 0.0,
        "degraded_ratio": round(degraded / samples, 3) if samples else 0.0,
        "wait_ms": gate,
        "tree_polls": summarize([float(v) for v in polls]),
        "dumps_per_gate": summarize([float(v) for v in dumps]),
        "screenshots_per_gate": summarize([float(v) for v in shots]),
        "legacy_observe_ms": base,
        "gate_overhead_pct": overhead,
        "reasons": reasons,
        "scope": {
            "covered": "页面稳定门禁自身耗时 / 抓树次数 / 截图次数 / 稳定率 / 相对老 observe() 的开销",
            "not_covered": "整例回放 A/B（含冷启动、定位、点击、断言）；"
                           "需同设备同 App 版本各跑 ≥10 次后另出 baseline",
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="真机页面稳定门禁基线（只读）")
    ap.add_argument("device", nargs="?", default="", help="设备序列号，留空自动挑第一台安卓设备")
    ap.add_argument("--samples", type=int, default=10, help="采样次数（默认 10）")
    ap.add_argument("--out", default=str(ROOT / "docs" / "mobile-exec-baseline.json"))
    args = ap.parse_args()

    device = args.device
    if not device:
        cfg = Config.load()
        from atp.agents.devices.manager import DeviceManager

        found = [d for d in DeviceManager(cfg).discover() if d.platform == "android"]
        device = found[0].device_id if found else ""
    if not device:
        print("没发现安卓设备，先 adb devices 确认")
        return 2

    data = run_probe(device, max(1, args.samples))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== 汇总 =====")
    print(f" 稳定率 {data['stable_ratio']:.0%} · 降级率 {data['degraded_ratio']:.0%}")
    print(f" 单拍耗时 P50 {data['wait_ms']['p50']}ms · P95 {data['wait_ms']['p95']}ms · "
          f"max {data['wait_ms']['max']}ms")
    print(f" 每拍抓树 {data['dumps_per_gate']['p50']:.1f} 次 · "
          f"截图 {data['screenshots_per_gate']['p50']:.1f} 次")
    print(f" 参照老路径 observe() P50 {data['legacy_observe_ms']['p50']}ms · "
          f"门禁开销 {data['gate_overhead_pct']:+.1f}%")
    print(f" 产物：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
