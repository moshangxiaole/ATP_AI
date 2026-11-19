"""配置加载：JSON 文件是基线，ATP_* 环境变量可逐项覆盖。
所有层只读 Config，不在各自模块里散落 os.environ 读取。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _env(name: str, default: str = "") -> str:
    """读取环境变量，未设置时返回默认值。"""
    return os.environ.get(name, default)


def _env_flag(name: str, default: bool) -> bool:
    """``ATP_XXX=0|1`` 覆盖布尔开关；没设置或值不合法时保留默认值。"""
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return default


@dataclass
class ModelConfig:
    """OpenAI 兼容的模型配置（云端 / 本地 vLLM、Ollama、LM Studio 均可）。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    vision_model: str = ""
    temperature: float = 0.0
    max_tokens: int = 2048
    # 本地模型（尤其 CPU 推理 / 7B 以下）首 token 常常要十几秒，默认 60s 不够。
    timeout_s: int = 60
    # 本地小模型多数没有视觉能力：关掉之后不再尝试"截图指位置/看图断言"，
    # 免得每步都拿一张图去换一次报错。
    vision: bool = True
    # 调用频次限制（默认开）。免费额度的模型限制的是每分钟请求数，而 AI 执行
    # 每步都要打一次模型，不压频率十几秒就 429；把上限调到付费额度或用本地
    # 模型时可以关掉换取速度。
    throttle: bool = True
    min_interval_ms: int = 1200      # 两次调用之间的最小间隔
    max_per_min: int = 20            # 每分钟最多调用次数（<=0 不限）

    @property
    def host(self) -> str:
        try:
            from urllib.parse import urlparse

            return (urlparse(self.base_url).hostname or "").strip().lower()
        except Exception:  # noqa: BLE001
            return ""

    @property
    def is_local(self) -> bool:
        """是不是本机 / 局域网里的模型服务。

        本机的 Ollama、LM Studio、vLLM 通常不需要 API Key，而且**绝对不能走系统
        代理**（很多机器上配了 http_proxy，127.0.0.1 一过代理就 502/连不上）。
        """
        host = self.host
        if not host:
            return False
        if host in ("localhost", "::1", "0.0.0.0", "host.docker.internal"):
            return True
        parts = host.split(".")
        if len(parts) != 4:
            return False
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return False
        if any(n < 0 or n > 255 for n in nums):
            return False
        # 127.0.0.0/8、10/8、192.168/16、172.16/12
        return (nums[0] == 127 or nums[0] == 10
                or (nums[0] == 192 and nums[1] == 168)
                or (nums[0] == 172 and 16 <= nums[1] <= 31))

    @property
    def configured(self) -> bool:
        # 本地/局域网服务允许不带 Key（Ollama、LM Studio 的默认行为）；
        # 公网服务仍然要求 Key，避免"看着配好了、一调 401"。
        return bool(self.base_url and self.model and (self.api_key or self.is_local))


@dataclass
class RemoteProviderConfig:
    """远程真机池接入配置。

    kind=adb_tcp    : 通过 adb connect host:port 接入，hosts 里逐条列出
    kind=http_farm  : 通用 HTTP 农机房，遵循 list / allocate / release 三个约定端点
    """

    name: str = "default"
    kind: str = "adb_tcp"
    hosts: list[str] = field(default_factory=list)
    base_url: str = ""
    token: str = ""
    platform: str = "android"


@dataclass
class VisionConfig:
    """视觉兜底配置。

    只在"元素定位不可用"时才走这条路（canvas、纯图界面、被屏蔽的无障碍树），
    因为图像识别比树定位贵一到两个数量级，能用树就绝不用图。
    """

    enabled: bool = True
    # 命中阈值：TM_CCOEFF_NORMED 口径下 0.8 已经能过滤掉绝大多数误命中
    threshold: float = 0.82
    # 搜索窗口放大比例：归一化坐标允许 ±pad 的漂移（窗口标题栏、滚动位置等）
    pad: float = 0.22
    # 多尺度搜索范围：以 (当前尺寸/基准尺寸) 为中心，±ratio 内逐档搜索
    scale_range: float = 0.18
    scale_steps: int = 7
    # 无稳定属性的节点一律额外沉淀一张模板图（多一层保险，代价是几 KB 磁盘）
    capture_for_weak_nodes: bool = True


