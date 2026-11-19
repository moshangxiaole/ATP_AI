"""控制台回归测试用的假设备与假模型（**只属于 tests，不进产品包**）。

Mock 设备功能已经从产品里下线：控制台只能连真机 / 桌面 / Web。
但回归测试仍然需要一台"不用插手机、不用 API Key"的稳定设备，
所以这套假 OA 界面搬到了这里，并配套提供脚本化规划器与假模型客户端，
通过 patch 注入到 atp.web.jobs，产品代码里不再有任何演示/离线后门。
"""

from __future__ import annotations

import contextlib
import io
import types
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

from atp.common.types import Action, UINode
from atp.agents.platforms.base import BaseDriver

W, H = 540, 960
PKG = "com.sunshine.oa:id"


def _font(size: int, bold: bool = False):
    cands = [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for c in cands:
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            continue
    return ImageFont.load_default()


def node(cls: str, nid: str, text: str, bounds: tuple[int, int, int, int],
         clickable: bool = False, desc: str = "") -> UINode:
    return UINode(
        cls=cls,
        id=f"{PKG}/{nid}" if nid else "",
        text=text,
        desc=desc,
        bounds=bounds,
        clickable=clickable,
    )


class FakeOaDriver(BaseDriver):
    platform = "android"

    def __init__(self, device_id: str = "fixture-oa-01", config=None):
        super().__init__(device_id, config)
        self.app_id = "com.sunshine.oa"
        self.state = "login"
        self.fields: dict[str, str] = {}
        self.toast = ""
        self.detail_of = "张三的请假申请"

    # ---------- 生命周期 ----------
    def connect(self) -> None:
        self._connected = True

    def screen_size(self) -> tuple[int, int]:
        return W, H

    # ---------- 界面定义 ----------
    def _screen(self) -> list[UINode]:
        if self.state == "login":
            nodes = [
                node("Text", "tv_header", "阳光OA", (0, 40, W, 130)),
                node("Text", "", "用户名", (40, 230, 140, 280)),
                node("EditText", "et_username",
                     self.fields.get("username") or "请输入用户名", (160, 220, 500, 280), True),
                node("Text", "", "密码", (40, 330, 140, 380)),
                node("EditText", "et_password",
                     self.fields.get("password") or "请输入密码", (160, 320, 500, 380), True),
                node("Button", "btn_login", "登录", (170, 460, 370, 530), True),
            ]
        elif self.state == "home":
            nodes = [
                node("Text", "tv_header", "工作台", (0, 40, W, 130)),
                node("Text", "tv_welcome", "早上好，测试员", (40, 170, 500, 220)),
                node("Button", "tab_todo", "待办 3", (40, 260, 260, 340), True),
                node("Button", "tab_msg", "消息 5", (280, 260, 500, 340), True),
                node("Button", "tab_mine", "我的", (40, 360, 500, 440), True),
            ]
        elif self.state == "todo":
            nodes = [
                node("Text", "tv_header", "待办事项", (0, 40, W, 130)),
                node("Button", "item_0", "张三的请假申请", (40, 180, 500, 260), True),
                node("Button", "item_1", "李四的报销单", (40, 280, 500, 360), True),
                node("Text", "", "共 2 条", (40, 400, 500, 440)),
            ]
        elif self.state == "detail":
            nodes = [
                node("Text", "tv_header", "请假申请详情", (0, 40, W, 130)),
                node("Text", "tv_applicant", "申请人：张三", (40, 180, 500, 230)),
                node("Text", "tv_days", "时长：1 天", (40, 240, 500, 290)),
                node("Text", "tv_reason", "事由：家中有事", (40, 300, 500, 350)),
                node("EditText", "et_comment",
                     self.fields.get("comment") or "请输入审批意见", (40, 500, 500, 570), True),
                node("Button", "btn_agree", "同意", (40, 640, 260, 710), True),
                node("Button", "btn_reject", "驳回", (280, 640, 500, 710), True),
            ]
        else:  # done
            nodes = [
                node("Text", "tv_header", "审批结果", (0, 40, W, 130)),
                node("Text", "tv_result", "审批已提交", (40, 220, 500, 280)),
                node("Text", "tv_flow", "流程已完成", (40, 300, 500, 350)),
                node("Button", "btn_home", "返回工作台", (40, 640, 500, 710), True),
            ]

        if self.toast:
            nodes.append(node("Text", "tv_toast", self.toast, (40, 800, 500, 860)))
        return nodes

    def dump_tree(self) -> UINode:
        return UINode(cls="Root", bounds=(0, 0, W, H), children=self._screen())

    # ---------- 动作 ----------
    def tap_xy(self, x: int, y: int) -> None:
        tree = self.dump_tree()
        target = self._focused if (self._focused and self._focused.contains(x, y)) else None
        if target is None:
            target = tree.find_smallest_at(x, y)
        if target is None:
            return
        self._focused = target
        self._handle(target.id.split("/")[-1])

    def input_text(self, text: str) -> None:
        key = (self._focused.id.split("/")[-1] if self._focused else "")
        mapping = {"et_username": "username", "et_password": "password", "et_comment": "comment"}
        if key in mapping:
            self.fields[mapping[key]] = text
            self.toast = ""

    def clear_text(self) -> None:
        self.input_text("")

    def press_key(self, name: str) -> None:
        if name == "back":
            self.state = {"todo": "home", "detail": "todo", "done": "todo"}.get(self.state, self.state)

    def launch_app(self, app_id: str) -> None:
        self.app_id = app_id or self.app_id
        self.state = "login"
        self.fields.clear()
        self.toast = ""

    # ---------- 交互逻辑 ----------
    def _handle(self, key: str) -> None:
        if key == "btn_login":
            if not self.fields.get("username") or not self.fields.get("password"):
                self.toast = "请输入用户名和密码"
            else:
                self.state, self.toast = "home", ""
        elif key == "tab_todo":
            self.state = "todo"
        elif key == "tab_msg":
            self.toast = "暂无新消息" if not self.toast else self.toast
        elif key in ("item_0", "item_1"):
            self.detail_of = "张三的请假申请" if key == "item_0" else "李四的报销单"
            self.state = "detail"
        elif key == "btn_agree":
            if not self.fields.get("comment"):
                self.toast = "请填写审批意见"
            else:
                self.state, self.toast = "done", ""
        elif key == "btn_reject":
            self.state, self.toast = "done", ""
        elif key == "btn_home":
            self.state, self.toast = "home", ""

    # ---------- 渲染 ----------
    def screenshot(self) -> bytes:
        img = Image.new("RGB", (W, H), "#F5F7FA")
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 130], fill="#2E5BFF")
        d.rectangle([0, 130, W, 132], fill="#D8DFEA")

        for n in self._screen():
            x1, y1, x2, y2 = n.bounds
            if n.id.endswith("tv_header"):
                d.text((40, y1 + 26), n.text, font=_font(30, True), fill="#FFFFFF")
                continue
            if n.id.endswith("tv_toast"):
                d.rounded_rectangle([x1, y1, x2, y2], 10, fill="#FFF4E5", outline="#F0C27B")
                d.text((x1 + 20, y1 + 16), n.text, font=_font(20), fill="#8A5A00")
                continue
            if n.clickable:
                fill = "#FFFFFF" if n.cls != "Button" else "#2E5BFF"
                outline = "#C9D0DB" if n.cls != "Button" else "#2E5BFF"
                d.rounded_rectangle([x1, y1, x2, y2], 10, fill=fill, outline=outline)
                color = "#1A1A1A" if n.cls != "Button" else "#FFFFFF"
                d.text((x1 + 20, y1 + (y2 - y1) // 2 - 14), n.text, font=_font(22), fill=color)
            else:
                d.text((x1, y1 + 6), n.text, font=_font(22), fill="#4A5160")

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


# ---------------------------------------------------------------- 注入替身
# 产品侧的 ScriptedPlanner / DEMO_PLAN 已删除：模型没配置时控制台会明确报错，
# 不再偷偷切到演示脚本。测试要跑通全链路，就在这里把依赖替换掉。
DEMO_PLAN = [
    {"action": "input", "node_index": 2, "text": "tester", "intent": "在用户名输入框填写 tester"},
    {"action": "input", "node_index": 4, "text": "123456", "intent": "在密码输入框填写 123456"},
    {"action": "tap", "node_index": 5, "intent": "点击登录按钮"},
    {"action": "tap", "node_index": 2, "intent": "点击待办入口"},
    {"action": "tap", "node_index": 1, "intent": "打开第一条请假申请"},
    {"action": "input", "node_index": 4, "text": "同意", "intent": "填写审批意见"},
    {"action": "tap", "node_index": 5, "intent": "点击同意按钮"},
    {"action": "done", "done_reason": "审批已提交"},
]


class ScriptedPlanner:
    """按预设脚本出决策，供单测替代真模型规划器。"""

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.i = 0

    def next(self, nl, obs, history):
        from atp.agents.runner import indexed_nodes

        nodes, _ = indexed_nodes(obs.root)
        if self.i >= len(self.script):
            return {"action": "done", "done_reason": "脚本结束", "_nodes": nodes}
        d = dict(self.script[self.i])
        self.i += 1
        d["_nodes"] = nodes
        return d


class FakeLLMClient:
    """假模型：只负责让首跑的结构/语义验收通过，不发任何网络请求。"""

    available = True
    model = "fixture-model"
    cfg = types.SimpleNamespace(model="fixture-model", vision_model="")

    def chat_json(self, messages, model=None, **kwargs):
        return {"passed": True, "confidence": 0.95,
                "evidence": "测试假设备，验收固定通过", "missing": ""}


class FakeDeviceManager:
    """把 jobs 里的 DeviceManager 换成假设备，测试不碰 adb。"""

    def __init__(self, config=None):
        self.config = config

    @staticmethod
    @contextlib.contextmanager
    def lease(*args, **kwargs):
        d = FakeOaDriver()
        d.connect()
        try:
            yield d
        finally:
            d.close()

    @staticmethod
    def open(*args, **kwargs):
        return FakeOaDriver()

    @staticmethod
    def discover():
        return []


_ORIGINALS: dict | None = None


def install() -> None:
    """把假设备/假模型挂到 Web 任务层；可重复调用。"""
    global _ORIGINALS
    import atp.webapp.jobs as jobs

    if _ORIGINALS is None:
        _ORIGINALS = {k: getattr(jobs, k) for k in
                      ("LLMClient", "LlmPlanner", "DeviceManager")}
    jobs.LLMClient = lambda cfg: FakeLLMClient()
    jobs.LlmPlanner = lambda client: ScriptedPlanner(DEMO_PLAN)
    jobs.DeviceManager = lambda cfg: FakeDeviceManager(cfg)


def restore() -> None:
    global _ORIGINALS
    if _ORIGINALS is None:
        return
    import atp.webapp.jobs as jobs

    for k, v in _ORIGINALS.items():
        setattr(jobs, k, v)
    _ORIGINALS = None
