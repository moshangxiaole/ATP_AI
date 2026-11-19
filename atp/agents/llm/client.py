"""极简 OpenAI 兼容客户端。

不引入 openai/requests 依赖——urllib 足够，且能保证在客户内网/离线环境里不会因
依赖装不上而卡住。豆包、Qwen、DeepSeek、GLM、GPT、vLLM 全部兼容此协议。
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Callable

from atp.common.types import RunCancelled
from atp.config import ModelConfig


class LLMError(RuntimeError):
    pass


class _ResponseCache:
    """进程级响应去重：同一 payload 在 TTL 内只真正打一次模型。

    视觉 grounding 经常对同一张图反复问同一句话；重试链路也会重复发相同请求。
    缓存只存**成功响应**，失败/限流不缓存（由 THROTTLE 单独处理）。
    """

    def __init__(self, ttl: float = 5.0, max_size: int = 128) -> None:
        self._data: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self.ttl = ttl
        self.max_size = max_size

    @staticmethod
    def _key(data: bytes) -> str:
        import hashlib
        return hashlib.sha256(data or b"").hexdigest()

    def get(self, data: bytes):
        key = self._key(data)
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None, False
            ts, value = hit
            if time.monotonic() - ts > self.ttl:
                self._data.pop(key, None)
                return None, False
            return value, True

    def put(self, data: bytes, value) -> None:
        key = self._key(data)
        with self._lock:
            if len(self._data) >= self.max_size:
                oldest = min(self._data, key=lambda k: self._data[k][0])
                self._data.pop(oldest, None)
            self._data[key] = (time.monotonic(), value)


RESPONSE_CACHE = _ResponseCache()


def _rate_limit_message(body: object) -> str:
    """识别"HTTP 200 + 错误 body"形态的业务限流（Agnes 免费额度）。

    正常响应（choices / data）一律不误判；只有明确带限流语义的错误体才返回提示。
    """
    if not isinstance(body, dict):
        return None
    if "choices" in body or "data" in body:
        return None
    text = " ".join(str(body.get(k, "")) for k in ("message", "msg", "error", "code", "type"))
    if not any(k in text for k in ("速率限制", "rate limit", "RateLimit", "限流", "429")):
        return None
    return (f"模型服务限流：{text[:120]}。"
            "请升级 Token Plan 套餐 / 切换更高额度的模型，或在设置里调大最小调用间隔后重试。")


class _Throttle:
    """进程级的模型调用闸门。

    免费额度的模型（本次 run-20261113-173646 的 429）限制的是**每分钟请求数**，
    而 AI 执行每一步都要打一次模型，单步几十毫秒就发一次，很容易在十几秒内撞墙。
    靠"出错再重试"是不够的：429 之后重试仍然超频，只会连续撞墙。
    这里在发请求**之前**就把频率压下来（最小间隔 + 每分钟上限），
    并且在撞到 429 之后给整个进程一段冷却时间，后续调用一起等。

    多个执行线程共用一把锁，等于顺带把模型调用串行化——对单机测试工具来说
    这正是想要的：并发打模型只会更快被限流。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.enabled = True
        self.min_interval = 1.2          # 秒：两次调用之间的最小间隔
        self.max_per_min = 20            # 每分钟最多调用次数（<=0 表示不限）
        self._last_ts = 0.0
        self._calls: deque[float] = deque()
        self._cooldown_until = 0.0

    def configure(self, enabled: bool | None = None,
                  min_interval_ms: int | float | None = None,
                  max_per_min: int | float | None = None) -> None:
        with self._lock:
            if enabled is not None:
                self.enabled = bool(enabled)
            if min_interval_ms is not None:
                try:
                    self.min_interval = max(0.0, float(min_interval_ms) / 1000.0)
                except (TypeError, ValueError):
                    pass
            if max_per_min is not None:
                try:
                    self.max_per_min = int(max_per_min)
                except (TypeError, ValueError):
                    pass

    def penalize(self, seconds: float) -> None:
        """被限流了：让整个进程都等一会儿，而不是让每个调用各自撞一次墙。"""
        try:
            sec = max(0.0, float(seconds))
        except (TypeError, ValueError):
            return
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + sec)

    def before_call(self) -> None:
        """在真正发请求前调用：必要时阻塞到允许发为止。"""
        while True:
            with self._lock:
                if not self.enabled:
                    return
                now = time.monotonic()
                wait = 0.0
                if now < self._cooldown_until:
                    wait = self._cooldown_until - now
                else:
                    if self._last_ts:
                        gap = self.min_interval - (now - self._last_ts)
                        if gap > 0:
                            wait = gap
                    if self.max_per_min > 0:
                        while self._calls and now - self._calls[0] > 60.0:
                            self._calls.popleft()
                        if len(self._calls) >= self.max_per_min:
                            wait = max(wait, 60.0 - (now - self._calls[0]) + 0.05)
                if wait <= 0:
                    self._last_ts = now
                    self._calls.append(now)
                    if len(self._calls) > max(self.max_per_min, 1) * 3:
                        self._calls.popleft()
                    return
            # 不持锁睡觉：配置更新、惩罚不会互相卡住
            time.sleep(min(wait, 15.0))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "min_interval_ms": int(round(self.min_interval * 1000)),
                "max_per_min": self.max_per_min,
                "cooling_s": max(0, int(round(self._cooldown_until - time.monotonic()))),
            }