@dataclass
class PopupConfig:
    """弹窗自动处置配置。

    移动端跑真实 App，十步里有三步会撞上开屏广告、更新提示、青少年模式、权限申请、
    营销浮层。这些都不是用例目标，但会把手动回放卡死。策略只有两条：
    **能安全关掉的就关掉，关不掉的不硬关**（比如"加入会员 跳过此广告"这种诱导位，
    点下去会跳会员页，比不点更糟）。
    """

    enabled: bool = True
    # 单步最多处置几个弹窗，防止"关不掉的弹窗"把一步拖成死循环
    max_per_step: int = 3
    # 处置后等界面反应的毫秒数
    settle_ms: int = 600
    # 额外的用户自定义关键词（品牌特有的"我知道了""稍后再说"等）
    extra_close_words: list[str] = field(default_factory=list)


@dataclass
class ColdStartConfig:
    """移动端冷启动：每次"打开应用"之前先结束进程。

    只 `am start` / `aa start` 是不够的：App 还活在后台时，启动往往只是把
    上一次停留的页面拉回前台——上次停在详情页，这次第一步找首页搜索入口就 fail。
    用例必须自己造出确定的初始状态，所以"杀干净再进"是移动端打开应用的默认语义，
    而不是某条用例的写法问题。

    留在用例里的显式 `terminate_app` 步骤不算重复：执行器看到上一步已经杀过，
    就不再补一刀（见 Executor._cold_start）。
    """

    enabled: bool = True
    platforms: list[str] = field(default_factory=lambda: ["android", "harmony", "ios"])
    # force-stop 之后等系统回收的毫秒数。杀完立刻 start 偶发会捡到旧任务栈
    settle_ms: int = 800
    # 启动指令返回 ≠ 界面可见：`am start` 是异步的，紧接着抓树会拿到转场中的白屏，
    # 后续点击/滑动全落空（真机实测：设置页在 s02 开始时还是空白，滑两下毫无反应）。
    # 冷启动后等这么一会儿再干正事，比"每步都猜界面好了没"便宜得多。
    launch_settle_ms: int = 1800


@dataclass
class AppsConfig:
    """被测应用台账（`apps.json`）的开关与查找预算。

    移动端「打开 xx」必须有包名，否则 `am start` 无从下手。台账把
    「应用名 → 包名」沉淀成文件：先查文件，查不到再去设备上找，找到就写回。
    """

    enabled: bool = True
    # 设备上找不到时，最多翻几页桌面（从当前页往后翻，含文件夹里的一层）
    scan_pages: int = 6
    # 一页里最多打开几个文件夹找应用，防止把桌面翻个底朝天
    scan_folders: int = 3
    # 找应用的整体时间上限：找不应用名就该早点失败，不能把一步拖成几分钟
    timeout_s: float = 90.0


@dataclass
class ScrollFindConfig:
    """找不到目标时滑动搜索。

    消息列表里的群聊、商品列表里的条目、设置页深处的开关，首屏本来就不展示。
    这时候"定位失败"不是定位器坏了，而是目标不在视野里：该做的是滑动，
    而不是让 AI 猜一个不存在的节点（猜出来的定位器回放必然错，还会污染用例库）。

    顺序上先滑再修：滑动是零成本的，AI 修复一次几十秒还未必对。
    """

    enabled: bool = True
    platforms: list[str] = field(default_factory=lambda: ["android", "harmony", "ios"])
    # 单方向最多滑几次（小步一屏的三分之一，所以比"一次一屏"要多几步）；到底/到顶会提前停。
    # 覆盖范围 ≈ max_swipes × swipe_ratio ≈ 4 屏，和"一次一屏"的老口径相当
    max_swipes: int = 12
    # 每次滑动后等界面稳定再抓树的毫秒数
    settle_ms: int = 450
    # 一次滑动的手势时长。太快（260ms 拖 1400px）会被系统当"甩"，
    # 一下滑过两三屏，目标正好卡在两次抓树之间就被跳过了。
    # 700ms 是"匀速拖"，抬手时几乎不带惯性
    swipe_ms: int = 700
    # 一次滑动翻过「容器可见高度」的多少。0.35 = 只翻三分之一屏，
    # 上一屏的内容还剩三分之二留在视野里，目标绝不会整块滑过去。
    # 早先按 0.55 走，一屏多的距离直接跨过目标行，越滑越找不到
    swipe_ratio: float = 0.35
    # 连续几次滑动界面完全没变化就认为到底了
    edge_stop: int = 2
    # 扫描方向：up=手指上滑（看下面的内容），down=手指下滑（看上面的内容）
    directions: list[str] = field(default_factory=lambda: ["up", "down"])
    # 左右滑默认关闭：横滑太容易触发侧滑删除、翻页、切 tab，副作用不可控
    horizontal: bool = False
    # 节点树没报 scrollable、也没有 scroll 类控件时，是否仍然硬着头皮滑
    assume_scrollable: bool = False


