# ATP · Agent Test Platform

> **语言 / Language:** **简体中文** · [English](README_EN.md)

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Platforms](https://img.shields.io/badge/Platforms-Android%20%C2%B7%20HarmonyOS%20%C2%B7%20Windows%20%C2%B7%20Web-2F80ED)
![Mode](https://img.shields.io/badge/Replay-Zero%20LLM-2E7D32)
![Console](https://img.shields.io/badge/Console-127.0.0.1%3A8765-6B7280)

**自然语言驱动的多端自动化测试平台。首次执行 AI 全接管，之后只跑脚本，AI 仅在异常时介入。**

一条用例的生命周期：

```
自然语言（.case.yaml）
   │  ai         AI 规划 + 定位 + 执行 + 每步截图
   ▼
StepIR（.stepir.json）   ← 唯一真相源，步骤即数据
   │  compile
   ▼
可执行脚本（.py）        ← 人可读、可改、可直接进 CI
   │  run        零 LLM，断言走 DSL
   ▼
报告（report.html）      ← 单文件，截图内嵌，可离线审计
        │
        └──── 定位器全 miss 时才唤醒 AI 修复，权重自动回写
```

## 目录

- [核心特性](#核心特性)
- [快速开始](#快速开始)
- [Web 控制台](#web-控制台)
- [工作原理](#工作原理)
- [项目架构](#项目架构)
- [平台支持](#平台支持)
- [配置](#配置)
- [测试与排障](#测试与排障)
- [扩展一个新平台](#扩展一个新平台)
- [已知限制](#已知限制)

## 核心特性

- **一次录制，长期零 AI 回放**：首跑把意图、定位策略、断言、证据写进 StepIR；回放只读数据，不调用模型。
- **六级定位降级**：`a11y_id → text → desc → xpath → bounds → visual`，命中回写权重，实现"在线自愈"。
- **真机优先**：Android（ADB）、HarmonyOS（HDC）抓树与截图；PC 支持 UIA / Electron CDP / 视觉三档降级；Web 走 Chrome CDP。
- **视觉兜底**：没有节点树的 canvas / WebGL / 自绘界面，首跑用截图指位置，回放做本地模板匹配，分辨率无关。
- **页面门禁 + 验收闸**：树-only 轮询等页面稳定；播放类场景确定性识别"详情页 ≠ 播放页"，杜绝假通过。
- **实时投屏与回注**：MJPEG 实时流 + 触控回注；鸿蒙走 JPEG 拉取，Android 可选 H.264；设备拔线自动断开。
- **完整排障日志**：每次执行落盘 `events.jsonl` / `llm.jsonl` / `actions.jsonl`，可逐拍还原模型决策。
- **记忆沉淀**：应用包名台账、弹窗长程记忆、失败用例不入库。

实测基线（真模型 + 真机，2026-11-10）：

| 用例 | 端 | 首跑 | 回放 |
|---|---|---|---|
| 给指定联系人发消息 | PC（Electron CDP） | 4 步全过，113s | 沉淀 4 步 |
| OA 后台登录 | Web（Chrome） | 6 步全过，105s | 6.6s，**AI 参与 0** |
| 示例视频应用 搜索并播放 | HarmonyOS（HDC） | 10 步全过 | 58.6s，**AI 参与 0** |
| 同上（换端） | Android（ADB） | 10 步全过 | 116.3s，**AI 参与 0** |

## 快速开始

### 环境要求

- Python **3.10+**
- 被测端工具按需安装：`adb`（Android）、`hdc`（HarmonyOS）、Chrome/Chromium（Web）、Windows UIA（桌面）
- 一个 OpenAI 兼容的模型服务（云端或本机 Ollama / LM Studio / vLLM 均可）

> 没有离线演示模式。Mock 虚拟终端已下线：首跑必须配好模型并连真机。
> 测试用的假设备只存在于 `tests/fixtures/console_fakes.py`，不进产品包。

### 一键启动

| 平台 | 操作 |
|---|---|
| Windows | 双击 `start.bat` |
| macOS / Linux | `./start.sh` |

首次运行自动创建 `venv` 并安装依赖，之后每次启动 = 起服务 + 开浏览器。
重复双击不会起第二个进程（端口上已有自己人就直接复用）；端口被占会自动顺延（8765 → 8766 …）。
启动时会顺手体检模型连通性，模型名写错或 Key 过期会在终端立即报出。

```bash
start.bat --port 9000     # 换端口
start.bat --status        # 查看是否在运行
start.bat --stop          # 停止（等价 stop.bat）
```

### 命令行

```bash
python -m atp.cli doctor                          # 环境自检：依赖 / 模型 / 设备工具
python -m atp.cli devices                         # 列出本地 + 远程设备
python -m atp.cli ai   cases/demo.case.yaml       # AI 首跑
python -m atp.cli run  cases/demo.stepir.json     # 零 LLM 回放
python -m atp.cli compile cases/demo.stepir.json  # 生成可执行 .py
python -m atp.cli db                              # 查看用例库
python -m atp.cli web                             # 启动控制台
```

## Web 控制台

```bash
python -m atp.cli web                          # http://127.0.0.1:8765/，自动开浏览器
python -m atp.cli web --port 9000 --no-open
python -m atp.cli web --stop
python -m atp.cli web --status
```

页面里完成三件事：**配模型 → 说一句话 → 看执行**。

- **模型设置**（右上角抽屉）：Base URL / API Key / 模型名 / 视觉模型 / 超时 / 视觉开关 / 调用限速，
  内置豆包、通义、DeepSeek、智谱与本机 Ollama / LM Studio / vLLM 预设，点「测试连接」当场验证。
  配置写回 `atp.config.json`；环境变量 `ATP_MODEL_*` 优先级更高。
- **执行方式**：输入框左下角选平台与设备（首次打开默认 Android），可选 `自动（先查库）` / `AI 首跑` / `仅回放`。
- **实时反馈**：每步推送事件，卡片里显示意图、命中策略、判定方式、耗时、执行前后截图；
  结束后可查看报告 / 下载脚本与 StepIR / 再次回放。点用例只"看"不"跑"，回放与删除走行尾 `···`。
- **调用限速**（默认开）：最小间隔 1200ms、每分钟上限 20 次，撞 429 自动冷却重试；
  付费额度或本地模型可关闭换速度（`ATP_MODEL_THROTTLE=0`）。
- **本地模型**：地址填 `http://127.0.0.1:11434/v1`（Ollama）等，API Key 可留空；
  本机地址自动绕过系统代理。纯文本模型请关闭「视觉能力」，断言改用界面文本判定。
- **中断执行**：执行卡片的【取消】对首跑与回放都生效；模型调用中也能立即中断（请求前 / 退避 / 响应读完都检查取消旗标）。

实现上**没有引入 FastAPI**：`ThreadingHTTPServer` + 长轮询足够支撑单机控制台，内网离线环境也能跑。

常用接口：

| 接口 | 说明 |
|---|---|
| `GET /api/state` | 模型配置、设备清单、用例库、最近运行 |
| `POST /api/model` / `/api/model/test` | 保存配置 / 连通性测试 |
| `POST /api/run` | 发起执行，立即返回 `run_id` |
| `GET /api/runs/{id}/events?since=N&wait=20` | 长轮询取事件（`since` 为游标） |
| `GET /api/runs/{id}/report` | 单文件 HTML 报告 |
| `GET /api/cases/{id}` / `DELETE /api/cases/{id}` | 用例详情 / 删除（同步清库与文件） |
| `POST /api/run/{id}/cancel` | 中断执行 |
| `GET /api/device/cast?platform=&device=` | 投屏状态（方式、帧率、尺寸、`present`） |
| `GET /api/device/cast/stream?platform=&device=&fps=12` | MJPEG 实时流 |
| `POST /api/device/cast/input` | 回注 tap / swipe / key / text，坐标 0~1 |

## 工作原理

### StepIR

每一步都是数据，不藏在执行器代码里：

```json
{
  "step_id": "s02",
  "intent": "在用户名输入框填写 test001",
  "action": "input_text",
  "strategies": [
    {"type": "a11y_id", "value": "com.sunshine.oa:id/et_username", "score": 1.0},
    {"type": "text", "value": "请输入用户名", "node_cls": "EditText", "score": 0.85},
    {"type": "xpath", "value": "//EditText[3]", "score": 0.4},
    {"type": "bounds", "value": [296, 229, 925, 291], "score": 0.2}
  ],
  "args": {"text": "test001"},
  "postcondition": {"kind": "assert_text", "claim": "工作台", "method": "dsl"}
}
```

### 节点编号与模型视野

桌面/Web 界面动辄几百个节点，固定截断会把目标挤出去。做法是从自然语言里抽「」括起来的实体名作为关键词，
按 **关键词命中 > 表单控件 > 可点击 > 其余** 排序，预算 110 条；可编辑区域没有文案时补语义标签
`可编辑输入区`，否则模型永远找不到消息输入框。输入框当前值以 `value='...'` 展示，防止重复输入。

### 六级定位降级

`a11y_id > text > desc > xpath > bounds > visual`

- 每次命中回写权重（命中 +0.05，miss −0.08）——所谓"自愈"就是定位器权重的在线学习。
- `node_cls` 消歧：同文案的输入框和按钮靠控件类型区分。
- `bounds` 归一化到 0~1000，跨分辨率可用；无节点树时合成坐标目标，纯视觉端也能跑同一份用例。

### 视觉兜底

canvas 图表、WebGL 播放器、游戏、`FLAG_SECURE` 页面没有可访问节点。链路是
**首跑 AI 指位置 → 裁模板 → 归一化坐标入库 → 回放本地匹配（零 AI）**；
模板矩形按「当前画布 / 基准画布」实时换算，不需要重新录制。

### 页面门禁与验收闸

- 移动端「树-only 轮询 → 稳定 → 单次截图」门禁，等待期间绝不截图；细指纹因轮播动画永不稳定的页面，
  会在双树校验（目标文案与位置一致）后降级执行，避免回放卡死。
- 验收以**当前界面能看到的东西**为唯一证据。「进入播放页」必须看到播放器画面 / 暂停按钮 / 进度条，
  只有【播放】按钮的详情页不算；模型判 pass 也会被确定性闸门覆写。
- 零交互 done、反复点输入框等打转行为会被主循环打回并注入纠正提示。

### 用例库

沉淀产物是双份，各有职责：

| 形态 | 位置 | 用途 |
|---|---|---|
| 可执行产物 | `cases/<case_id>.stepir.json` + `.py` | 进 Git、进 CI、给人改 |
| 可查询索引 | `data/atp.db`（SQLite） | 相似检索、命中率与 AI 占比统计 |
| 视觉模板 | `cases/assets/<case_id>/*.png` | 图像识别小图，跟用例一起版本管理 |

**文件始终是唯一真相，数据库是索引 + 全量快照**；SQLite 打不开时自动降级为扫文件，不会让控制台停摆。

## 项目架构

七层结构，依赖方向自上而下（下层不知道上层存在）：

```
atp/agents/       ① 智能体：首跑 / 回放 / 感知 / 规划 / 端驱动 / 设备池 / LLM
  runner.py          AI 首跑统一入口（仅按平台分发）
  executor.py        回放统一入口（仅按平台分发）
  runners/           core 组合 mixin；base/setup/perceive/guard/steps/accept/loop
                     pc.py / mobile.py / web.py 三端脚本只覆盖端策略
  executors/         core 组合 mixin；base/observe/popup/recovery/steps
                     pc.py / mobile.py / web.py 三端脚本只覆盖端策略
  planner.py         提示词 / 节点编号 / LlmPlanner / LlmHealer
  perception/        locator / judge / deviation / page_stabilizer / scroller
                     sheets / frames / vision
  platforms/         pc(desktop,electron) mobile(android,harmony) web(cdp,dom_js)
  devices/           本机三端 + 远程真机
  llm/               模型客户端（频控 / 重试 / trace）与视觉定位
atp/webapp/       ② WEB 服务：server.py / jobs.py / cast/ / static/
atp/db/           ③ 数据库及操作：store.py（SQLite）
atp/report/       ④ 报告生成：html.py（截图内嵌的单文件报告）
atp/memory/       ⑤ 记忆沉淀：应用台账 / 弹窗记忆 / 失败沉淀
atp/scriptgen/    ⑥ 脚本生成：compiler.py / dsl.py
atp/runs/         ⑦ 日志与执行产物：artifacts / events / recorder / library
atp/common/       跨层纯数据：types / stepir / profiles
atp/config.py     配置加载（JSON 基线 + ATP_* 环境变量覆盖）
atp/cli.py        命令行入口
```

端差异只写在 `common/profiles.py` 的 `PlatformProfile` 矩阵里，执行链路只问
`profile_for(platform).xxx`，不再散落 `platform in (...)` 判断。

## 平台支持

| 端 | 驱动 | 特点 |
|---|---|---|
| Android | `adb` | UIA 树 + 截图；视频投屏走 `screenrecord`；中文输入需 ADBKeyBoard |
| HarmonyOS | `hdc` | `dumpLayout` + `snapshot_display`；投屏走 JPEG 拉取；自动解析入口 Ability |
| PC 桌面 | UIA / 视觉 | 窗口最大化、录屏；Electron 需以 `--remote-debugging-port` 启动才能走 CDP |
| Web | Chrome CDP | URL 入口、DOM 树、窗口录屏；应用台账不生效 |

## 配置

配置文件 `atp.config.json`（控制台「模型设置」会写回）：

| 分组 | 关键字段 |
|---|---|
| `model` | `base_url` `api_key` `model` `vision_model` `timeout_s` `vision` `throttle` `min_interval_ms` `max_per_min` |
| `vision` | `enabled` `threshold` `scale_range` `scale_steps` |
| `popup` / `cold_start` | 弹窗自动处置、冷启动与等待时长 |
| `apps` / `scroll_find` | 应用台账查找预算、找不到目标时的滑动参数 |
| `page_ready` / `deviation` | 页面门禁、偏离检测与自动回退 |
| 顶层 | `db_path` `cases_dir` `artifacts_dir` `apps_path` `ai_step_budget` |

常用环境变量（优先级高于配置文件）：

| 变量 | 作用 |
|---|---|
| `ATP_MODEL_*` | 覆盖模型 URL / Key / 名称 / 限速等 |
| `ATP_MODEL_NO_PROXY=1` | 本机模型强制直连，绕过系统代理 |
| `ATP_RECORD=0` | 关闭录屏 |
| `ATP_RECORD_LONG_SIDE` / `ATP_RECORD_QUALITY` / `ATP_RECORD_CRF` | 调整 PC/WEB 录屏清晰度与体积 |

## 测试与排障

```bash
python -m unittest discover -s tests -p "test_*.py"   # 592 项
python -m pytest tests/test_video_cast.py -q           # 视频投屏专项
```

每次执行在 `artifacts/<run_id>/` 下留三份 JSONL 日志：

| 文件 | 内容 |
|---|---|
| `events.jsonl` | 全链路主日志：决策、步骤、界面观测、动作请求与结果 |
| `llm.jsonl` | 完整 system/user 提示词 + 模型原始返回 + token 用量 + 耗时 |
| `actions.jsonl` | 动作参数、目标节点快照、驱动返回与证据图 |

`call_id` 把一次调用的请求、返回、解析串起来。排查「模型为什么反复做同一个动作」的顺序：
先看 `llm.jsonl` 的节点列表与原始返回，再看 `events.jsonl` 的 `observation`，
最后用 `actions.jsonl` 确认驱动实际点了哪里。

> 节点树空壳不等于界面坏了：Electron 自绘应用的无障碍树有时只暴露空 Pane/TitleBar，
> 但截图正常。系统在这种情况下会要求模型用 `action=point` 按截图定位，不会让它直接 `done`。

## 扩展一个新平台

一个端由三部分组成，都放在 `atp/agents/platforms/<端>/`：

1. **驱动**：继承 `BaseDriver`，实现 `connect` / `screen_size` / `screenshot` / `dump_tree`，输入原语按需覆盖。
2. **端能力**：在 `common/profiles.py` 加一行 `PlatformProfile`，声明冷启动 / 页面门禁 / 录屏 / 遮挡排除等能力。
3. **发现与构造**：端包提供 `discover(config)` 与 `build(...)`，并注册到 `platforms/__init__.py` 的 `GROUPS`。

定位、判定、沉淀、报告、投屏全部自动可用，不需要改一行用例。

## 已知限制

- 控制台默认只监听 `127.0.0.1` 且**没有鉴权**，暴露给他人前必须先加登录。
- API Key 明文存在 `atp.config.json`；介意就用 `ATP_MODEL_API_KEY` 环境变量。
- ADB 中文输入依赖设备装了 ADBKeyBoard，缺字会明确报错而非静默丢字。
- 播放/直播这类界面永不 idle 是确定性失败，不重试，直接交视觉兜底。
- macOS 原生应用目前只有视觉档位；移动端没有 `maximize` 概念。
- 语义断言在回放时优先走首跑证据与关键词覆盖，想彻底零模型请写 `assert_text` / `assert_url` 等 DSL 断言。
- 视觉模板只解决"位置"不解决"语义"：界面改版后需重跑首跑重新采集。
- Electron 实例必须带 `--remote-debugging-port` 启动，运行中的进程无法事后附加。
- 鸿蒙每步约 4~6s、Android 每步约 5~15s（hdc / uiautomator 往返开销），不会像 DOM 那样毫秒级。
- 有副作用的用例（发消息、提交表单）回放会真的落到系统里，请先准备可回滚的测试数据。

---

ATP 使用 MIT 许可证（见 [LICENSE](LICENSE)）。问题与改进建议欢迎通过 GitHub Issues 反馈。
