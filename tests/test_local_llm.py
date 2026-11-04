"""Pruebas de la IA local sin red real ni binarios: servidor HTTP falso y proceso simulado."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cronista.ai import local_llm
from cronista.ai.local_llm import LLMUnavailable, LlamaServer, LocalLLM, build_chat_llm, find_model, free_port
from cronista.ai.providers import AnthropicProvider, OpenAICompatibleProvider, strip_think
from cronista.core.settings import LLMSettings, Settings

ANSWER = '<think>x</think>{"ok": true}'
MSG = [{"role": "user", "content": "hola"}]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silencia el log del servidor de prueba
        pass

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "no encontrado"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) or b"{}"
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": "no encontrado"})
            return
        self.server.bodies.append(json.loads(raw))
        self._send(200, {"choices": [{"message": {"content": ANSWER}}]})


@pytest.fixture
def fake_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    srv.bodies = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _base(srv) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _FakeProc:
    """Imita subprocess.Popen: vive hasta que se le llama a terminate()/kill()."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.rc = None
        self.terminated = False

    def poll(self):
        return self.rc

    def terminate(self) -> None:
        self.terminated = True
        self.rc = 0

    def kill(self) -> None:
        self.rc = -9

    def wait(self, timeout=None):
        return self.rc


@pytest.fixture
def managed(tmp_path, monkeypatch, fake_server):
    """Entorno con runtime y modelo falsos; Popen simulado; el 'health' lo atiende fake_server."""
    rt = tmp_path / "rt"
    rt.mkdir()
    (rt / "llama-server.exe").write_bytes(b"")
    models = tmp_path / "models"
    models.mkdir()
    (models / "modelo.gguf").write_bytes(b"")
    monkeypatch.setattr(local_llm, "llm_runtime_dir", lambda: rt)
    monkeypatch.setattr(local_llm, "llm_models_dir", lambda: models)
    monkeypatch.setattr(local_llm, "logs_dir", lambda: tmp_path)
    monkeypatch.setattr(local_llm, "_assign_to_job", lambda pid: 0)
    monkeypatch.setattr(local_llm, "_lower_priority", lambda pid: None)

    procs: list[_FakeProc] = []
    cmds: list[list[str]] = []

    def fake_popen(cmd, **kw):
        cmds.append(cmd)
        p = _FakeProc(1000 + len(procs))
        procs.append(p)
        return p

    monkeypatch.setattr(local_llm.subprocess, "Popen", fake_popen)
    return {"procs": procs, "cmds": cmds, "models": models, "port": fake_server.server_address[1]}


def _arg(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


# ---- contrato y limpieza de respuesta -------------------------------------------
@pytest.mark.parametrize("texto, esperado", [
    ("<think>a</think>{}", "{}"),
    ("<think>sin cerrar", ""),
    ('razonamiento</think>  {"a": 1}', '{"a": 1}'),
    ("hola", "hola"),
])
def test_strip_think(texto, esperado):
    assert strip_think(texto) == esperado


def test_chat_devuelve_texto_sin_think(fake_server):
    llm = LocalLLM(LLMSettings(), base_url=_base(fake_server))
    assert llm.chat(MSG) == '{"ok": true}'
    assert llm.name == "local" and llm.online is False


def test_chat_envia_no_thinking_y_esquema(fake_server):
    llm = LocalLLM(LLMSettings(), base_url=_base(fake_server))
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    llm.chat(MSG, json_schema=schema, max_tokens=64)
    body = fake_server.bodies[-1]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["max_tokens"] == 64 and body["stream"] is False
    assert body["response_format"] == {"type": "json_schema",
                                       "json_schema": {"name": "result", "schema": schema}}
    llm.chat(MSG)
    assert "response_format" not in fake_server.bodies[-1]


def test_error_http_es_llm_unavailable(fake_server):
    llm = LocalLLM(LLMSettings(), base_url=_base(fake_server) + "/ruta-inexistente")
    with pytest.raises(LLMUnavailable, match="HTTP 404"):
        llm.chat(MSG)


def test_error_de_conexion_es_llm_unavailable():
    llm = LocalLLM(LLMSettings(), base_url=f"http://127.0.0.1:{free_port()}")
    with pytest.raises(LLMUnavailable):
        llm.chat(MSG)


def test_proveedor_openai_chat(fake_server):
    p = OpenAICompatibleProvider(_base(fake_server) + "/v1", "", "modelo")
    assert p.chat(MSG, json_schema={"type": "object"}) == '{"ok": true}'
    assert fake_server.bodies[-1]["response_format"]["type"] == "json_schema"


def test_proveedor_anthropic_chat_con_esquema(monkeypatch):
    enviado: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"type": "text", "text": ANSWER}]}

    def fake_post(url, **kw):
        enviado["url"] = url
        enviado["body"] = json.loads(kw["data"])
        return _Resp()

    monkeypatch.setattr("requests.post", fake_post)
    p = AnthropicProvider("clave", "")
    msgs = [{"role": "system", "content": "Sé breve."}, {"role": "user", "content": "hola"}]
    assert p.chat(msgs, json_schema={"type": "object"}) == '{"ok": true}'
    assert "Responde solo con JSON" in enviado["body"]["system"]
    assert "Sé breve." in enviado["body"]["system"]
    assert enviado["body"]["messages"] == [{"role": "user", "content": "hola"}]


