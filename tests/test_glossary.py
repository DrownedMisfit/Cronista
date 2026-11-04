"""Glosario bilingüe: títulos y términos en inglés dentro de comentarios en español."""
from __future__ import annotations

import numpy as np

from cronista.core.settings import Settings
from cronista.stt.glossary import GAMING_TERMS, Glossary, norm

GAME = "Rise of Iron: D-Day"


def g():
    return Glossary.from_lines(["Rise of Iron: D-Day = rise of iron di de, rise of airon di day", "Resident Evil 4",
                                "Lucas Dravek", "# comentario ignorado", ""], extra_terms=GAMING_TERMS)


def test_parse_lines_terms_and_aliases():
    gl = g()
    assert gl.terms[:3] == ["Rise of Iron: D-Day", "Resident Evil 4", "Lucas Dravek"]
    assert gl.aliases[norm("rise of iron di de")] == "Rise of Iron: D-Day"
    assert "gameplay" in gl.terms and not any(t.startswith("#") for t in gl.terms)


def test_fixes_misheard_titles_without_eating_neighbours():
    gl = g()
    cases = {
        "Estoy jugando Rise of Iron Di-Day y el combate es lento": "Estoy jugando Rise of Iron: D-Day y el combate es lento",
        "estoy jugando rise of iron di de y me gusta": "estoy jugando Rise of Iron: D-Day y me gusta",
        "Rise of Iron D Day es genial": "Rise of Iron: D-Day es genial",
        "me recuerda a Resident Ebil 4 con la cámara": "me recuerda a Resident Evil 4 con la cámara",
        "Lucas Drabek dice que no dejemos a nadie": "Lucas Dravek dice que no dejemos a nadie",
    }
    for said, expected in cases.items():
        assert gl.fix(said, game=GAME)[0] == expected, said


def test_leaves_correct_or_unrelated_text_alone():
    gl = g()
    for text in ["Esto me recuerda a Rise of Iron 2 la verdad", "Rise of Iron: D-Day está muy bien hecho",
                 "y de verdad que el boss es difícil", "el frame rate y el input lag",
                 "las cutscenes son largas", "el Boss final y el boss secreto", ""]:
        assert gl.fix(text, game=GAME)[0] == text, text


def test_prompt_is_short_without_example_sentence_or_hotwords():
    gl = Glossary.from_lines(Settings().stt.vocabulary, extra_terms=GAMING_TERMS)
    prompt, hot = gl.prompt(GAME)
    assert prompt.startswith(f"Notas sobre {GAME}.") and hot is None
    assert "Estoy jugando" not in prompt                         # una frase de ejemplo se copia a la transcripción
    assert len(prompt.split(",")) <= 25


def test_stt_engine_applies_glossary_to_final_text():
    from cronista.core import hardware
    from cronista.core.events import EventBus, Topics
    from cronista.stt.engine import STTEngine, _Task

    class FakeBackend:
        name = "fake"

        def transcribe(self, audio, **kw):
            self.kw = kw
            return "Estoy jugando Rise of Iron Di-Day y me encanta", {}

    bus = EventBus()
    eng = STTEngine(bus, Settings(), hardware.HardwareInfo(8, 16, 32, False))
    fake = FakeBackend()
    eng._ensure_main = lambda: fake
    eng.context_game = lambda: GAME
    got = []
    bus.subscribe(Topics.STT_FINAL, got.append)
    eng._handle(_Task(1, 1, "final", "u1", np.zeros(16000 * 2, np.float32), {"forced": False}))
    assert got[0].text == "Estoy jugando Rise of Iron: D-Day y me encanta"
    assert got[0].info["text_before_glossary"].endswith("Y-Day y me encanta")
    assert fake.kw["initial_prompt"].startswith(f"Notas sobre {GAME}.")
    eng.settings.stt.fix_terms = False
    eng._handle(_Task(1, 2, "final", "u2", np.zeros(16000 * 2, np.float32), {"forced": False}))
    assert got[1].text.endswith("Y-Day y me encanta")
