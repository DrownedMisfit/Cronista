"""Pruebas del trabajador de IA con un modelo falso (sin llama.cpp ni red)."""
from __future__ import annotations

import json
import threading
import time

import pytest

from cronista.ai.worker import AnalysisWorker
from cronista.context.engine import ContextEngine
from cronista.core.events import EventBus, Topics
from cronista.core.settings import Settings
from cronista.learning.personalizer import Personalizer
from cronista.notes.manager import NoteManager
from cronista.notes.store import Store
from cronista.session.manager import SessionManager


class FakeLLM:
    name, online = "fake", False

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.device = "gpu"
        self.loaded = False
        self.unloads = 0

    def available(self):
        return True, ""

    def set_device(self, device):
        self.device = device

    def idle_check(self):
        pass

    def unload(self):
        self.loaded = False
        self.unloads += 1

    @property
    def state(self):
        return "idle" if self.loaded else "off"

    def chat(self, messages, *, json_schema=None, max_tokens=512, temperature=0.2, timeout=180.0):
        self.loaded = True
        self.calls.append({"messages": messages, "schema": json_schema})
        user = messages[-1]["content"]
        props = (json_schema or {}).get("properties", {})
        if "scope" in props:   # clasificación
            return json.dumps({"scope": "other", "functions": ["crítica"], "topics": ["combate"],
                               "subtopic": "cobertura", "refs": [{"name": "Resident Evil 4", "relation": "comparación"}],
                               "chapter": "Capítulo 3", "section": "", "confidence": 0.9, "ambiguous": False,
                               "rationale": "habla del sistema de cobertura"})
        if "criticas" in props:   # resumen
            return json.dumps({"criticas": [{"texto": "Combate lento", "timestamps": ["00:12:34", "09:99:99"]}],
                               "positivos": [], "temas_recurrentes": ["combate"], "ideas_video": [],
                               "desarrollar": [{"timestamp": "00:12:34", "texto": "ritmo del combate"}]})
        return json.dumps({"text": "Este combate se siente demasiado lento."})


@pytest.fixture
def env(tmp_path):
    bus = EventBus()
    s = Settings()
    s.learning.learn_corrections = True
    store = Store(tmp_path / "t.db")
    sessions = SessionManager(bus, s, store)
    context = ContextEngine()
    pers = Personalizer(store, s)
    notes = NoteManager(bus, s, store, sessions, context)
    llm = FakeLLM()
    worker = AnalysisWorker(bus, s, store, sessions, context, pers, llm_factory=lambda st: llm)
    events: list = []
    bus.subscribe(EventBus.WILDCARD, events.append)
    worker.start()
    sess = sessions.start(mode="manual", game="Rise of Iron D-Day")
    yield {"bus": bus, "s": s, "store": store, "sessions": sessions, "notes": notes, "llm": llm,
           "worker": worker, "events": events, "sess": sess}
    worker.stop()
    sessions.stop()
    store.close()