# ---- localizar runtime y modelo -------------------------------------------------
def test_find_model_ignora_mmproj(tmp_path, monkeypatch):
    monkeypatch.setattr(local_llm, "llm_models_dir", lambda: tmp_path)
    for nombre in ("aaa-mmproj-F16.gguf", "qwen.gguf", "zeta.gguf", "notas.txt"):
        (tmp_path / nombre).write_bytes(b"")
    assert find_model(LLMSettings()).name == "qwen.gguf"
    assert find_model(LLMSettings(model_path=str(tmp_path / "zeta.gguf"))).name == "zeta.gguf"
    # una ruta configurada que no existe cae al automático
    assert find_model(LLMSettings(model_path=str(tmp_path / "no.gguf"))).name == "qwen.gguf"


def test_available_indica_motivo(tmp_path, monkeypatch):
    rt = tmp_path / "rt"
    rt.mkdir()
    models = tmp_path / "m"
    models.mkdir()
    monkeypatch.setattr(local_llm, "llm_runtime_dir", lambda: rt)
    monkeypatch.setattr(local_llm, "llm_models_dir", lambda: models)

    ok, motivo = LocalLLM(LLMSettings()).available()
    assert ok is False and "llama-server" in motivo

    (rt / "llama-server.exe").write_bytes(b"")
    ok, motivo = LocalLLM(LLMSettings()).available()
    assert ok is False and "gguf" in motivo

    (models / "x.gguf").write_bytes(b"")
    assert LocalLLM(LLMSettings()).available() == (True, "")
    assert LocalLLM(LLMSettings(), base_url="http://127.0.0.1:1").available() == (True, "")


def test_build_chat_llm(monkeypatch):
    s = Settings()
    s.llm.enabled = False
    assert build_chat_llm(s) is None
    s.llm.enabled = True
    llm = build_chat_llm(s)
    assert isinstance(llm, LocalLLM) and llm.online is False
    assert build_chat_llm(s, "api") is None   # provider por defecto: none


# ---- ciclo de vida del servidor (proceso simulado) ------------------------------
def test_arranque_comando_chat_y_cambio_de_dispositivo(managed):
    cfg = LLMSettings(port=managed["port"], threads=3, gpu_layers=99, context_size=4096)
    llm = LocalLLM(cfg, clock=_Clock())
    assert llm.state == "off"

    assert llm.chat(MSG) == '{"ok": true}'
    cmd = managed["cmds"][0]
    assert _arg(cmd, "-m") == str(managed["models"] / "modelo.gguf")
    assert _arg(cmd, "--host") == "127.0.0.1"
    assert _arg(cmd, "--port") == str(managed["port"])
    assert _arg(cmd, "-ngl") == "99" and _arg(cmd, "-t") == "3" and _arg(cmd, "-c") == "4096"
    assert _arg(cmd, "-np") == "1" and "--jinja" in cmd
    assert llm.state == "idle" and llm._server.running

    # cambiar a CPU reinicia el servidor en la siguiente llamada
    llm.set_device("cpu")
    llm.chat(MSG)
    assert managed["procs"][0].terminated
    assert _arg(managed["cmds"][1], "-ngl") == "0"
    llm.unload()


def test_idle_descarga_tras_inactividad(managed):
    clock = _Clock()
    llm = LocalLLM(LLMSettings(port=managed["port"], idle_unload_min=5), clock=clock)
    llm.chat(MSG)
    proc = managed["procs"][0]

    clock.t += 4 * 60
    assert llm.idle_check() is False and llm._server.running

    clock.t += 2 * 60                     # 6 min sin uso
    assert llm.idle_check() is True
    assert proc.terminated and not llm._server.running
    assert llm.state == "off"
    assert llm.idle_check() is False      # ya descargado, nada que hacer


def test_idle_nunca_si_esta_desactivado(managed):
    clock = _Clock()
    llm = LocalLLM(LLMSettings(port=managed["port"], idle_unload_min=0), clock=clock)
    llm.chat(MSG)
    clock.t += 10 * 3600
    assert llm.idle_check() is False and llm._server.running
    llm.unload()


def test_proceso_que_muere_da_llm_unavailable(managed, monkeypatch):
    class _Muerto(_FakeProc):
        def poll(self):
            return 1

    monkeypatch.setattr(local_llm.subprocess, "Popen", lambda cmd, **kw: _Muerto(1))
    srv = LlamaServer(LLMSettings(port=free_port(), threads=2))
    with pytest.raises(LLMUnavailable, match="terminó"):
        srv.start("cpu")
    assert not srv.running