# 整个进程共用一个闸门：控制台里可能同时有「测试连接」「AI 执行」「断言」三条调用线，
# 各自限速没有意义，被限流的是同一个账号额度。
THROTTLE = _Throttle()


def configure_throttle(cfg: ModelConfig | None) -> None:
    """按配置刷新限速闸门（模型设置保存后调用即生效，无需重启）。"""
    if cfg is None:
        return
    THROTTLE.configure(
        enabled=bool(getattr(cfg, "throttle", True)),
        min_interval_ms=getattr(cfg, "min_interval_ms", 1200),
        max_per_min=getattr(cfg, "max_per_min", 20),
    )


class LLMClient:
    def __init__(self, cfg: ModelConfig | None = None, timeout: int | None = None,
                 attempts: int = 4, trace: Callable[[str, dict], None] | None = None,
                 cancel_probe: Callable[[], bool] | None = None):
        self.cfg = cfg or ModelConfig()
        # 取消探针：AI 用例点「取消」时，执行线程往往正卡在一次模型调用里
        # （规划/验收/视觉定位，单次最长 60s × 4 次重试）。仅靠每步之间检查
        # 让用户干等，看起来像"点了没反应"。注入探针后，发请求前、退避等待、
        # 响应读完都会检查，一旦取消立刻抛 RunCancelled 中断。
        self.cancel_probe = cancel_probe
        # 本地模型首 token 慢，默认超时从配置取（默认 60s），显式传入优先
        self.timeout = int(timeout or getattr(self.cfg, "timeout_s", 60) or 60)
        self.attempts = attempts
        # 本机代理配错是 Windows 上最常见的「服务不可用」来源。设
        # ATP_MODEL_NO_PROXY=1 可以强制全程直连，不走系统/环境代理。
        self.force_direct = os.environ.get("ATP_MODEL_NO_PROXY", "").strip().lower() \
            in ("1", "true", "yes", "on")
        self.last_path = ""       # 最近一次成功走的网络路径，给「测试连接」展示
        # 调用链日志钩子：把完整提示词 / 原始返回 / 异常落到本次执行的产物里。
        # 默认关；AiRunner 会在 run() 前挂上自己的事件日志，控制台的
        # 「测试连接」聊天不写执行日志。
        self.trace = trace
        self._call_seq = 0
        self._last_call_id = ""
        # 频次限制是"账号级"的：新建客户端时把当前配置同步给进程级闸门，
        # 界面保存设置后立刻生效，不必重启控制台。
        configure_throttle(self.cfg)

    def set_trace(self, trace: Callable[[str, dict], None] | None) -> None:
        """挂载/卸载日志钩子（同一个 client 可能被规划、修复、断言共用）。"""
        self.trace = trace

    def set_cancel_probe(self, probe: Callable[[], bool] | None) -> None:
        """挂载/卸载取消探针（由执行任务的 cancel_event 提供）。"""
        self.cancel_probe = probe

    def _check_cancel(self) -> None:
        if self.cancel_probe is None:
            return
        try:
            if self.cancel_probe():
                raise RunCancelled("用户取消执行（模型调用已中断）")
        except RunCancelled:
            raise
        except Exception:  # noqa: BLE001 —— 探针自身出错不能影响模型调用
            pass

    def _interruptible_sleep(self, seconds: float) -> None:
        """退避等待期间也响应取消：最多等 0.2s 就复查一次旗子。"""
        end = time.monotonic() + seconds
        while True:
            self._check_cancel()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.2, left))

    def _trace(self, event: str, payload: dict) -> None:
        """日志写失败绝不能影响模型调用。"""
        cb = self.trace
        if cb is None:
            return
        try:
            cb(event, payload)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _loggable_messages(messages: list[dict]) -> list[dict]:
        """提示词原样入日志，但把图片 base64 换成占位符。

        一张截图编码后 100KB~1MB，写进 jsonl 既撑爆文件又没法读；
        保留"这里有一次图片输入 + 多大"就够排障了。
        """
        out: list[dict] = []
        for msg in messages:
            if not isinstance(msg, dict):
                out.append({"raw": str(msg)})
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                out.append(dict(msg))
                continue
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    parts.append(part)
                    continue
                url = str(part.get("image_url", {}).get("url", "")) \
                    if isinstance(part.get("image_url"), dict) else ""
                if url.startswith("data:image"):
                    parts.append({"type": "image_url",
                                  "image_url": {"url": f"<image: {len(url)} chars>"}})
                else:
                    parts.append(part)
            item = dict(msg)
            item["content"] = parts
            out.append(item)
        return out

    @property
    def available(self) -> bool:
        return self.cfg.configured

    def _endpoint(self) -> str:
        base = self.cfg.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _models_endpoint(self) -> str:
        base = self.cfg.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        return base + "/models"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = str(self.cfg.api_key or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        # 本地模型（Ollama / LM Studio / vLLM）不需要 Key；而部分网关对空的
        # `Authorization: Bearer ` 会直接返回 400，所以没 Key 就干脆不发这个头。
        return headers

    _RETRY_CODES = {429, 500, 502, 503, 504}

    @staticmethod
    def _opener(mode: str):
        """auto = 遵循环境/系统代理；direct = 显式绕过全部代理直连。"""
        if mode == "auto":
            return urllib.request.build_opener()
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _network_modes(self) -> tuple[str, ...]:
        """网络路径尝试顺序。

        本机/局域网服务**优先直连**：Windows 上经常配着 http_proxy，
        127.0.0.1 一旦走代理就是 502 / 连接被拒，这是本地模型最常见的坑。
        """
        if self.force_direct:
            return ("direct",)
        if self.cfg.is_local:
            return ("direct", "auto")
        return ("auto", "direct")

    def _open_with_retry(self, req: urllib.request.Request,
                         attempts: int | None = None) -> dict:
        """限流和临时故障要退避重试，否则免费额度下跑用例几乎是必挂。

        429 时优先读 Retry-After，没有就指数退避；并且会把冷却时间广播给整个
        进程（THROTTLE.penalize），避免后续调用接着撞墙。
        连接层故障（代理拒绝连接、网关提前断流）会在「遵循代理」和「强制直连」
        两条网络路径间交替重试：本机代理临时挂掉时，下一拍直连就能自愈，
        不必等用户手工改环境；错误信息里会列出实际试过的路径，方便定位。
        """
        attempts = attempts or self.attempts
        modes = self._network_modes()
        tried: list[str] = []
        for i in range(attempts):
            self._check_cancel()
            mode = modes[i % len(modes)]
            tried.append(mode)
            # 发请求前先过限速闸门：宁可慢一点，也别把免费额度打到 429
            THROTTLE.before_call()
            self._check_cancel()
            try:
                with self._opener(mode).open(req, timeout=self.timeout) as resp:
                    self.last_path = mode
                    raw = resp.read().decode("utf-8", "ignore")
                    self._check_cancel()
                    body = json.loads(raw) if raw.strip() else {}
                    # HTTP 200 + 限流错误体（Agnes 免费额度）：当成软性 429，
                    # 广播冷却；重试仍失败就由 chat() 抛带指导信息的 LLMError。
                    if _rate_limit_message(body):
                        THROTTLE.penalize()
                    return body
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "ignore")[:400]
                if e.code == 429:
                    wait = self._rate_limit_wait(e, i)
                    # 广播冷却：同一进程里排队等着的调用一起停下来，
                    # 否则下一个调用立刻又发一次，冷却形同虚设。
                    THROTTLE.penalize(wait)
                    if i == attempts - 1:
                        raise LLMError(
                            f"模型限流（HTTP 429）：{detail}；已按 {int(wait)}s 冷却重试仍失败。"
                            "可在「模型设置」里开启调用限速（降低每分钟请求数），"
                            "或改用不限频的模型后再执行。"
                        ) from e
                    continue
                if e.code not in self._RETRY_CODES or i == attempts - 1:
                    raise LLMError(f"模型调用失败 HTTP {e.code}: {detail}") from e
                wait = 0.0
                header = e.headers.get("Retry-After") if e.headers else None
                if header:
                    try:
                        wait = float(header)
                    except ValueError:
                        wait = 0.0
                self._interruptible_sleep(min(wait or (2 * (i + 1) + i * 2), 30))
            except (OSError, http.client.HTTPException) as e:
                # URLError(WinError 10061)、RemoteDisconnected、超时都归到这里。
                # 统一转成 LLMError，planner 才能走 abort 正常收尾出报告。
                if i == attempts - 1:
                    raise LLMError(
                        f"模型服务不可达 {self._endpoint()} "
                        f"（已尝试 {' -> '.join(tried)}）：{type(e).__name__}: {e}"
                    ) from e
                self._interruptible_sleep(min(2 * (i + 1), 8))
        raise LLMError("模型调用失败：重试次数用尽")

    @staticmethod
    def _rate_limit_wait(err: urllib.error.HTTPError, attempt: int) -> float:
        """429 要等多久：服务端说了算，没说就指数退避（5/10/20/40s，封顶 60）。"""
        header = err.headers.get("Retry-After") if err.headers else None
        if header:
            try:
                return max(1.0, min(float(header), 120.0))
            except ValueError:
                pass
        return float(min(5 * (2 ** attempt), 60))

    # 兼容旧调用名
    _post_with_retry = _open_with_retry

    def list_models(self) -> list[str]:
        """列出服务端可用模型（OpenAI 兼容 GET /models）。

        本地模型的部署名千奇百怪（qwen2.5:7b-instruct、llama3.1:8b…），
        让人手打容易错，界面上直接给一份下拉。
        """
        if not self.cfg.base_url:
            raise LLMError("未配置 Base URL")
        req = urllib.request.Request(
            self._models_endpoint(), headers=self._headers(), method="GET")
        try:
            body = self._open_with_retry(req, attempts=2)
        except LLMError:
            raise
        except ValueError as e:
            raise LLMError(f"/models 返回的不是 JSON：{e}") from e
        data = body.get("data") if isinstance(body, dict) else None
        out: list[str] = []
        for item in data or []:
            if isinstance(item, dict) and item.get("id"):
                out.append(str(item["id"]))
            elif isinstance(item, str):
                out.append(item)
        return out

    def chat(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        if not self.available:
            raise LLMError("模型未配置：请设置 ATP_MODEL_BASE_URL / ATP_MODEL_API_KEY / ATP_MODEL_NAME")

        payload: dict = {
            "model": model or self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        req = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        self._call_seq += 1
        call_id = f"{id(self):x}-{self._call_seq}"
        self._last_call_id = call_id
        self._trace("llm_request", {
            "call_id": call_id,
            "model": payload["model"],
            "messages": self._loggable_messages(messages),
            "json_mode": json_mode,
            "max_tokens": payload["max_tokens"],
        })
        t0 = time.monotonic()
        try:
            cached, hit = RESPONSE_CACHE.get(req.data)
            if hit:
                self._trace("llm_cache", {"call_id": call_id, "hit": True,
                                          "payload_size": len(req.data or b"")})
                body = cached
            else:
                body = self._open_with_retry(req)
                RESPONSE_CACHE.put(req.data, body)
            elapsed = int((time.monotonic() - t0) * 1000)
            raw = ""
            usage = {}
            try:
                raw = str(body["choices"][0]["message"]["content"] or "")
                usage = dict(body.get("usage") or {})
            except (KeyError, IndexError, TypeError):
                limited = _rate_limit_message(body)
                if limited:
                    raise LLMError(limited)
            self._trace("llm_response", {
                "call_id": call_id, "raw": raw, "usage": usage,
                "elapsed_ms": elapsed, "path": self.last_path,
            })
            return raw
        except LLMError as e:
            self._trace("llm_error", {"call_id": call_id, "error": str(e),
                                      "elapsed_ms": int((time.monotonic() - t0) * 1000)})
            raise
        except Exception as e:
            self._trace("llm_error", {"call_id": call_id,
                                      "error": f"{type(e).__name__}: {e}",
                                      "elapsed_ms": int((time.monotonic() - t0) * 1000)})
            raise

    def chat_json(self, messages: list[dict], **kw) -> dict:
        call_id = self._last_call_id
        try:
            raw = self.chat(messages, json_mode=True, **kw)
            parsed = _loads_loose(raw)
            self._trace("llm_parsed", {"call_id": call_id, "parsed": parsed})
            return parsed
        except LLMError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as e:
            # 规划器只认 LLMError。连接中断、网关返回半截 JSON 这类异常必须在这里
            # 收口，否则执行器会直接崩掉，连已跑完的步骤报告都留不下来。
            raise LLMError(f"模型调用异常: {type(e).__name__}: {e}") from e

    # ---------- 视觉 ----------
    @staticmethod
    def image_part(png: bytes, detail: str = "low") -> dict:
        b64 = base64.b64encode(png).decode()
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": detail},
        }


def _loads_loose(raw: str) -> dict:
    """模型经常在 JSON 外面裹一层 ```json，这里容错。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise
