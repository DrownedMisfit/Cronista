"""Pruebas del núcleo sin hardware: reloj de grabación, segmentador, contexto, base de datos, exportación."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from cronista.audio.segmenter import SegmenterConfig, UtteranceSegmenter
from cronista.context.engine import ContextEngine
from cronista.export import exporters
from cronista.notes.manager import is_relevant
from cronista.notes.store import Store
from cronista.session.clock import RecordingClock, fmt_timestamp
from cronista.stt.engine import looks_like_hallucination


# ---- reloj -------------------------------------------------------------------
def test_clock_basic_and_pause():
    c = RecordingClock()
    c.start(mono=100.0, pos_ms=0)
    assert c.position_at(100.0) == 0
    assert c.position_at(112.5) == pytest.approx(12_500)
    c.pause(mono=120.0)
    assert c.position_at(125.0) == pytest.approx(20_000)   # no avanza en pausa
    c.resume(mono=130.0)
    assert c.position_at(135.0) == pytest.approx(25_000)
    # consulta de un instante pasado (antes de la pausa) sigue siendo correcta
    assert c.position_at(115.0) == pytest.approx(15_000)


def test_clock_sync_corrects_drift():
    c = RecordingClock()
    c.start(mono=0.0, pos_ms=0)
    assert c.sync(10_000, mono=10.0) == 0          # sin deriva: no cambia
    drift = c.sync(13_000, mono=12.0)              # OBS dice 13 s cuando estimábamos 12 s
    assert drift == pytest.approx(1000)
    assert c.position_at(13.0) == pytest.approx(14_000)


def test_clock_start_with_offset():
    c = RecordingClock()
    c.start(mono=50.0, pos_ms=3_600_000)           # nos conectamos con OBS ya grabando 1 h
    assert fmt_timestamp(c.position_at(60.0)) == "01:00:10"


def test_fmt_timestamp():
    assert fmt_timestamp(0) == "00:00:00"
    assert fmt_timestamp(45_17_000 // 1) == "01:15:17"
    assert fmt_timestamp(None) == "--:--:--"
    assert fmt_timestamp(1234, with_ms=True) == "00:00:01.234"


# ---- segmentador ---------------------------------------------------------------
def _run_segmenter(probs: list[float], cfg: SegmenterConfig | None = None, forced_at: dict | None = None):
    cfg = cfg or SegmenterConfig(pre_roll_ms=100, post_roll_ms=100, end_silence_ms=300, min_speech_ms=100)
    ends = []
    seg = UtteranceSegmenter(cfg, on_end=lambda u, r: ends.append((u, r)))
    chunk = np.ones(cfg.chunk, dtype=np.float32) * 0.1
    t = 0.0
    for i, p in enumerate(probs):
        if forced_at and i in forced_at:
            seg.set_forced(forced_at[i], t)
        seg.process(chunk, p, t)
        t += cfg.chunk / cfg.sample_rate
    return seg, ends


def test_segmenter_closes_on_silence_and_keeps_pause():
    # 20 bloques de voz, pausa corta de 5 bloques (160 ms < 300 ms), 20 más de voz, luego silencio largo
    probs = [0.9] * 20 + [0.0] * 5 + [0.9] * 20 + [0.0] * 15
    seg, ends = _run_segmenter(probs)
    assert len(ends) == 1
    utt, reason = ends[0]
    assert reason == "silence"
    assert utt.speech_ms > 1000


def test_segmenter_discards_too_short():
    probs = [0.9] * 2 + [0.0] * 15
    _, ends = _run_segmenter(probs)
    assert ends and ends[0][1] == "discard"


def test_segmenter_forced_capture():
    probs = [0.0] * 5 + [0.0] * 30 + [0.0] * 10
    # hotkey pulsado en el bloque 5, soltado en el 35; sin voz real → se descarta sin ruido
    _, ends = _run_segmenter(probs, forced_at={5: True, 35: False})
    assert ends and ends[0][1] == "discard"
    probs = [0.0] * 5 + [0.9] * 30 + [0.0] * 10
    _, ends = _run_segmenter(probs, forced_at={5: True, 35: False})
    assert ends and ends[0][1] == "forced" and ends[0][0].forced


# ---- relevancia y alucinaciones ---------------------------------------------------
def test_relevance_filter():
    assert is_relevant("eh... mm")[0] is False
    assert is_relevant("sí")[0] is False
    assert is_relevant("Este combate se siente demasiado lento")[0] is True
    assert is_relevant("ok", forced=True)[0] is True


def test_hallucination_filter():
    assert looks_like_hallucination("Subtítulos realizados por la comunidad de Amara.org")
    assert looks_like_hallucination("no no no no no no no")
    assert not looks_like_hallucination("Creo que el problema está en la distribución de los enemigos")


# ---- contexto ------------------------------------------------------------------------
def test_context_description_vs_opinion():
    ce = ContextEngine(known_games=["Resident Evil 4"])
    r = ce.classify("Estoy entrando a una zona bastante amplia, hay edificios destruidos a los lados y "
                    "prácticamente no hay enemigos.", game="Rise of Iron D-Day")
    assert "descripción" in r["functions"]
    assert "diseño de niveles" in r["topics"]
    assert r["scope"] == "current"
    r2 = ce.classify("Y creo que funciona bastante bien porque por primera vez siento que el juego está dejando "
                     "que el escenario respire.", game="Rise of Iron D-Day")
    assert set(r2["functions"]) & {"análisis", "opinión"}


def test_context_reference_and_general():
    ce = ContextEngine()
    r = ce.classify("Esto me recuerda muchísimo a Resident Evil 4.", game="Rise of Iron D-Day")
    assert r["refs"] and r["refs"][0]["name"].startswith("Resident Evil 4")
    assert "comparación" in r["functions"]
    r = ce.classify("Últimamente siento que muchos juegos están diseñando sus niveles de esta manera.", game="X")
    assert r["scope"] == "general"
    r = ce.classify("No sé por qué, pero esto me hace pensar en cuando jugaba este tipo de juegos de pequeño.", game="X")
    assert r["scope"] == "personal" and "pensamiento" in r["functions"]


def test_context_ambiguous():
    ce = ContextEngine()
    r = ce.classify("vale vamos", game="X")
    assert r["ambiguous"] is True


# ---- base de datos y exportación ------------------------------------------------------
def test_store_roundtrip(tmp_path):
    st = Store(tmp_path / "t.db")
    sid = st.create_session({"game": "Rise", "title": "Rise — hoy", "started_at": "2026-10-09T10:00:00", "mode": "obs"})
    nid = st.create_note({"session_id": sid, "position_ms": 754_000, "text_raw": "Este combate es lento",
                          "functions": ["opinión"], "topics": ["combate"], "scope": "current", "confidence": 0.8})
    st.update_note(nid, marker="important", text="Este combate es demasiado lento")
    notes = st.list_notes(sid)
    assert len(notes) == 1 and notes[0]["marker"] == "important" and notes[0]["topics"] == ["combate"]
    st.add_correction(nid, "scope", "current", "other", "txt")
    assert len(st.list_corrections()) == 1
    stats = st.session_stats(sid)
    assert stats["notes"] == 1 and stats["markers"]["important"] == 1
    sess = st.get_session(sid)
    md = exporters.to_markdown(sess, notes)
    assert "[00:12:34]" in md and "⭐ Importante" in md
    csv_text = exporters.to_csv(sess, notes)
    assert "00:12:34" in csv_text
    srt = exporters.to_srt(sess, notes)
    assert "00:12:34,000 -->" in srt
    prompt = exporters.build_chatbot_prompt(sess, notes, "ludonarrativa")
    assert "disonancias" in prompt and "[00:12:34]" in prompt
    assert len(st.list_sessions()) == 1 and st.list_sessions()[0]["notes_count"] == 1
    st.delete_session(sid)
    assert st.list_sessions() == []
    st.close()


def test_no_autostart_and_no_obs_watcher():
    """El usuario abre Cronista a mano: no hay opciones de inicio automático ni vigilante de OBS."""
    import cronista.__main__ as entry
    from cronista.core.settings import GeneralSettings
    names = {f.name for f in __import__("dataclasses").fields(GeneralSettings)}
    assert "run_with_windows" not in names and "obs_watcher" not in names
    assert not (Path(entry.__file__).parent / "watcher.py").exists()
    with pytest.raises(SystemExit):
        entry.main(["--watch-obs"])           # el argumento ya no existe


def test_split_prompt():
    text = "\n".join(f"- línea {i} " + "x" * 50 for i in range(500))
    parts = exporters.split_prompt(text, max_chars=5000)
    assert len(parts) > 1 and all(len(p) <= 5200 for p in parts)
