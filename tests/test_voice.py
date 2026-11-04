"""Pruebas del perfil de voz sin el modelo real: codificador simulado con 'timbres' sintéticos."""
from __future__ import annotations

import numpy as np
import pytest

import cronista.voice.speaker as sp
from cronista.core.settings import Settings
from cronista.voice.features import fbank
from cronista.voice.speaker import VoiceGate, VoiceProfile, build_profile, speech_only

SR = 16000


def tone(freq: float, seconds: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(SR * seconds)) / SR
    return (0.2 * np.sin(2 * np.pi * freq * t) + 0.01 * rng.standard_normal(len(t))).astype(np.float32)


class FakeEncoder:
    """'Timbre' = la banda mel dominante del audio: tonos parecidos dan embeddings parecidos."""

    def embed(self, audio):
        f = fbank(audio, cmn=False)
        if len(f) < 50:
            return None
        v = np.exp(f.mean(axis=0) - f.mean(axis=0).max())
        return (v / np.linalg.norm(v)).astype(np.float32)


@pytest.fixture
def voice_env(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "profile_path", lambda: tmp_path / "profile.json")
    monkeypatch.setattr(sp, "model_available", lambda: True)
    s = Settings()
    s.voice.use_voice_id = True
    gate = VoiceGate(s)
    gate._encoder = FakeEncoder()
    return s, gate, tmp_path


def test_build_profile_requires_enough_speech():
    with pytest.raises(ValueError):
        build_profile(FakeEncoder(), tone(220, 5))


def test_profile_roundtrip_and_threshold_bounds(voice_env):
    s, gate, tmp = voice_env
    prof = build_profile(FakeEncoder(), tone(220, 30))
    assert 0.25 <= prof.threshold <= 0.45 and prof.chunks == 10 and prof.consistency > 0.9
    prof.save()
    loaded = VoiceProfile.load()
    assert loaded is not None and np.allclose(loaded.vector, prof.vector, atol=1e-5)
    assert len(loaded.embedding) == len(prof.embedding)
    VoiceProfile.delete()
    assert VoiceProfile.load() is None


def test_gate_accepts_me_marks_other(voice_env):
    s, gate, _ = voice_env
    build_profile(FakeEncoder(), tone(220, 30)).save()
    me = gate(tone(220, 3, seed=1))
    other = gate(tone(3000, 3, seed=2))
    assert me["action"] == "process" and me["match"] is True
    assert other["action"] == "mark" and other["match"] is False and other["score"] < me["score"]


def test_gate_skip_mode_and_safety_rules(voice_env):
    s, gate, _ = voice_env
    build_profile(FakeEncoder(), tone(220, 30)).save()
    s.voice.non_match_action = "skip"
    assert gate(tone(3000, 3))["action"] == "skip"
    # el hotkey y las frases cortas nunca se filtran (prioridad: no perder palabras)
    assert gate(tone(3000, 3), forced=True)["action"] == "process"
    assert gate(tone(3000, 0.6))["action"] == "process"


def test_gate_disabled_or_without_profile_processes_everything(voice_env):
    s, gate, _ = voice_env
    assert gate(tone(3000, 3))["action"] == "process"        # sin perfil
    build_profile(FakeEncoder(), tone(220, 30)).save()
    s.voice.use_voice_id = False
    assert gate(tone(3000, 3))["action"] == "process"        # desactivado


def test_gate_errors_never_lose_the_phrase(voice_env):
    s, gate, _ = voice_env
    build_profile(FakeEncoder(), tone(220, 30)).save()

    class Broken:
        def embed(self, audio):
            raise RuntimeError("fallo del modelo")

    gate._encoder = Broken()
    assert gate(tone(3000, 3))["action"] == "process"


def test_manual_threshold_overrides_profile(voice_env):
    s, gate, _ = voice_env
    build_profile(FakeEncoder(), tone(220, 30)).save()
    other = tone(3000, 3, seed=3)
    assert gate(other)["match"] is False                   # umbral automático: otra voz no pasa
    s.voice.threshold = 0.01
    assert gate.threshold() == 0.01
    assert gate(other)["match"] is True                    # umbral manual permisivo: ahora sí pasa


def test_speech_only_keeps_voiced_blocks():
    a = np.arange(512 * 4, dtype=np.float32)
    out = speech_only(a, [0.9, 0.1, 0.8, 0.2])
    assert len(out) == 1024 and out[0] == 0 and out[512] == 1024


def test_stt_skips_transcription_for_other_voice():
    """Con acción 'skip', el transcriptor ni carga Whisper: publica NOTE_DISCARDED."""
    from cronista.core import hardware
    from cronista.core.events import EventBus, Topics
    from cronista.stt.engine import STTEngine, _Task

    bus = EventBus()
    eng = STTEngine(bus, Settings(), hardware.HardwareInfo(8, 16, 32, False))
    eng.voice_gate = lambda audio, forced: {"action": "skip", "match": False, "score": 0.1, "threshold": 0.4}
    eng._ensure_main = lambda: (_ for _ in ()).throw(AssertionError("no debía transcribir"))
    got = []
    bus.subscribe(Topics.NOTE_DISCARDED, got.append)
    eng._handle(_Task(1, 1, "final", "u1", np.zeros(SR * 2, np.float32), {"forced": False}))
    assert got and got[0].reason == "other_voice"


def test_short_phrases_get_relaxed_threshold_and_skip_needs_certainty(voice_env):
    s, gate, _ = voice_env
    build_profile(FakeEncoder(), tone(220, 30)).save()
    base = gate.threshold()
    assert gate.effective_threshold(1.0) == pytest.approx(max(0.2, base - sp.SHORT_RELAX))
    assert gate.effective_threshold(3.0) == pytest.approx(base)
    assert gate.effective_threshold(1.5) < gate.effective_threshold(2.5) < base
    s.voice.non_match_action = "skip"
    # otra voz pero frase de 1,5 s: no hay certeza suficiente → solo se marca, no se descarta
    assert gate(tone(3000, 1.5))["action"] == "mark"
    assert gate(tone(3000, 3))["action"] == "skip"
    # a medio camino del umbral (dentro del margen): se marca
    gate.score = lambda audio: base - sp.SKIP_MARGIN / 2
    assert gate(tone(3000, 3))["action"] == "mark"


def test_ui_helpers_for_other_voice_and_quality():
    from cronista.ui.voice_dialog import quality_label
    from cronista.ui.widgets import fmt_score, is_other_voice
    other = {"stt_info": {"voice": {"match": False, "score": 0.21}}}
    assert is_other_voice(other)
    assert not is_other_voice({"stt_info": {"voice": {"match": False, "confirmed": True}}})   # "Es mi voz"
    assert not is_other_voice({"stt_info": {"voice": {"match": None}}})                         # sin verificar
    assert not is_other_voice({"stt_info": {}}) and not is_other_voice(None)
    assert fmt_score(0.214) == "0,21" and fmt_score(None) == "—"
    assert quality_label(0.8)[0] == "buena" and quality_label(0.65)[0] == "aceptable"
    assert quality_label(0.4)[0] == "baja" and quality_label(0.4)[1]
