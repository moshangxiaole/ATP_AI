"""ATP · Agent Test Platform

自然语言驱动、多端统一的自动化测试平台。
核心不变式：一次执行产出 StepIR，之后只跑脚本，AI 仅在异常时介入。

代码按七层组织（依赖单向，下层不认识上层）：

    agents/     ① 智能体：首跑规划、回放执行、感知判定、三端（PC / 移动 / WEB）接入
    webapp/     ② WEB 服务：控制台 HTTP、任务调度、设备投屏、前端
    db/         ③ 数据库及操作：SQLite 用例库 / 执行记录
    report/     ④ 报告生成：单文件 HTML
    memory/     ⑤ 记忆沉淀：应用台账（包名）、弹窗记忆、失败沉淀
    scriptgen/  ⑥ 脚本生成：StepIR → Python 脚本 / DSL
    runs/       ⑦ 日志及执行文件：产物命名、事件日志、录屏、用例文件库
    common/     跨层共享的纯数据：动作与节点、用例 IR、端能力矩阵
"""

__version__ = "0.1.0"
