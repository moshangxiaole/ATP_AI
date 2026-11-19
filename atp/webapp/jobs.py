"""Web 控制台的任务层。

一次「对话 → 执行」被包成一个 Job：
- 事件流（step_started / step_finished / heal / run_finished）供前端长轮询消费；
- 用例库命中优先回放，没有命中才让 AI 首跑并沉淀；
- 取消是协作式的：事件回调里抛 RunCancelled，执行器会在两步之间退出。
"""

from __future__ import annotations

import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from atp.agents.runner import AiRunner, LlmHealer, LlmPlanner
from atp.agents.llm.client import LLMClient
from atp.config import Config
from atp.scriptgen.compiler import compile_case
from atp.agents.executor import Executor
from atp.agents.perception.judge import Judge
from atp.memory.sediment import sediment_blocker
from atp.common.stepir import CaseIR, NLCase
from atp.common.types import RunCancelled
from atp.agents.devices.manager import DeviceManager
from atp.db.store import CaseDB, record_run
from atp.runs.library import find_similar_case, slug_id


# ---------------------------------------------------------------- Job
@dataclass
class Job:
    """一次异步执行任务：持有取消事件、状态与工作线程。"""
    run_id: str
    case_id: str
    title: str
    platform: str
    device_id: str
    mode: str
    text: str = ""
    status: str = "queued"          # queued | running | passed | failed | error | cancelled
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    events: list[dict] = field(default_factory=list)
    summary: dict | None = None
    error: str = ""
    artifacts_dir: str = ""
    report_path: str = ""
    stepir_path: str = ""
    script_path: str = ""
    # 取消用事件而不是布尔量：执行线程会在每步、每次等待、每次滑动之间轮询它，
    # 布尔量在多线程里读写时序不保证，事件还顺便能 wait()。
    cancel_event: threading.Event = field(default_factory=threading.Event)
    cancel_requested_at: float = 0.0

    @property
    def cancel_requested(self) -> bool:
        return self.cancel_event.is_set()

    @cancel_requested.setter
    def cancel_requested(self, value: bool) -> None:
        """保留布尔语义的赋值方式：测试与老调用点都按这个写法用。"""
        if value:
            self.cancel_event.set()
        else:
            self.cancel_event.clear()

    def request_cancel(self) -> None:
        self.cancel_requested_at = time.time()
        self.cancel_event.set()

    @property
    def duration_ms(self) -> int:
        if not self.started_at:
            return 0
        return int(((self.finished_at or time.time()) - self.started_at) * 1000)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "case_id": self.case_id,
            "title": self.title,
            "platform": self.platform,
            "device_id": self.device_id,
            "mode": self.mode,
            "status": self.status,
            "created_at": self.created_at,
            "duration_ms": self.duration_ms,
            "summary": self.summary,
            "error": self.error,
            "event_count": len(self.events),
            "cancel_requested": self.cancel_requested,
            "has_report": bool(self.report_path),
            "has_stepir": bool(self.stepir_path),
            "has_script": bool(self.script_path),
        }


class RunStore:
    """进程内任务表。单机控制台够用，未来接数据库只需换掉这一层。"""

    def __init__(self, keep: int = 60):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._cv = threading.Condition()
        self._keep = keep

    def create(self, **kw) -> Job:
        with self._cv:
            job = Job(**kw)
            self._jobs[job.run_id] = job
            self._order.append(job.run_id)
            while len(self._order) > self._keep:
                self._jobs.pop(self._order.pop(0), None)
            return job

    def get(self, run_id: str) -> Job | None:
        return self._jobs.get(run_id)

    def publish(self, job: Job, event: dict) -> None:
        with self._cv:
            event = dict(event)
            event["seq"] = len(job.events)
            job.events.append(event)
            self._cv.notify_all()

    def wait(self, job: Job, since: int, timeout: float = 20.0) -> list[dict]:
        with self._cv:
            if len(job.events) <= since:
                self._cv.wait(timeout)
            return job.events[since:]

    def recent(self, limit: int = 20) -> list[dict]:
        with self._cv:
            return [self._jobs[r].to_dict() for r in reversed(self._order[-limit:])
                    if r in self._jobs]


