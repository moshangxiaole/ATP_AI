"""真机首跑：打开示例视频应用 → 搜索示例演员 → 点进《示例电视剧》→ 播放。

这不是"又一个录制工具"。它和 AI 首跑做的是同一件事——边操作边把每一步沉淀成
StepIR（含多级定位策略、基准画布、后置断言），产物直接进 cases/ 和用例库，
之后 `atp run` 纯回放，不再碰这里。

用法：
    python scripts/build_videoapp_case.py android DEMO_ANDROID_SERIAL
    python scripts/build_videoapp_case.py harmony DEMO_HARMONY_SERIAL
    python scripts/build_videoapp_case.py            # 自动挑第一个可用设备

四端共用一份流程，差异全部收敛到 PlatformProfile：
搜索入口怎么认、输入框是什么 class、怎么提交、广告长什么样。
新增端只加一个 profile，不改流程。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.config import Config  # noqa: E402
from atp.agents.perception.judge import Judge  # noqa: E402
from atp.agents.perception.locator import derive_strategies  # noqa: E402
from atp.memory.popups import PopupGuard, dismiss_popups  # noqa: E402
from atp.common.stepir import (CaseIR, Postcondition, ScreenProfile, Step,  # noqa: E402
                             StepResult)
from atp.common.types import Action, Observation, UINode  # noqa: E402
from atp.agents.perception.vision import frame_diff_ratio  # noqa: E402

NL = "打开示例视频应用，搜索示例演员，点击示例电视剧，播放"
TITLE = "示例视频应用 搜索示例演员并播放《示例电视剧》"


class PlatformProfile:
    """一个端在"同一个 App"上的差异点。"""

    name = ""
    app = ""                 # 包名 / Ability
    case_id = ""
    input_cls = ""           # 输入框的节点 class
    episode_cls = ""         # 选集格子里集号文本的 class

    def search_entry(self, n: UINode) -> bool:
        raise NotImplementedError

    def submit(self, rec: "Recorder") -> None:
        """提交搜索。鸿蒙靠回车，安卓靠点「搜索」按钮。"""
        raise NotImplementedError

    def ad_visible(self, root: UINode) -> bool:
        """当前是否还有贴片广告。"""
        raise NotImplementedError

    @property
    def ad_claim(self) -> str:
        """写进 StepIR 的「广告还在」断言文本。

        这个值必须和录制时的判定同源，否则录制"通过"、回放"失败"：
        Android 播放器右下角常驻「广告·RM」水印，claim 用「广告」两个字
        回放时会永远命中（真机上撞过一次，189 秒超时判失败，其实正片早在播）。
        """
        raise NotImplementedError

    def on_home(self, root: UINode) -> bool:
        """是不是停在 App 首页。用来确认冷启动的起点，也用来判断要不要重试。"""
        raise NotImplementedError

    home_claim = "首页"


class HarmonyProfile(PlatformProfile):
    name = "harmony"
    app = "com.example.videoapp.harmony/EntryAbility"
    case_id = "videoapp_search_play"
    input_cls = "TextInput"
    episode_cls = "Text"

    def search_entry(self, n: UINode) -> bool:
        return n.id == "v_searchBar"

    def submit(self, rec: "Recorder") -> None:
        rec.press("enter", "提交搜索")

    def ad_visible(self, root: UINode) -> bool:
        return any((n.text or "").strip() == "广告" for n in root.walk())

    @property
    def ad_claim(self) -> str:
        return "广告"

    def on_home(self, root: UINode) -> bool:
        return _find(root, by_text("首页")) is not None


class AndroidProfile(PlatformProfile):
    name = "android"
    app = "com.example.videoapp"
    case_id = "videoapp_search_play_android"
    input_cls = "EditText"
    episode_cls = "TextView"

    def search_entry(self, n: UINode) -> bool:
        # 首页右上角的搜索容器，里面有放大镜 aivSearch
        return n.id.endswith("clSearchContainer") or n.id.endswith("aivSearch")

    def submit(self, rec: "Recorder") -> None:
        rec.tap(lambda n: (n.text or "").strip() == "搜索", "点击【搜索】按钮提交")

    def ad_visible(self, root: UINode) -> bool:
        # Android 版广告有明确的两个节点：倒计时 countTime + 诱导位 adSkip。
        for n in root.walk():
            if n.id.endswith("countTime") or n.id.endswith("adSkip"):
                if n.bounds[2] > n.bounds[0] and n.bounds[3] > n.bounds[1]:
                    return True
        return False

    @property
    def ad_claim(self) -> str:
        # 只认「跳过此广告」：它是贴片广告独有的。用「广告」会命中常驻角标「广告·RM」。
        return "跳过此广告"

    def on_home(self, root: UINode) -> bool:
        # 首页标志：右上角搜索容器在，且没有搜索输入框（搜索页会盖住首页）
        return (_find(root, lambda n: n.id.endswith("clSearchContainer")) is not None
                and _find(root, lambda n: n.cls == self.input_cls) is None)


PROFILES = {"harmony": HarmonyProfile(), "android": AndroidProfile()}


class Recorder:
    """边操作边记 StepIR。每个动作都带现场采到的定位策略与基准画布。"""

    def __init__(self, driver, cfg: Config, prof: PlatformProfile):
        self.driver = driver
        self.cfg = cfg
        self.prof = prof
        self.case = CaseIR(case_id=prof.case_id, title=TITLE, platform=prof.name,
                           app_id=prof.app, created_by="human", nl=NL)
        self.guard = PopupGuard(extra_close_words=cfg.popup.extra_close_words,
                                limit=cfg.popup.max_per_step)
        self.results: list[StepResult] = []

    # ---------- 感知 ----------
    def observe(self) -> Observation:
        obs = self.driver.observe()
        obs, hits = dismiss_popups(self.driver, obs, self.guard,
                                   settle_ms=self.cfg.popup.settle_ms)
        for h in hits:
            print(f"   [弹窗] 自动处置：{h['keyword']} @ ({h['x']}, {h['y']})")
        return obs

    def screen(self) -> tuple[int, int]:
        return self.driver.screen_size()

    # ---------- 记录 ----------
    def _add(self, action: Action, intent: str, args: dict | None = None,
             node: UINode | None = None, obs: Observation | None = None,
             post: Postcondition | None = None) -> Step:
        sid = f"s{len(self.case.steps) + 1:02d}"
        strategies = []
        if node is not None and obs is not None:
            strategies = derive_strategies(node, obs.root, (obs.width, obs.height))
        step = Step(step_id=sid, intent=intent, action=action, strategies=strategies,
                    args=args or {}, postcondition=post or Postcondition(),
                    source="human")
        self.case.steps.append(step)
        return step

    def _record_result(self, step: Step, status: str, detail: str = "") -> None:
        self.results.append(StepResult(step_id=step.step_id, intent=step.intent,
                                       action=step.action.value, status=status,
                                       detail=detail))

    # ---------- 动作 ----------
    def tap(self, match, intent: str) -> UINode:
        obs = self.observe()
        node = _find(obs.root, match)
        if node is None:
            raise RuntimeError(f"没找到目标：{intent}")
        step = self._add(Action.TAP, intent, node=node, obs=obs)
        self.driver.perform(Action.TAP, node, {})
        self._record_result(step, "passed", f"点击 {node.label!r} @ {node.center}")
        print(f" [+] {step.step_id} 点击 {node.label or node.cls!r} {node.center}")
        return node

    def input_text(self, text: str, intent: str) -> None:
        obs = self.observe()
        node = _find(obs.root, lambda n: n.cls == self.prof.input_cls)
        if node is None:
            raise RuntimeError("页面上没有输入框")
        # 先清空：搜索框会自动带上一句热词，不清就会搜成"热词+关键词"
        s1 = self._add(Action.CLEAR_TEXT, "清空搜索框里残留的默认词", node=node, obs=obs)
        self.driver.perform(Action.CLEAR_TEXT, node, {})
        time.sleep(0.4)
        self._record_result(s1, "passed", "已清空")
        print(f" [+] {s1.step_id} 清空输入框")

        s2 = self._add(Action.INPUT_TEXT, intent, args={"text": text}, node=node, obs=obs)
        self.driver.perform(Action.INPUT_TEXT, node, {"text": text})
        time.sleep(0.6)
        self._record_result(s2, "passed", f"输入 {text!r}")
        print(f" [+] {s2.step_id} 输入 {text!r}")

    def press(self, key: str, intent: str) -> None:
        step = self._add(Action.PRESS_KEY, intent, args={"key": key})
        self.driver.perform(Action.PRESS_KEY, None, {"key": key})
        self._record_result(step, "passed", key)
        print(f" [+] {step.step_id} 按键 {key}")

    def launch(self) -> None:
        step = self._add(Action.LAUNCH_APP, f"启动应用 {self.prof.app}",
                         args={"app": self.prof.app})
        self.driver.perform(Action.LAUNCH_APP, None, {"app": self.prof.app})
        time.sleep(2.5)
        self._record_result(step, "passed", "已启动")
        print(f" [+] {step.step_id} 启动示例视频应用")

    def terminate(self) -> None:
        """先杀进程再启动。

        不杀的话启动只会把后台的旧页面拉到前台——上次停在详情页，
        这次第一步就找不到首页的搜索入口。用例必须自己能造出确定的初始状态。
        """
        pkg = self.prof.app.split("/")[0]
        step = self._add(Action.TERMINATE_APP, f"结束 {pkg} 进程",
                         args={"app": self.prof.app})
        self.driver.perform(Action.TERMINATE_APP, None, {"app": self.prof.app})
        time.sleep(1.0)
        self._record_result(step, "passed", "已结束进程")
        print(f" [+] {step.step_id} 结束示例视频应用进程")

    def wait(self, post: Postcondition, intent: str, timeout_ms: int,
             checker) -> bool:
        """带断言的一步：checker(post, deadline) -> (ok, detail)。"""
        step = self._add(Action.WAIT_FOR, intent, post=post)
        # 超时时间必须写进用例：等广告播完要一两分钟，
        # 用默认的 15 秒回放必挂——而这个字段漏记过一次，回放报"广告仍在"。
        step.timeout_ms = timeout_ms
        deadline = time.time() + timeout_ms / 1000
        ok, detail = checker(post, deadline)
        self._record_result(step, "passed" if ok else "failed", detail)
        mark = "✓" if ok else "✗"
        print(f" [{mark}] {step.step_id} {intent} —— {detail}")
        return ok

    def save(self, out_dir: Path) -> Path:
        obs = self.driver.observe()
        w, h = obs.width, obs.height
        self.case.screen = ScreenProfile(
            width=w, height=h, dpr=self.driver.device_pixel_ratio(),
            platform=self.prof.name, label=f"{self.prof.name}-{w}x{h}",
        )
        path = out_dir / f"{self.prof.case_id}.stepir.json"
        self.case.save(path)
        return path


def _find(root: UINode, match) -> UINode | None:
    for n in root.walk():
        try:
            if match(n):
                return n
        except Exception:  # noqa: BLE001
            continue
    return None


def by_text(text: str, cls: str = ""):
    def _m(n: UINode) -> bool:
        if (n.text or "").strip() != text:
            return False
        return not cls or n.cls == cls
    return _m


def contains_text(text: str):
    """包含匹配——和 judge 里 assert_absent 的口径一致。"""
    def _m(n: UINode) -> bool:
        return bool(text) and text in ((n.text or "") + (n.desc or ""))
    return _m


def wait_text(driver, guard, text: str, timeout: float = 12.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        obs = driver.observe()
        obs, _ = dismiss_popups(driver, obs, guard, settle_ms=300)
        if _find(obs.root, by_text(text)) is not None:
            return True
        time.sleep(0.5)
    return False


def _episode_cell(root: UINode, num_cls: str) -> UINode | None:
    """推出"选集"第一格的位置。

    正在播放的那一格不显示集号（换成音轨图标），所以不能按"1"去找。
    用第 2、3 格的间距倒推第一格中心，存成归一化坐标——换分辨率照样点得中。
    """
    num = {}
    for n in root.walk():
        t = (n.text or "").strip()
        if t in ("2", "3") and n.cls == num_cls:
            num.setdefault(t, n)
    if "2" not in num or "3" not in num:
        return None
    n2, n3 = num["2"], num["3"]
    pitch = n3.center[0] - n2.center[0]
    if pitch <= 0:
        return None
    cx = n2.center[0] - pitch
    cy = n2.center[1]
    # 故意不给 text：这一格的可读文案会随播放状态变化，只留坐标定位
    return UINode(cls=num_cls, text="", bounds=(cx - 20, cy - 40, cx + 20, cy + 40))


def make_driver(platform: str, device: str, cfg: Config):
    if platform == "android":
        from atp.agents.platforms.mobile.android import AndroidAdbDriver

        return AndroidAdbDriver(device, cfg)
    if platform == "harmony":
        from atp.agents.platforms.mobile.harmony import HarmonyHdcDriver

        return HarmonyHdcDriver(device, cfg)
    raise SystemExit(f"不支持的平台 {platform}")


def pick_device(platform: str, cfg: Config) -> str:
    from atp.agents.devices.manager import DeviceManager

    found = [d for d in DeviceManager(cfg).discover() if d.platform == platform]
    return found[0].device_id if found else ""


def main() -> int:
    cfg = Config.load()

    args = sys.argv[1:]
    platform = args[0] if args else ""
    device = args[1] if len(args) > 1 else ""
    if platform not in PROFILES:
        platform = ""
    if not platform:
        for name in ("android", "harmony"):
            if pick_device(name, cfg):
                platform = name
                break
    if not platform:
        print("没发现移动端设备，先 adb devices / hdc list targets 确认")
        return 2

    prof = PROFILES[platform]
    device = device or pick_device(platform, cfg)
    if not device:
        print(f"没有发现 {platform} 设备")
        return 2

    driver = make_driver(platform, device, cfg)
    driver.connect()
    rec = Recorder(driver, cfg, prof)

    print(f"设备 {device} · {platform} · 画布 {driver.screen_size()}")

    try:
        # 从干净状态起步：先杀进程，再冷启动。
        rec.terminate()
        rec.launch()

        # 2. 等首页真的就绪再往下点。
        #    不加这步的话，紧接着的"点搜索入口"会撞上还没加载完的首页：
        #    id 定位 miss → 降级到坐标，靠"碰巧点中"过关（.stepir 里没有 a11y_id，
        #    换台分辨率不同的机器就挂）。把等待显式写成一步，既是断言也是节流阀。
        def wait_home_ready(post: Postcondition, deadline: float):
            j = Judge(None)
            t0 = time.time()
            while time.time() < deadline:
                obs = rec.observe()      # 顺手把开屏/青少年模式之类弹窗清掉
                if j.check(post, obs).passed:
                    return True, f"{time.time() - t0:.1f}s 后首页就绪"
                time.sleep(0.6)
            return False, "首页没加载出来"

        rec.wait(Postcondition(kind="assert_visible", claim=prof.home_claim,
                               method="dsl"),
                 "等待首页加载完成", 25000, wait_home_ready)

        # 1. 搜索入口（首页右上角的放大镜，只有 id 没有文案）
        rec.tap(prof.search_entry, "点击首页右上角搜索入口")
        if not wait_text(driver, rec.guard, "搜索历史", 10):
            print(" ! 搜索页可能没打开")

        # 2. 输入关键词并搜索
        rec.input_text("示例演员", "在搜索框输入 示例演员")
        prof.submit(rec)
        if not wait_text(driver, rec.guard, "示例电视剧", 15):
            print(" ! 搜索结果里没看到《示例电视剧》")

        # 3. 点进结果
        rec.tap(by_text("示例电视剧"), "点击搜索结果《示例电视剧》")
        if not wait_text(driver, rec.guard, "选集", 25):
            print(" ! 详情页没加载出来")

        # 4. 播放：点选集第一集
        #    坑：正在播的那一集会把集号换成跳动的音轨图标，所以"1"这个文案时有时无，
        #    不能拿它当定位。改成用相邻两集的位置推出第一格的中心，再存归一化坐标。
        obs = rec.observe()
        cell = _episode_cell(obs.root, prof.episode_cls)
        if cell is None:
            print(" ! 选集区域没渲染出来，详情页可能已自动开播，直接等广告")
        else:
            step = rec._add(Action.TAP, "点击第 1 集开始播放", node=cell, obs=obs)
            driver.perform(Action.TAP, cell, {})
            rec._record_result(step, "passed", "点击第 1 集")
            print(f" [+] {step.step_id} 点击第 1 集 {cell.center}")

        # 5. 等贴片广告播完（非会员绕不过去，只能等；接口层不做诱导位点击）
        #    判定必须比回放更严：回放只按 claim 文本查（assert_absent），
        #    所以录制时除了 claim 不出现，还要求广告节点也没了——
        #    录制通过就一定能推出回放通过，反过来不成立。
        def wait_ad_gone(post: Postcondition, deadline: float):
            t0 = time.time()
            while time.time() < deadline:
                obs = driver.observe()
                if (not prof.ad_visible(obs.root)
                        and _find(obs.root, contains_text(post.claim)) is None):
                    return True, f"{time.time() - t0:.1f}s 后广告结束"
                time.sleep(1.5)
            return False, "广告仍未结束"

        rec.wait(Postcondition(kind="assert_absent", claim=prof.ad_claim,
                               method="dsl"),
                 "等待贴片广告播完", 240000, wait_ad_gone)

        # 6. 断言真的在播：节点树看不出黑屏/暂停，只能比像素
        def wait_motion(post: Postcondition, deadline: float):
            best = 0.0
            while time.time() < deadline:
                a = driver.screenshot()
                time.sleep(0.6)
                b = driver.screenshot()
                best = max(best, frame_diff_ratio(a, b))
                if best >= 0.03:
                    return True, f"画面变化 {best:.1%}"
            return False, f"画面几乎没变（最大 {best:.1%}）"

        rec.wait(Postcondition(kind="assert_motion", claim="正在播放",
                               method="dsl", min_ratio=0.03),
                 "断言：视频正在播放", 60000, wait_motion)

        path = rec.save(ROOT / "cases")
        print(f"\nStepIR 已生成：{path}")
        passed = sum(1 for r in rec.results if r.status == "passed")
        print(f"步骤 {len(rec.case.steps)} 个，现场执行通过 {passed} 个")
        return 0 if passed == len(rec.results) else 1
    finally:
        driver.close()   # Android 会把输入法还给用户


if __name__ == "__main__":
    raise SystemExit(main())
