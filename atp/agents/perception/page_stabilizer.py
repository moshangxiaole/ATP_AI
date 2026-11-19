"""移动端页面「完全加载完成」门禁（零模型、树-only 轮询）。

设计要点：
* 稳定判据只用 ``fine_fp``（含 bounds 的树指纹）；``coarse_sig``（排除 bounds）
  只用于页面身份 / 偏离检测，二者严格不混用。
* 等待期间只做 ``observe_tree(with_shot=False)`` 树轮询，绝不截图；
  稳定后由 ``stable_observe`` 取且仅取一次截图。
* 超时 fail-closed：返回 ``stable=False``，由调用方决定是否放弃点击。
* Web / Desktop / Electron 完全 no-op。
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

__all__ = [
    "DEFAULT_DYNAMIC_MARKERS",
    "DEFAULT_LOADING_MARKERS",
    "SAME_PAGE_THRESHOLD",
    "StableResult",
    "PageFrame",
    "PageStack",
    "PageStabilizer",
    "page_signature",
    "jaccard",
    "has_loading",
    "node_count",
    "walk_nodes",
]


# ---------------------------------------------------------------------------
# 动态节点 / 加载态标记
# ---------------------------------------------------------------------------

#: 时间戳、进度、计数、轮播、加载态等天然易变节点，粗粒度签名需排除。
DEFAULT_DYNAMIC_MARKERS: tuple[str, ...] = (
    "loading", "progress", "skeleton", "shimmer", "placeholder", "spinner",
    "carousel", "banner", "countdown", "timestamp", "marquee",
    "加载中", "正在加载", "请稍候", "拼命加载", "刷新中", "加载更多",
    "轮播", "倒计时", "剩余时间", "播放中", "缓冲中",
)

#: 明确的「页面尚未加载完」标记；命中即不允许判定稳定。
DEFAULT_LOADING_MARKERS: tuple[str, ...] = (
    "loading", "skeleton", "shimmer", "spinner", "placeholder",
    "加载中", "正在加载", "请稍候", "拼命加载", "下拉刷新", "缓冲中",
)

_TIMEISH_RE = re.compile(
    r"^(?:"
    r"\d{1,4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}"
    r"|\d{1,2}:\d{2}(?::\d{2})?"
    r"|\d+\s*%"
    r"|\d+\s*/\s*\d+"
    r"|\d{1,3}(?:,\d{3})+"
    r"|第\s*\d+\s*页"
    r"|\d+\s*(?:秒|分钟|小时|天|条|条评论|人|次)"
    r")[\s\S]*$"
)


def _get(node: Any, *names: str, default: Any = "") -> Any:
    """按多个候选字段名读取节点属性，兼容 UINode / dict / 各类驱动节点。"""
    for name in names:
        if isinstance(node, dict):
            if name in node and node[name] is not None:
                return node[name]
        else:
            value = getattr(node, name, None)
            if value is not None:
                return value
    return default


def _text_of(node: Any) -> str:
    """从节点取文本，缺失时回退到描述字段。"""
    return str(_get(node, "text", "label", "name", "content", default="") or "").strip()


def _desc_of(node: Any) -> str:
    """从节点取无障碍描述。"""
    return str(
        _get(node, "desc", "content_desc", "content_description", "description", default="")
        or ""
    ).strip()


def _cls_of(node: Any) -> str:
    """从节点取控件类型。"""
    return str(
        _get(node, "cls", "class_name", "class", "node_cls", "type", default="") or ""
    ).strip()


def _id_of(node: Any) -> str:
    """从节点取自动化 ID。"""
    return str(
        _get(node, "id", "resource_id", "a11y_id", "accessibility_id", "rid", default="")
        or ""
    ).strip()


def _children_of(node: Any) -> tuple[Any, ...]:
    """枚举一个节点的直接子节点。"""
    children = _get(node, "children", "nodes", "kids", "subnodes", default=())
    if isinstance(children, dict):
        children = children.get("nodes", ())
    if children is None:
        return ()
    if isinstance(children, (list, tuple)):
        return tuple(children)
    try:
        return tuple(children)
    except TypeError:
        return ()


def walk_nodes(root: Any) -> Iterable[tuple[Any, int]]:
    """深度优先遍历整棵树，产出 ``(node, depth)``；root 为 None 时产出空序列。"""
    if root is None:
        return
    stack: list[tuple[Any, int]] = [(root, 0)]
    guard = 0
    while stack and guard < 20000:
        guard += 1
        node, depth = stack.pop()
        yield node, depth
        for child in reversed(_children_of(node)):
            stack.append((child, depth + 1))


def node_count(root: Any) -> int:
    """返回树节点总数（无上限截断，用于稳定性判据）。"""
    return sum(1 for _ in walk_nodes(root))


def _haystack(node: Any) -> str:
    """把节点的可匹配文案拼成字符串（文本 + 描述 + ID）。"""
    parts = (
        _text_of(node),
        _desc_of(node),
        _cls_of(node),
        _id_of(node),
    )
    return " ".join(p for p in parts if p).lower()


def _is_volatile_text(text: str) -> bool:
    """时间戳 / 百分比 / 纯计数等易变文本视为动态节点。"""
    if not text:
        return False
    if text.isdigit():
        return True
    return bool(_TIMEISH_RE.match(text))


def _is_dynamic_node(node: Any, markers: tuple[str, ...]) -> bool:
    """节点是否属于动态内容（时间戳、轮播等），这类节点不参与稳定指纹。"""
    if _is_volatile_text(_text_of(node)):
        return True
    hay = _haystack(node)
    if not hay:
        return False
    return any(marker in hay for marker in markers)


def _marker_tuple(markers: Any, defaults: tuple[str, ...]) -> tuple[str, ...]:
    """把配置里的 marker 序列与内置默认合并、小写去重。"""
    result: list[str] = []
    seen: set[str] = set()
    source: list[Any] = list(defaults)
    if markers:
        if isinstance(markers, str):
            source.append(markers)
        else:
            try:
                source.extend(list(markers))
            except TypeError:
                source.append(markers)
    for item in source:
        token = str(item or "").strip().lower()
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return tuple(result)


def page_signature(root: Any, dynamic_markers: Iterable[str] = ()) -> frozenset:
    """生成排除 bounds 的粗粒度页面身份签名。

    由 ``("n", cls, id, text)`` 节点集合与 ``("c", cls, id, depth)`` 容器骨架
    组成；动态节点（时间戳 / 进度 / 计数 / 轮播 / 加载态）被排除。
    **绝不含绝对屏幕坐标**，因此滑动、软键盘、滚动条位置变化不会改变签名。
    """
    markers = _marker_tuple(dynamic_markers, DEFAULT_DYNAMIC_MARKERS)
    sig: set[tuple[Any, ...]] = set()
    for node, depth in walk_nodes(root):
        if _is_dynamic_node(node, markers):
            continue
        cls = _cls_of(node)
        rid = _id_of(node)
        text = _text_of(node)
        sig.add(("n", cls, rid, text))
        if _children_of(node):
            # 容器骨架只记录类型 / 资源 id / 层级，刻意不记录子节点数量，
            # 这样列表刷新、局部插入不会让页面身份漂移。
            sig.add(("c", cls, rid, min(depth, 12)))
    return frozenset(sig)


def has_loading(root: Any, markers: Iterable[str] = ()) -> bool:
    """树中是否存在明确的加载中标记；命中则不允许判定页面稳定。"""
    ms = _marker_tuple(markers, DEFAULT_LOADING_MARKERS)
    for node, _depth in walk_nodes(root):
        if any(marker in _haystack(node) for marker in ms):
            return True
    return False


def jaccard(a: Any, b: Any) -> float:
    """两个签名集合的 Jaccard 相似度；两者皆空时返回 1.0。"""
    if not a or not b:
        return 1.0 if not a and not b else 0.0
    try:
        sa, sb = set(a), set(b)
    except TypeError:
        return 0.0
    union = len(sa | sb)
    if union == 0:
        return 1.0
    return len(sa & sb) / float(union)


# ---------------------------------------------------------------------------
# 结果 / 页面栈
# ---------------------------------------------------------------------------


@dataclass
class StableResult:
    """一次页面稳定门禁的结果。"""

    stable: bool = False
    obs: Any = None
    fine_fp: str = ""
    coarse_sig: frozenset = field(default_factory=frozenset)
    wait_ms: int = 0
    tree_polls: int = 0
    degraded: bool = False
    timed_out: bool = False
    reused: bool = False
    reason: str = ""
    at: float = field(default_factory=time.monotonic)

    @property
    def ok(self) -> bool:
        return bool(self.stable and not self.degraded)


@dataclass
class PageFrame:
    """一次成功门禁后压入页面栈的帧快照。

    只记录「路径 / 指纹 / 签名 / 包名」，**绝不存截图二进制、不存整棵树**：
    落盘产物可能被客户带走，页面文本属于敏感信息（设计 Q11）。
    """

    frame_id: str = ""
    step_id: str = ""
    fine_fp: str = ""
    coarse_sig: frozenset = field(default_factory=frozenset)
    package: str = ""
    before_shot: str = ""
    at: float = 0.0
    last_seen: float = 0.0
    stable: bool = True

    def sig_summary(self) -> dict:
        """粗签名的可上报摘要：只给节点数 + 短 hash，不整集透传。

        hash 用 md5 而非内置 ``hash()``：后者按进程随机加盐，产物文件两次跑
        出来对不上，排查时会以为是页面变了。
        """
        n = len(self.coarse_sig or ())
        if n == 0:
            return {"nodes": 0, "hash": ""}
        payload = "\x1f".join(sorted(repr(item) for item in self.coarse_sig))
        return {"nodes": n, "hash": hashlib.md5(payload.encode("utf-8")).hexdigest()[:8]}

    def as_dict(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "step_id": self.step_id,
            "fine_fp": self.fine_fp,
            "coarse_sig": self.sig_summary(),
            "package": self.package,
            "before_shot": self.before_shot,
            "at": round(float(self.at or 0.0), 3),
            "last_seen": round(float(self.last_seen or self.at or 0.0), 3),
            "stable": bool(self.stable),
        }


#: 判定「还是同一页」的默认阈值；与 BACK 校验共用同一把尺子。
SAME_PAGE_THRESHOLD: float = 0.80


class PageStack:
    """运行期页面栈：记录「从哪来」，供偏离回退时比对目标页。

    帧数语义 = 跳页数 + 1：同页操作（输入文本、开关、滚动）不新增帧，
    只刷新当前帧的 ``last_seen`` / ``fine_fp``，否则 ``stack[-2]`` 会指向
    同一页，BACK 校验就永远拿不到「上一页」。
    """

    def __init__(self) -> None:
        self._frames: list[PageFrame] = []
        self._seq: int = 0
        self._run_id: str = ""

    def push(self, frame: PageFrame) -> PageFrame:
        if not isinstance(frame, PageFrame):
            raise TypeError("PageStack.push 需要 PageFrame")
        self._seq += 1
        if not frame.frame_id:
            frame.frame_id = "frame-%d" % self._seq
        if not frame.at:
            frame.at = time.time()
        if not frame.last_seen:
            frame.last_seen = frame.at
        self._frames.append(frame)
        return frame

    def same_page(self, frame: PageFrame, *, threshold: float = SAME_PAGE_THRESHOLD) -> bool:
        """``frame`` 是否与栈顶「还是同一页」。

        判据只看包名 + 粗签名 Jaccard：包名不同一定不是同一页；包名拿不到时
        只信签名。阈值默认与 BACK 校验一致（0.80），避免出现「回退算成功、
        入栈算跳页」两套尺子。
        """
        cur = self.current()
        if cur is None or frame is None:
            return False
        cur_pkg = str(getattr(cur, "package", "") or "")
        new_pkg = str(getattr(frame, "package", "") or "")
        if cur_pkg and new_pkg and cur_pkg != new_pkg:
            return False
        if not cur.coarse_sig or not frame.coarse_sig:
            return False
        return jaccard(cur.coarse_sig, frame.coarse_sig) >= float(threshold)

    def update_current(self, *, fine_fp: str = "", coarse_sig: Any = None,
                       package: str = "", before_shot: Optional[str] = None) -> Optional[PageFrame]:
        """同页操作不新增帧，只刷新栈顶（设计 §3.5：帧数 = 跳页数 + 1）。

        刻意**不**覆盖 ``frame_id`` / ``at`` / ``step_id``：这三个字段回答的是
        「这一页是第几步进来的」，被同页输入改写后就查不出回溯路径了。
        """
        cur = self.current()
        if cur is None:
            return None
        now = time.time()
        if fine_fp:
            cur.fine_fp = str(fine_fp)
        if coarse_sig:
            cur.coarse_sig = coarse_sig
        if package:
            cur.package = str(package)
        if before_shot:
            cur.before_shot = str(before_shot)
        cur.last_seen = now
        return cur

    def frames(self) -> list[PageFrame]:
        """栈内帧的浅拷贝列表（外部只读，改不动内部状态）。"""
        return list(self._frames)

    def set_run_id(self, run_id: str) -> None:
        self._run_id = str(run_id or "")

    def current(self) -> Optional[PageFrame]:
        return self._frames[-1] if self._frames else None

    def previous(self) -> Optional[PageFrame]:
        """返回当前帧的上一帧（即 BACK 之后期望回到的页面）。"""
        return self._frames[-2] if len(self._frames) >= 2 else None

    def expected(self) -> Optional[PageFrame]:
        """BACK 校验的目标帧，语义等价于 :meth:`previous`。"""
        return self.previous()

    def pop(self) -> Optional[PageFrame]:
        return self._frames.pop() if self._frames else None

    def clear(self) -> None:
        self._frames.clear()

    def as_dict(self) -> dict:
        """供 ``artifacts/<run_id>/page_stack.json``（设计 Q11：默认关）。"""
        return {
            "version": 1,
            "run_id": self._run_id,
            "depth": len(self._frames),
            "frames": [f.as_dict() for f in self._frames],
        }

    def persist(self, path: Any) -> bool:
        """原子写页面栈产物；任何异常都只降级返回 False，绝不影响用例结论。"""
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            payload = json.dumps(self.as_dict(), ensure_ascii=False, indent=2)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
            return True
        except Exception:  # noqa: BLE001
            return False

    def __len__(self) -> int:
        return len(self._frames)

    def __iter__(self):
        return iter(self._frames)

    def __getitem__(self, index: int) -> PageFrame:
        return self._frames[index]


# ---------------------------------------------------------------------------
# 稳定门禁
# ---------------------------------------------------------------------------


def _as_float(value: Any, default: float) -> float:
    """把值安全地转成浮点数，失败返回默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    """把值安全地转成整数，失败返回默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class PageStabilizer:
    """移动端「树-only 轮询 → 稳定 → 单次截图」门禁。

    Args:
        driver: 实现 ``observe`` / ``observe_tree`` / ``screenshot`` 的驱动。
        cfg: ``Config`` 或 ``PageReadyConfig``；后者可直接传入便于单测。
        should_cancel: 取消回调，每轮轮询都会调用。
        on_event: 事件回调，兼容 ``fn(type, **payload)`` 与 ``fn(dict)``。
    """

    def __init__(
        self,
        driver: Any,
        cfg: Any,
        should_cancel: Optional[Callable[[], bool]] = None,
        on_event: Optional[Callable[..., None]] = None,
    ) -> None:
        self._driver = driver
        self._cfg = cfg
        self._pr = getattr(cfg, "page_ready", cfg)
        self._should_cancel = should_cancel
        self._on_event = on_event
        self._last: Optional[StableResult] = None
        self._dirty: bool = True
        self._unstable_fps: set[str] = set()
        self._thin_fps: set[str] = set()
        self.last_result: Optional[StableResult] = None

    # -- 配置读取 ---------------------------------------------------------

    def _opt(self, name: str, default: Any) -> Any:
        return getattr(self._pr, name, default)

    def _enabled(self) -> bool:
        return bool(self._opt("enabled", True))

    def _platforms(self) -> tuple[str, ...]:
        raw = self._opt("platforms", ("android", "harmony", "ios"))
        if isinstance(raw, str):
            return tuple(p.strip().lower() for p in raw.split(",") if p.strip())
        try:
            return tuple(str(p).strip().lower() for p in raw)
        except TypeError:
            return ("android", "harmony", "ios")

    def _mobile(self) -> bool:
        """Web / Desktop / Electron 完全 no-op。"""
        platform = str(getattr(self._driver, "platform", "") or "").lower()
        return self._enabled() and platform in self._platforms()

    def _max_wait_ms(self, reason: str, ceiling_ms: int) -> int:
        """合并本次等待的多个上限，**取最紧的那个（min）**。

        AC-1.8 约束的是「一次等待的总时长 ≤ 单次上限」，不是各 ceiling 之和，
        所以 reason 默认上限（``max_wait_ms`` / ``launch_max_wait_ms``）与调用方
        传入的 ``ceiling_ms``（旧 settle_ms 的新语义）必须取 **最紧** 值，绝不叠加：
        ``min(默认上限, ceiling_ms)``。若改成取 max，launch 场景的 5000ms 会突破
        AC-1.8 的 3000ms 总时长约束。
        """
        is_launch = "launch" in str(reason or "").lower() or "cold" in str(reason or "").lower()
        default_max = self._opt("launch_max_wait_ms", 5000) if is_launch else self._opt("max_wait_ms", 3000)
        limit = _as_int(default_max, 3000)
        ceiling = _as_int(ceiling_ms, 0)
        if ceiling > 0:
            limit = min(limit, ceiling)      # 取最紧上限：总时长不叠加（AC-1.8）
        return max(_as_int(self._opt("min_wait_ms", 150), 150), limit)

    # -- 事件 / 取消 ------------------------------------------------------

    def _fire(self, name: str, **payload: Any) -> None:
        cb = self._on_event
        if cb is None:
            return
        try:
            cb(name, **payload)
            return
        except TypeError:
            pass
        except Exception:
            return
        try:
            cb({"type": name, **payload})
        except Exception:
            return

    def _cancel(self) -> None:
        cb = self._should_cancel
        if cb is None:
            return
        try:
            hit = bool(cb())
        except Exception:  # noqa: BLE001
            return
        if hit:
            from atp.common.types import RunCancelled

            raise RunCancelled("用户取消执行")

    # -- 树采样 -----------------------------------------------------------

    def _tree_observation(self) -> Any:
        """树-only 采样；优先复用 scroller.observe_tree，失败时本地降级。"""
        try:
            from atp.agents.perception.scroller import observe_tree

            return observe_tree(self._driver, with_shot=False)
        except Exception:
            pass
        direct = getattr(self._driver, "observe_tree", None)
        if callable(direct):
            try:
                return direct(with_shot=False)
            except TypeError:
                try:
                    return direct()
                except Exception:
                    pass
            except Exception:
                pass
        dump = getattr(self._driver, "dump_tree", None)
        if callable(dump):
            try:
                root = dump()
            except Exception:
                return None
            from atp.common.types import Observation

            try:
                return Observation(
                    root=root,
                    screenshot=None,
                    width=_as_int(getattr(self._driver, "width", 0), 0),
                    height=_as_int(getattr(self._driver, "height", 0), 0),
                    platform=str(getattr(self._driver, "platform", "") or ""),
                    device_id=str(getattr(self._driver, "device_id", "") or ""),
                    url=str(getattr(self._driver, "url", "") or ""),
                    taken_at=time.time(),
                )
            except TypeError:
                try:
                    return Observation(root=root)
                except Exception:
                    return None
        return None

    @staticmethod
    def _root(obs: Any) -> Any:
        return getattr(obs, "root", None)

    @staticmethod
    def _fine_fp(obs: Any) -> str:
        value = getattr(obs, "fingerprint", None)
        if isinstance(value, str):
            return value
        if callable(value):
            try:
                return str(value() or "")
            except Exception:
                return ""
        root = PageStabilizer._root(obs)
        fp = getattr(root, "fingerprint", None) if root is not None else None
        if callable(fp):
            try:
                return str(fp() or "")
            except Exception:
                return ""
        if isinstance(fp, str):
            return fp
        return ""

    def _explicit_not_idle(self, obs: Any) -> bool:
        """驱动显式上报 not_idle / unsupported_page 时立即终止，不做三遍重试。"""
        for holder in (obs, self._driver):
            for key in ("not_idle", "unsupported_page", "tree_error", "page_unsupported"):
                if bool(getattr(holder, key, False)):
                    return True
        return False

    def _result(self, **kwargs: Any) -> StableResult:
        result = StableResult(**kwargs)
        self.last_result = result
        return result

    # -- 对外 API ---------------------------------------------------------

    def invalidate(self) -> None:
        """每个设备动作后调用，使会话级 reuse 立即失效。"""
        self._dirty = True

    def note_unstable(self, fp: str) -> None:
        """登记「同屏长期不稳」指纹，后续不再重复等满上限。"""
        if not fp:
            return
        self._unstable_fps.add(fp)
        if len(self._unstable_fps) > 128:
            self._unstable_fps.clear()
            self._unstable_fps.add(fp)

    def note_thin(self, fp: str) -> None:
        """登记「薄树 / 白屏半树」指纹。"""
        if not fp:
            return
        self._thin_fps.add(fp)
        if len(self._thin_fps) > 128:
            self._thin_fps.clear()
            self._thin_fps.add(fp)

    def wait_stable(
        self,
        reason: str,
        ceiling_ms: int = 0,
        baseline: Any = None,
    ) -> StableResult:
        """等待页面完全加载完成；全程树-only，绝不截图。

        Args:
            reason: 触发原因（``step_before`` / ``launch`` / ``post_action`` ...）。
            ceiling_ms: 本次等待上限（旧 settle_ms 的新语义）；0 表示用配置上限。
                与 ``max_wait_ms`` / ``launch_max_wait_ms`` 合并时**取最紧（min）**，
                保证一次等待的总时长不叠加（AC-1.8：≤ 单次上限，不是各 ceiling 之和）。
            baseline: 可选的基线观察对象，仅用于事件可见性，不参与稳定判据。

        Returns:
            :class:`StableResult`；超时/降级一律 fail-closed ``stable=False``。
        """
        if not self._mobile():
            return self._result(
                stable=True,
                reason="non_mobile_noop",
                wait_ms=0,
                tree_polls=0,
            )

        start = time.monotonic()
        limit_ms = self._max_wait_ms(reason, ceiling_ms)
        min_wait_ms = max(0, _as_int(self._opt("min_wait_ms", 150), 150))
        poll_ms = max(1, _as_int(self._opt("poll_interval_ms", 150), 150))
        poll_max_ms = max(poll_ms, _as_int(self._opt("poll_max_ms", 400), 400))
        stable_samples = max(2, _as_int(self._opt("stable_samples", 2), 2))
        max_tree_polls = max(2, _as_int(self._opt("max_tree_polls", 8), 8))
        min_nodes = max(0, _as_int(self._opt("min_nodes", 20), 20))
        delta_ratio = max(0.0, _as_float(self._opt("node_delta_ratio", 0.10), 0.10))
        reuse_window_ms = max(0, _as_int(self._opt("reuse_window_ms", 500), 500))
        loading_markers = _marker_tuple(
            self._opt("loading_markers", DEFAULT_LOADING_MARKERS),
            DEFAULT_LOADING_MARKERS,
        )
        dynamic_markers = _marker_tuple(
            self._opt("dynamic_markers", DEFAULT_DYNAMIC_MARKERS),
            DEFAULT_DYNAMIC_MARKERS,
        )
        baseline_fp = self._fine_fp(baseline) if baseline is not None else ""
        # 真机上单次 dumpLayout 就要 2~3 秒，而一次门禁至少要两拍才敢判稳。
        # 「上一轮门禁的稳定指纹」本身就是一拍合格证据：只要期间没有任何设备动作
        # （_dirty=False）、且本轮第一拍指纹与它完全相同，就没有必要再为同一帧
        # 抓一次树。省下来的是每步 1 次全树抓取。
        carry_ms = max(0, _as_int(self._opt("carry_stable_ms", 1500), 1500))
        carry_fp = ""
        if (
            carry_ms
            and not self._dirty
            and self._last is not None
            and self._last.stable
            and not self._last.degraded
            and (time.monotonic() - self._last.at) * 1000.0 <= carry_ms
        ):
            carry_fp = str(self._last.fine_fp or "")
        carried = False

        # 会话级复用：窗口内、期间无设备动作、且指纹未变。
        if (
            reuse_window_ms > 0
            and not self._dirty
            and self._last is not None
            and self._last.stable
            and (time.monotonic() - self._last.at) * 1000.0 <= reuse_window_ms
        ):
            try:
                probe = self._tree_observation()
            except Exception:
                probe = None
            if probe is not None and self._fine_fp(probe) == self._last.fine_fp:
                result = self._result(
                    stable=True,
                    obs=probe,
                    fine_fp=self._last.fine_fp,
                    coarse_sig=self._last.coarse_sig,
                    wait_ms=0,
                    tree_polls=1,
                    reused=True,
                    reason="reuse",
                )
                # 复用结果也要成为「最近一次可信稳定样本」：否则下一拍跨门禁
                # 复用时拿到的是更早的 _last，时间窗一过就白抓一次树。
                self._last = result
                return result

        prev_fp: Optional[str] = None
        prev_count: Optional[int] = None
        streak = 0
        last_obs: Any = None
        last_fp = ""
        last_sig: frozenset = frozenset()
        polls = 0
        cached_degrade = ""
        thin_streak = 0
        thin_tolerated = False
        timed_out = False

        while True:
            self._cancel()
            try:
                obs = self._tree_observation()
            except Exception as exc:
                return self._fail("observe_error:" + str(exc)[:80], start, polls, timed_out=True)
            polls += 1
            if obs is None:
                return self._fail("observe_failed", start, polls, timed_out=True)

            root = self._root(obs)
            fp = self._fine_fp(obs)
            count = node_count(root)
            sig = page_signature(root, dynamic_markers)
            last_obs, last_fp, last_sig = obs, fp, sig

            if self._explicit_not_idle(obs):
                # 立刻判定 unsupported_page，不重试、不把 coarse_sig 当稳定判据。
                return self._fail(
                    "unsupported_page",
                    start,
                    polls,
                    obs=obs,
                    fine_fp=fp,
                    coarse_sig=sig,
                    timed_out=False,
                )

            stale = bool(getattr(self._driver, "tree_stale", False))
            loading = has_loading(root, loading_markers)
            thin = count < min_nodes
            sample_ok = (not stale) and (not loading) and (not thin)
            # 历史缓存只用来「提前收手少等」，绝不参与当下的稳定判定。
            # 每次采样都按当前指纹重新判定，避免上一拍的降级结论误伤已翻案的页面。
            cached_degrade = ""
            if fp:
                if fp in self._unstable_fps:
                    cached_degrade = "cached_unstable"
                elif fp in self._thin_fps:
                    cached_degrade = "cached_thin"
            if thin:
                thin_tolerated = True

            same = (
                prev_fp is not None
                and prev_fp == fp
                and prev_count is not None
                and abs(count - prev_count) <= max(1, int(prev_count * delta_ratio))
            )
            if sample_ok and same:
                streak += 1
            else:
                streak = 1
            # 上一轮门禁的稳定样本充作第 1 拍：本轮只需再抓一次树即可确认
            # 「两拍之间没有变化」，判据强度与门禁内的两拍完全一致。
            if carry_fp and not carried and sample_ok and fp == carry_fp:
                streak += 1
                carried = True
            # 薄树绝不「提前放行」：必须连续多拍指纹、节点数都完全一致且无加载标记，
            # 才承认这是「本来就只有几个控件的合法小页面」，而不是白屏半树。
            if thin and same and not stale and not loading:
                thin_streak += 1
            else:
                thin_streak = 0
            prev_fp, prev_count = fp, count

            # 当下已满足全部稳定判据（连续 N 拍 fine_fp 一致 + 节点数达标 +
            # tree_stale=False + 无 loading）就必须返回 stable=True：
            # _unstable_fps / _thin_fps 只能让循环「少等几拍」，无权否决当下证据。
            if sample_ok and streak >= stable_samples:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                if elapsed_ms < min_wait_ms:
                    time.sleep((min_wait_ms - elapsed_ms) / 1000.0)
                    elapsed_ms = int((time.monotonic() - start) * 1000)
                self._dirty = False
                result = self._result(
                    stable=True,
                    obs=last_obs,
                    fine_fp=last_fp,
                    coarse_sig=last_sig,
                    wait_ms=elapsed_ms,
                    tree_polls=polls,
                    reason="stable",
                )
                self._last = result
                self._fire(
                    "page_ready",
                    reason=reason,
                    wait_ms=elapsed_ms,
                    tree_polls=polls,
                    fine_fp=last_fp,
                    baseline_fp=baseline_fp,
                    nodes=count,
                    degraded=False,
                    carried=bool(carried),
                )
                return result

            # 连续相同薄树 / 已登记薄树：有界收手，不重复等满上限，也绝不当成稳定。
            if thin_tolerated and (thin_streak >= 2 or cached_degrade == "cached_thin"):
                break

            elapsed_ms = int((time.monotonic() - start) * 1000)
            effective_limit = min_wait_ms if cached_degrade else limit_ms
            # 同屏已登记不稳：最多再采 3 拍就收手，绝不重复等满上限；
            # 仍保留这几拍是为了让「动作之后其实已经稳了」有机会翻案。
            if (cached_degrade and polls >= 3) or elapsed_ms >= effective_limit \
                    or polls >= max_tree_polls:
                timed_out = True
                break
            self._cancel()
            time.sleep(min(poll_ms, poll_max_ms) / 1000.0)
            poll_ms = min(int(poll_ms * 1.5) + 1, poll_max_ms)

        tolerated = bool(thin_tolerated and not timed_out)
        return self._fail(
            "thin_stable" if tolerated else (cached_degrade or "timeout_unstable"),
            start,
            polls,
            obs=last_obs,
            fine_fp=last_fp,
            coarse_sig=last_sig,
            timed_out=timed_out,
            reason_label=reason,
            tolerated=tolerated,
        )

    def _fail(
        self,
        why: str,
        start: float,
        polls: int,
        obs: Any = None,
        fine_fp: str = "",
        coarse_sig: frozenset = frozenset(),
        timed_out: bool = True,
        reason_label: str = "",
        tolerated: bool = False,
    ) -> StableResult:
        """统一构造 fail-closed 结果并发出门禁事件。"""
        elapsed_ms = int((time.monotonic() - start) * 1000)
        result = self._result(
            stable=False,
            obs=obs,
            fine_fp=fine_fp,
            coarse_sig=coarse_sig,
            wait_ms=elapsed_ms,
            tree_polls=polls,
            degraded=True,
            timed_out=timed_out,
            reason=why,
        )
        self._dirty = True
        if tolerated:
            # 薄但确实稳定：只登记薄树，避免同屏每步都重新等；绝不算稳定。
            if fine_fp:
                self.note_thin(fine_fp)
        elif fine_fp:
            self.note_unstable(fine_fp)
        self._fire(
            "page_ready_timeout",
            reason=reason_label,
            detail=why,
            wait_ms=elapsed_ms,
            tree_polls=polls,
            fine_fp=fine_fp,
            degraded=True,
            tolerated=tolerated,
        )
        return result

    def stable_observe(self, reason: str, ceiling_ms: int = 0) -> Any:
        """稳定后取且仅取一次截图，组装成完整 ``Observation``。

        失败时返回 ``driver.observe()`` 的结果并打 ``page_ready=False`` 标记，
        由调用方决定不点击（fail-closed）。
        """
        if not self._mobile():
            return self._driver.observe()

        result = self.wait_stable(reason, ceiling_ms=ceiling_ms)
        self.last_result = result
        if result.stable and result.obs is not None:
            try:
                shot = self._driver.screenshot()
            except Exception:
                shot = None
            obs = _attach_screenshot(result.obs, self._driver, shot)
            _mark_ready(obs, True, result.reason)
            return obs

        # 薄树但连续多拍完全一致：库级仍判 stable=False（不提前放行），
        # 回放端照常定位，但打上 degraded 标记，禁用一切依赖门禁可信度的能力。
        if result.reason == "thin_stable" and result.obs is not None:
            try:
                shot = self._driver.screenshot()
            except Exception:
                shot = None
            obs = _attach_screenshot(result.obs, self._driver, shot)
            _mark_ready(obs, True, "thin_stable")
            _set_degraded(obs, True)
            return obs

        # 超时 / 门禁不可信：单张兜底截图 + 打标，绝不把 coarse_sig 冒充稳定。
        try:
            obs = self._driver.observe()
        except Exception:
            obs = result.obs
        _mark_ready(obs, False, result.reason)
        _set_degraded(obs, True)
        return obs


def _attach_screenshot(obs: Any, driver: Any, shot: Any) -> Any:
    """把唯一一次截图挂到树-only 观察对象上；必要时降级构造 Observation。"""
    if obs is None:
        try:
            return driver.observe()
        except Exception:
            return None
    try:
        setattr(obs, "screenshot", shot)
        return obs
    except Exception:
        pass
    try:
        from atp.common.types import Observation

        return Observation(
            root=getattr(obs, "root", None),
            screenshot=shot,
            width=getattr(obs, "width", getattr(driver, "width", 0)),
            height=getattr(obs, "height", getattr(driver, "height", 0)),
            platform=getattr(obs, "platform", getattr(driver, "platform", "")),
            device_id=getattr(obs, "device_id", getattr(driver, "device_id", "")),
            url=getattr(obs, "url", getattr(driver, "url", "")),
            taken_at=time.time(),
        )
    except TypeError:
        try:
            from atp.common.types import Observation

            return Observation(
                platform=str(getattr(obs, "platform", getattr(driver, "platform", "")) or ""),
                device_id=str(getattr(obs, "device_id", getattr(driver, "device_id", "")) or ""),
                root=getattr(obs, "root", None),
                screenshot=shot or b"",
            )
        except Exception:
            return obs
    except Exception:
        return obs


def _mark_ready(obs: Any, ready: bool, reason: str) -> None:
    """在观察对象上打轻量标记（不改任何模型 / 步骤字段）。"""
    if obs is None:
        return
    for key, value in (
        ("page_ready", ready),
        ("page_ready_reason", reason),
        ("page_gate_degraded", not ready),
    ):
        try:
            setattr(obs, key, value)
        except Exception:
            return


def _set_degraded(obs: Any, degraded: bool) -> None:
    """单独覆写 degraded 标记（薄树放行时 page_ready 仍为 True）。"""
    if obs is None:
        return
    try:
        setattr(obs, "page_gate_degraded", bool(degraded))
    except Exception:
        return
