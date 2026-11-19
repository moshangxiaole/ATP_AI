"""QA 测试辅助模块（**只含测试代码，不含断言，不含生产逻辑**）。

作用：
* 统一把项目根与 ``tests/fixtures`` 加进 ``sys.path``（``tests/`` 与
  ``tests/fixtures/`` 都没有 ``__init__.py``）；
* 提供可精确编排的假设备 ``TreeDriver``（可记录 dump/screenshot/tap 的**调用序列**，
  这是 AC-1.1 / AC-1.2 唯一可靠的证据来源）；
* 提供弹窗页构造函数（可换分辨率）、记忆沉淀 helper、``PopupGuard`` 计数替身、
  计数版 ``LLMClient`` / ``Healer``。

刻意不继承 ``unittest.TestCase``：本模块本身不应被 pytest 收集成用例。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = Path(__file__).resolve().parent
for _p in (str(ROOT), str(_FIXTURES)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from atp.agents.llm.client import LLMClient  # noqa: E402
from atp.agents.perception.page_stabilizer import (  # noqa: E402
    PageFrame, PageStack, PageStabilizer, StableResult, page_signature,
)
from atp.memory.popups import DANGER_WORDS, PopupGuard, PopupHit  # noqa: E402
from atp.memory.popup_memory import (  # noqa: E402
    PopupContext, PopupEntry, PopupMemory, build_signature,
)
from atp.common.types import Observation, UINode  # noqa: E402
from atp.agents.platforms.base import BaseDriver  # noqa: E402
from mobile_scenarios import (  # noqa: E402
    make_case, make_config, make_driver, make_executor,
)

APP = "com.example.atp"
OTHER_APP = "com.example.other"
W, H = 1080, 2400

__all__ = [
    "APP", "OTHER_APP", "W", "H", "ROOT",
    "make_case", "make_config", "make_driver", "make_executor",
    "TreeDriver", "obs_of", "page", "popup_page", "thin_page",
    "popup_hit", "sediment", "popup_context", "memory_for",
    "SpyGuard", "CountingLLM", "CountingHealer", "FrameGuard",
    "stable_result", "built_stack", "events_of",
]


# ---------------------------------------------------------------------------
# 假设备
# ---------------------------------------------------------------------------

class TreeDriver(BaseDriver):
    """可编排的假移动设备：树可整体替换，记录感知 / 输入的**顺序**与次数。"""

    platform = "android"
    # 用类属性覆盖 BaseDriver 的 property：测试里可以直接改这两个标记
    tree_stale = False
    not_idle = False

    def __init__(self, tree: UINode | None = None, w: int = W, h: int = H,
                 device_id: str = "mock-qa"):
        super().__init__(device_id)
        self.tree = tree if tree is not None else page()
        self.w, self.h = int(w), int(h)
        self.dumps = 0
        self.shots = 0
        self.taps: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int, int]] = []
        self.keys: list[str] = []
        self.events: list[dict] = []
        self.foreground_package = APP
        self.cancelled = False
        #: 有序调用序列：("dump",) / ("shot",) / ("tap", x, y) / ("key", name)
        self.log: list[tuple] = []
        self.after_tap = None
        self.on_key = None

    # ---------- 感知 ----------
    def connect(self) -> None:
        self._connected = True

    def screen_size(self) -> tuple[int, int]:
        return self.w, self.h

    def screenshot(self) -> bytes:
        self.shots += 1
        self.log.append(("shot",))
        return b"\x89PNG\r\n\x1a\n" + bytes([self.shots % 256])

    def dump_tree(self) -> UINode:
        self.dumps += 1
        self.log.append(("dump",))
        return self.tree

    # ---------- 输入 ----------
    def tap_xy(self, x: int, y: int) -> None:
        self.taps.append((x, y))
        self.log.append(("tap", x, y))
        hook, self.after_tap = self.after_tap, None
        if hook is not None:
            hook(self)

    def swipe_xy(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.swipes.append((x1, y1, x2, y2, ms))

    def press_key(self, name: str) -> None:
        self.keys.append(name)
        self.log.append(("key", name))
        hook, self.on_key = self.on_key, None
        if hook is not None:
            hook(self)

    def launch_app(self, app_id: str) -> None:
        self.app_id = app_id
        self.foreground_package = app_id.split("/")[0]

    def terminate_app(self, app_id: str) -> None:
        return None

    # ---------- 辅助 ----------
    @property
    def current_package(self) -> str:
        return self.foreground_package

    def should_cancel(self) -> bool:
        return bool(self.cancelled)

    def event_sink(self):
        return self.events.append

    def back_count(self) -> int:
        return sum(1 for k in self.keys if k == "back")

    @property
    def backs(self) -> list[str]:
        """与 fixture 的 ``ScenarioDriver.backs`` 对齐，用例里可统一写 ``drv.backs``。"""
        return self.keys


def obs_of(driver: TreeDriver) -> Observation:
    """按设备当前状态造一份观察对象。"""
    return Observation(platform=driver.platform, device_id=driver.device_id,
                       width=driver.w, height=driver.h, root=driver.tree)


# ---------------------------------------------------------------------------
# 页面构造
# ---------------------------------------------------------------------------

def page(count: int = 30, target_text: str = "目标按钮",
         w: int = W, h: int = H) -> UINode:
    """一屏健康的页面：count 个列表行 + 一个目标按钮（指纹恒定）。"""
    rows = [
        UINode(cls="TextView", id=f"row_{i}", text=f"字段{i}",
               bounds=(60, 200 + i * 26, 1020, 224 + i * 26), clickable=True)
        for i in range(count)
    ]
    rows.append(UINode(cls="Button", id="btn_target", text=target_text,
                       bounds=(400, h - 400, 680, h - 320), clickable=True))
    return UINode(cls="Root", bounds=(0, 0, w, h), children=[
        UINode(cls="FrameLayout", id="content", bounds=(0, 100, w, h - 100),
               scrollable=True, children=rows),
    ])


def shifted(root: UINode, dx: int = 0, dy: int = 1) -> UINode:
    """整棵树所有节点平移 (dx, dy)：细粒度指纹变、粗粒度签名不变（AC-1.10）。"""

    def clone(node: UINode) -> UINode:
        x1, y1, x2, y2 = node.bounds
        return UINode(
            cls=node.cls, id=node.id, text=node.text, desc=node.desc,
            bounds=(x1 + dx, y1 + dy, x2 + dx, y2 + dy),
            clickable=node.clickable, enabled=node.enabled, checked=node.checked,
            scrollable=node.scrollable, children=[clone(c) for c in node.children],
        )

    return clone(root)


def thin_page(w: int = W, h: int = H) -> UINode:
    """白屏半树：壳子 + 一个「附件」（节点数远小于 min_nodes=20）。"""
    return UINode(cls="Root", bounds=(0, 0, w, h), children=[
        UINode(cls="WebView", bounds=(0, 100, w, h - 100), scrollable=True, children=[
            UINode(cls="TextView", text="附件", bounds=(0, h - 220, w, h - 140),
                   clickable=True),
        ]),
    ])


def popup_page(w: int = W, h: int = H, *, skip_text: str = "跳过此广告",
               close_id: str = "btn_skip", rows: int = 20) -> UINode:
    """带一个可安全关闭弹窗的页面；坐标按 (w, h) 等比缩放，供跨分辨率用例复用。"""
    sx, sy = w / float(W), h / float(H)

    def b(x1: int, y1: int, x2: int, y2: int) -> tuple[int, int, int, int]:
        return (int(round(x1 * sx)), int(round(y1 * sy)),
                int(round(x2 * sx)), int(round(y2 * sy)))

    body = [
        UINode(cls="TextView", id=f"row_{i}", text=f"字段{i}",
               bounds=b(60, 200 + i * 26, 1020, 224 + i * 26), clickable=True)
        for i in range(rows)
    ]
    overlay = UINode(cls="FrameLayout", id="popup_container",
                     bounds=b(140, 600, 940, 1300), children=[
                         UINode(cls="TextView", text="版本更新提示",
                                bounds=b(200, 660, 880, 740)),
                         UINode(cls="Button", id=close_id, text=skip_text,
                                bounds=b(700, 1120, 900, 1220), clickable=True),
                     ])
    return UINode(cls="Root", bounds=(0, 0, w, h), children=[
        UINode(cls="FrameLayout", id="content", bounds=b(0, 100, 1080, 2300),
               scrollable=True, children=body),
        overlay,
    ])


# ---------------------------------------------------------------------------
# 弹窗记忆辅助
# ---------------------------------------------------------------------------

def popup_hit(obs: Observation, keyword: str = "跳过此广告",
              kind: str = "skip", close_id: str = "btn_skip") -> PopupHit:
    """从观察里按资源 id 取关闭节点，造一个 ``PopupHit``（等价于全量决策命中）。"""
    for node in obs.root.walk():
        if node.id == close_id:
            x, y = node.center
            return PopupHit(keyword=keyword, text=(node.text or "")[:40], x=x, y=y,
                            kind=kind, node_cls=node.cls)
    raise AssertionError(f"夹具里没有 id={close_id} 的关闭节点")


def popup_context(mode: str = "replay", app_id: str = APP,
                  platform: str = "android", run_id: str = "run-1",
                  enabled: bool = True) -> PopupContext:
    return PopupContext(app_id=app_id, platform=platform, run_id=run_id,
                        mode=mode, enabled=enabled)


def memory_for(cfg) -> PopupMemory:
    return PopupMemory(cfg.popup_memory_file, cfg)


def sediment(memory: PopupMemory, obs: Observation, hit: PopupHit,
             ctx: PopupContext, runs: int = 2) -> PopupEntry:
    """模拟「全量决策成功 + 后续 2 步无偏离」跑了 ``runs`` 个 run，返回最终条目。

    每个 run 需要两次 ``confirm_window``（确认窗口要求连续 2 步无偏离）才计一次 hit。
    """
    entry = None
    for i in range(runs):
        run_id = f"run-{i + 1}"
        ctx.run_id = run_id
        sig = build_signature(obs, hit, ctx.app_id, ctx.platform)
        entry = memory.observe_success(
            {"sig": sig, "obs": obs, "hit": hit}, ctx, run_id)
        memory.confirm_window(run_id=run_id)
        committed = memory.confirm_window(run_id=run_id)
        if committed:
            entry = committed[0]
    return entry


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------

class SpyGuard(PopupGuard):
    """计数版 ``PopupGuard``：记录 find 调用次数与「扫描时弹窗是否还在」。"""

    def __init__(self, *args, popup_id: str = "popup_container", **kwargs):
        super().__init__(*args, **kwargs)
        self.popup_id = popup_id
        self.calls = 0
        self.popup_present: list[bool] = []

    def find(self, obs):
        self.calls += 1
        self.popup_present.append(_contains_id(obs.root, self.popup_id))
        return super().find(obs)


def _contains_id(root: UINode | None, node_id: str) -> bool:
    if root is None:
        return False
    return any(n.id == node_id for n in root.walk())


class CountingGuard(SpyGuard):
    """只关心调用次数的别名。"""


class CountingLLM(LLMClient):
    """计数版模型客户端：只记账，绝不联网。"""

    def __init__(self):
        super().__init__()
        self.call_count = 0
        self.messages: list = []

    def chat(self, messages, **kw):  # type: ignore[override]
        self.call_count += 1
        self.messages.append(messages)
        return "ok"

    def chat_json(self, messages, **kw):  # type: ignore[override]
        self.call_count += 1
        self.messages.append(messages)
        return {"passed": True, "reason": "counting-stub"}

    def _post_with_retry(self, req, attempts: int = 4):  # type: ignore[override]
        self.call_count += 1
        return {}


class CountingHealer:
    """计数版定位器修复替身：回放全命中时一次都不该被调用。"""

    def __init__(self):
        self.calls = 0

    def heal(self, step, obs, case):
        self.calls += 1
        return None


class FrameGuard:
    """``_push_frame`` 计数替身：用来断言「某条路径没有新增页面栈帧」。"""

    def __init__(self, stack: PageStack | None = None):
        self.stack = stack or PageStack()
        self.pushes: list[PageFrame] = []

    def __len__(self) -> int:
        return len(self.pushes)

    def push(self, frame: PageFrame) -> PageFrame:
        self.pushes.append(frame)
        return frame


# ---------------------------------------------------------------------------
# 门禁 / 页面栈辅助
# ---------------------------------------------------------------------------

def stable_result(stable: bool = True, degraded: bool = False,
                  root: UINode | None = None, **kw) -> StableResult:
    """造一份门禁结果（供偏离守卫的「门禁可信度」判据使用）。"""
    sig = page_signature(root) if root is not None else frozenset()
    return StableResult(stable=stable, degraded=degraded, coarse_sig=sig, **kw)


def built_stack(root: UINode | None = None, package: str = APP,
                steps: int = 2) -> PageStack:
    """造一个已经压了 ``steps`` 帧的页面栈。"""
    stack = PageStack()
    sig = page_signature(root if root is not None else page())
    for i in range(steps):
        stack.push(PageFrame(step_id=f"s{i:02d}", fine_fp=f"fp-{i}",
                             coarse_sig=sig, package=package))
    return stack


def events_of(driver: TreeDriver, *types: str) -> list[dict]:
    if not types:
        return list(driver.events)
    return [e for e in driver.events if str(e.get("type", "")) in types]


def danger_free_label(root: UINode | None) -> bool:
    if root is None:
        return True
    for node in root.walk():
        label = f"{node.text or ''} {node.desc or ''}"
        if any(word in label for word in DANGER_WORDS):
            return False
    return True


# 供子类化 PageStabilizer 的用例使用：可控指纹 + 可控节点数的观察替身
class FakeObs:
    """只有 ``root`` 与 ``fingerprint`` 两件事的轻量观察对象。"""

    __slots__ = ("root", "fingerprint", "page_ready", "page_gate_degraded")

    def __init__(self, root: UINode, fingerprint: str):
        self.root = root
        self.fingerprint = fingerprint
        self.page_ready = True
        self.page_gate_degraded = False


class SeqStabilizer(PageStabilizer):
    """按脚本逐拍返回观察，用来独立验证「指纹 + 节点数波动」判据（AC-1.3）。"""

    def __init__(self, driver, cfg, seq, **kwargs):
        super().__init__(driver, cfg, **kwargs)
        self._seq = list(seq)
        self._i = 0

    def _tree_observation(self):  # type: ignore[override]
        root, fp = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return FakeObs(root, fp)