@dataclass
class PageReadyConfig:
    """移动端页面「完全加载完成」门禁。

    等待期间只做树-only 轮询（绝不截图），稳定后才取唯一一张截图。
    旧的四处 ``settle_ms`` 值不变，语义从「固定 sleep」改成「本次等待上限 ceiling」。
    """

    enabled: bool = True
    platforms: list[str] = field(default_factory=lambda: ["android", "harmony", "ios"])
    min_wait_ms: int = 150
    max_wait_ms: int = 3000
    launch_max_wait_ms: int = 5000
    poll_interval_ms: int = 150
    poll_max_ms: int = 400
    stable_samples: int = 2
    max_tree_polls: int = 8
    node_delta_ratio: float = 0.10
    min_nodes: int = 20
    loading_markers: list[str] = field(default_factory=lambda: [
        "loading", "skeleton", "shimmer", "spinner", "placeholder",
        "加载中", "正在加载", "请稍候", "拼命加载", "下拉刷新", "缓冲中",
    ])
    dynamic_markers: list[str] = field(default_factory=lambda: [
        "loading", "progress", "skeleton", "shimmer", "placeholder", "spinner",
        "carousel", "banner", "countdown", "timestamp", "marquee",
        "加载中", "正在加载", "请稍候", "刷新中", "加载更多", "轮播",
        "倒计时", "剩余时间", "播放中", "缓冲中",
    ])
    reuse_window_ms: int = 500
    carry_stable_ms: int = 1500     # 上一轮门禁的稳定指认可充当本轮第 1 拍（省一次全树抓取）
    timeout_policy: str = "fail_closed"


@dataclass
class PopupMemoryConfig:
    """跨运行弹窗长程记忆：下次遇到同一弹窗不经决策直接安全关掉。"""

    enabled: bool = True
    fast_path_enabled: bool = True
    min_hits: int = 2
    icon_min_hits: int = 3          # close_id 类图标按钮要求 K=3
    confidence_min: float = 0.90
    disable_after_misses: int = 2
    retire_after_misses: int = 3
    ttl_days: int = 30
    disabled_apps: list[str] = field(default_factory=list)
    readonly: bool = False
    allow_kinds: list[str] = field(default_factory=lambda: [
        "skip", "close", "close_id", "allow",
    ])
    read_in_ai_mode: bool = False   # mode="ai" 只写不读
    write_in_ai_mode: bool = True
    path: str = "popup_memory.json"  # 项目根，与 apps.json 同级


@dataclass
class DeviationConfig:
    """误触 / 进入非步骤页的偏离检测与自动回退。"""

    enabled: bool = True
    auto_back: bool = True
    sim_threshold: float = 0.35
    recover_match_threshold: float = 0.80
    coarse_sig_min_nodes: int = 8
    detect_timeout_ms: int = 1500
    max_back_per_step: int = 2
    max_back_per_run: int = 3
    retry_current_step: int = 1
    irreversible_markers: list[str] = field(default_factory=lambda: [
        "支付", "提交", "下单", "删除", "转账", "注销", "确认支付", "立即购买",
        "发送", "提交订单", "支付成功", "提交成功", "订单已生成",
    ])
    block_actions: list[str] = field(default_factory=lambda: [
        "back", "home", "terminate_app", "launch_app", "open_url",
    ])
    persist_stack: bool = False     # 页面栈只在内存里，不写产物
    frame_dedupe: bool = True       # 同页操作（输入/开关/滚动）不新增帧，只刷新栈顶


