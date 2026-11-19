"""Web 控制台服务端（标准库实现，零额外依赖）。

为什么不用 FastAPI：这套东西要能塞进客户内网、离线机器、以及只有 Python 标准库的
CI 镜像里。http.server + ThreadingHTTPServer 足够撑住单机控制台的并发，代价只是
少一点语法糖。事件推送用长轮询而不是 SSE——浏览器断线重连、代理超时那些坑不值得踩。
"""

from __future__ import annotations

import json
import mimetypes
import queue
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# 执行录屏（recording.mp4）的 Content-Type：Windows 注册表不一定认 .mp4，
# 猜成 octet-stream 的话浏览器只会下载而不会内嵌播放。
mimetypes.add_type("video/mp4", ".mp4")

from atp.agents.llm.client import LLMClient, LLMError, configure_throttle
from atp.config import Config, ModelConfig
from atp.common.stepir import CaseIR
from atp.agents.devices.manager import DeviceManager
from atp.db.store import CaseDB
from atp.runs.artifacts import RECORDING_FILE
from atp.webapp.cast.hub import DEFAULT_FPS, MAX_FPS, SUPPORTED, CastHub
from atp.webapp.jobs import RunStore, start_run
from atp.webapp.cast.video import VideoHub

STATIC = Path(__file__).parent / "static"
MAX_BODY = 4 * 1024 * 1024
APP_MARK = "atp-console"
APP_VERSION = "0.2"


