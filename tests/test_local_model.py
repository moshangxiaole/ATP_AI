"""本地模型（Ollama / LM Studio / vLLM）支持回归。

本地部署和云端的差别不在协议（都是 OpenAI 兼容），而在三件事上，每件都踩过：
1. **没有 API Key**：Ollama/LM Studio 默认不校验 Key，旧实现要求 Key 非空 →
   界面直接显示「模型未配置」，整场执行 abort；
2. **不能走系统代理**：Windows 上常配着 http_proxy，127.0.0.1 一过代理就 502；
3. **没有视觉能力**：本地纯文本模型看图只会报错，得允许关掉图像判定。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atp.agents.llm.client import LLMClient  # noqa: E402
from atp.config import ModelConfig  # noqa: E402


class ModelConfigLocalTest(unittest.TestCase):
    def test_loopback_and_lan_are_local(self):
        for url in ("http://127.0.0.1:11434/v1", "http://localhost:1234/v1",
                    "http://192.168.1.20:8000/v1", "http://10.0.0.5/v1",
                    "http://172.16.3.4:9997/v1"):
            self.assertTrue(ModelConfig(base_url=url).is_local, url)

    def test_public_hosts_are_not_local(self):
        for url in ("https://api.deepseek.com/v1",
                    "https://ark.cn-beijing.volces.com/api/v3",
                    "http://8.8.8.8/v1"):
            self.assertFalse(ModelConfig(base_url=url).is_local, url)

    def test_configured_without_key_when_local(self):
        local = ModelConfig(base_url="http://127.0.0.1:11434/v1", model="qwen2.5:7b")
        self.assertTrue(local.configured, "本地模型没有 Key 也算配置完整")

    def test_remote_still_requires_key(self):
        remote = ModelConfig(base_url="https://api.deepseek.com/v1", model="deepseek-chat")
        self.assertFalse(remote.configured, "公网服务没 Key 不能算配置完整")


class LLMClientLocalTest(unittest.TestCase):
    def test_no_authorization_header_without_key(self):
        cfg = ModelConfig(base_url="http://127.0.0.1:11434/v1", model="qwen2.5:7b")
        headers = LLMClient(cfg)._headers()
        self.assertNotIn("Authorization", headers,
                         "空 Key 还发 `Bearer ` 会被部分网关判 400")

    def test_authorization_header_with_key(self):
        cfg = ModelConfig(base_url="https://api.deepseek.com/v1", model="deepseek-chat",
                          api_key="sk-test")
        self.assertEqual(LLMClient(cfg)._headers()["Authorization"], "Bearer sk-test")

    def test_local_prefers_direct_connection(self):
        cfg = ModelConfig(base_url="http://127.0.0.1:11434/v1", model="qwen2.5:7b")
        self.assertEqual(LLMClient(cfg)._network_modes(), ("direct", "auto"),
                         "本机服务必须优先直连，别让系统代理截胡")

    def test_remote_prefers_system_proxy_first(self):
        cfg = ModelConfig(base_url="https://api.deepseek.com/v1", model="d",
                          api_key="k")
        self.assertEqual(LLMClient(cfg)._network_modes(), ("auto", "direct"))

    def test_timeout_comes_from_config(self):
        cfg = ModelConfig(base_url="http://127.0.0.1:8000/v1", model="m", timeout_s=180)
        self.assertEqual(LLMClient(cfg).timeout, 180)
        self.assertEqual(LLMClient(cfg, timeout=25).timeout, 25, "显式传入优先")

    def test_models_endpoint(self):
        cfg = ModelConfig(base_url="http://127.0.0.1:11434/v1", model="m")
        self.assertEqual(LLMClient(cfg)._models_endpoint(),
                         "http://127.0.0.1:11434/v1/models")


class VisionToggleTest(unittest.TestCase):
    def test_vision_grounder_disabled_when_model_has_no_vision(self):
        from atp.agents.llm.vision_ground import VisionGrounder

        cfg = ModelConfig(base_url="http://127.0.0.1:11434/v1", model="qwen2.5:7b",
                          vision=False)
        self.assertFalse(VisionGrounder(LLMClient(cfg)).available,
                         "关了视觉就不能再拿截图去换报错")


class StubServerTest(unittest.TestCase):
    """起一个假的本地 OpenAI 兼容服务，验证 /models 与无 Key 调用链路。"""

    @classmethod
    def setUpClass(cls):
        import http.server
        import json
        import threading

        seen = {"auth": []}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静音
                pass

            def _send(self, payload):
                raw = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                if self.path.endswith("/models"):
                    return self._send({"data": [{"id": "qwen2.5:7b"},
                                                {"id": "llama3.1:8b"}]})
                self._send({"error": "not found"})

            def do_POST(self):  # noqa: N802
                seen["auth"].append(self.headers.get("Authorization"))
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self._send({"choices": [{"message": {"content": "就绪"}}]})

        cls.seen = seen
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _cfg(self, **kw):
        return ModelConfig(base_url=f"http://127.0.0.1:{self.port}/v1",
                           model="qwen2.5:7b", **kw)

    def test_list_models(self):
        models = LLMClient(self._cfg()).list_models()
        self.assertEqual(models, ["qwen2.5:7b", "llama3.1:8b"])

    def test_chat_without_key(self):
        client = LLMClient(self._cfg())
        self.seen["auth"].clear()
        self.assertEqual(client.chat([{"role": "user", "content": "ping"}], max_tokens=8),
                         "就绪")
        self.assertEqual(self.seen["auth"], [None], "本地模型不应发送 Authorization 头")
        self.assertEqual(client.last_path, "direct", "本机服务应走直连")


if __name__ == "__main__":
    unittest.main()
