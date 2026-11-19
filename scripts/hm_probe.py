"""鸿蒙真机探针：抓一张截图 + 一份节点树，落盘到 build_tmp/hm/。

调试期专用：不依赖模型、不改设备状态，只读。
用法：
    python scripts/hm_probe.py home            # 抓当前界面
    python scripts/hm_probe.py search --tap 搜索 # 顺便按文案点一下再抓
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.config import Config  # noqa: E402
from atp.agents.platforms.mobile.harmony import HarmonyHdcDriver  # noqa: E402

OUT = ROOT / "build_tmp" / "hm"


def dump(driver: HarmonyHdcDriver, tag: str, limit: int = 60) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    obs_png = driver.screenshot()
    (OUT / f"{tag}.png").write_bytes(obs_png)
    try:
        root = driver.dump_tree()
    except Exception as e:  # noqa: BLE001
        print("dump_tree 失败:", e)
        return

    lines: list[str] = []

    def walk(n, depth: int = 0):
        label = (n.text or n.desc or "").replace("\n", " ")[:30]
        lines.append(f"{'  ' * depth}{n.cls[:26]:28} id={n.id[:26]:28} {label}")
        for c in n.children:
            walk(c, depth + 1)

    walk(root)
    (OUT / f"{tag}.tree.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"节点 {len(lines)} 个（全量见 build_tmp/hm/{tag}.tree.txt），截图 build_tmp/hm/{tag}.png")
    for ln in lines[:limit]:
        print(ln)


def main() -> int:
    cfg = Config.load()
    device = sys.argv[1] if len(sys.argv) > 1 else ""
    tag = sys.argv[2] if len(sys.argv) > 2 else "probe"
    driver = HarmonyHdcDriver(device or cfg.harmony_device, cfg)
    driver.connect()
    print("画布:", driver.screen_size())
    dump(driver, tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
