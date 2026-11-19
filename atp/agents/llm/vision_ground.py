"""视觉 grounding：把「点那个登录按钮」变成截图上的一个框。

为什么需要它
------------
canvas / 纯图界面 / 被屏蔽的无障碍树里，节点列表是空的，规划器根本"看不见"目标。
这时唯一的线索就是截图本身 —— 让多模态模型在图里指个位置，再由本地做模板采集。

边界很清楚：
- **只在首跑用一次**。指完就把那一小块裁成模板 + 记下归一化坐标，回放阶段纯本地匹配，
  不再调模型（否则每条用例每次都要付一次视觉推理的钱）。
- **只负责"在哪"**，不负责"要不要点"。语义判断仍归规划器与判定层。
- **失败就失败**：模型给不出可信框，宁可让这一步报错，也不许瞎点。
"""

from __future__ import annotations

from typing import Any

from atp.agents.llm.client import LLMClient, LLMError

_GROUND_SYSTEM = """你是界面视觉定位器。用户给出界面截图和目标元素描述，你只需要回答它在图上的位置。

只输出 JSON：
{"found": true/false,
 "rect": [x1, y1, x2, y2],
 "confidence": 0~1,
 "reason": "一句话依据"}

要求：
- rect 用**归一化坐标**（0~1，相对整张图片的宽高），必须紧贴目标元素的外接矩形，不要整块面板。
- 目标不存在、被遮挡、或你只能猜时，found=false 且 confidence 给低分。
- 不要输出像素坐标，不要输出解释性文字。"""


class VisionGrounder:
    """多模态模型的"指位置"能力封装。"""

    def __init__(self, client: LLMClient):
        self.client = client

    @property
    def available(self) -> bool:
        # 本地模型多数没有视觉能力：配置里关掉 vision 后，直接判定不可用，
        # 调用方就不会每步都拿截图去换一次报错/超时。
        cfg = getattr(self.client, "cfg", None)
        if cfg is not None and not bool(getattr(cfg, "vision", True)):
            return False
        return bool(getattr(self.client, "available", False))

    def find(self, screenshot: bytes, target: str,
             min_confidence: float = 0.4) -> dict[str, Any] | None:
        """返回 {"rect": [0~1 ×4], "confidence": float, "reason": str} 或 None。"""
        if not screenshot or not target or not self.available:
            return None
        content: list[dict] = [
            {"type": "text", "text": f"目标元素：{target}\n请在截图中指出它的位置。"},
            LLMClient.image_part(screenshot),
        ]
        try:
            data = self.client.chat_json(
                [{"role": "system", "content": _GROUND_SYSTEM},
                 {"role": "user", "content": content}],
                model=self.client.cfg.vision_model or self.client.cfg.model,
            )
        except (LLMError, Exception):  # noqa: BLE001 - 模型侧任何异常都按"没找到"处理
            return None

        if not data.get("found"):
            return None
        rect = data.get("rect")
        if not (isinstance(rect, list) and len(rect) == 4):
            return None
        try:
            box = [float(v) for v in rect]
        except (TypeError, ValueError):
            return None
        # 兼容模型直接吐像素坐标的情况：数值明显超过 1 就按画布尺寸归一化
        if max(box) > 1.5:
            box = [v / 1000.0 for v in box]
            if max(box) > 1.2:
                return None
        x1, y1, x2, y2 = [min(1.0, max(0.0, v)) for v in box]
        if x2 - x1 < 0.005 or y2 - y1 < 0.005:
            return None
        try:
            conf = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        if conf < min_confidence:
            return None
        return {"rect": [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)],
                "confidence": conf, "reason": str(data.get("reason", ""))[:120]}


__all__ = ["VisionGrounder"]
