"""Proveedores de modelos de lenguaje para el envío OPCIONAL de notas a un chatbot.

Por defecto no hay ningún proveedor activo: el "paquete para chatbot" se copia al
portapapeles y el usuario lo pega donde quiera. Si configura un proveedor por API
(OpenAI-compatible: OpenAI, LM Studio, Ollama, OpenRouter…; o Anthropic), la
aplicación lo indica claramente como función ONLINE y solo envía el texto de las
notas (nunca audio) cuando el usuario pulsa el botón.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Protocol

log = logging.getLogger(__name__)

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think(text: str) -> str:
    """Quita el razonamiento de modelos tipo Qwen3: bloques <think>…</think>, cierres huérfanos y aperturas sin cerrar."""
    text = _THINK_BLOCK.sub("", text)
    if "</think>" in text:          # el bloque se abrió en el prompt y solo llega su cierre
        text = text.rsplit("</think>", 1)[1]
    if "<think>" in text:           # bloque sin cerrar (p. ej. cortado por max_tokens)
        text = text.split("<think>", 1)[0]
    return text.strip()

PROVIDERS = {
    "none": "Ninguno (copiar al portapapeles)",
    "openai": "API compatible con OpenAI (online)",
    "anthropic": "API de Anthropic / Claude (online)",
}

CHATBOT_URLS = {
    "ChatGPT": "https://chatgpt.com/",
    "Claude": "https://claude.ai/new",
    "Gemini": "https://gemini.google.com/app",
    "Copilot": "https://copilot.microsoft.com/",
}


class LLMProvider(Protocol):
    name: str
    online: bool

    def complete(self, prompt: str, system: str = "") -> str: ...

    def chat(self, messages: list[dict], *, json_schema: dict | None = None, max_tokens: int = 512,
             temperature: float = 0.2, timeout: float = 180.0) -> str: ...


class OpenAICompatibleProvider:
    name = "openai"
    online = True

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0) -> None:
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key
        self.model = model or "gpt-4o-mini"
        self.timeout = timeout

    def complete(self, prompt: str, system: str = "") -> str:
        import requests
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        r = requests.post(f"{self.base_url}/chat/completions", headers=headers, timeout=self.timeout,
                          data=json.dumps({"model": self.model, "messages": msgs, "temperature": 0.4}))
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()

    def chat(self, messages: list[dict], *, json_schema: dict | None = None, max_tokens: int = 512,
             temperature: float = 0.2, timeout: float = 180.0) -> str:
        import requests
        body: dict = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                      "temperature": temperature}
        if json_schema:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "result", "schema": json_schema}}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        r = requests.post(f"{self.base_url}/chat/completions", headers=headers, timeout=timeout,
                          data=json.dumps(body))
        r.raise_for_status()
        return strip_think(r.json()["choices"][0]["message"]["content"] or "")


class AnthropicProvider:
    name = "anthropic"
    online = True

    def __init__(self, api_key: str, model: str, timeout: float = 120.0) -> None:
        self.api_key = api_key
        self.model = model or "claude-sonnet-5-5"
        self.timeout = timeout

    def complete(self, prompt: str, system: str = "") -> str:
        import requests
        body = {"model": self.model, "max_tokens": 4000, "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=self.timeout,
                          headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"}, data=json.dumps(body))
        r.raise_for_status()
        data = r.json()
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()

    def chat(self, messages: list[dict], *, json_schema: dict | None = None, max_tokens: int = 512,
             temperature: float = 0.2, timeout: float = 180.0) -> str:
        import requests
        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        rest = [m for m in messages if m.get("role") != "system"]
        system = "\n\n".join(system_parts)
        if json_schema:
            instr = "Responde solo con JSON que cumpla este esquema: " + json.dumps(json_schema, ensure_ascii=False)
            system = f"{system}\n\n{instr}" if system else instr
        body: dict = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature, "messages": rest}
        if system:
            body["system"] = system
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=timeout,
                          headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"}, data=json.dumps(body))
        r.raise_for_status()
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return strip_think(text)


def build_provider(ai_settings) -> LLMProvider | None:
    p = (ai_settings.provider or "none").lower()
    if p == "openai":
        return OpenAICompatibleProvider(ai_settings.base_url, ai_settings.api_key, ai_settings.model)
    if p == "anthropic":
        return AnthropicProvider(ai_settings.api_key, ai_settings.model)
    return None


SYSTEM_PROMPT = ("Eres un asistente para un creador de videoensayos sobre videojuegos. Trabajas solo con las notas "
                 "que te da, no inventas hechos ni opiniones suyas, citas los timestamps y respondes en español.")