def _wait(pred, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _new_note(env, text, pos):
    note = {"session_id": env["sess"]["id"], "position_ms": pos, "text_raw": text, "text": text,
            "scope": "current", "functions": ["opinión"], "topics": ["ritmo"], "context": {"source": "heuristic"}}
    note["id"] = env["store"].create_note(note)
    env["bus"].publish(Topics.NOTE_CREATED, note=dict(note))
    return note["id"]


def test_classify_refines_note_and_relations(env):
    nid = _new_note(env, "Este combate se siente demasiado lento comparado con Resident Evil 4", 754_000)
    assert _wait(lambda: (env["store"].get_note(nid)["context"] or {}).get("source") == "llm")
    n = env["store"].get_note(nid)
    assert n["topics"] == ["combate"] and n["functions"] == ["crítica"] and n["scope"] == "other"
    assert n["context"]["subtopic"] == "cobertura" and n["context"]["heuristic"]["topics"] == ["ritmo"]
    assert any(e.topic == Topics.AI_RESULT and e.kind == "classify" for e in env["events"])
    nid2 = _new_note(env, "El combate con cobertura se siente lento otra vez", 790_000)
    assert _wait(lambda: any(r["id"] == nid for r in env["store"].related_notes(nid2)))


def test_user_locked_fields_are_not_overwritten(env):
    env["s"].llm.auto_classify = False
    nid = _new_note(env, "Esta zona está muy bien diseñada", 100_000)
    env["notes"].update(nid, learn_field="scope", scope="current", topics=["diseño de niveles"])
    assert set(env["store"].get_note(nid)["context"]["user_locked"]) == {"scope", "topics"}
    env["worker"].request_classify(nid)
    assert _wait(lambda: (env["store"].get_note(nid)["context"] or {}).get("source") == "llm")
    n = env["store"].get_note(nid)
    assert n["scope"] == "current" and n["topics"] == ["diseño de niveles"]   # respetado
    assert n["functions"] == ["crítica"]                                      # no bloqueado → IA


def test_defer_while_gaming_then_resume(env):
    env["bus"].publish(Topics.PERF_GAME_DETECTED, detected=True, title="Rise")
    nid = _new_note(env, "El sistema de cobertura falla bastante", 200_000)
    assert _wait(lambda: env["worker"].state == "deferred")
    time.sleep(0.3)
    assert (env["store"].get_note(nid)["context"] or {}).get("source") != "llm"   # aún no
    assert env["llm"].calls == []
    env["bus"].publish(Topics.PERF_GAME_DETECTED, detected=False, title="")
    assert _wait(lambda: (env["store"].get_note(nid)["context"] or {}).get("source") == "llm")


def test_user_requests_run_even_while_gaming(env):
    env["s"].llm.auto_classify = False
    nid = _new_note(env, "este combate se siente demasiado lento", 754_000)
    env["bus"].publish(Topics.PERF_GAME_DETECTED, detected=True, title="Rise")
    env["worker"].request_transform(nid, "correct")
    assert _wait(lambda: env["store"].get_note(nid)["text_corrected"])
    assert env["store"].get_note(nid)["text_corrected"] == "Este combate se siente demasiado lento."


def test_game_start_unloads_model_with_defer_policy(env):
    env["s"].llm.auto_classify = False
    nid = _new_note(env, "este combate se siente demasiado lento", 754_000)
    env["worker"].request_transform(nid, "improve")
    assert _wait(lambda: env["llm"].loaded)
    env["bus"].publish(Topics.PERF_GAME_DETECTED, detected=True, title="Rise")
    assert env["llm"].unloads >= 1 and not env["llm"].loaded


def test_summary_drops_invented_timestamps(env):
    env["s"].llm.auto_classify = False
    _new_note(env, "Este combate se siente demasiado lento", 754_000)
    got = threading.Event()
    env["bus"].subscribe(Topics.AI_RESULT, lambda e: e.kind == "summary" and got.set())
    env["worker"].request_summary(env["sess"]["id"])
    assert got.wait(5)
    stored = env["store"].get_session(env["sess"]["id"])["summary"]["local"]
    assert stored["criticas"][0]["timestamps"] == ["00:12:34"]
    assert "Principales críticas" in stored["markdown"]


def test_unavailable_llm_keeps_heuristic_silently(env, tmp_path):
    class Missing(FakeLLM):
        def available(self):
            return False, "Falta el modelo"
    env["worker"].reset_llm()
    env["worker"]._llm_factory = lambda st: Missing()
    nid = _new_note(env, "Esta zona está muy bien diseñada", 100_000)
    assert _wait(lambda: env["worker"].state == "unavailable")
    assert (env["store"].get_note(nid)["context"] or {}).get("source") == "heuristic"
    assert not any(e.topic == Topics.AI_ERROR for e in env["events"])   # sin errores ruidosos


def test_unload_after_job_if_game_started_meanwhile(env):
    """El juego pasa a primer plano a mitad de una tarea: el modelo se libera al terminarla."""
    env["s"].llm.auto_classify = False
    nid = _new_note(env, "este combate se siente demasiado lento", 754_000)
    llm = env["llm"]
    real_chat, real_unload = llm.chat, llm.unload
    llm.unload = lambda: None          # mientras la llamada está en curso, unload no surte efecto

    def chat_during_which_game_starts(*a, **k):
        out = real_chat(*a, **k)
        env["bus"].publish(Topics.PERF_GAME_DETECTED, detected=True, title="Rise")
        llm.unload = real_unload       # la llamada termina: el unload vuelve a funcionar
        return out

    llm.chat = chat_during_which_game_starts
    env["worker"].request_transform(nid, "correct")
    assert _wait(lambda: env["store"].get_note(nid)["text_corrected"])
    assert _wait(lambda: not llm.loaded)
