"""⑥ 脚本生成层：StepIR → 可执行脚本。

    compiler   StepIR → Python 脚本 / YAML 清单
    dsl        脚本里用的链式 DSL（Script），内部仍构建同一个 CaseIR
"""

from atp.scriptgen.compiler import compile_case, to_python, to_yaml
from atp.scriptgen.dsl import Script, record_strategies

__all__ = ["Script", "record_strategies", "compile_case", "to_python", "to_yaml"]
