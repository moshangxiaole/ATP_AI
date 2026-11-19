# ATP · Agent Test Platform

> **Language / 语言:** **English** · [简体中文](README.md)

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Platforms](https://img.shields.io/badge/Platforms-Android%20%C2%B7%20HarmonyOS%20%C2%B7%20Windows%20%C2%B7%20Web-2F80ED)
![Mode](https://img.shields.io/badge/Replay-Zero%20LLM-2E7D32)
![Console](https://img.shields.io/badge/Console-127.0.0.1%3A8765-6B7280)

**A natural-language-driven, multi-platform automated testing framework. The first run is fully AI-driven; after that, tests run as plain scripts, and AI steps in only on anomalies.**

The lifecycle of a test case:

```
Natural language (.case.yaml)
   │  ai         AI planning + location + execution + per-step screenshots
   ▼
StepIR (.stepir.json)        ← single source of truth; steps are data
   │  compile
   ▼
Executable script (.py)      ← human-readable, editable, CI-ready
   │  run        zero LLM; assertions via DSL
   ▼
Report (report.html)         ← single file, embedded screenshots, offline-auditable
        │
        └──── AI is woken up only when every locator misses; weights are written back
```

## Table of Contents

- [Key Features](#key-features)
- [Quick Start](#quick-start)
- [Web Console](#web-console)
- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [Platform Support](#platform-support)
- [Configuration](#configuration)
- [Testing & Troubleshooting](#testing--troubleshooting)
- [Adding a New Platform](#adding-a-new-platform)
- [Known Limitations](#known-limitations)

## Key Features

- **Record once, replay with zero AI**: the first run writes intent, locator strategies, assertions, and evidence into StepIR; replay only reads data and never calls a model.
- **Six-level locator fallback**: `a11y_id → text → desc → xpath → bounds → visual`, with weights written back on every hit for online self-healing.
- **Real devices first**: Android (ADB) and HarmonyOS (HDC) tree/screenshot capture; PC supports UIA / Electron CDP / vision tiers; Web uses Chrome CDP.
- **Vision fallback**: for canvases, WebGL, and self-drawn UIs with no accessibility tree — point on a screenshot during the first run, then match templates locally on replay, resolution-independent.
- **Page gate + acceptance gate**: tree-only polling waits for a stable page; playback scenarios deterministically distinguish "detail page ≠ playing page" to rule out false passes.
- **Live casting & input injection**: MJPEG stream + touch injection; HarmonyOS uses JPEG polling, Android can use H.264; auto-disconnect on device unplug.
- **Full troubleshooting logs**: every run writes `events.jsonl` / `llm.jsonl` / `actions.jsonl` so model decisions can be replayed step by step.
- **Memory sedimentation**: app package ledger, long-term popup memory, and failed cases excluded from the case library.

Measured baselines (real model + real devices, 2026-11-10):

| Case | Platform | First run | Replay |
|---|---|---|---|
| Send a message to a contact | PC (Electron CDP) | 4/4 steps, 113s | 4 steps recorded |
| Admin console login | Web (Chrome) | 6/6 steps, 105s | 6.6s, **AI involvement 0** |
| Video app search & play | HarmonyOS (HDC) | 10/10 steps | 58.6s, **AI involvement 0** |
| Same case, different platform | Android (ADB) | 10/10 steps | 116.3s, **AI involvement 0** |

## Quick Start

### Requirements

- Python **3.10+**
- Device tools as needed: `adb` (Android), `hdc` (HarmonyOS), Chrome/Chromium (Web), Windows UIA (desktop)
- An OpenAI-compatible model service (cloud or local Ollama / LM Studio / vLLM)

> There is no offline demo mode. The mock virtual device has been removed: the first run requires a configured model and a real device.
> Fake devices for tests live only in `tests/fixtures/console_fakes.py` and never ship with the product.

### One-click launch

| Platform | Action |
|---|---|
| Windows | Double-click `start.bat` |
| macOS / Linux | `./start.sh` |

The first run creates `venv` and installs dependencies automatically; afterwards, each launch = start the server + open the browser.
Double-clicking twice won't start a second process (it detects its own service on the port and reuses it); an occupied port rolls forward automatically (8765 → 8766 …).
Startup also checks model connectivity, so a wrong model name or expired key is reported in the terminal immediately.

```bash
start.bat --port 9000     # custom port
start.bat --status        # check if running
start.bat --stop          # stop (same as stop.bat)
```

### Command line

```bash
python -m atp.cli doctor                          # environment self-check: deps / model / device tools
python -m atp.cli devices                         # list local + remote devices
python -m atp.cli ai   cases/demo.case.yaml       # AI first run
python -m atp.cli run  cases/demo.stepir.json     # zero-LLM replay
python -m atp.cli compile cases/demo.stepir.json  # generate executable .py
python -m atp.cli db                              # inspect the case library
python -m atp.cli web                             # start the web console
```

## Web Console

```bash
python -m atp.cli web                          # http://127.0.0.1:8765/, opens browser automatically
python -m atp.cli web --port 9000 --no-open
python -m atp.cli web --stop
python -m atp.cli web --status
```

Three things on the page: **configure a model → say one sentence → watch it run**.

- **Model settings** (top-right drawer): Base URL / API Key / model name / vision model / timeout / vision toggle / rate limiting, with presets for Doubao, Tongyi, DeepSeek, Zhipu and local Ollama / LM Studio / vLLM; click "Test Connection" to verify on the spot. Settings are written back to `atp.config.json`; `ATP_MODEL_*` environment variables take precedence.
- **Execution mode**: pick platform and device at the bottom-left of the input box (Android by default on first open); choose `Auto (search library first)` / `AI first run` / `Replay only`.
- **Live feedback**: each step pushes an event; the card shows intent, matched strategy, verdict method, latency, and before/after screenshots; on completion you can view the report, download the script and StepIR, or replay again. Clicking a case only "views" it — replay and delete live in the row's `···` menu.
- **Rate limiting** (on by default): 1200ms minimum interval, 20 calls/minute cap, automatic cooldown and retry on 429; disable on paid quotas or local models (`ATP_MODEL_THROTTLE=0`).
- **Local models**: use e.g. `http://127.0.0.1:11434/v1` (Ollama); API Key can be left empty; local addresses automatically bypass the system proxy. For text-only models, turn off "Vision capability" so assertions use on-screen text.
- **Cancellation**: the card's Cancel button works for both first runs and replays; an in-flight model call is interrupted immediately (checked before requests, during backoff, and after response read).

The console deliberately **does not use FastAPI**: `ThreadingHTTPServer` + long polling is enough for a single-machine console and works in offline intranets.

Common endpoints:

| Endpoint | Description |
|---|---|
| `GET /api/state` | Model config, device list, case library, recent runs |
| `POST /api/model` / `/api/model/test` | Save config / connectivity test |
| `POST /api/run` | Start a run, returns `run_id` immediately |
| `GET /api/runs/{id}/events?since=N&wait=20` | Long-poll events (`since` is a cursor) |
| `GET /api/runs/{id}/report` | Single-file HTML report |
| `GET /api/cases/{id}` / `DELETE /api/cases/{id}` | Case detail / delete (clears DB rows and files) |
| `POST /api/run/{id}/cancel` | Cancel a run |
| `GET /api/device/cast?platform=&device=` | Cast status (method, fps, size, `present`) |
| `GET /api/device/cast/stream?platform=&device=&fps=12` | MJPEG live stream |
| `POST /api/device/cast/input` | Inject tap / swipe / key / text, coords in 0~1 |

## How It Works

### StepIR

Every step is data, not hidden in executor code:

```json
{
  "step_id": "s02",
  "intent": "Fill test001 into the username field",
  "action": "input_text",
  "strategies": [
    {"type": "a11y_id", "value": "com.example.app:id/et_username", "score": 1.0},
    {"type": "text", "value": "Enter username", "node_cls": "EditText", "score": 0.85},
    {"type": "xpath", "value": "//EditText[3]", "score": 0.4},
    {"type": "bounds", "value": [296, 229, 925, 291], "score": 0.2}
  ],
  "args": {"text": "test001"},
  "postcondition": {"kind": "assert_text", "claim": "Dashboard", "method": "dsl"}
}
```

### Node numbering and model vision

Desktop/Web UIs can expose hundreds of nodes; a fixed truncation pushes the target out of view. The fix: extract entities enclosed in 「」 from the natural language as keywords, sort by **keyword hit > form control > clickable > rest** with a budget of 110, and give editable regions without text a semantic label `editable input area`. The current input value is shown as `value='...'` to prevent repeated typing.

### Six-level locator fallback

`a11y_id > text > desc > xpath > bounds > visual`

- Every hit writes back its weight (hit +0.05, miss −0.08) — "self-healing" is simply online learning of locator weights.
- `node_cls` disambiguates an input and a button sharing the same text.
- `bounds` are normalized to 0~1000 for cross-resolution use; a coordinate target is synthesized when no tree is available.

### Vision fallback

Canvas charts, WebGL players, games, and `FLAG_SECURE` screens have no accessible nodes. The chain is
**AI points on the screenshot → crop a template → normalized coordinates in DB → local matching on replay (zero AI)**;
template rectangles are rescaled by "current canvas / recorded canvas", so no re-recording is needed.

### Page gate & acceptance gate

- Mobile: "tree-only polling → stable → single screenshot" gate; pages whose fine fingerprint never settles due to animation are downgraded after a two-tree check (same target text and position) so replay cannot deadlock.
- Acceptance uses only **what is visible on the current screen**. "Enter the playing page" requires a player surface / pause button / progress bar — a detail page with only a Play button does not count; even a model "pass" is overridden by the deterministic gate.
- Zero-interaction `done` and repeated taps on an input field are pushed back by the main loop with corrective hints.

### Case library

Sedimented artifacts come in two forms:

| Form | Location | Purpose |
|---|---|---|
| Executable artifacts | `cases/<case_id>.stepir.json` + `.py` | Git, CI, human editing |
| Queryable index | `data/atp.db` (SQLite) | Similarity search, hit rate and AI-ratio stats |
| Vision templates | `cases/assets/<case_id>/*.png` | Template images versioned with the case |

**Files are always the source of truth; the database is an index + full snapshot.** If SQLite fails to open, it falls back to scanning files rather than taking down the console.

## Architecture

Seven layers, dependencies point downward (lower layers know nothing about upper ones):

```
atp/agents/       ① Agents: first run / replay / perception / planning / drivers / devices / LLM
  runner.py          AI first-run entry (platform dispatch only)
  executor.py        Replay entry (platform dispatch only)
  runners/           core composes mixins; base/setup/perceive/guard/steps/accept/loop
                     pc.py / mobile.py / web.py override platform-specific strategies only
  planner.py         Prompts / node numbering / LlmPlanner / LlmHealer
  perception/        locator / judge / deviation / page_stabilizer / scroller / sheets / frames / vision
  platforms/         pc(desktop,electron) mobile(android,harmony) web(cdp,dom_js)
  devices/           Local platforms + remote devices
  llm/               Model client (throttling / retry / trace) and visual grounding
atp/webapp/       ② Web service: server.py / jobs.py / cast/ / static/
atp/db/           ③ Database: store.py (SQLite)
atp/report/       ④ Reports: html.py (single file, embedded screenshots)
atp/memory/       ⑤ Memory: app ledger / popup memory / failure sedimentation
atp/scriptgen/    ⑥ Script generation: compiler.py / dsl.py
atp/runs/         ⑦ Logs & artifacts: artifacts / events / recorder / library
atp/common/        Cross-layer pure data: types / stepir / profiles
atp/config.py      Config loading (JSON baseline + ATP_* overrides)
atp/cli.py         CLI entry
```

Platform differences live only in the `PlatformProfile` matrix in `common/profiles.py`; the execution chain asks `profile_for(platform).xxx` instead of scattering `platform in (...)` checks.

## Platform Support

| Platform | Driver | Notes |
|---|---|---|
| Android | `adb` | UIA tree + screenshots; video casting via `screenrecord`; Chinese input needs ADBKeyBoard |
| HarmonyOS | `hdc` | `dumpLayout` + `snapshot_display`; JPEG casting; auto-resolves the entry Ability |
| PC desktop | UIA / vision | Window maximize and recording; Electron requires `--remote-debugging-port` for CDP |
| Web | Chrome CDP | URL entry, DOM tree, window recording; app ledger not applied |

## Configuration

Config file `atp.config.json` (the console's "Model settings" writes back to it):

| Group | Key fields |
|---|---|
| `model` | `base_url` `api_key` `model` `vision_model` `timeout_s` `vision` `throttle` `min_interval_ms` `max_per_min` |
| `vision` | `enabled` `threshold` `scale_range` `scale_steps` |
| `popup` / `cold_start` | Popup handling, cold start and settle durations |
| `apps` / `scroll_find` | App ledger lookup budget, scroll-to-find parameters |
| `page_ready` / `deviation` | Page gate, deviation detection and auto-rollback |
| Top level | `db_path` `cases_dir` `artifacts_dir` `apps_path` `ai_step_budget` |

Common environment variables (higher priority than the config file):

| Variable | Purpose |
|---|---|
| `ATP_MODEL_*` | Override model URL / key / name / rate limits |
| `ATP_MODEL_NO_PROXY=1` | Force direct connection for local models, bypassing the system proxy |
| `ATP_RECORD=0` | Disable recording |
| `ATP_RECORD_LONG_SIDE` / `ATP_RECORD_QUALITY` / `ATP_RECORD_CRF` | Tune PC/Web recording quality and size |

## Testing & Troubleshooting

```bash
python -m unittest discover -s tests -p "test_*.py"   # 592 tests
python -m pytest tests/test_video_cast.py -q           # video casting suite
```

Every run leaves three JSONL logs in `artifacts/<run_id>/`:

| File | Content |
|---|---|
| `events.jsonl` | Full-chain main log: decisions, steps, observations, action requests and results |
| `llm.jsonl` | Complete system/user prompts + raw model response + token usage + latency |
| `actions.jsonl` | Action params, target node snapshot, driver result and evidence images |

`call_id` ties a call's request, response, and parse together. To debug "why does the model keep doing the same thing":
check the node list and raw response in `llm.jsonl` first, then `observation` in `events.jsonl`, and finally `actions.jsonl` for what the driver actually clicked.

> A hollow node tree does not mean the UI is broken: self-drawn Electron apps sometimes expose only an empty Pane/TitleBar in the accessibility tree while screenshots render fine. The system then requires `action=point` with a screenshot description instead of letting the model `done`.

## Adding a New Platform

A platform consists of three parts under `atp/agents/platforms/<platform>/`:

1. **Driver**: subclass `BaseDriver`, implement `connect` / `screen_size` / `screenshot` / `dump_tree`, override input primitives as needed.
2. **Capabilities**: add a `PlatformProfile` row in `common/profiles.py` declaring cold start / page gate / recording / occlusion exclusion.
3. **Discovery & construction**: provide `discover(config)` and `build(...)`, and register the package in `GROUPS` in `platforms/__init__.py`.

Locators, verdicts, sedimentation, reports, and casting work automatically — no test case changes needed.

## Known Limitations

- The console listens on `127.0.0.1` by default with **no authentication**; add a login before exposing it to others.
- The API Key is stored in plaintext in `atp.config.json`; use the `ATP_MODEL_API_KEY` environment variable if that is a concern.
- ADB Chinese input needs ADBKeyBoard installed on the device; missing it reports an explicit error rather than silently dropping characters.
- Playback/live UIs that never go idle are a deterministic failure — no retry, straight to vision fallback.
- macOS native apps currently have vision tier only; mobile platforms have no `maximize`.
- Replay assertions prefer first-run evidence and keyword coverage; for strictly zero-model runs use DSL assertions such as `assert_text` / `assert_url`.
- Vision templates solve "where", not "what": a UI redesign drops the match score and requires a new first run to recapture.
- Electron instances must be launched with `--remote-debugging-port`; a running process cannot be attached to afterwards.
- HarmonyOS takes ~4–6s per step and Android ~5–15s (hdc / uiautomator round trips); not DOM-level milliseconds.
- Cases with side effects (sending messages, submitting forms) really happen on replay — prepare rollback-friendly test data.

---

ATP is released under the MIT License (see [LICENSE](LICENSE)). Issues and suggestions are welcome via GitHub Issues.
