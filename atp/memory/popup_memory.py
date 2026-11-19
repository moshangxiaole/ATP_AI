"""跨运行弹窗长程记忆：把「已经安全关过好几次的弹窗」沉淀成零决策快路径。

设计原则（宁可不快，也不能点错）：

* 快路径只是**跳过决策**，绝不跳过任何安全闸：新鲜树二次确认、
  ``tree_stale`` 校验、``DANGER_WORDS`` 复核、``_onscreen``、点击后签名节点消失，
  一样都不能少；任何一条不成立立刻 ``miss`` 并回退全量决策。
* 记忆签名是**结构化的**，绝不含绝对屏幕坐标；坐标一律在当前新鲜树上重新算。
* 只有「全量决策成功 + 安全闸全过 + 该步 passed + 后续 2 步无偏离」才计一次 hit；
  ``hits>=2`` 且来自 ≥2 个不同 run 才升为 active；连续 miss 会禁用/退役。
* 四类总开关：全局 enabled、disabled_apps、Executor.use_popup_memory、
  环境变量 ``ATP_POPUP_MEMORY=0``；``mode="ai"`` 只写不读；readonly 只读不落盘。
* 文件落在项目根 ``popup_memory.json``（与 apps.json 同级），原子写、坏文件降级为空。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atp.common.profiles import profile_for
from atp.memory.popups import DANGER_WORDS, PopupHit, _label, _onscreen

__all__ = [
    "PopupEntry",
    "PopupContext",
    "PopupMemory",
    "build_signature",
    "normalized_rect",
]

_VERSION = 1
_STATE_ORDER = {"retired": 0, "disabled": 1, "candidate": 2, "active": 3}
_VOLATILE_TEXT = re.compile(
    r"^(?:\d+|\d{1,2}:\d{2}|\d+\s*%|\d+\s*/\s*\d+|\d+秒|\d+%)$"
)


def normalized_rect(bounds: Any, w: int, h: int) -> tuple[float, float, float, float]:
    """把绝对像素矩形换成 0~1 归一化矩形（签名里唯一允许出现的位置信息）。

    Args:
        bounds: ``(x1, y1, x2, y2)``。
        w: 屏幕宽度；0 时按 1 兜底，避免除零。
        h: 屏幕高度；0 时按 1 兜底。

    Returns:
        四位浮点元组，保留三位小数。
    """
    try:
        x1, y1, x2, y2 = (float(v) for v in bounds)
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)
    sw = float(w) if w else 1.0
    sh = float(h) if h else 1.0
    return (
        round(min(1.0, max(0.0, x1 / sw)), 3),
        round(min(1.0, max(0.0, y1 / sh)), 3),
        round(min(1.0, max(0.0, x2 / sw)), 3),
        round(min(1.0, max(0.0, y2 / sh)), 3),
    )


def _stable_text(node: Any) -> str:
    text = str(getattr(node, "text", "") or "").strip()
    if not text or _VOLATILE_TEXT.match(text):
        return ""
    return text[:24]


def _node_area(node: Any) -> int:
    try:
        x1, y1, x2, y2 = node.bounds
        return max(0, int(x2) - int(x1)) * max(0, int(y2) - int(y1))
    except Exception:  # noqa: BLE001
        return 0


def _paths(root: Any) -> list[tuple[Any, tuple[Any, ...]]]:
    """返回 ``(node, ancestors)``，ancestors 从根到父（不含自身）。"""
    out: list[tuple[Any, tuple[Any, ...]]] = []
    if root is None:
        return out
    stack: list[tuple[Any, tuple[Any, ...]]] = [(root, ())]
    guard = 0
    while stack and guard < 20000:
        guard += 1
        node, parents = stack.pop()
        out.append((node, parents))
        for child in reversed(list(getattr(node, "children", ()) or ())):
            stack.append((child, parents + (node,)))
    return out


def _hit_context(obs: Any, hit: Any) -> tuple[Any, Any, tuple[Any, ...], list[str], bool]:
    """定位命中节点与其弹窗容器，返回 (节点, 容器, 路径, 稳定文本, danger_free)。"""
    root = getattr(obs, "root", None)
    w = int(getattr(obs, "width", 0) or 0)
    h = int(getattr(obs, "height", 0) or 0)
    x = int(getattr(hit, "x", 0) or 0)
    y = int(getattr(hit, "y", 0) or 0)
    keyword = str(getattr(hit, "keyword", "") or "")

    best: Any = None
    best_path: tuple[Any, ...] = ()
    best_area = 0
    for node, parents in _paths(root):
        if not _onscreen(node, w, h):
            continue
        try:
            contains = node.contains(x, y, 2)
        except Exception:  # noqa: BLE001
            contains = False
        if not contains:
            continue
        area = _node_area(node)
        label = _label(node)
        score_kw = 1 if (keyword and keyword in label) else 0
        if best is None or (score_kw, -area) > (
            1 if (keyword and keyword in _label(best)) else 0, -best_area
        ):
            best, best_path, best_area = node, parents, area

    if best is None:
        return None, None, (), [], False

    # 容器：从根往命中点走，取「有子节点、面积不超过屏幕 90%、最靠外」的那一层。
    chain = best_path + (best,)
    screen_area = max(1, w * h)
    container: Any = None
    for node in chain[:-1]:
        if len(getattr(node, "children", ()) or ()) < 2:
            continue
        if _node_area(node) > screen_area * 0.9:
            continue
        container = node
        break
    if container is None:
        container = chain[-2] if len(chain) >= 2 else chain[0]

    texts: list[str] = []
    danger_free = True
    for node, _ in _paths(container):
        label = _label(node)
        if any(word in label for word in DANGER_WORDS):
            danger_free = False
        text = _stable_text(node)
        if text and text not in texts:
            texts.append(text)
    # 位置排序让「前 3 条稳定文本」与渲染顺序一致
    texts.sort(key=lambda t: t)
    texts = texts[:3]
    return best, container, chain, texts, danger_free


def _close_attrs(node: Any, chain: tuple[Any, ...], keyword: str) -> dict:
    """关闭节点的稳定属性：a11y_id > text/desc > cls 路径。"""
    rid = str(getattr(node, "id", "") or "").strip()
    text = str(getattr(node, "text", "") or "").strip()
    desc = str(getattr(node, "desc", "") or "").strip()
    cls_path = "/".join(
        str(getattr(n, "cls", "") or "") for n in chain[-3:] if getattr(n, "cls", "")
    )
    return {
        "a11y_id": rid,
        "text": text[:40],
        "desc": desc[:40],
        "cls_path": cls_path[-120:],
        "keyword": keyword,
    }


def build_signature(obs: Any, hit: Any, app_id: str, platform: str) -> str:
    """生成弹窗结构化签名（**绝不含绝对屏幕坐标**）。

    组成：platform + app_id + 弹窗容器归一化 rect/cls/id + close 节点稳定属性
    + keyword + kind + 弹窗内前 3 条稳定文本 + danger_free 标记。
    """
    w = int(getattr(obs, "width", 0) or 0)
    h = int(getattr(obs, "height", 0) or 0)
    node, container, chain, texts, danger_free = _hit_context(obs, hit)
    if node is None or container is None:
        return ""

    rect = tuple(round(v, 2) for v in normalized_rect(getattr(container, "bounds", ()), w, h))
    attrs = _close_attrs(node, chain, str(getattr(hit, "keyword", "") or ""))
    if attrs["a11y_id"]:
        close_key = ("id", attrs["a11y_id"])
    elif attrs["text"] or attrs["desc"]:
        close_key = ("label", attrs["text"], attrs["desc"])
    else:
        close_key = ("cls", attrs["cls_path"])

    payload = {
        "platform": str(platform or ""),
        "app_id": str(app_id or ""),
        "container": {
            "rect": rect,
            "cls": str(getattr(container, "cls", "") or ""),
            "id": str(getattr(container, "id", "") or ""),
        },
        "close": close_key,
        "keyword": str(getattr(hit, "keyword", "") or ""),
        "kind": str(getattr(hit, "kind", "") or ""),
        "texts": list(texts),
        "danger_free": bool(danger_free),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass
class PopupEntry:
    """一条沉淀下来的弹窗处置记忆。"""

    sig: str = ""
    app_id: str = ""
    platform: str = ""
    keyword: str = ""
    kind: str = "close"
    close_attrs: dict = field(default_factory=dict)
    container: dict = field(default_factory=dict)
    texts: list = field(default_factory=list)
    hits: int = 0
    misses: int = 0
    confidence: float = 0.0
    state: str = "candidate"        # candidate | active | disabled | retired
    # ---- 学习过程的运行期 / 持久化附加信息（都有默认值，不影响上面的公开字段） ----
    runs: list = field(default_factory=list)
    consecutive_misses: int = 0
    last_hit_at: float = 0.0
    app_version: str = ""
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "sig": self.sig,
            "app_id": self.app_id,
            "platform": self.platform,
            "keyword": self.keyword,
            "kind": self.kind,
            "close_attrs": dict(self.close_attrs),
            "container": dict(self.container),
            "texts": list(self.texts),
            "hits": self.hits,
            "misses": self.misses,
            "confidence": round(self.confidence, 4),
            "state": self.state,
            "runs": list(self.runs),
            "consecutive_misses": self.consecutive_misses,
            "last_hit_at": round(self.last_hit_at, 3),
            "app_version": self.app_version,
            "created_at": round(self.created_at, 3),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "PopupEntry":
        try:
            return cls(
                sig=str(raw.get("sig") or ""),
                app_id=str(raw.get("app_id") or ""),
                platform=str(raw.get("platform") or ""),
                keyword=str(raw.get("keyword") or ""),
                kind=str(raw.get("kind") or "close"),
                close_attrs=dict(raw.get("close_attrs") or {}),
                container=dict(raw.get("container") or {}),
                texts=[str(t) for t in (raw.get("texts") or [])][:3],
                hits=max(0, int(raw.get("hits") or 0)),
                misses=max(0, int(raw.get("misses") or 0)),
                confidence=float(raw.get("confidence") or 0.0),
                state=str(raw.get("state") or "candidate"),
                runs=[str(r) for r in (raw.get("runs") or [])],
                consecutive_misses=max(0, int(raw.get("consecutive_misses") or 0)),
                last_hit_at=float(raw.get("last_hit_at") or 0.0),
                app_version=str(raw.get("app_version") or ""),
                created_at=float(raw.get("created_at") or time.time()),
            )
        except (TypeError, ValueError, AttributeError):
            return cls()


@dataclass
class PopupContext:
    """一次执行里与弹窗记忆相关的上下文。"""

    app_id: str = ""
    platform: str = ""
    run_id: str = ""
    mode: str = "replay"           # replay | ai
    enabled: bool = True           # Executor.use_popup_memory
    app_version: str = ""


class PopupMemory:
    """跨运行弹窗记忆库（零模型）。

    Args:
        path: ``popup_memory.json`` 路径；相对路径按 ``cfg.home`` 解析。
        cfg: ``Config`` 或 ``PopupMemoryConfig``。
    """

    def __init__(self, path: Path, cfg: Any) -> None:
        self.cfg = cfg
        self._pc = getattr(cfg, "popup_memory", cfg)
        raw_path = Path(path)
        if not raw_path.is_absolute():
            home = getattr(cfg, "home", None)
            raw_path = (Path(home) / raw_path) if home is not None else raw_path
        self.path = raw_path
        self._entries: dict[str, PopupEntry] = {}
        # 待确认窗口：sig -> {"run_id", "clean_steps", "passed", "at"}
        self._pending: dict[str, dict] = {}
        self.load()

    # -- 配置 -------------------------------------------------------------

    def _opt(self, name: str, default: Any) -> Any:
        return getattr(self._pc, name, default)

    def _env_disabled(self) -> bool:
        return os.environ.get("ATP_POPUP_MEMORY", "").strip() in ("0", "false", "False", "off")

    def enabled_for(self, ctx: PopupContext, for_write: bool = False) -> bool:
        """四类禁用开关 + mode 读写隔离。"""
        if not bool(self._opt("enabled", True)):
            return False
        if self._env_disabled():
            return False
        if not bool(getattr(ctx, "enabled", True)):
            return False
        # Web / Desktop / Electron 完全 no-op：记忆与快路径只在移动端启用
        platform = str(getattr(ctx, "platform", "") or "")
        if platform and not profile_for(platform).popup_memory:
            return False
        app_id = str(getattr(ctx, "app_id", "") or "")
        disabled_apps = [str(a) for a in (self._opt("disabled_apps", []) or [])]
        if app_id and app_id in disabled_apps:
            return False
        mode = str(getattr(ctx, "mode", "replay") or "replay")
        if for_write:
            if bool(self._opt("readonly", False)):
                return False
            return bool(self._opt("write_in_ai_mode", True)) if mode == "ai" else True
        # 读（快路径）：ai 模式只写不读
        if not bool(self._opt("fast_path_enabled", True)):
            return False
        return bool(self._opt("read_in_ai_mode", False)) if mode == "ai" else True

    def _kind_allowed(self, kind: str) -> bool:
        allowed = [str(k) for k in (self._opt("allow_kinds", []) or [])]
        if not allowed:
            return True
        return kind in allowed

    # -- 持久化 -----------------------------------------------------------

    def load(self) -> "PopupMemory":
        """读取记忆文件；损坏 / 缺字段一律降级为空，绝不抛异常。"""
        self._entries = {}
        try:
            if not self.path.exists():
                return self
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("entries"), dict):
                return self
            ttl_days = max(0, int(self._opt("ttl_days", 30) or 30))
            now = time.time()
            for sig, item in raw["entries"].items():
                if not isinstance(item, dict):
                    continue
                entry = PopupEntry.from_dict(item)
                if not entry.sig:
                    entry.sig = str(sig)
                if entry.state not in _STATE_ORDER:
                    entry.state = "candidate"
                # 30 天没命中 / 应用版本变更 → 退回 candidate，重新观察
                if ttl_days and entry.last_hit_at and (
                    now - entry.last_hit_at > ttl_days * 86400
                ) and entry.state == "active":
                    entry.state = "candidate"
                self._entries[entry.sig] = entry
        except Exception:  # noqa: BLE001
            self._entries = {}
        return self

    def save(self) -> bool:
        """原子写（.tmp + os.replace）；readonly / 异常时静默返回 False。"""
        if (not bool(self._opt("enabled", True))
                or self._env_disabled()
                or bool(self._opt("readonly", False))):
            return False
        try:
            payload = {"version": _VERSION,
                       "entries": {sig: e.as_dict() for sig, e in sorted(self._entries.items())}}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- 查询 -------------------------------------------------------------

    def _active_entries(self, ctx: PopupContext) -> list[PopupEntry]:
        out: list[PopupEntry] = []
        for entry in self._entries.values():
            if entry.state != "active":
                continue
            if entry.platform and ctx.platform and entry.platform != ctx.platform:
                continue
            if entry.app_id and ctx.app_id and entry.app_id != ctx.app_id:
                continue
            if entry.app_version and ctx.app_version and entry.app_version != ctx.app_version:
                continue
            if not self._kind_allowed(entry.kind):
                continue
            if entry.confidence < float(self._opt("confidence_min", 0.90) or 0.0):
                continue
            out.append(entry)
        out.sort(key=lambda e: (-e.confidence, -e.hits))
        return out

    def has_active(self, ctx: PopupContext) -> bool:
        """库里是否存在可用于快路径的 active 条目（零抓树，O(条目数)）。"""
        if not self.enabled_for(ctx):
            return False
        return bool(self._active_entries(ctx))

    def find_close_node(self, obs: Any, entry: PopupEntry) -> Any:
        """用条目里的**稳定属性**在当前新鲜树上重新定位关闭节点。

        绝不读取条目里的绝对坐标；返回节点本身，由调用方按当前 bounds 计算点击点。
        """
        root = getattr(obs, "root", None)
        w = int(getattr(obs, "width", 0) or 0)
        h = int(getattr(obs, "height", 0) or 0)
        attrs = entry.close_attrs or {}
        rid = str(attrs.get("a11y_id") or "")
        text = str(attrs.get("text") or "")
        desc = str(attrs.get("desc") or "")
        cls_path = str(attrs.get("cls_path") or "")
        keyword = str(attrs.get("keyword") or entry.keyword or "")

        candidates: list[Any] = []
        for node in (root.walk() if root is not None and hasattr(root, "walk") else []):
            if not _onscreen(node, w, h):
                continue
            label = _label(node)
            if rid:
                if str(getattr(node, "id", "") or "") != rid:
                    continue
            elif text or desc:
                if text and text not in label:
                    continue
                if not text and desc and desc not in label:
                    continue
            elif cls_path:
                if str(getattr(node, "cls", "") or "") not in cls_path:
                    continue
            else:
                continue
            if keyword and keyword not in label and not rid:
                # 有 id 的图标按钮允许无文字；文字通道必须仍带关键词
                continue
            candidates.append(node)
        if not candidates:
            return None
        candidates.sort(key=lambda n: _node_area(n))
        return candidates[0]

    def match_active(self, obs: Any, ctx: PopupContext) -> list[PopupEntry]:
        """当前界面上真正匹配到的 active 条目（按置信度降序）。"""
        if not self.enabled_for(ctx):
            return []
        matched: list[PopupEntry] = []
        for entry in self._active_entries(ctx):
            node = self.find_close_node(obs, entry)
            if node is None:
                continue
            x, y = node.center
            hit = PopupHit(keyword=entry.keyword, text=_label(node)[:40], x=x, y=y,
                           kind=entry.kind, node_cls=str(getattr(node, "cls", "") or ""))
            sig = build_signature(obs, hit, ctx.app_id, ctx.platform)
            if sig and sig == entry.sig:
                matched.append(entry)
        return matched

    def lookup(self, obs: Any, ctx: PopupContext) -> PopupEntry | None:
        """快路径查表：只返回 active、未禁用、且当前屏上确实存在的条目。"""
        matched = self.match_active(obs, ctx)
        return matched[0] if matched else None

    def entry_for(self, sig: str) -> PopupEntry | None:
        """按签名取条目，供点击后「签名节点是否消失」校验使用。"""
        return self._entries.get(str(sig or ""))

    def container_of(self, obs: Any, entry: PopupEntry) -> Any:
        """返回条目对应的弹窗容器节点（用于「点击后签名节点消失」校验）。"""
        node = self.find_close_node(obs, entry)
        if node is None:
            return None
        x, y = node.center
        hit = PopupHit(keyword=entry.keyword, text="", x=x, y=y, kind=entry.kind)
        _best, container, _chain, _texts, _free = _hit_context(obs, hit)
        return container

    def signature_present(self, obs: Any, entry: PopupEntry, ctx: PopupContext) -> bool:
        """签名节点是否还在（点击后必须变成 False 才算真的关掉）。"""
        return entry in self.match_active(obs, ctx)

    # -- 学习 -------------------------------------------------------------

    def _new_entry(self, sig: str, obs: Any, hit: Any, ctx: PopupContext) -> PopupEntry:
        node, container, chain, texts, danger_free = _hit_context(obs, hit)
        return PopupEntry(
            sig=sig,
            app_id=ctx.app_id,
            platform=ctx.platform,
            keyword=str(getattr(hit, "keyword", "") or ""),
            kind=str(getattr(hit, "kind", "") or "close"),
            close_attrs=_close_attrs(node, chain, str(getattr(hit, "keyword", "") or ""))
            if node is not None else {},
            container={
                "rect": [round(v, 2) for v in normalized_rect(
                    getattr(container, "bounds", ()),
                    int(getattr(obs, "width", 0) or 0),
                    int(getattr(obs, "height", 0) or 0))],
                "cls": str(getattr(container, "cls", "") or ""),
                "id": str(getattr(container, "id", "") or ""),
                "danger_free": bool(danger_free),
            } if container is not None else {},
            texts=list(texts),
            state="candidate",
            app_version=ctx.app_version,
        )

    def observe_success(self, entry: Any, ctx: PopupContext, run_id: str = "") -> PopupEntry:
        """全量决策成功：登记/更新候选条目，并开启「后续 2 步无偏离」确认窗口。

        ``entry`` 可以是 :class:`PopupEntry`、签名字符串，或
        ``{"sig":..., "obs":..., "hit":...}`` 三元组字典（popup.py 用后者）。
        """
        if not self.enabled_for(ctx, for_write=True):
            return entry if isinstance(entry, PopupEntry) else PopupEntry()
        obs: Any = None
        hit: Any = None
        sig = ""
        if isinstance(entry, PopupEntry):
            sig = entry.sig
        elif isinstance(entry, str):
            sig = entry
        elif isinstance(entry, dict):
            sig = str(entry.get("sig") or "")
            obs = entry.get("obs")
            hit = entry.get("hit")
        if not sig and obs is not None and hit is not None:
            sig = build_signature(obs, hit, ctx.app_id, ctx.platform)
        if not sig:
            return PopupEntry()

        current = self._entries.get(sig)
        if current is None:
            if obs is not None and hit is not None:
                current = self._new_entry(sig, obs, hit, ctx)
            else:
                current = PopupEntry(sig=sig, app_id=ctx.app_id, platform=ctx.platform,
                                     kind=str(getattr(hit, "kind", "close") or "close"))
            self._entries[sig] = current
        if ctx.app_version and not current.app_version:
            current.app_version = ctx.app_version
        # 曾因 miss 被禁用/退役的条目不因一次成功自动复活，只恢复成 candidate
        if current.state in ("disabled", "retired"):
            current.state = "candidate"
            current.consecutive_misses = 0
        self._pending[sig] = {
            "run_id": str(run_id or ctx.run_id or ""),
            "clean_steps": 0,
            "at": time.time(),
        }
        self.save()
        return current

    def confirm_window(self, sig: str = "", run_id: str = "", passed: bool = True,
                       deviation: bool = False) -> list[PopupEntry]:
        """步骤收尾时推进确认窗口；返回本次真正升级 / 计数的条目。

        ``passed=False`` 或 ``deviation=True`` 的窗口直接作废（绝不沉淀）。
        """
        committed: list[PopupEntry] = []
        for key, pending in list(self._pending.items()):
            if sig and key != sig:
                continue
            if not passed or deviation:
                self._pending.pop(key, None)
                continue
            pending["clean_steps"] = int(pending.get("clean_steps", 0)) + 1
            if pending["clean_steps"] < 2:
                continue
            self._pending.pop(key, None)
            entry = self._entries.get(key)
            if entry is None:
                continue
            entry.hits += 1
            rid = str(pending.get("run_id") or run_id or "")
            if rid and rid not in entry.runs:
                entry.runs.append(rid)
            entry.last_hit_at = time.time()
            entry.consecutive_misses = 0
            total = entry.hits + entry.misses
            entry.confidence = round(entry.hits / total, 4) if total else 0.0
            required = int(self._opt("icon_min_hits", 3) or 3) if entry.kind == "close_id" \
                else int(self._opt("min_hits", 2) or 2)
            if (entry.hits >= required and len(entry.runs) >= 2
                    and entry.confidence >= float(self._opt("confidence_min", 0.90) or 0.0)
                    and self._kind_allowed(entry.kind)):
                entry.state = "active"
            committed.append(entry)
        if committed:
            self.save()
        return committed

    def miss(self, entry: PopupEntry, reason: str = "") -> PopupEntry:
        """快路径失败：连续 2 次禁用，连续 3 次退役；置信度同步下降。"""
        if entry is None or not getattr(entry, "sig", ""):
            return entry
        current = self._entries.get(entry.sig) or entry
        current.misses += 1
        current.consecutive_misses += 1
        total = current.hits + current.misses
        current.confidence = round(current.hits / total, 4) if total else 0.0
        if current.consecutive_misses >= int(self._opt("retire_after_misses", 3) or 3):
            current.state = "retired"
        elif current.consecutive_misses >= int(self._opt("disable_after_misses", 2) or 2):
            current.state = "disabled"
        self._entries[current.sig] = current
        self._pending.pop(current.sig, None)
        self.save()
        return current

    def retire(self, entry: PopupEntry, reason: str = "") -> None:
        """显式退役（例如应用版本变更或人工清理），仅改状态不抛异常。"""
        if entry is None or not getattr(entry, "sig", ""):
            return
        current = self._entries.get(entry.sig) or entry
        current.state = "retired"
        self._entries[current.sig] = current
        self.save()

    def note_version_change(self, ctx: PopupContext) -> None:
        """应用版本变化时，相关 active 条目退回 candidate 重新观察。"""
        version = str(getattr(ctx, "app_version", "") or "")
        if not version:
            return
        changed = False
        for entry in self._entries.values():
            if entry.app_id and ctx.app_id and entry.app_id != ctx.app_id:
                continue
            if entry.app_version and entry.app_version != version:
                entry.state = "candidate"
                entry.app_version = version
                changed = True
        if changed:
            self.save()

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[PopupEntry]:
        return list(self._entries.values())
