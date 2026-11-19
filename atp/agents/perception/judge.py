"""判定层。

JEV 已放弃，所以判定拆成两条路：
- dsl：确定性断言（文本/存在性/正则），零成本、可审计，占 90%
- llm：语义断言（"页面看起来已提交成功"），只在 dsl 表达不了时使用
两者都返回统一结构，执行器不关心用的是哪种。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from atp.agents.llm.client import LLMClient, LLMError
from atp.common.stepir import Postcondition
from atp.common.types import Observation, UINode


@dataclass
class JudgeResult:
    """语义判定结果：是否通过、判定方式与置信度。"""
    passed: bool
    method: str = "dsl"
    score: float = 1.0
    detail: str = ""
    extracted: str = ""


def _all_text(root: UINode) -> str:
    """把观测树里的可见文本拼成判定语料。"""
    return "\n".join(n.text or n.desc for n in root.walk() if (n.text or n.desc))


def text_corpus(root: UINode) -> str:
    """界面上所有可见文案拼成的语料，供断言与证据校验共用。"""
    return _all_text(root)


class Judge:
    """语义验收器：让模型判断当前界面是否满足步骤意图。"""
    def __init__(self, llm: LLMClient | None = None, text_threshold: float = 0.6):
        self.llm = llm
        self.text_threshold = text_threshold

    def check(self, post: Postcondition, obs: Observation) -> JudgeResult:
        if post.kind == "none" or not post.claim:
            return JudgeResult(passed=True, method="none", detail="无后置断言")

        kind = post.kind
        claim = post.claim.strip()
        root = obs.root

        if kind == "assert_visible":
            hits = root.find_by_text(claim) or root.find_by_text(claim, contains=True)
            if not hits:
                hits = root.find_by_id(claim)
            ok = bool(hits)
            return JudgeResult(
                passed=ok, method="dsl", score=1.0 if ok else 0.0,
                detail=f"找到 {len(hits)} 个节点" if ok else f"未找到「{claim}」",
            )

        if kind == "assert_absent":
            hits = root.find_by_text(claim, contains=True)
            ok = not hits
            return JudgeResult(
                passed=ok, method="dsl", score=1.0 if ok else 0.0,
                detail="未出现" if ok else f"意外出现 {len(hits)} 处",
            )

        if kind == "assert_url":
            url = obs.url or ""
            ok = bool(url) and claim in url
            return JudgeResult(
                passed=ok, method="dsl", score=1.0 if ok else 0.0,
                detail=f"当前地址 {url[:120]}" if ok else f"地址 {url[:120]} 不含「{claim}」",
            )

        if kind == "assert_text":
            corpus = _all_text(root)
            ok = claim in corpus
            return JudgeResult(
                passed=ok, method="dsl", score=1.0 if ok else 0.0,
                detail="命中" if ok else f"界面文本中未包含「{claim}」",
            )

        if kind == "extract":
            corpus = _all_text(root)
            m = re.search(claim, corpus)
            return JudgeResult(
                passed=bool(m), method="dsl", score=1.0 if m else 0.0,
                detail=f"提取到「{m.group(0)}」" if m else "正则未匹配",
                extracted=m.group(0) if m else "",
            )

        if kind == "assert_screen":
            corpus = _all_text(root)
            # 第一道闸：首跑留下的断言证据。命中即通过，不碰模型。
            # 这是把"回放零 AI"从口号变成数字的地方——没有它，每条用例每轮回放
            # 都要为同一句断言再付一次多模态费用。
            if post.witness and post.witness in corpus:
                return JudgeResult(
                    passed=True, method="dsl-witness", score=1.0,
                    detail=f"断言证据命中「{post.witness}」",
                )

            # 第二道闸：零成本的 DSL 关键词覆盖：命中就直返，不调模型。
            # "断言：登录成功进入首页" 这种话，界面文本里基本都能覆盖住，
            # 白调一次多模态判定要 30~90 秒，完全是浪费。
            words = re.findall(r"[\u4e00-\u9fa5]{2,}", claim)
            if words:
                hit = sum(1 for w in words if w in corpus)
                ratio = hit / len(words)
                if ratio >= 0.8:
                    return JudgeResult(
                        passed=True, method="dsl-precheck", score=round(ratio, 3),
                        detail=f"界面文本覆盖 {ratio:.0%}，跳过语义判定",
                    )
                if post.method == "dsl":
                    return JudgeResult(
                        passed=ratio >= self.text_threshold, method="dsl-degraded",
                        score=round(ratio, 3), detail=f"关键词覆盖率 {ratio:.0%}",
                    )
            elif post.method == "dsl":
                return JudgeResult(passed=False, method="dsl-degraded",
                                   detail="断言中没有可用的中文关键词")
            return self._llm_assert(claim, obs, post.threshold)

        return JudgeResult(passed=False, method="unknown", detail=f"未知断言类型 {kind}")

    def accept(self, instruction: str, history: list[str], obs: Observation,
               threshold: float = 0.6) -> JudgeResult:
        """首跑验收：判断**整条场景**到底做完没有。

        步骤级判定只能证明"这个手势发出去了"，证明不了"这件事办成了"。线上真实踩过：
        指令是「点击【+】→ 点【钱包】→ 进入【我的钱包】则成功」，AI 只点了一次、
        下拉菜单根本没展开、界面还停在消息首页，但因为 tap 返回成功、每步都是 passed，
        整条用例被判通过并沉淀，回放次次失败。

        所以首跑收尾必须单独问一次：**当前界面能证明场景完成吗**。
        只给模型「原始指令 + 做过哪些动作 + 当前界面」，并明确要求以当前界面为唯一证据
        ——不认"我做过相关动作"这种理由。
        """
        if not self.llm or not self.llm.available:
            return JudgeResult(passed=False, method="llm-unavailable", score=0.0,
                               detail="未配置模型：无法验收场景是否真的完成")

        hist = "\n".join(history[-14:]) or "（没有任何动作记录）"
        content: list[dict] = [
            {
                "type": "text",
                "text": (
                    "你是测试验收器。判断「用户要求的测试场景」是否**真的已经完成**，只输出 JSON。\n\n"
                    f"场景要求：{instruction}\n\n"
                    f"执行器做过的动作：\n{hist}\n\n"
                    f"当前界面节点：\n{obs.root.to_prompt(80)}\n\n"
                    "判定规则（严格执行）：\n"
                    "1. 唯一证据是**当前界面能看到的东西**。做过某个动作、或界面只是「看起来相关」，都不算完成；\n"
                    "2. 场景要求里点名的结果（页面/弹窗/数字/文案/已发送的消息）必须在当前界面出现；\n"
                    "3. 只要还有一个必须做的动作没做（没输入、没发送、没提交、没进到目标页），就是 false；\n"
                    "4. **逐条核对场景里的每个动作**（点搜索、输入、选择第几个、进入目标页……），\n"
                    "   最后一个动作没有发生就不能 passed——「到达了它的前一步」不等于完成；\n"
                    "5. 「进入播放页/播放页面/开始播放」类要求：**视频真正开始播放**才算完成——\n"
                    "   界面出现播放器画面/暂停按钮/进度条/时长。只有一个【播放】按钮的详情页\n"
                    "   **不算**进入播放页面（run-20261117-184531：停在详情页就被判成功）；\n"
                    "6. 判断不了就是 false——宁可让人重跑一次，也不能把没做完的写成通过。\n\n"
                    '输出：{"passed": true/false, "confidence": 0~1, "evidence": "当前界面上支持结论的证据",'
                    ' "missing": "还差什么（通过时留空）"}'
                ),
            }
        ]
        if obs.screenshot and self._vision_ok():
            content.append(LLMClient.image_part(obs.screenshot))

        try:
            data = self.llm.chat_json(
                [{"role": "user", "content": content}],
                model=self.llm.cfg.vision_model or self.llm.cfg.model,
            )
        except LLMError as e:
            return JudgeResult(passed=False, method="llm-error", detail=f"验收调用失败: {e}")

        conf = float(data.get("confidence", 0.5))
        passed = bool(data.get("passed")) and conf >= threshold
        evidence = str(data.get("evidence", "") or "")
        missing = str(data.get("missing", "") or "")
        detail = evidence if passed else (f"未完成：{missing or evidence or '当前界面无法证明场景已完成'}")
        return JudgeResult(passed=passed, method="llm-accept", score=conf, detail=detail)

    def _vision_ok(self) -> bool:
        """当前模型是否具备视觉能力。

        本地纯文本模型（Qwen2.5-Coder、DeepSeek-R1 之类）看图只会报错，
        这时改为**只用界面文本**判定；配置里 model.vision=false 即关闭。
        """
        cfg = getattr(getattr(self, "llm", None), "cfg", None)
        return bool(getattr(cfg, "vision", True))

    def _llm_assert(self, claim: str, obs: Observation, threshold: float) -> JudgeResult:
        if not self.llm or not self.llm.available:
            return JudgeResult(
                passed=False, method="llm-unavailable", score=0.0,
                detail="语义断言需要配置模型，或改用 dsl 断言",
            )

        content: list[dict] = [
            {
                "type": "text",
                "text": (
                    "你是测试判定器。判断当前界面是否满足断言，只输出 JSON。\n"
                    f"断言：{claim}\n\n"
                    f"界面节点：\n{obs.root.to_prompt(80)}\n\n"
                    '输出：{"passed": true/false, "confidence": 0~1, "evidence": "一句话依据"}'
                ),
            }
        ]
        if obs.screenshot and self._vision_ok():
            content.append(LLMClient.image_part(obs.screenshot))

        try:
            data = self.llm.chat_json(
                [{"role": "user", "content": content}],
                model=self.llm.cfg.vision_model or self.llm.cfg.model,
            )
        except LLMError as e:
            return JudgeResult(passed=False, method="llm-error", detail=str(e))

        conf = float(data.get("confidence", 0.5))
        passed = bool(data.get("passed")) and conf >= threshold
        return JudgeResult(
            passed=passed, method="llm", score=conf,
            detail=str(data.get("evidence", "")),
        )