@dataclass
class Config:
    """运行时配置：合并 atp.config.json 与 ATP_* 环境变量，供所有层读取。"""
    home: Path = field(default_factory=lambda: Path.cwd())
    artifacts_dir: str = "artifacts"
    cases_dir: str = "cases"
    db_path: str = "data/atp.db"
    apps_path: str = "apps.json"
    adb: str = "adb"
    hdc: str = "hdc"
    model: ModelConfig = field(default_factory=ModelConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    popup: PopupConfig = field(default_factory=PopupConfig)
    cold_start: ColdStartConfig = field(default_factory=ColdStartConfig)
    apps: AppsConfig = field(default_factory=AppsConfig)
    scroll_find: ScrollFindConfig = field(default_factory=ScrollFindConfig)
    page_ready: PageReadyConfig = field(default_factory=PageReadyConfig)
    popup_memory: PopupMemoryConfig = field(default_factory=PopupMemoryConfig)
    deviation: DeviationConfig = field(default_factory=DeviationConfig)
    remote_providers: list[RemoteProviderConfig] = field(default_factory=list)

    # 执行策略
    default_timeout_ms: int = 15000
    allow_healing: bool = True
    ai_step_budget: int = 200
    # AI 首跑熔断：真机跑过 31 步还在原地换动作打转，必须有硬性天花板。
    # - ai_max_seconds：单次 AI 执行的最长墙钟时间，超过直接判失败收尾；
    # - ai_stall_steps：连续 N 个已执行步骤页面签名无变化（提示词压不住的
    #   "换着花样原地转"），判 stalled 中止。
    ai_max_seconds: int = 600
    ai_stall_steps: int = 6
    screenshot_on_success: bool = True
    bbox_tolerance: int = 12
    # 执行记录是否落库（离线压测时可以关掉，省 IO）
    persist_runs: bool = True

    # ---------- 路径 ----------
    @property
    def artifacts(self) -> Path:
        p = self.home / self.artifacts_dir
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def cases(self) -> Path:
        p = self.home / self.cases_dir
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def assets(self) -> Path:
        """视觉模板图目录：模板必须跟用例一起走版本管理，不能放 artifacts。"""
        p = self.cases / "assets"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_file(self) -> Path:
        p = self.home / self.db_path
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def apps_file(self) -> Path:
        """应用台账文件：包名/应用名的唯一真相，跟用例一起做版本管理。"""
        return self.home / self.apps_path

    @property
    def popup_memory_file(self) -> Path:
        """弹窗记忆文件：项目根，与 apps.json 同级；不进 cases/、不进 artifacts/。"""
        p = Path(self.popup_memory.path)
        return p if p.is_absolute() else self.home / p

    def run_dir(self, run_id: str) -> Path:
        p = self.artifacts / run_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ---------- 加载 ----------
    @classmethod
    def load(cls, home: str | Path | None = None, config_file: str | None = None) -> "Config":
        home = Path(home or os.getcwd())
        cfg = cls(home=home)

        path = Path(config_file) if config_file else home / "atp.config.json"
        if path.exists():
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            cfg.artifacts_dir = raw.get("artifacts_dir", cfg.artifacts_dir)
            cfg.cases_dir = raw.get("cases_dir", cfg.cases_dir)
            cfg.db_path = raw.get("db_path", cfg.db_path)
            cfg.apps_path = raw.get("apps_path", cfg.apps_path)
            cfg.adb = raw.get("adb", cfg.adb)
            cfg.hdc = raw.get("hdc", cfg.hdc)
            cfg.default_timeout_ms = raw.get("default_timeout_ms", cfg.default_timeout_ms)
            cfg.allow_healing = raw.get("allow_healing", cfg.allow_healing)
            cfg.ai_step_budget = raw.get("ai_step_budget", cfg.ai_step_budget)
            cfg.ai_max_seconds = raw.get("ai_max_seconds", cfg.ai_max_seconds)
            cfg.ai_stall_steps = raw.get("ai_stall_steps", cfg.ai_stall_steps)
            cfg.persist_runs = raw.get("persist_runs", cfg.persist_runs)
            if "model" in raw:
                cfg.model = ModelConfig(**raw["model"])
            if "vision" in raw:
                cfg.vision = VisionConfig(**{**cfg.vision.__dict__, **raw["vision"]})
            if "popup" in raw:
                cfg.popup = PopupConfig(**{**cfg.popup.__dict__, **raw["popup"]})
            if "cold_start" in raw:
                cfg.cold_start = ColdStartConfig(
                    **{**cfg.cold_start.__dict__, **raw["cold_start"]})
            if "apps" in raw:
                cfg.apps = AppsConfig(**{**cfg.apps.__dict__, **raw["apps"]})
            if "scroll_find" in raw:
                cfg.scroll_find = ScrollFindConfig(
                    **{**cfg.scroll_find.__dict__, **raw["scroll_find"]})
            if "page_ready" in raw:
                cfg.page_ready = PageReadyConfig(
                    **{**cfg.page_ready.__dict__, **raw["page_ready"]})
            if "popup_memory" in raw:
                cfg.popup_memory = PopupMemoryConfig(
                    **{**cfg.popup_memory.__dict__, **raw["popup_memory"]})
            if "deviation" in raw:
                cfg.deviation = DeviationConfig(
                    **{**cfg.deviation.__dict__, **raw["deviation"]})
            for item in raw.get("remote_providers", []):
                cfg.remote_providers.append(RemoteProviderConfig(**item))

        # 环境变量优先级最高
        cfg.model.base_url = _env("ATP_MODEL_BASE_URL", cfg.model.base_url)
        cfg.model.api_key = _env("ATP_MODEL_API_KEY", cfg.model.api_key)
        cfg.model.model = _env("ATP_MODEL_NAME", cfg.model.model)
        cfg.model.vision_model = _env("ATP_VISION_MODEL", cfg.model.vision_model or cfg.model.model)
        # 本地模型常用环境变量临时切换，不必改 atp.config.json
        try:
            cfg.model.timeout_s = int(_env("ATP_MODEL_TIMEOUT", str(cfg.model.timeout_s)))
        except (TypeError, ValueError):
            pass
        cfg.model.vision = _env_flag("ATP_MODEL_VISION", cfg.model.vision)
        # 频次限制也可用环境变量临时改：ATP_MODEL_THROTTLE=0 关掉，
        # ATP_MODEL_MIN_INTERVAL / ATP_MODEL_MAX_PER_MIN 调具体值。
        cfg.model.throttle = _env_flag("ATP_MODEL_THROTTLE", cfg.model.throttle)
        try:
            cfg.model.min_interval_ms = int(
                _env("ATP_MODEL_MIN_INTERVAL", str(cfg.model.min_interval_ms)))
            cfg.model.max_per_min = int(
                _env("ATP_MODEL_MAX_PER_MIN", str(cfg.model.max_per_min)))
        except (TypeError, ValueError):
            pass
        cfg.page_ready.enabled = _env_flag("ATP_PAGE_READY", cfg.page_ready.enabled)
        cfg.popup_memory.enabled = _env_flag("ATP_POPUP_MEMORY", cfg.popup_memory.enabled)
        cfg.deviation.enabled = _env_flag("ATP_DEVIATION", cfg.deviation.enabled)

        # 工具链兜底：PATH 里找不到就用常见安装路径
        if not shutil.which(cfg.adb):
            cfg.adb = _guess_tool("adb", [r"<SDK>\platform-tools\adb.exe"]) or cfg.adb
        if not shutil.which(cfg.hdc):
            cfg.hdc = _guess_tool("hdc", [r"<TOOLCHAIN>\hdc.exe"]) or cfg.hdc
        return cfg


def _guess_tool(name: str, candidates: list[str]) -> str:
    """根据可执行文件所在路径推断工具类型（adb / hdc / 浏览器）。"""
    for c in candidates:
        if Path(c).exists():
            return c
    return ""


def tool_path(name: str) -> str:
    """在 PATH 中定位工具，找不到返回空串。"""
    return shutil.which(name) or ""
