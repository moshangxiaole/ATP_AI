"""真机探针（Android / HarmonyOS 通用）：抓截图 + 节点树，落盘到 build_tmp/<platform>/。

调试期专用：只读，不点不输，不改设备状态。
用法：
    python scripts/dev_probe.py android DEMO_ANDROID_SERIAL home
    python scripts/dev_probe.py harmony DEMO_HARMONY_SERIAL home
可选：--tap-text "搜索" / --tap-id "v_searchBar" / --tap-xy 100,200  先点一下再抓
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))

from atp.config import Config  # noqa: E402
from atp.agents.platforms.mobile.android import AndroidAdbDriver  # noqa: E402
from atp.agents.platforms.mobile.harmony import HarmonyHdcDriver  # noqa: E402


def make_driver(platform: str, device: str):
    cfg = Config.load()
    if platform == "android":
        return AndroidAdbDriver(device, cfg)
    if platform == "harmony":
        return HarmonyHdcDriver(device, cfg)
    raise SystemExit(f"不支持的平台 {platform}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("platform")
    ap.add_argument("device")
    ap.add_argument("tag")
    ap.add_argument("--tap-text", default="")
    ap.add_argument("--tap-id", default="")
    ap.add_argument("--tap-xy", default="")
    ap.add_argument("--limit", type=int, default=60)
    a = ap.parse_args()

    d = make_driver(a.platform, a.device)
    d.connect()
    print("画布:", d.screen_size(), "dpr:", d.device_pixel_ratio())

    if a.tap_xy:
        x, y = (int(v) for v in a.tap_xy.split(","))
        d.tap_xy(x, y)
        print(f"tap {x},{y}")
        time.sleep(2.0)
    elif a.tap_text or a.tap_id:
        root = d.dump_tree()
        target = None
        for n in root.walk():
            if a.tap_id and a.tap_id in (n.id or ""):
                target = n
                break
            if a.tap_text and a.tap_text in ((n.text or "") + (n.desc or "")):
                target = n
                break
        if target is None:
            print("没找到目标节点")
            return 1
        print("tap 目标:", target.cls, target.bounds, target.text or target.desc)
        d.tap_xy(*target.center)
        time.sleep(2.5)

    out = ROOT / "build_tmp" / ({"android": "ad", "harmony": "hm"}.get(a.platform, a.platform))
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    (out / f"{a.tag}.png").write_bytes(d.screenshot())
    t1 = time.perf_counter()
    try:
        root = d.dump_tree()
    except Exception as e:  # noqa: BLE001
        print("dump_tree 失败:", e)
        return 1
    t2 = time.perf_counter()
    print(f"截图 {(t1 - t0) * 1000:.0f}ms  抓树 {(t2 - t1) * 1000:.0f}ms")

    lines: list[str] = []

    def walk(n, depth: int = 0):
        label = (n.text or n.desc or "").replace("\n", " ")[:32]
        flag = "C" if n.clickable else " "
        lines.append(f"{'  ' * depth}{flag} {n.cls[:24]:26} {str(n.bounds):26} id={n.id[:34]:36} {label}")
        for c in n.children:
            walk(c, depth + 1)

    walk(root)
    (out / f"{a.tag}.tree.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"节点 {len(lines)} 个 -> build_tmp/{out.name}/{a.tag}.tree.txt")
    for ln in lines[: a.limit]:
        print(ln)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
