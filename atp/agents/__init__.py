"""① 智能体层。

    runner      首跑主循环（AI 规划 → 执行 → 沉淀），端无关
    planner     提示词、节点编号、规划器与修复器
    executor    回放执行器（StepIR → 驱动原语）
    perception  看与判：定位、判定、偏离、页面门禁、滚动、底部确认条、视觉兜底
    platforms   三端：PC / 移动 / WEB（端策略 + 驱动 + 设备发现）
    devices     设备池（本地三端 + 远程真机）
    llm         模型客户端与视觉定位

端差异一律从 ``atp.common.profiles`` 的端能力矩阵读取，本层不写平台名判断。
"""