def job_title(payload: dict, text: str) -> str:
    """任务/用例标题 = 用户那句原话，**不截断**。

    标题不是内部标识（case_id 才是），而是用户在控制台卡片、用例库、执行记录里
    认出"这是我发的那句"的唯一线索。早先按 22/24 字硬切，卡片上只剩
    「打开【示例聊天应用】，消息首页点击右上角的【+】，在」——看着像被吞了半句。
    这里只做一件事：把换行、连续空格压成单空格，避免多行输入把卡片撑变形。
    """
    raw = str(payload.get("title") or "").strip() or str(text or "").strip()
    return re.sub(r"\s+", " ", raw).strip()


# ---------------------------------------------------------------- 执行
def start_run(cfg: Config, store: RunStore, payload: dict) -> Job:
    """创建并排队一个执行任务，返回任务 ID 供前端轮询。"""
    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError("用例描述不能为空")

    # 控制台与 CLI 一样默认 Android：页面首次打开就是真机终端，
    # 不再有"没插设备也能演示"的虚拟平台兜底。
    platform = str(payload.get("platform") or "android")
    mode = str(payload.get("mode") or "auto")
    title = job_title(payload, text)
    job = store.create(
        run_id=f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}",
        case_id=str(payload.get("case_id") or ""),
        title=title,
        platform=platform,
        device_id=str(payload.get("device") or ""),
        mode=mode,
        text=text,
    )
    thread = threading.Thread(
        target=_execute, args=(job, cfg, store, payload),
        daemon=True, name=f"atp-{job.run_id}",
    )
    thread.start()
    return job


def _execute(job: Job, cfg: Config, store: RunStore, payload: dict) -> None:
    """任务主入口：按模式分派到 AI 首跑或脚本回放，统一异常处理。"""
    job.status = "running"
    job.started_at = time.time()
    # 先定死产物目录：事件里的截图地址在跑的过程中就要能取到
    job.artifacts_dir = str(cfg.artifacts / job.run_id)
    store.publish(job, {
        "type": "run_started", "run_id": job.run_id, "case_id": job.case_id,
        "title": job.title, "platform": job.platform, "mode": job.mode,
        "device": job.device_id,
    })

    def emit(event: dict) -> None:
        if job.cancel_requested:
            raise RunCancelled("用户取消执行")
        event = dict(event)
        for key in ("images",):
            event.pop(key, None)
        if event.get("type") == "step_finished":
            event["images"] = _images_of(job.run_id, event)
        store.publish(job, event)

    cancel = job.cancel_event.is_set

    try:
        # 取消要在真正开工之前就生效：模型没配、设备没连时也不该先报一堆错，
        # 用户点了取消就该干净地停在 cancelled。
        if cancel():
            raise RunCancelled("用户取消执行")
        mode = job.mode
        platform = job.platform
        text = job.text
        device = str(payload.get("device") or "")
        prefer_remote = bool(payload.get("prefer_remote"))
        app_id = str(payload.get("app_id") or "")
        max_steps = int(payload.get("max_steps") or 40)
        asserts = [a for a in (payload.get("asserts") or []) if str(a).strip()]

        hit = None
        if mode in ("auto", "replay"):
            hit = find_similar_case(cfg, text, platform)
            if hit:
                store.publish(job, {
                    "type": "library_hit", "case_id": hit.case.case_id,
                    "title": hit.case.title, "score": round(hit.score, 3),
                    "steps": len(hit.case.steps), "mode": "replay",
                })

        if hit:
            job.case_id = hit.case.case_id
            _run_replay(job, cfg, store, payload, emit, hit.path, platform, device,
                        prefer_remote, bool(payload.get("heal", True)), cancel=cancel)
        elif mode == "replay":
            raise RuntimeError(
                "用例库中没有匹配的用例。请把执行方式改为「自动」或「AI 首跑」先沉淀一条。"
            )
        else:
            _run_ai(job, cfg, store, payload, emit, platform, device, prefer_remote,
                    app_id, max_steps, asserts, cancel=cancel)

    except RunCancelled as exc:
        # 先把"停在哪了"记下来：取消不是运行失败，但用户需要看见已经跑到哪一步。
        partial = getattr(exc, "partial", None)
        summary = None
        if partial is not None:
            summary = partial.summary()
            job.artifacts_dir = partial.artifacts_dir or job.artifacts_dir
            job.report_path = partial.report_path or job.report_path
        job.summary = summary
        # 先发事件再改状态：任何读到终态的请求，必然已经能取到 run_finished
        store.publish(job, {"type": "run_finished", "status": "cancelled",
                            "case_id": job.case_id,
                            "summary": summary, "report_url":
                                f"/api/runs/{job.run_id}/report" if job.report_path else "",
                            "message": "执行已取消"})
        job.status = "cancelled"
    except Exception as e:
        job.error = f"{type(e).__name__}: {e}"
        store.publish(job, {
            "type": "error", "message": job.error,
            "trace": traceback.format_exc(limit=4),
        })
        store.publish(job, {"type": "run_finished", "status": "error",
                            "case_id": job.case_id,
                            "summary": None, "message": job.error})
        job.status = "error"
    finally:
        job.finished_at = time.time()


