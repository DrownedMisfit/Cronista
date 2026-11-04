"""Segunda pasada (Whisper sobre lo transcrito en vivo con Parakeet), con un Whisper simulado."""
from __future__ import annotations

import time

import numpy as np
import pytest
import soundfile as sf

from cronista.core import hardware
from cronista.core.events import EventBus, Topics
from cronista.core.settings import Settings
from cronista.notes.store import Store
from cronista.stt.engine import STTEngine
from cronista.stt.refiner import SecondPass, cleanup_orphans, similarity


class FakeWhisper:
    name = "fake-whisper"

    def __init__(self, text):
        self.text = text
        self.calls = 0

    def transcribe(self, audio, **kw):
        self.calls += 1
        return self.text, {}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    bus = EventBus()
    s = Settings()
    st = Store(tmp_path / "t.db")
    stt = STTEngine(bus, s, hardware.HardwareInfo(8, 16, 32, False))
    sp = SecondPass(bus, s, st, stt, hardware.HardwareInfo(8, 16, 32, False))
    sid = st.create_session({"game": "Rise of Iron: D-Day", "started_at": "2026-10-09T10:00:00"})
    events = []
    bus.subscribe(Topics.NOTE_UPDATED, events.append)

    def note(live_text, user_edited=False):
        wav = tmp_path / f"a{time.monotonic_ns()}.flac"
        sf.write(str(wav), np.zeros(16000, np.float32), 16000, format="FLAC")
        nid = st.create_note({"session_id": sid, "position_ms": 1000, "text_raw": live_text, "text": live_text,
                              "stt_info": {"backend": "parakeet", "audio_cache": str(wav)}})
        if user_edited:
            st.update_note(nid, user_edited=1)
        return nid, wav

    yield {"s": s, "st": st, "sp": sp, "note": note, "events": events, "tmp": tmp_path}
    st.close()


def _run_one(env, whisper_text):
    env["sp"]._whisper = FakeWhisper(whisper_text)
    env["sp"]._refine(env["st"].notes_pending_refine(limit=1)[0])


def test_applies_better_text_and_deletes_audio(env):
    nid, wav = env["note"]("Estoy jugando Rise of Ironed y el boss es difícil")
    _run_one(env, "Estoy jugando Rise of Iron D-Day y el boss es difícil")
    n = env["st"].get_note(nid)
    assert n["text"] == "Estoy jugando Rise of Iron: D-Day y el boss es difícil"   # + corrector del glosario
    assert n["stt_info"]["live_text"].endswith("Wared y el boss es difícil")
    assert n["stt_info"]["second_pass"]["applied"] is True
    assert "audio_cache" not in n["stt_info"] and not wav.exists()
    assert env["events"] and env["events"][-1].get("source") == "refine"
    assert env["st"].notes_pending_refine() == []


def test_rejects_hallucination_or_very_different_text(env):
    nid, wav = env["note"]("El combate se siente lento")
    _run_one(env, "Subtítulos realizados por la comunidad de Amara.org")
    n = env["st"].get_note(nid)
    assert n["text"] == "El combate se siente lento" and n["stt_info"]["second_pass"]["applied"] is False
    nid2, _ = env["note"]("El combate se siente lento")
    _run_one(env, "Me encanta la música de este nivel tan bonito")
    assert env["st"].get_note(nid2)["text"] == "El combate se siente lento"
    assert not wav.exists()


def test_never_touches_user_edited_notes(env):
    nid, _ = env["note"]("texto que corregí yo", user_edited=True)
    _run_one(env, "texto que corregi yo mismo")
    n = env["st"].get_note(nid)
    assert n["text"] == "texto que corregí yo" and n["stt_info"]["second_pass"]["reason"] == "editada por ti"


def test_waits_while_gaming_and_discards_when_disabled(env):
    nid, wav = env["note"]("El combate se siente lento")
    fake = FakeWhisper("El combate se siente muy lento")
    env["sp"]._backend = lambda: fake
    env["sp"].start()
    env["sp"]._on_game(type("E", (), {"get": lambda self, k, d=None: True})())
    time.sleep(0.5)
    assert fake.calls == 0 and wav.exists()                           # jugando: no repasa
    env["s"].stt.second_pass = False
    env["sp"]._wake.set()
    t0 = time.monotonic()
    while env["st"].notes_pending_refine() and time.monotonic() - t0 < 5:
        time.sleep(0.05)
    env["sp"].stop()
    assert not wav.exists() and fake.calls == 0                       # desactivada: se borra el audio sin repasar
    assert env["st"].get_note(nid)["stt_info"]["second_pass"]["reason"] == "segunda pasada desactivada"


def test_similarity_and_orphan_cleanup(env, monkeypatch):
    assert similarity("Rise of Ironed", "Rise of Iron D-Day") > 0.7
    assert similarity("hola", "") == 0.0
    d = env["tmp"] / "Cronista" / "audio_pendiente"
    d.mkdir(parents=True, exist_ok=True)
    orphan = d / "huerfano.flac"
    orphan.write_bytes(b"x")
    assert cleanup_orphans(env["st"], max_age_s=-1) == 1 and not orphan.exists()


def test_second_pass_status_texts():
    from cronista.ui.widgets import second_pass_status
    assert "Repasada con Whisper" in second_pass_status({"second_pass": {"done": True, "applied": True, "similarity": 0.87}})
    assert "editada por ti" in second_pass_status({"second_pass": {"done": True, "applied": False, "reason": "editada por ti"}})
    assert "Pendiente" in second_pass_status({"audio_cache": "x.flac"})
    assert not second_pass_status({})
