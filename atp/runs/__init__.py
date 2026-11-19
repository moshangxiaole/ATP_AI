"""⑦ 日志及执行文件层：一次执行落下来的所有"文件"。

    artifacts   运行目录与产物命名约定（report.html / events.jsonl / recording.mp4 / stepir.json）
    events      执行事件日志：events.jsonl 主日志 + llm.jsonl / actions.jsonl 旁路
    recorder    执行录屏（PC / WEB）
    library     用例文件库：cases/ 下的 .stepir.json / .nl.json 读写与相似检索
"""