def _run_ai(job: Job, cfg: Config, store: RunStore, payload: dict, emit,
            platform: str, device: str, prefer_remote: bool,
            app_id: str, max_steps: int, asserts: list[str],
            cancel=None) -> None:
    """AI 首跑路径：构造 runner，逐步骤执行并实时推送事件。"""
    case_id = job.case_id
    if not case_id:
        # 同一场景反复首跑不该把用例库撑成垃圾场：高度相似就沿用原 case_id 覆盖沉淀
        near = find_similar_case(cfg, job.text, platform, threshold=0.85)
        if near:
            case_id = near.case.case_id
            emit({"type": "log", "level": "info",
                  "message": f"场景与已有用例「{near.case.title}」相似度 {near.score:.0%}，"
                             f"沿用 case_id 覆盖沉淀"})
        else:
            case_id = slug_id(job.text, job.title)
    job.case_id = case_id

    nl = NLCase(
        case_id=case_id, title=job.title, platform=platform, app_id=app_id,
        nl=job.text, asserts=asserts, max_steps=max_steps,
        url=str(payload.get("url") or ""),
        endpoint=str(payload.get("endpoint") or ""),
        variables={str(k): str(v) for k, v in (payload.get("variables") or {}).items()},
    )
    client = LLMClient(cfg.model)
    # AI 用例点取消时，执行线程多半卡在一次模型调用里（规划最长 60s×4 次重试）。
    # 注入取消探针：发请求前 / 退避等待 / 响应读完都检查，立刻抛 RunCancelled，
    # 不等模型慢慢返回——否则用户点取消后干等几十秒，以为没生效。
    if cancel is not None and hasattr(client, "set_cancel_probe"):
        client.set_cancel_probe(cancel)

    if client.available:
        planner = LlmPlanner(client)
        emit({"type": "log", "level": "info",
              "message": f"模型 {cfg.model.model} 已就绪，AI 全程接管规划"})
    else:
        raise RuntimeError(
            "模型未配置。请在右上角「模型设置」填写 Base URL / API Key / 模型名后重试。"
        )

    mgr = DeviceManager(cfg)
    with mgr.lease(platform, device, prefer_remote,
                   endpoint=nl.endpoint, url="") as driver:
        emit({"type": "log", "level": "info",
              "message": f"已连接 {platform} · {driver.device_id}"})
        runner = AiRunner(driver, cfg, planner=planner, client=client, on_event=emit,
                          should_cancel=cancel)
        case, result = runner.run(nl, out_dir=cfg.artifacts, run_id=job.run_id)

    base = cfg.cases / case_id
    # 失败的用例不沉淀：失败的首跑录下来的是一串"没走通"的步骤和错位的定位器，
    # 存进库下次必然还是失败，只会把用例库变成垃圾场、把命中率搅乱。
    # 产物（截图/报告/stepir）仍留在本次 run 的 artifacts 目录里，够排障用。
    blocked = sediment_blocker(result)
    if blocked:
        emit({"type": "not_sedimented", "case_id": case_id,
              "steps": len(case.steps),
              "failed": sum(1 for s in result.steps if not s.ok),
              "message": blocked})
        record_run(cfg, result, screen=case.screen, artifacts_dir=result.artifacts_dir,
                   report_path=result.report_path, case=case, ensure_case=False)
        _finish(job, store, result, platform, device, screen=case.screen)
        return

    stepir = case.save(str(base) + ".stepir.json")
    script = compile_case(stepir, str(base) + ".py")
    job.stepir_path, job.script_path = str(stepir), str(script)
    emit({"type": "sedimented", "case_id": case_id, "steps": len(case.steps),
          "stepir": stepir.name, "script": script.name})

    # 落库：数据库是"可查询索引"，磁盘上的 StepIR / .py 仍是可执行产物，两边同时写
    saved = _save_case(cfg, case, stepir, script)
    record_run(cfg, result, screen=case.screen, artifacts_dir=result.artifacts_dir,
               report_path=result.report_path, case=case)
    emit({"type": "db_saved", "case_id": case_id, "saved": saved,
          "images": len(case.visual_assets),
          "screen": case.screen.as_dict(),
          "message": f"已落库 {len(case.steps)} 步"
                     + (f"（含 {len(case.visual_assets)} 张视觉模板）" if case.visual_assets else "")})

    _finish(job, store, result, platform, device, screen=case.screen)


