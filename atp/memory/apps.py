"""被测应用的包名台账（app registry）。

移动端用例的第一句永远是「打开 xx」。包名（鸿蒙叫 bundleName）是这句话唯一的硬依赖：
没有它 `am start` 无从下手，首跑只能在当前那一屏瞎点——真机上就撞过：用户说
「打开【示例聊天应用】，消息首页点右上角【+】…」，控制台没解析出包名，首跑只在屏幕上
戳了一下「钱包」就宣布成功，还沉淀成了一条 1 步用例。

所以把「应用名 → 包名」沉淀成一份可读、可手改的文件 `apps.json`：

    {
      "version": 1,
      "android": {
        "com.example.chatapp.desktop": {"name": "示例聊天应用", "activity": "com.example.chatapp.desktop/.MainActivity",
                          "aliases": ["示例聊天应用"], "source": "device", "screen": "1260x2800",
                          "updated_at": 1794299999.0}
      },
      "harmony": { ... }
    }

两个平台分区存放、互不影响——一台机器上安卓真机和鸿蒙真机经常同时接着。
查找顺序永远是：**配置文件 → 设备上找 → 写回配置文件**；
找到之后的执行永远是**冷启动**（先杀进程，再启动），见 `Executor._cold_start`。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# 名字里常见的装饰符号：用户在自然语言里喜欢写【示例聊天应用】「示例聊天应用」
_DECOR = "【】[]()（）「」『』\"'“”‘’ \t\u3000"
# 明显不是应用名的词（"打开应用"/"打开页面" 这类套话）
_STOP_WORDS = {
    "应用", "app", "app应用", "软件", "程序", "页面", "首页", "界面", "手机",
    "设备", "系统", "浏览器", "他", "它", "我",
}
# 「打开/启动/运行/进入 + 名字」，名字里允许中文、字母、数字、-、_、·
_OPEN_RE = re.compile(
    r"(?:打开|启动|运行|进入|拉起)\s*"
    r"[【\[「『\"'“”]?\s*"
    r"([A-Za-z0-9\u4e00-\u9fa5][A-Za-z0-9\u4e00-\u9fa5\-_.·]{1,15})"
)
# 句末常见分隔符：名字到这些字符就停
_NAME_TAIL = "，,。；;：:、！!？?（）()【】[]「」『』\"'“” \t\n"
# 包名的形状：至少两级、首段是字母
_PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")


def norm_name(text: str) -> str:
    """把名字归一化成可比较的形式：去装饰符号、去空格、忽略大小写。"""
    s = str(text or "")
    for ch in _DECOR:
        s = s.replace(ch, "")
    return s.strip().casefold()


def looks_like_package(text: str) -> bool:
    return bool(_PKG_RE.match(str(text or "").strip()))


def guess_app_names(text: str) -> list[str]:
    """从自然语言里猜「要打开的应用名」，按出现顺序返回候选。

    「打开【示例聊天应用】，消息首页点击右上角的【+】…」→ ['示例聊天应用']
    「打开示例视频应用，搜索示例演员并播放」        → ['示例视频应用']
    猜不出来就返回空列表——猜错比不猜更糟（会去启动一个无关的应用）。
    """
    out: list[str] = []
    for m in _OPEN_RE.finditer(str(text or "")):
        raw = m.group(1)
        # 名字后面可能还粘着逗号句号，按 _NAME_TAIL 再切一刀
        for ch in _NAME_TAIL:
            raw = raw.split(ch)[0]
        raw = raw.strip(_DECOR).strip()
        if len(raw) < 2 or norm_name(raw) in _STOP_WORDS:
            continue
        if raw.lower().startswith(("app", "应用")):
            continue
        if raw not in out:
            out.append(raw)
    return out


@dataclass
class AppEntry:
    """一个被测应用：名字（用户怎么叫）+ 包名（设备怎么认）。"""

    name: str
    app_id: str
    # Android 是入口组件 `pkg/.Act`；鸿蒙是 ability 名。留着可以少一次设备查询
    activity: str = ""
    aliases: list[str] = field(default_factory=list)
    source: str = "manual"       # manual | case | device | user
    screen: str = ""             # 第一次发现时的画布，换机器排障时有用
    updated_at: float = 0.0

    def labels(self) -> list[str]:
        """所有能指代它的写法。"""
        return [self.name, *self.aliases]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "activity": self.activity,
            "aliases": list(self.aliases),
            "source": self.source,
            "screen": self.screen,
            "updated_at": round(self.updated_at or time.time(), 3),
        }

    @classmethod
    def from_dict(cls, app_id: str, raw: dict[str, Any]) -> "AppEntry":
        return cls(
            name=str(raw.get("name") or app_id),
            app_id=app_id,
            activity=str(raw.get("activity") or ""),
            aliases=[str(a) for a in (raw.get("aliases") or []) if str(a).strip()],
            source=str(raw.get("source") or "manual"),
            screen=str(raw.get("screen") or ""),
            updated_at=float(raw.get("updated_at") or 0.0),
        )


class AppRegistry:
    """`apps.json` 的读写与查询。文件是唯一真相，随时可以手工编辑。"""

    VERSION = 1

    def __init__(self, path: str | Path, *, enabled: bool = True,
                 readonly: bool = False):
        self.path = Path(path)
        self.enabled = enabled
        # readonly 给测试/只读介质用：查得到但绝不落盘
        self.readonly = readonly
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._loaded = False

    # ---------- 加载 / 保存 ----------
    @classmethod
    def from_config(cls, cfg, *, readonly: bool = False) -> "AppRegistry":
        apps_cfg = getattr(cfg, "apps", None)
        enabled = bool(getattr(apps_cfg, "enabled", True))
        path = getattr(cfg, "apps_file", None)
        if path is None:                       # 极简 Config（测试里常见）
            path = Path(getattr(cfg, "home", ".")) / "apps.json"
        return cls(path, enabled=enabled, readonly=readonly)

    def load(self) -> "AppRegistry":
        self._loaded = True
        if not self.path.exists():
            self._data = {}
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:                      # noqa: BLE001 —— 手改坏了不能让执行挂掉
            self._data = {}
            return self
        data: dict[str, dict[str, dict[str, Any]]] = {}
        for platform, table in (raw or {}).items():
            if platform.startswith("_") or not isinstance(table, dict):
                continue
            data[str(platform)] = {str(k): v for k, v in table.items() if isinstance(v, dict)}
        self._data = data
        return self

    def save(self) -> None:
        if self.readonly:
            return
        payload: dict[str, Any] = {"version": self.VERSION}
        for platform in sorted(self._data):
            payload[platform] = dict(sorted(self._data[platform].items()))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.path)             # 原子替换：执行中被打断也不会写半个文件

    # ---------- 查询 ----------
    def entries(self, platform: str) -> list[AppEntry]:
        if not self._loaded:
            self.load()
        table = self._data.get(platform) or {}
        return [AppEntry.from_dict(k, v) for k, v in sorted(table.items())]

    def get(self, platform: str, key: str) -> AppEntry | None:
        """按包名 / 应用名 / 别名查，命中不了再做包含匹配。

        包含匹配放在最后且要求两边都 ≥2 个字：`芒` 这种单字会同时命中
        「示例视频应用」「示例视频应用极速版」，宁可查不到让用户补全，也不能瞎认。
        """
        if not self._loaded:
            self.load()
        table = self._data.get(platform) or {}
        raw_key = str(key or "").strip()
        if not raw_key or not table:
            return None

        # 1) 原样就是包名（最常见：用例里存的就是包名）
        if raw_key in table:
            return AppEntry.from_dict(raw_key, table[raw_key])
        key_norm = norm_name(raw_key)
        if not key_norm:
            return None

        best: tuple[int, int, AppEntry] | None = None   # (score, -len, entry)
        for app_id, raw in table.items():
            entry = AppEntry.from_dict(app_id, raw)
            score = 0
            for cand in entry.labels():
                c = norm_name(cand)
                if not c:
                    continue
                if c == key_norm:
                    score = max(score, 3)          # 名称/别名完全一致
                elif min(len(c), len(key_norm)) >= 2 and (c.startswith(key_norm)
                                                          or key_norm.startswith(c)):
                    score = max(score, 2)          # 前缀：芒果 ⊂ 示例视频应用
                elif min(len(c), len(key_norm)) >= 2 and (key_norm in c or c in key_norm):
                    score = max(score, 1)
            # 包名里带关键词也算（用户可能直接说 videoapp / samplechat）
            if len(key_norm) >= 3 and key_norm in app_id.casefold():
                score = max(score, 1)
            if score and (best is None or score > best[0]
                          or (score == best[0] and len(entry.name) < -best[1])):
                best = (score, -len(entry.name), entry)
        return best[2] if best else None

    # ---------- 写入 ----------
    def remember(self, platform: str, name: str, app_id: str, *,
                 activity: str = "", aliases: Iterable[str] = (),
                 source: str = "device", screen: str = "") -> AppEntry:
        """把「名字 → 包名」记下来；已存在就合并，不覆盖手工填的信息。"""
        if not self._loaded:
            self.load()
        app_id = str(app_id or "").strip()
        if "/" in app_id and looks_like_package(app_id.split("/")[0]):
            # 用例里可能写的是 `pkg/.MainActivity` 或 `bundle/EntryAbility`：
            # 台账只把"包名"当键，入口组件单独存，查起来才干净。
            # 注意：只有"首段长得像包名"才切，否则桌面端的启动路径
            # `C:/Program Files/...` 会被误切成 `C:`（见 remember 的 Windows 路径场景）。
            pkg, _, act = app_id.partition("/")
            app_id = pkg.strip()
            activity = activity or act.strip()
        if not app_id:
            raise ValueError("包名不能为空")
        table = self._data.setdefault(platform, {})
        raw = dict(table.get(app_id) or {})
        existed = bool(raw)
        entry = (AppEntry.from_dict(app_id, raw) if existed
                 else AppEntry(name="", app_id=app_id, source=source))
        entry.app_id = app_id

        new_name = str(name or "").strip()
        if new_name:
            if not entry.name:
                entry.name = new_name
            elif norm_name(new_name) != norm_name(entry.name):
                # 用户这次是另一种叫法（示例视频应用 / SampleVideoApp），当别名留着
                if all(norm_name(new_name) != norm_name(a) for a in entry.aliases):
                    entry.aliases.append(new_name)
        if not entry.name:
            entry.name = app_id
        for alias in aliases:
            alias = str(alias or "").strip()
            if alias and norm_name(alias) != norm_name(entry.name) \
                    and all(norm_name(alias) != norm_name(a) for a in entry.aliases):
                entry.aliases.append(alias)

        entry.activity = str(activity or entry.activity or "")
        # 手工登记的条目不被自动发现改写成 device/case——不然用户标的信息会悄悄丢
        if existed and entry.source == "manual":
            pass
        else:
            entry.source = source
        entry.screen = str(screen or entry.screen or "")
        entry.updated_at = time.time()

        table[app_id] = entry.as_dict()
        if not self.readonly:
            self.save()
        return entry

    def forget(self, platform: str, key: str) -> bool:
        entry = self.get(platform, key)
        if entry is None:
            return False
        self._data.get(platform, {}).pop(entry.app_id, None)
        if not self.readonly:
            self.save()
        return True


# ---------------------------------------------------------------- 解析
def _emit(on_event, **event) -> None:
    if on_event is None:
        return
    try:
        on_event({"type": "app_resolved", **event})
    except Exception:      # noqa: BLE001 —— 事件通道坏了不该影响执行
        pass


def resolve_app(key: str, *, registry: AppRegistry, driver=None,
                platform: str = "", discover: bool = True, screen: str = "",
                on_event=None) -> AppEntry | None:
    """把「应用名」解析成包名：**配置文件优先，其次去设备上找，找到就写回配置**。

    返回 None 表示没找到——调用方应保持原有行为（别硬编一个包名去启动），
    同时给用户一条能自己补的提示。
    """
    raw = str(key or "").strip()
    if not raw:
        return None
    plat = platform or getattr(driver, "platform", "") or ""
    if not registry.enabled or not plat:
        return None

    hit = registry.get(plat, raw)
    if hit is not None:
        _emit(on_event, name=hit.name or raw, app_id=hit.app_id,
              activity=hit.activity, source="config", platform=plat)
        return hit

    if not discover or driver is None:
        return None
    finder = getattr(driver, "find_app", None)
    if finder is None:
        return None
    try:
        found = finder(raw)
    except Exception:      # noqa: BLE001 —— 找应用是尽力而为，失败不拦执行
        found = None
    if not found:
        return None
    app_id, activity = found if isinstance(found, tuple) else (str(found), "")
    entry = registry.remember(plat, raw, app_id, activity=activity,
                              source="device", screen=screen)
    _emit(on_event, name=entry.name, app_id=entry.app_id, activity=entry.activity,
          source="device", platform=plat)
    return entry


def resolve_from_text(text: str, **kwargs) -> AppEntry | None:
    """从整句自然语言里找应用：先猜名字，再按先后顺序逐个解析。"""
    for name in guess_app_names(text):
        entry = resolve_app(name, **kwargs)
        if entry is not None:
            return entry
    return None
