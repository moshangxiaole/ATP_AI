"""⑤ 记忆沉淀层：跨执行存活的知识。

    apps           应用台账（应用名 ↔ 包名 / exe 路径）
    popups         弹窗识别与处置（开屏广告、更新提示、权限申请…）
    popup_memory   弹窗长程记忆：同一个弹窗第二次就不再问模型
    sediment       失败沉淀：首跑没走通的用例不写进用例库

这些文件的共同点是"跑得越多越准"，所以单独成层、落盘可读可改。
"""

from atp.memory.apps import AppEntry, AppRegistry, resolve_app
from atp.memory.popup_memory import PopupContext, PopupMemory
from atp.memory.popups import PopupGuard, dismiss_popups
from atp.memory.sediment import sediment_blocker

__all__ = ["AppEntry", "AppRegistry", "resolve_app", "PopupContext", "PopupMemory",
           "PopupGuard", "dismiss_popups", "sediment_blocker"]