class _Server(ThreadingHTTPServer):
    """屏蔽客户端断连的噪音。

    浏览器关标签、刷新、长轮询超时都会让 sendall 抛 ConnectionAborted(10053)。
    默认实现会打一整屏 traceback，看起来像服务挂了，实际上一点事都没有。
    """

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class App:
    """控制台应用：持有配置、用例库、设备池与正在运行的任务。"""
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = RunStore()
        self.cast = CastHub(cfg)          # 右侧设备面板的实时投屏（按设备共享抓帧线程）
        self.video = VideoHub(cfg)        # 视频模式投屏（screenrecord H264 → WebCodecs）
        self.config_file = cfg.home / "atp.config.json"
        self.db = CaseDB(cfg.db_file)
        # 老工程的用例都是文件形态，启动时回填一次库，之后检索直接吃索引
        try:
            self.imported = self.db.import_cases(cfg.cases)
        except Exception:  # noqa: BLE001
            self.imported = 0

    # ---------- 模型配置 ----------
    def model_view(self) -> dict:
        m = self.cfg.model
        return {
            "base_url": m.base_url,
            "model": m.model,
            "vision_model": m.vision_model,
            "temperature": m.temperature,
            "max_tokens": m.max_tokens,
            "timeout_s": m.timeout_s,
            "vision": m.vision,
            "throttle": m.throttle,
            "min_interval_ms": m.min_interval_ms,
            "max_per_min": m.max_per_min,
            "is_local": m.is_local,
            "configured": m.configured,
            "api_key_masked": _mask(m.api_key),
            "has_key": bool(m.api_key),
        }

    @staticmethod
    def _apply_model_form(m, data: dict, *, create: bool = False):
        """把界面表单写进 ModelConfig（save / test / list 共用一份口径）。"""
        if "base_url" in data:
            m.base_url = str(data.get("base_url") or "").strip()
        if "model" in data:
            m.model = str(data.get("model") or "").strip()
        if "vision_model" in data:
            m.vision_model = str(data.get("vision_model") or "").strip()
        if data.get("api_key"):
            m.api_key = str(data["api_key"]).strip()
        try:
            if "temperature" in data and data.get("temperature") not in (None, ""):
                m.temperature = float(data["temperature"])
        except (TypeError, ValueError):
            pass
        try:
            if data.get("timeout_s") not in (None, ""):
                m.timeout_s = max(5, min(600, int(float(data["timeout_s"]))))
        except (TypeError, ValueError):
            pass
        if data.get("vision") is not None:
            m.vision = bool(data["vision"])
        # 频次限制：开关 + 两个具体数值，界面保存后立即生效（写库即用）
        if data.get("throttle") is not None:
            m.throttle = bool(data["throttle"])
        for key, lo, hi in (("min_interval_ms", 0, 60000), ("max_per_min", 0, 600)):
            val = data.get(key)
            if val in (None, ""):
                continue
            try:
                setattr(m, key, max(lo, min(hi, int(float(val)))))
            except (TypeError, ValueError):
                pass
        if create:      # 台账里 vision_model 留空 = 复用主模型
            if not m.vision_model:
                m.vision_model = m.model
        return m

    def save_model(self, data: dict) -> dict:
        m = self._apply_model_form(self.cfg.model, data, create=True)
        if not m.vision_model:
            m.vision_model = m.model

        raw: dict = {}
        if self.config_file.exists():
            try:
                raw = json.loads(self.config_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raw = {}
        raw["model"] = {
            "base_url": m.base_url, "api_key": m.api_key, "model": m.model,
            "vision_model": m.vision_model, "temperature": m.temperature,
            "max_tokens": m.max_tokens, "timeout_s": m.timeout_s, "vision": m.vision,
            "throttle": m.throttle, "min_interval_ms": m.min_interval_ms,
            "max_per_min": m.max_per_min,
        }
        self.config_file.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 保存即生效：同步刷新进程级限速闸门
        configure_throttle(m)
        return self.model_view()

    def _probe_config(self, data: dict | None) -> ModelConfig:
        """按表单临时拼一份配置做探测：不落盘，用户点「测试」就能试完再保存。"""
        base = self.cfg.model
        m = ModelConfig(
            base_url=base.base_url, api_key=base.api_key, model=base.model,
            vision_model=base.vision_model, temperature=base.temperature,
            max_tokens=base.max_tokens, timeout_s=base.timeout_s, vision=base.vision,
            throttle=base.throttle, min_interval_ms=base.min_interval_ms,
            max_per_min=base.max_per_min,
        )
        if data:
            self._apply_model_form(m, data)
        return m

    def test_model(self, data: dict | None = None) -> dict:
        m = self._probe_config(data)
        # 本地模型首次加载可能要几十秒，按配置给足时间；云端仍压到 40s 以免界面干等
        timeout = min(40, m.timeout_s) if not m.is_local else max(30, m.timeout_s)
        client = LLMClient(m, timeout=timeout)
        if not client.available:
            return {"ok": False,
                    "detail": "Base URL 与模型名必须填写；公网服务还需要 API Key"
                              "（本地/局域网可留空）"}
        started = _now_ms()
        try:
            reply = client.chat(
                [{"role": "user", "content": "只回复两个字：就绪"}], max_tokens=16
            )
        except LLMError as e:
            return {"ok": False, "detail": str(e), "path": client.last_path,
                    "latency_ms": _now_ms() - started}
        except Exception as e:  # noqa: BLE001 - 网络层异常统一回报给界面
            return {"ok": False, "detail": f"{type(e).__name__}: {e}",
                    "path": client.last_path, "latency_ms": _now_ms() - started}
        return {"ok": True, "detail": (reply or "").strip()[:40] or "连通",
                "path": client.last_path, "latency_ms": _now_ms() - started}

    def list_models(self, data: dict | None = None) -> dict:
        """列出服务端可用模型（本地模型部署名千奇百怪，给个下拉省得手打）。"""
        m = self._probe_config(data)
        if not m.base_url:
            return {"ok": False, "detail": "请先填写 Base URL", "models": []}
        client = LLMClient(m, timeout=min(20, max(8, m.timeout_s)))
        try:
            models = client.list_models()
        except LLMError as e:
            return {"ok": False, "detail": str(e), "models": [], "path": client.last_path}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"{type(e).__name__}: {e}", "models": []}
        return {"ok": True, "detail": f"共 {len(models)} 个模型", "models": models,
                "path": client.last_path}

    # ---------- 设备与用例库 ----------
    def devices(self) -> list[dict]:
        try:
            return [d.as_dict() for d in DeviceManager(self.cfg).discover()]
        except Exception:
            return []

    def cases(self) -> list[dict]:
        """用例列表：优先读库（带命中次数/上次结果），库不可用再扫文件。"""
        out: list[dict] = []
        try:
            rows = self.db.list_cases(limit=300)
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            for r in rows:
                out.append({
                    "case_id": r["case_id"],
                    "title": r["title"],
                    "platform": r["platform"],
                    "steps": int(r["step_count"] or 0),
                    "images": int(r["image_steps"] or 0),
                    "nl": r["nl"],
                    "created_at": float(r["created_at"] or 0),
                    "updated_at": float(r["updated_at"] or 0),
                    "hit_count": int(r["hit_count"] or 0),
                    "last_status": r["last_status"] or "",
                    "screen": f"{r['base_width']}x{r['base_height']}",
                    "has_script": bool(r["script_path"]),
                    "source": r["source"] or "",
                })
            out.sort(key=lambda c: c["updated_at"] or c["created_at"], reverse=True)
            return out

        for path in sorted(self.cfg.cases.glob("*.stepir.json")):
            try:
                case = CaseIR.load(path)
            except Exception:
                continue
            out.append({
                "case_id": case.case_id,
                "title": case.title,
                "platform": case.platform,
                "steps": len(case.steps),
                "images": sum(1 for s in case.steps if s.visual),
                "nl": case.nl,
                "created_at": case.created_at,
                "updated_at": case.created_at,
                "hit_count": 0,
                "last_status": "",
                "screen": f"{case.screen.width}x{case.screen.height}",
                "has_script": path.with_suffix(".py").exists(),
                "source": case.created_by,
            })
        out.sort(key=lambda c: c["created_at"], reverse=True)
        return out

    def db_info(self) -> dict:
        try:
            info = self.db.info()
        except Exception as e:  # noqa: BLE001
            info = {"engine": "sqlite", "ok": False, "error": str(e), "tables": []}
        info["imported_on_start"] = getattr(self, "imported", 0)
        return info

    def case_file(self, case_id: str, kind: str) -> Path | None:
        safe = _safe_name(case_id)
        if not safe:
            return None
        base = self.cfg.cases / safe
        path = base.with_suffix(".stepir.json") if kind == "stepir" else base.with_suffix(".py")
        return path if path.exists() else None

    # ---------- 用例详情 / 删除 ----------
    def case_detail(self, case_id: str) -> dict | None:
        """用例详情：主表信息 + 步骤清单 + 历史执行列表。

        左侧列表点一下要看的正是这个——"这条用例到底是什么、跑过几次、每次都什么样"。
        执行明细（每一步的截图）按需再取，避免一次把几百张图的信息推到前端。
        """
        safe = _safe_name(case_id)
        if not safe:
            return None
        meta = self.db.case_meta(safe)
        case = self.db.get_case(safe)
        if meta is None and case is None:
            return None

        steps = self.db.case_steps(safe)
        if not steps and case is not None:
            steps = [{"idx": i, "step_id": s.step_id, "intent": s.intent,
                      "action": s.action.value, "expect_kind": s.postcondition.kind,
                      "expect_claim": s.postcondition.claim, "source": s.source,
                      "locators": [f"{x.type}:{x.value}" for x in s.strategies][:3]}
                     for i, s in enumerate(case.steps, 1)]

        runs = [self._run_view(r) for r in self.db.runs(limit=20, case_id=safe)]
        info = {
            "case_id": safe,
            "title": (meta or {}).get("title") or (case.title if case else ""),
            "platform": (meta or {}).get("platform") or (case.platform if case else ""),
            "nl": (meta or {}).get("nl") or (case.nl if case else ""),
            "source": (meta or {}).get("source") or (case.created_by if case else ""),
            "steps": int((meta or {}).get("step_count") or len(steps)),
            "images": int((meta or {}).get("image_steps") or 0),
            "hit_count": int((meta or {}).get("hit_count") or 0),
            "run_count": int((meta or {}).get("run_count") or len(runs)),
            "last_status": (meta or {}).get("last_status") or "",
            "updated_at": float((meta or {}).get("updated_at") or 0),
            "created_at": float((meta or {}).get("created_at") or 0),
            "screen": f"{(meta or {}).get('base_width') or 0}x{(meta or {}).get('base_height') or 0}",
            "has_script": bool((meta or {}).get("script_path")),
        }
        return {"case": info, "steps": steps, "runs": runs}

    @staticmethod
    def _run_view(row: dict) -> dict:
        run_id = row.get("run_id", "")
        report = str(row.get("report_path") or "")
        return {
            "run_id": run_id,
            "case_id": row.get("case_id", ""),
            "title": row.get("title", ""),
            "platform": row.get("platform", ""),
            "device": row.get("device_id", ""),
            "mode": row.get("mode", ""),
            "status": row.get("status", ""),
            "started_at": float(row.get("started_at") or 0),
            "duration_ms": int(row.get("duration_ms") or 0),
            "total_steps": int(row.get("total_steps") or 0),
            "ai_steps": int(row.get("ai_steps") or 0),
            "has_report": bool(report) and Path(report).exists(),
        }

    def history_run(self, run_id: str) -> dict | None:
        """历史执行（不在内存任务表里也能取）：主信息 + 每步明细 + 截图地址。"""
        row = self.db.get_run(run_id)
        if row is None:
            return None
        view = self._run_view(row)
        steps = []
        for s in self.db.run_steps(run_id):
            shot = str(s.get("screenshot") or "")
            steps.append({
                "idx": int(s.get("idx") or 0),
                "step_id": s.get("step_id", ""),
                "action": s.get("action", ""),
                "intent": s.get("intent", ""),
                "status": s.get("status", ""),
                "strategy_used": s.get("strategy_used", ""),
                "judge_method": s.get("judge_method", ""),
                "latency_ms": int(s.get("latency_ms") or 0),
                "ai_used": bool(s.get("ai_used")),
                "error": s.get("error", ""),
                "image": f"/api/runs/{run_id}/artifacts/{shot}" if shot else "",
            })
        view["artifacts_dir"] = row.get("artifacts_dir", "")
        view["report_path"] = row.get("report_path", "")
        art = Path(view["artifacts_dir"]) if view["artifacts_dir"] else None
        view["video"] = (f"/api/runs/{run_id}/artifacts/recording.mp4"
                         if art is not None and (art / RECORDING_FILE).exists() else "")
        return {"run": view, "steps": steps}

    def delete_case(self, case_id: str) -> dict | None:
        """删除用例：库里的记录 + 磁盘上的可执行产物 + 历史执行产物。

        "相关文件"包括三类，缺一类都会留下垃圾：脚本（.stepir.json / .py）、
        视觉模板（cases/assets/<case_id>/）、每次执行的 artifacts 目录。
        删除只允许落在 cases / artifacts 这两个根目录下，防止一个手滑的
        case_id 把整个工程带走。
        """
        import shutil

        safe = _safe_name(case_id)
        if not safe:
            return None
        meta = self.db.case_meta(safe)
        row_runs = self.db.runs(limit=500, case_id=safe)
        has_file = any((self.cfg.cases / f"{safe}{ext}").exists()
                       for ext in (".stepir.json", ".py"))
        if meta is None and not row_runs and not has_file:
            return None

        counts = self.db.delete_case(safe)
        removed: list[str] = []
        for p in (self.cfg.cases / f"{safe}.stepir.json", self.cfg.cases / f"{safe}.py"):
            if p.exists():
                try:
                    p.unlink()
                    removed.append(str(p))
                except OSError:
                    pass

        asset_dir = self.cfg.assets / safe
        if asset_dir.is_dir() and _under(asset_dir, self.cfg.cases):
            shutil.rmtree(asset_dir, ignore_errors=True)
            removed.append(str(asset_dir))

        for r in row_runs:
            art = Path(r.get("artifacts_dir") or (self.cfg.artifacts / r["run_id"]))
            if art.is_dir() and _under(art, self.cfg.artifacts):
                shutil.rmtree(art, ignore_errors=True)
                removed.append(str(art))

        return {
            "case_id": safe,
            "title": (meta or {}).get("title", ""),
            "db": counts,
            "files": removed,
        }


    # ---------- 取消 ----------
    def cancel_run(self, job) -> dict:
        """请求中断一次执行。

        这里只负责"举起旗子"，真正停下来靠执行线程自己轮询：它在每一步之间、
        每次等待界面稳定、每次滑动查找、每轮断言轮询时都会看这面旗子。
        早先靠"下一个事件到达时抛异常"，一步动作要是耗 30 秒，用户就干等 30 秒，
        看起来就像点了没反应。
        """
        terminal = job.status in ("passed", "failed", "error", "cancelled")
        if terminal:
            return {"status": job.status, "cancelling": False,
                    "message": "任务已经结束，无需中断"}
        job.request_cancel()
        if job.status == "queued":
            job.status = "cancelled"
        return {"status": job.status, "cancelling": True,
                "message": "已请求中断，当前动作返回后立即停止"}

    def state(self) -> dict:
        return {
            # app 是给「一键启动」用的身份标记：端口上跑的是不是 ATP，靠它认
            "app": APP_MARK,
            "version": APP_VERSION,
            "model": self.model_view(),
            "devices": self.devices(),
            "cases": self.cases(),
            "runs": self.store.recent(12),
            "db": self.db_info(),
            "vision": {
                "enabled": self.cfg.vision.enabled,
                "threshold": self.cfg.vision.threshold,
                "assets_dir": str(self.cfg.assets),
            },
            "home": str(self.cfg.home),
            "toolchain": {"adb": bool(self.cfg.adb), "hdc": bool(self.cfg.hdc)},
            "cast": {
                "supported": sorted(SUPPORTED),
                "default_fps": DEFAULT_FPS,
                "max_fps": MAX_FPS,
                "max_width": 420,
            },
        }


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    """HTTP 请求处理器：静态资源、REST API 与投屏流的统一入口。"""
    server_version = "ATP-Console/0.2"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return  # 保持终端干净，只打启动信息

    # ---------- 分发 ----------
    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except (ConnectionError, BrokenPipeError):
            return  # 浏览器关标签 / 刷新 / 长轮询超时都会走到这，不是错误
        except Exception as e:  # noqa: BLE001
            self._fail(500, f"{type(e).__name__}: {e}")

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route_post()
        except (ConnectionError, BrokenPipeError):
            return
        except ValueError as e:
            self._fail(400, str(e))
        except Exception as e:  # noqa: BLE001
            self._fail(500, f"{type(e).__name__}: {e}")

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            self._route_delete()
        except (ConnectionError, BrokenPipeError):
            return
        except ValueError as e:
            self._fail(400, str(e))
        except Exception as e:  # noqa: BLE001
            self._fail(500, f"{type(e).__name__}: {e}")

    def _fail(self, code: int, msg: str) -> None:
        """回错误也要防着对端已经跑了，否则又是一条 traceback。"""
        try:
            self._json(code, {"error": msg})
        except (ConnectionError, BrokenPipeError, OSError):
            pass

    def _route_get(self) -> None:
        url = urlparse(self.path)
        path, query = unquote(url.path), parse_qs(url.query)
        parts = [p for p in path.split("/") if p]

        if not parts or path == "/":
            return self._file(STATIC / "index.html", "text/html; charset=utf-8")
        if parts[0] in ("app.css", "app.js", "app.svg"):
            ctype = mimetypes.guess_type(parts[0])[0] or "application/octet-stream"
            return self._file(STATIC / parts[0], ctype + "; charset=utf-8")

        if parts[:1] == ["api"] and len(parts) >= 2:
            if parts[1] == "state":
                return self._json(200, self.app.state())
            if parts[1] == "devices":
                return self._json(200, {"devices": self.app.devices()})
            if parts[1] == "cases" and len(parts) == 4:
                kind = parts[3]
                f = self.app.case_file(parts[2], kind)
                if not f:
                    return self._json(404, {"error": "用例文件不存在"})
                ctype = "application/json; charset=utf-8" if kind == "stepir" else "text/plain; charset=utf-8"
                return self._file(f, ctype, download=f.name)
            if parts[1] == "cases" and len(parts) == 3:
                detail = self.app.case_detail(parts[2])
                if detail is None:
                    return self._json(404, {"error": "用例不存在"})
                return self._json(200, detail)
            if parts[1] == "history" and len(parts) == 3:
                hist = self.app.history_run(parts[2])
                if hist is None:
                    return self._json(404, {"error": "执行记录不存在"})
                return self._json(200, hist)
            if parts[1] == "cases":
                return self._json(200, {"cases": self.app.cases()})
            if parts[1] == "db":
                return self._json(200, self.app.db_info())
            if parts[1] == "device" and len(parts) >= 3 and parts[2] == "cast":
                return self._cast(parts, query)
            if parts[1] == "runs":
                return self._runs(parts)

        return self._json(404, {"error": "not found", "path": path})

    # ---------- 实时投屏 ----------
    def _cast(self, parts: list[str], query: dict) -> None:
        hub = self.app.cast
        device = (query.get("device") or [""])[0]
        platform = (query.get("platform") or ["android"])[0]
        try:
            fps = int((query.get("fps") or [str(DEFAULT_FPS)])[0])
        except (TypeError, ValueError):
            fps = DEFAULT_FPS
        action = parts[3] if len(parts) > 3 else ""

        if not action:
            return self._json(200, hub.status(platform, device))
        if action == "frame":
            try:
                jpeg = hub.grab_once(platform, device)
            except Exception as e:  # noqa: BLE001
                return self._json(503, {"error": str(e)})
            return self._blob(jpeg, "image/jpeg")
        if action == "stream":
            return self._cast_stream(platform, device, fps)
        if action == "latest":
            try:
                after = int((query.get("after") or ["0"])[0])
            except (TypeError, ValueError):
                after = 0
            return self._cast_latest(platform, device, fps, after)
        if action == "video":
            return self._cast_video(platform, device)
        if action == "frame":
            return self._cast_frame(platform, device)
        return self._json(404, {"error": "not found"})

    def _cast_frame(self, platform: str, device: str) -> None:
        """单帧 JPEG 快照 —— 视频模式的海报帧。

        screenrecord 在画面静止时连 SPS 都不输出，刚连上的几秒视频流是静默的；
        前端先抓这张快照画上 canvas，视频流一到无缝接管。缺了它就是黑屏。
        """
        try:
            frame = self.app.cast.grab_once(platform, device)
        except Exception as exc:  # noqa: BLE001
            return self._json(400, {"error": str(exc)})
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(frame)

    def _cast_video(self, platform: str, device: str) -> None:
        """视频模式（首选）：screenrecord 裸 H264 长轮询式推流。

        浏览器用 WebCodecs 硬解画到 canvas 上：PC 端零解码只搬运字节，
        canvas.drawImage 原子渲染 —— 不存在「换 src 闪烁」和「积压回放旧页」。
        线协议：每条记录 = 4 字节大端长度 + 一个 NAL（不含起始码）。
        前端把 SPS/PPS 作为配置、首个 IDR 起才开始解码。
        """
        from atp.webapp.cast.video import NAL_IDR, NAL_SPS, NAL_PPS  # noqa: F401  (文档用)
        # 视频模式是 adb screenrecord 专用：鸿蒙没有这条通道，硬走只会
        # 挂一个永远产不出帧的 screenrecord，前端画面冻在海报帧（真机复现）。
        # 鸿蒙必须走 JPEG 拉取链路（/cast/latest），由前端按平台分流。
        if platform != "android":
            return self._json(400, {"error": "视频模式仅支持 Android 真机，鸿蒙走 JPEG 实时帧"})
        hub = self.app.video
        ch = hub.channel(device)
        sub = ch.subscribe()
        import struct

        def write_record(nal: bytes) -> bool:
            try:
                self.wfile.write(struct.pack(">I", len(nal)) + nal)
                return True
            except (OSError, ValueError):
                return False

        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/h264")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("X-Video-Size", f"{ch.size[0]}x{ch.size[1]}")
            # 不带 Content-Length / chunked：HTTP/1.1 下读直到连接关闭，
            # 浏览器 fetch 会随到随读。这样不用自己写 chunk 分帧。
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            resync = True
            waiting_idr = True
            while True:
                try:
                    _cfg, is_idr, nal = sub.q.get(timeout=2.0)
                except queue.Empty:
                    if not ch.status()["running"]:
                        break       # 通道已停（设备拔了）：关流，前端自动重连
                    continue
                if not sub.alive:
                    break
                if resync:
                    cfg_nals = ch.config_nals()
                    for c in cfg_nals:
                        if not write_record(c):
                            return
                    resync = False
                    waiting_idr = True
                if waiting_idr and not is_idr:
                    continue        # 还没等到关键帧，普通帧跳过（给出去也解不了）
                waiting_idr = False
                if not write_record(nal):
                    break
        finally:
            ch.unsubscribe(sub)

    def _cast_latest(self, platform: str, device: str, fps: int, after: int = 0) -> None:
        """拉取模式（长轮询）：只返回比 `after` 更新的那一帧，客户端控节奏。

        为什么不用 MJPEG 长连接（`/stream`）：那是服务端往浏览器灌，客户端一旦
        解码跟不上，帧就在浏览器里排队、开始回放几秒前的画面——真机复现过：
        用户已经点回消息首页，面板还在重播刚才那几页（聊天详情 → 通讯录 → 我的）
        来回闪。拉取模式下客户端拿到一帧、画完、再来要下一帧，自己就是节拍器，
        结构上不可能积压，慢的客户端只会看到帧率下降，而不是画面时间线倒退。

        `after` 是客户端手上那一帧的序号：没有更新时最多挂 1.5s 再回 204，
        静止画面因此几乎不产生流量（否则客户端会以百次/秒重复拉同一张图）。
        """
        hub = self.app.cast
        ch = hub.subscribe(platform, device, fps)
        if ch is None:
            return self._json(400, {"error": "平台不支持投屏，或设备号无效"})
        try:
            # 抓帧线程空闲 20s 会退出并丢掉通道，序号从 0 重来；这时客户端的
            # after 会比服务端大，不夹一下就会永远等不到新帧（面板直接冻住）。
            if after > ch.seq:
                after = 0
            frame, seq, err = hub.wait_frame(ch, after, timeout=1.5)
            if seq <= after:
                self.send_response(204)          # 没有新画面：不重复传图
                self.send_header("X-Frame-Seq", str(seq))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if not frame:
                frame = hub.placeholder(ch, err or "screencast unavailable")
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Frame-Seq", str(seq))   # 客户端下一轮带上它
            self.end_headers()
            self.wfile.write(frame)
        finally:
            hub.unsubscribe(ch)

    def _cast_stream(self, platform: str, device: str, fps: int) -> None:
        """MJPEG：一条长连接持续推帧，浏览器 <img> 直接渲染。

        关键改动（解决「切换页面/操作时画面卡 5 秒、字一个字蹦」）：
        - 用短超时（1s）等帧，没有新帧就不写——浏览器会保留上一帧，绝不会
          出现 5 秒黑窗/转圈；帧一到位立刻推，延迟压到 1 秒以内。
        - 只在「序号前进」或「出现错误」时写，不在静止时反复刷同一张图。
        - 连接初期若 1.5s 还拿不到首帧，推一张「连接中」占位，避免一直转圈。
        """
        hub = self.app.cast
        ch = hub.subscribe(platform, device, fps)
        if ch is None:
            return self._json(400, {"error": "平台不支持投屏，或设备号无效"})

        boundary = b"atpcast"
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=atpcast")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        # 关掉 Nagle：每帧只有一次写，开着它会让小包等 ACK，凭空多出几十毫秒抖动。
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        def write_part(frame: bytes) -> None:
            # 头 + 图 + 结尾一次写完：分三次写会在高帧率下被拆成多个 TCP 段，
            # 客户端更容易读到「半个帧」，视觉上就是撕裂/闪一下。
            head = (b"--" + boundary + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n")
            self.wfile.write(head + frame + b"\r\n")
            self.wfile.flush()

        seq = 0
        started = time.time()
        # 「连接中」占位只允许发一次，**绝不能**在收到真实帧后再置回 False：
        # 静止画面下推流端会按内容去重，本来就不产生新帧；一旦每张真实帧都重置
        # 这个标记，之后每次静止超过 1.5s 就会再推一张占位图，面板变成
        # 「画面 ↔ NO SIGNAL 黑框」来回闪（真机复现：连点几次后不停闪烁切换）。
        intro_sent = False
        try:
            while True:
                frame, nseq, err = hub.wait_frame(ch, seq, timeout=1.0)
                if nseq == seq and not err:
                    # 没新帧：浏览器保留上一帧即可，不重发、也不制造空窗。
                    if not intro_sent and (time.time() - started) > 1.5:
                        write_part(hub.placeholder(ch, "连接中…"))
                        intro_sent = True
                    continue
                seq = nseq
                if not frame:
                    write_part(hub.placeholder(ch, err or "screencast unavailable"))
                else:
                    write_part(frame)
                # 只要已经给过一帧画面，「连接中」占位就永远不再发（见上）。
                intro_sent = True
        finally:
            hub.unsubscribe(ch)

    def _blob(self, data: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _runs(self, parts: list[str]) -> None:
        store = self.app.store
        if len(parts) == 2:
            return self._json(200, {"runs": store.recent(20)})

        job = store.get(parts[2])
        # 内存里只留最近几十个任务：刷新页面、隔天回来看，任务早就被顶掉了。
        # 这时退到库里取——用例详情里翻历史执行靠的就是这条路。
        hist = None if job is not None else self.app.history_run(parts[2])
        if job is None and hist is None:
            return self._json(404, {"error": "run 不存在或已被清理"})

        if len(parts) == 3:
            if job is not None:
                return self._json(200, job.to_dict())
            return self._json(200, hist["run"])

        action = parts[3]
        if action == "events":
            if job is None:
                return self._json(200, {"events": [], "status": hist["run"].get("status", ""),
                                        "next": 0})
            q = parse_qs(urlparse(self.path).query)
            since = int((q.get("since") or ["0"])[0])
            wait = min(30.0, float((q.get("wait") or ["0"])[0]))
            events = store.wait(job, since, timeout=wait) if wait > 0 else job.events[since:]
            return self._json(200, {"events": events, "status": job.status,
                                    "next": since + len(events)})

        if action == "artifacts" and len(parts) >= 5:
            name = _safe_name("/".join(parts[4:]), allow_dot=True)
            art_dir = (job.artifacts_dir if job is not None
                       else (hist or {}).get("run", {}).get("artifacts_dir", ""))
            art = Path(art_dir) if art_dir else self.app.cfg.artifacts / parts[2]
            if not name or not art.is_dir():
                return self._json(404, {"error": "产物不存在"})
            target = (art / name).resolve()
            if art.resolve() not in target.parents or not target.exists():
                return self._json(404, {"error": "产物不存在"})
            # 排障日志直接在浏览器里看，不要变成"下载一个未知类型文件"
            ctype = ("text/plain; charset=utf-8" if name.endswith(".jsonl")
                     else mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            return self._file(target, ctype)

        if action == "report":
            report = (job.report_path if job is not None
                      else (hist or {}).get("run", {}).get("report_path", ""))
            if not report or not Path(report).exists():
                return self._json(404, {"error": "报告尚未生成"})
            return self._file(Path(report), "text/html; charset=utf-8")

        return self._json(404, {"error": "not found"})

    def _route_delete(self) -> None:
        url = urlparse(self.path)
        parts = [p for p in unquote(url.path).split("/") if p]
        if parts[:2] == ["api", "cases"] and len(parts) == 3:
            info = self.app.delete_case(parts[2])
            if info is None:
                return self._json(404, {"error": "用例不存在"})
            return self._json(200, {"ok": True, **info})
        return self._json(404, {"error": "not found"})

    def _route_post(self) -> None:
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        if parts[:2] != ["api", "run"] and parts[:2] != ["api", "model"] \
                and parts[:3] != ["api", "device", "cast"]:
            return self._json(404, {"error": "not found"})

        body = self._body()

        if parts[:3] == ["api", "device", "cast"] and len(parts) == 4 and parts[3] == "stop":
            dev = str(body.get("device") or "")
            self.app.video.stop(dev)        # 视频模式通道一并收掉
            return self._json(200, self.app.cast.stop(
                str(body.get("platform") or "android"), dev))

        if parts[:3] == ["api", "device", "cast"] and len(parts) == 4 and parts[3] == "input":
            try:
                return self._json(200, self.app.cast.control(
                    str(body.get("platform") or "android"), str(body.get("device") or ""), body))
            except (ValueError, RuntimeError) as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._json(503, {"error": f"设备操作失败: {e}"})

        if parts[:2] == ["api", "model"]:
            if len(parts) >= 3 and parts[2] == "list":
                # 只探测、不落盘：先把服务端的模型名拉下来，用户选好再保存
                return self._json(200, self.app.list_models(body))
            if len(parts) >= 3 and parts[2] == "test":
                if body:
                    self.app.save_model(body)
                return self._json(200, self.app.test_model())
            return self._json(200, {"model": self.app.save_model(body)})

        if len(parts) == 3 and parts[2] == "cancel":
            return self._json(404, {"error": "not found"})
        if len(parts) == 2:
            job = start_run(self.app.cfg, self.app.store, body)
            return self._json(200, {"run_id": job.run_id, "status": job.status})

        run_id = parts[2]
        job = self.app.store.get(run_id)
        if job is None:
            return self._json(404, {"error": "run 不存在"})
        if len(parts) == 4 and parts[3] == "cancel":
            return self._json(200, self.app.cancel_run(job))
        return self._json(404, {"error": "not found"})

    # ---------- 工具 ----------
    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"请求体不是合法 JSON: {e}") from e
        return data if isinstance(data, dict) else {}

    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path: Path, ctype: str, download: str = "") -> None:
        if not path.exists():
            return self._json(404, {"error": f"缺少文件 {path.name}"})
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


def _mask(key: str) -> str:
    """脱敏显示密钥，避免日志与接口泄露完整 API Key。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 6}{key[-4:]}"


def _safe_name(name: str, allow_dot: bool = False) -> str:
    """把用户提供的名字收敛成安全的文件名，防目录穿越。"""
    name = (name or "").strip().replace("\\", "/")
    if not name or ".." in name or name.startswith("/") or ":" in name:
        return ""
    if not allow_dot and "." in name:
        return ""
    return name


def _under(path: Path, root: Path) -> bool:
    """path 必须真的在 root 里面，才允许删它。

    删除是不可逆的，唯一能接受的失败是"什么都没删"：case_id 来自 HTTP，
    哪怕 _safe_name 漏了一种写法，这一层也不让 rmtree 跑出工程目录。
    """
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:  # noqa: BLE001
        return False


def _now_ms() -> int:
    """返回当前毫秒时间戳。"""
    import time

    return int(time.time() * 1000)


# ---------------------------------------------------------------- 一键启动支撑
# 设计要点：所谓「一键」的逻辑全部放在 Python 里，bat / sh 只是找到解释器后
# 调一条命令。否则每个平台都要抄一份判断逻辑，改一次漏三处。
def runtime_file(cfg: Config) -> Path:
    """记录正在运行的控制台：pid、端口、url。放在工程根，便于脚本读取。"""
    return cfg.home / ".atp-web.json"


def _probe(host: str, port: int, timeout: float = 0.7) -> dict | None:
    """探测端口上是不是 ATP 控制台，是则返回它的 state（含 home / url）。"""
    import urllib.request

    if host in ("0.0.0.0", "", "::"):
        host = "127.0.0.1"
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/state", timeout=timeout) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read(262144).decode("utf-8", "ignore"))
    except Exception:
        return None
    if data.get("app") != APP_MARK:
        return None
    data["_url"] = f"http://{host}:{port}/"
    return data


def find_running(cfg: Config, host: str = "127.0.0.1", port: int = 8765) -> str:
    """找一台已在同工程上跑着的控制台，返回它的 URL；没有则空串。"""
    try:
        info = json.loads(runtime_file(cfg).read_text(encoding="utf-8"))
    except Exception:
        info = {}
    candidates: list[int] = []
    if isinstance(info, dict) and info.get("port"):
        candidates.append(int(info["port"]))
    if port not in candidates:
        candidates.append(port)
    for p in candidates:
        st = _probe(host, p)
        # 同一个工程才复用，避免把另一个 home 的控制台当自己的
        if st and Path(st.get("home", "")) == Path(cfg.home):
            return st["_url"]
    return ""


def stop(cfg: Config, host: str = "127.0.0.1", port: int = 8765) -> int:
    """停掉后台控制台。

    不能只杀运行记录里那个 pid：调试时反复启动、换端口启动，很容易留下
    「记录被人覆盖了、进程还活着占着 8765」的影子实例。只杀记录里的一个，
    用户双击 stop 之后 status 仍然显示运行中，看起来像没停掉。
    所以这里把端口段扫一遍，凡是注册在本工程名下的都清掉。
    """
    import os
    import signal
    import subprocess

    targets: list[int] = []
    pid = 0
    try:
        pid = int(json.loads(runtime_file(cfg).read_text(encoding="utf-8")).get("pid") or 0)
    except Exception:
        pid = 0
    if pid and _pid_alive(pid):
        targets.append(pid)

    for p in range(port, port + 12):
        st = _probe(host, p, timeout=0.4)
        if not st or Path(st.get("home", "")) != Path(cfg.home):
            continue
        other = _pid_on_port(p)
        if other and other not in targets:
            targets.append(other)

    if not targets:
        print("没有正在运行的控制台")
        runtime_file(cfg).unlink(missing_ok=True)
        return 0

    killed, failed = [], []
    for p in targets:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(p), "/F"],
                               capture_output=True, timeout=10)
            else:
                os.kill(p, signal.SIGTERM)
            killed.append(p)
        except Exception:  # noqa: BLE001
            failed.append(p)

    for _ in range(20):
        if not any(_pid_alive(p) for p in killed):
            break
        import time as _t

        _t.sleep(0.1)
    runtime_file(cfg).unlink(missing_ok=True)

    if failed:
        print(f"部分进程停止失败：{failed}")
        return 1
    print(f"已停止控制台（pid={', '.join(str(p) for p in killed)}）")
    return 0


def _pid_alive(pid: int) -> bool:
    """判断进程是否还活着（用于控制台实例探测）。"""
    import os

    if not pid:
        return False
    if os.name == "nt":
        import subprocess

        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, errors="ignore")
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_on_port(port: int) -> int:
    """查询指定端口上的进程 PID。"""
    import os
    import subprocess

    if os.name != "nt":
        # macOS 自带 lsof，多数 Linux 也有；没有就退化成只靠运行记录的 pid
        try:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=5).stdout
            first = (out or "").split()
            return int(first[0]) if first else 0
        except Exception:
            return 0
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             errors="ignore", timeout=10).stdout
    except Exception:
        return 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" \
                and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
            return int(parts[4])
    return 0


# ---------------------------------------------------------------- 启动
def _model_probe(cfg: Config) -> None:
    """启动时顺手体检模型。

    模型名写错（大小写、版本号）在配置里完全看不出来，只有真调一次才报
    model_not_found。让这个坑在启动后 3 秒内暴露，而不是等用户输完用例才发现。
    """
    if not cfg.model.configured:
        print("\n  · 未配置模型：AI 首跑需要模型，请到页面右上角「模型设置」填写 Base URL / API Key")
        return
    # 体检只探一次连通性，别因网络抖动把启动拖成半分钟：两条路径各试一次即可。
    # 本地模型首次加载可能很慢，按配置放宽（上限 60s）。
    probe_timeout = max(15, min(int(getattr(cfg.model, "timeout_s", 60) or 60), 60)) \
        if cfg.model.is_local else 15
    c = LLMClient(cfg.model, timeout=probe_timeout, attempts=2)
    try:
        c.chat([{"role": "user", "content": "ping"}], max_tokens=16, json_mode=False)
    except Exception as e:  # noqa: BLE001
        msg = str(e).replace("\n", " ")[:160]
        print(f"\n  ⚠ 模型 {cfg.model.model} 不可用：{msg}")
        print("    → 页面右上角「模型设置」点「测试连接」可实时复测；"
              "本机代理异常时设 ATP_MODEL_NO_PROXY=1 强制直连")
    else:
        print(f"\n  ✓ 模型 {cfg.model.model} 已连通")


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True, reuse: bool = True) -> int:
    """启动 HTTP 服务并监听端口。"""
    # 重定向到日志文件时 stdout 是块缓冲，启动信息会憋到几 KB 才吐，
    # 看起来像卡死了。行缓冲让每一行立刻可见。
    import sys

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    # 已经在跑就别再起一个：双击两次不该出现两套进程抢同一个 artifacts
    if reuse:
        url = find_running(cfg, host, port)
        if url:
            print("控制台已在运行，直接打开")
            print("-" * 60)
            print(f"  地址   {url}")
            print("-" * 60)
            if open_browser:
                webbrowser.open(url)
            return 0

    app = App(cfg)
    httpd = None
    for candidate in range(port, port + 12):
        try:
            httpd = _Server((host, candidate), Handler)
            httpd.daemon_threads = True
            port = candidate
            break
        except OSError:
            continue
    if httpd is None:
        print(f"端口 {port}~{port + 11} 都被占用，请用 --port 指定其它端口")
        return 2

    httpd.app = app  # type: ignore[attr-defined]
    url = f"http://{host}:{port}/"
    print("ATP Web 控制台已启动")
    print("-" * 60)
    print(f"  地址   {url}")
    print(f"  工程   {cfg.home}")
    print(f"  模型   {cfg.model.model or '未配置（可在页面右上角设置）'}")
    print(f"  产物   {cfg.artifacts}")
    print("-" * 60)
    print("  Ctrl+C 停止（或双击 stop.bat）")

    import os

    try:
        runtime_file(cfg).write_text(json.dumps({
            "app": APP_MARK, "version": APP_VERSION, "pid": os.getpid(),
            "port": port, "url": url, "host": str(cfg.home),
            "started_at": _now_ms(),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # 记录失败不影响服务本身

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    threading.Thread(target=_model_probe, args=(cfg,), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
        try:
            app.video.close()     # 收掉 screenrecord 进程
            app.cast.close()      # 收掉常驻 adb shell，别把它留成孤儿进程
        except Exception:
            pass
        try:
            runtime_file(cfg).unlink(missing_ok=True)
        except Exception:
            pass
    return 0