def _save_case(cfg: Config, case: CaseIR, stepir: Path, script: Path) -> bool:
    """用例入库（失败只记日志，不影响已完成的执行结果）。"""
    import hashlib

    db = CaseDB(cfg.db_file)
    try:
        sha = hashlib.sha1(script.read_bytes()).hexdigest()[:12] if script.exists() else ""
        return db.upsert_case(case, stepir_path=stepir.name, script_path=script.name,
                              source=case.created_by, script_sha=sha)
    except Exception:  # noqa: BLE001
        return False
    finally:
        db.close()


def _run_replay(job: Job, cfg: Config, store: RunStore, payload: dict, emit,
                path: Path, platform: str, device: str, prefer_remote: bool,
                heal: bool, cancel=None) -> None:
    """脚本回放路径：零 LLM，按编译产物顺序执行。"""
    case = CaseIR.load(path)
    job.case_id = case.case_id
    job.title = job.title or case.title
    client = LLMClient(cfg.model)
    if cancel is not None and hasattr(client, "set_cancel_probe"):
        client.set_cancel_probe(cancel)
    judge = Judge(client if client.available else None)
    healer = LlmHealer(client) if (client.available and heal) else None

    mgr = DeviceManager(cfg)
    target = platform or case.platform
    executor = None
    with mgr.lease(target, device, prefer_remote,
                   endpoint=case.variables.get("endpoint", ""),
                   url=case.variables.get("url", "")) as driver:
        executor = Executor(driver, cfg, judge, healer, on_event=emit,
                            should_cancel=cancel)
        result = executor.run(case, mode="replay", persist_path=path, run_id=job.run_id)
    job.stepir_path, job.script_path = str(path), str(Path(path).with_suffix(".py"))

    # 命中计数落库：命中率是判断"用例库到底有没有用"的核心指标
    db = CaseDB(cfg.db_file)
    try:
        db.bump_hit(case.case_id)
    finally:
        db.close()

    screen = executor.screen if executor is not None else case.screen
    _finish(job, store, result, target, device, screen=screen)


def _finish(job: Job, store: RunStore, result, platform: str, device: str,
            screen=None) -> None:
    """收尾：写回用例库、生成报告、沉淀弹窗与应用记忆。"""
    status = "passed" if result.passed else "failed"
    job.summary = result.summary()
    job.artifacts_dir = result.artifacts_dir
    job.report_path = result.report_path
    screen_info = ""
    if screen is not None and getattr(screen, "ok", False):
        screen_info = f"{screen.width}x{screen.height}"
        if abs(screen.scale_x - 1.0) > 0.001 or abs(screen.scale_y - 1.0) > 0.001:
            screen_info += f" · 缩放 {screen.scale_x:.2f}x"
        job.summary["screen"] = screen.as_dict()
    store.publish(job, {
        "type": "run_finished",
        "status": status,
        "case_id": job.case_id,
        "summary": job.summary,
        "screen": screen.as_dict() if screen is not None and getattr(screen, "ok", False) else None,
        "screen_label": screen_info,
        "report_url": f"/api/runs/{job.run_id}/report" if job.report_path else "",
        "stepir_url": f"/api/cases/{job.case_id}/stepir" if job.stepir_path else "",
        "script_url": f"/api/cases/{job.case_id}/script" if job.script_path else "",
        "platform": platform,
        "device": device or result.device_id,
    })
    job.status = status


def _images_of(run_id: str, event: dict) -> list[dict]:
    """收集任务产物目录里的截图路径，供控制台展示。"""
    ev = event.get("evidence") or {}
    out = []
    for label, key in (("执行前", "before"), ("执行后", "after")):
        name = ev.get(key) or ""
        if name:
            out.append({"label": label,
                        "url": f"/api/runs/{run_id}/artifacts/{name}"})
    if not out:
        shot = (event.get("result") or {}).get("screenshot") or ""
        if shot:
            out.append({"label": "执行后",
                        "url": f"/api/runs/{run_id}/artifacts/{shot}"})
    return out
