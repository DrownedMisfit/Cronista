"""Pruebas de las tareas de lenguaje (fase 2) con un cliente LLM falso; sin red ni modelos."""
from __future__ import annotations

import json

import pytest

from cronista.ai import tasks
from cronista.ai.tasks import (
    CLASSIFY_SCHEMA,
    FUNCTION_DEFS,
    SUMMARY_SCHEMA,
    TRANSFORM_MODES,
    chunk_lines,
    classify_note,
    format_notes_for_llm,
    parse_json,
    summarize_session,
    summary_to_markdown,
    transform_note,
)
from cronista.context.engine import FUNCTIONS, TOPICS


class FakeLLM:
    """Guarda cada llamada y devuelve respuestas en cola (o las calcula con una función)."""

    def __init__(self, responses=None):
        self.responses = responses if callable(responses) else list(responses or [])
        self.calls: list[dict] = []

    def chat(self, messages, *, json_schema=None, max_tokens=512, temperature=0.2, timeout=180.0):
        self.calls.append({"messages": messages, "json_schema": json_schema, "max_tokens": max_tokens,
                           "temperature": temperature, "timeout": timeout})
        if callable(self.responses):
            return self.responses(messages)
        if not self.responses:
            raise AssertionError("FakeLLM sin respuestas")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def user_text(self, i: int = -1) -> str:
        return "\n".join(m["content"] for m in self.calls[i]["messages"] if m["role"] == "user")


def _note(text, pos=0, **kw):
    return {"text": text, "position_ms": pos, **kw}


# ---- parse_json -----------------------------------------------------------------
def test_parse_json_strips_think_and_fences():
    raw = '<think>razono {no} es json</think>\n```json\n{"a": 1, "b": "}{"}\n```'
    assert parse_json(raw) == {"a": 1, "b": "}{"}


def test_parse_json_finds_object_in_prose_and_nested():
    assert parse_json('Aquí tienes: {"x": {"y": 2}} gracias') == {"x": {"y": 2}}
    assert parse_json('{"t": "llave } suelta \\" y { más"}') == {"t": 'llave } suelta " y { más'}


@pytest.mark.parametrize("raw", ["no hay nada", "{rota", "{\"a\": }", ""])
def test_parse_json_invalid_raises(raw):
    with pytest.raises(ValueError):
        parse_json(raw)


# ---- classify_note --------------------------------------------------------------
def test_classify_schema_and_definitions_match_engine():
    assert set(FUNCTION_DEFS) == set(FUNCTIONS)
    assert set(CLASSIFY_SCHEMA["required"]) == set(CLASSIFY_SCHEMA["properties"])
    assert CLASSIFY_SCHEMA["properties"]["topics"]["items"]["enum"] == list(TOPICS)


def test_classify_normalizes_enums_and_clamps():
    resp = json.dumps({
        "scope": "current",
        "functions": ["Crítica", "inventada", "crítica", "opinión", "análisis"],
        "topics": ["combate", "xyz", "COMBATE", "mecánicas", "gráficos", "sonido"],
        "subtopic": "  ritmo  ",
        "refs": [{"name": "Dark Souls", "relation": "comparación"},
                 {"name": "Elden Ring", "relation": "raro"},
                 {"name": "   ", "relation": "referencia"},
                 {"name": "juego actual", "relation": "referencia"},
                 {"name": "dark souls", "relation": "referencia"}],
        "chapter": "", "section": "",
        "confidence": 1.7, "ambiguous": False, "rationale": "ok",
    })
    llm = FakeLLM([resp])
    out = classify_note(llm, "esto me recuerda a Dark Souls", game="Juego Actual")
    assert out["functions"] == ["crítica", "opinión"]
    assert out["topics"] == ["combate", "mecánicas", "gráficos"]
    assert out["subtopic"] == "ritmo"
    assert out["refs"] == [{"name": "Dark Souls", "relation": "comparación"},
                           {"name": "Elden Ring", "relation": "mención"}]
    assert out["confidence"] == 1.0
    assert out["ambiguous"] is False
    assert out["scope"] == "current"
    assert out["source"] == "llm"
    call = llm.calls[0]
    assert call["json_schema"] is CLASSIFY_SCHEMA
    assert call["temperature"] == 0.1
    assert call["max_tokens"] == 350


def test_classify_invalid_scope_is_ambiguous_current():
    llm = FakeLLM([json.dumps({"scope": "raro", "functions": ["opinión"], "topics": ["música"],
                               "confidence": 0.9, "ambiguous": False})])
    out = classify_note(llm, "me gusta la música")
    assert out["scope"] == "current"
    assert out["ambiguous"] is True


def test_classify_low_or_bad_confidence():
    llm = FakeLLM([
        json.dumps({"scope": "current", "functions": ["opinión"], "topics": [], "confidence": 0.42,
                    "ambiguous": False}),
        json.dumps({"scope": "current", "functions": ["opinión"], "topics": [], "confidence": 0.5,
                    "ambiguous": False}),
        json.dumps({"scope": "current", "functions": ["opinión"], "topics": [], "confidence": "alta",
                    "ambiguous": False}),
        json.dumps({"scope": "current", "functions": ["opinión"], "topics": [], "confidence": 0.8,
                    "ambiguous": "true"}),
    ])
    low = classify_note(llm, "a")
    assert low["confidence"] == 0.42 and low["ambiguous"] is True
    mid = classify_note(llm, "b")
    assert mid["confidence"] == 0.5 and mid["ambiguous"] is False
    bad = classify_note(llm, "c")
    assert bad["confidence"] == 0.0 and bad["ambiguous"] is True
    flagged = classify_note(llm, "d")
    assert flagged["ambiguous"] is True


def test_classify_prompt_includes_context_and_examples():
    llm = FakeLLM([json.dumps({"scope": "current", "functions": ["crítica"], "topics": ["combate"],
                               "confidence": 0.9, "ambiguous": False})])
    classify_note(
        llm, "la pelea final es floja",
        game="Dead Island",
        recent=[{"text": "nota reciente uno"}, {"text": "nota reciente dos"},
                {"text": "nota reciente tres"}, {"text": "nota reciente cuatro"}],
        examples=[{"text": "el combate del boss es lento", "scope": "current",
                   "functions": ["crítica"], "topics": ["combate"]}],
        prev_state={"topic": "combate", "subtopic": "", "chapter": "Acto 2"},
    )
    system = llm.calls[0]["messages"][0]["content"]
    user = llm.user_text()
    assert "Así clasificó el usuario notas parecidas" in user
    assert "el combate del boss es lento" in user
    assert "nota reciente cuatro" in user and "nota reciente dos" in user
    assert "nota reciente uno" not in user  # solo las 3 últimas
    assert "tema: combate" in user and "capítulo: Acto 2" in user
    assert "Dead Island" in user and "la pelea final es floja" in user
    assert "no inventes" in system.lower()
    assert "RE4" in system


def test_classify_propagates_client_errors():
    llm = FakeLLM([RuntimeError("servidor caído")])
    with pytest.raises(RuntimeError):
        classify_note(llm, "hola")


def test_classify_invalid_json_raises():
    llm = FakeLLM(["no es json"])
    with pytest.raises(ValueError):
        classify_note(llm, "hola")


# ---- transform_note -------------------------------------------------------------
def test_transform_rejects_unknown_mode():
    with pytest.raises(ValueError):
        transform_note(FakeLLM(), "hola", "inventado")


def test_transform_rejects_too_long_and_empty():
    source = "no sé cómo explicarlo bien"
    too_long = json.dumps({"text": "x" * (int(1.6 * len(source)) + 200)})
    llm = FakeLLM([too_long])
    with pytest.raises(ValueError, match="respuesta no fiable"):
        transform_note(llm, source, "correct")
    llm = FakeLLM([json.dumps({"text": "   "})])
    with pytest.raises(ValueError, match="respuesta no fiable"):
        transform_note(llm, source, "improve")


def test_transform_accepts_good_response_and_uses_modes():
    llm = FakeLLM([json.dumps({"text": "Hola, ¿qué tal?"}), json.dumps({"text": "Una idea clara."})])
    assert transform_note(llm, "hola que tal", "correct", game="RE4") == "Hola, ¿qué tal?"
    assert llm.calls[0]["temperature"] == 0.1
    assert llm.calls[0]["max_tokens"] == 400
    assert llm.calls[0]["json_schema"]["required"] == ["text"]
    assert "RE4" in llm.user_text()
    assert transform_note(llm, "idea sobre el guion", "script") == "Una idea clara."
    assert llm.calls[1]["temperature"] == 0.3
    assert TRANSFORM_MODES["correct"]["title"] == "Corregir"
    assert TRANSFORM_MODES["improve"]["title"] == "Mejorar expresión"
    assert TRANSFORM_MODES["script"]["title"] == "Idea para guion"


# ---- formato y troceado ---------------------------------------------------------
def test_format_notes_for_llm():
    notes = [
        _note("  hay un\n puente  ", 65_000, functions=["descripción"], topics=["mecánicas"], scope="current"),
        _note("me recuerda a otro", None, functions=["comparación"], scope="other", marker="important"),
        _note("nada", 0),
    ]
    lines = format_notes_for_llm(notes)
    assert lines[0] == "[00:01:05] hay un puente (función: descripción; tema: mecánicas)"
    assert lines[1].startswith("[--:--:--] me recuerda a otro (")
    assert "ámbito: Otro videojuego" in lines[1]
    assert "marcador: ⭐ Importante" in lines[1]
    assert lines[2] == "[00:00:00] nada"


def test_chunk_lines_never_empty_and_long_line_alone():
    lines = ["a" * 10, "b" * 10, "c" * 500, "d" * 10]
    chunks = chunk_lines(lines, max_chars=50)
    assert all(chunks)
    assert [c for ch in chunks for c in ch] == lines
    assert ["c" * 500] in chunks
    assert chunk_lines([], 50) == []


# ---- resumen de sesión ----------------------------------------------------------
def test_summarize_without_notes_does_not_call_llm():
    llm = FakeLLM([])
    out = summarize_session(llm, {"game": "X"}, [])
    assert llm.calls == []
    assert out == {"criticas": [], "positivos": [], "temas_recurrentes": [], "ideas_video": [],
                   "desarrollar": [], "generated_by": "llm", "notes_count": 0}


def test_summarize_filters_invented_timestamps():
    notes = [_note("el mapa es confuso", 65_000), _note("la banda sonora es preciosa", 600_000)]
    resp = json.dumps({
        "criticas": [{"texto": "mapa confuso", "timestamps": ["[00:01:05]", "00:09:99"]},
                     {"texto": "inventada", "timestamps": ["00:30:00"]}],
        "positivos": [{"texto": "música", "timestamps": ["00:10:00", "00:10:00"]}],
        "temas_recurrentes": ["mapa", "mapa", ""],
        "ideas_video": [{"texto": "sin marca", "timestamps": []}],
        "desarrollar": [{"timestamp": "00:01:05", "texto": "explicar el mapa"},
                        {"timestamp": "00:50:00", "texto": "fantasma"}],
    })
    llm = FakeLLM([resp])
    out = summarize_session(llm, {"game": "Juego"}, notes)
    assert out["criticas"] == [{"texto": "mapa confuso", "timestamps": ["00:01:05"]}]
    assert out["positivos"] == [{"texto": "música", "timestamps": ["00:10:00"]}]
    assert out["ideas_video"] == []
    assert out["temas_recurrentes"] == ["mapa"]
    assert out["desarrollar"] == [{"timestamp": "00:01:05", "texto": "explicar el mapa"}]
    assert out["generated_by"] == "llm"
    assert out["notes_count"] == 2
    call = llm.calls[0]
    assert call["json_schema"] is SUMMARY_SCHEMA
    assert call["temperature"] == 0.2 and call["max_tokens"] == 900
    assert "Juego" in llm.user_text()
    assert "exclusivamente" in llm.calls[0]["messages"][0]["content"]


def test_summarize_many_notes_uses_map_reduce():
    notes = [_note("nota larga " + "x" * 400, i * 1000) for i in range(300)]
    expected_chunks = len(chunk_lines(format_notes_for_llm(notes), 6000))
    assert expected_chunks > 1

    def responder(messages):
        return json.dumps({"criticas": [{"texto": "crítica de prueba", "timestamps": ["00:00:00"]}],
                           "positivos": [], "temas_recurrentes": [], "ideas_video": [], "desarrollar": []})

    llm = FakeLLM(responder)
    out = summarize_session(llm, {"game": "Juego"}, notes, max_chars=6000)
    assert len(llm.calls) == expected_chunks + 1
    assert "Fusiónalos" in llm.user_text(-1)
    assert all(c["json_schema"] is SUMMARY_SCHEMA for c in llm.calls)
    assert out["criticas"] == [{"texto": "crítica de prueba", "timestamps": ["00:00:00"]}]
    assert out["notes_count"] == 300


# ---- markdown -------------------------------------------------------------------
def test_summary_to_markdown_sections_and_empty_placeholder():
    summary = {"criticas": [{"texto": "mapa confuso", "timestamps": ["00:01:05"]}], "positivos": [],
               "temas_recurrentes": ["mapa"], "ideas_video": [],
               "desarrollar": [{"timestamp": "00:01:05", "texto": "explicar"}]}
    md = summary_to_markdown(summary, {"game": "Juego"})
    assert md.startswith("# Juego — resumen de la sesión")
    for title in ("## Principales críticas", "## Aspectos positivos", "## Temas recurrentes",
                  "## Posibles ideas para video", "## Comentarios que debería desarrollar"):
        assert title in md
    assert "1. mapa confuso [00:01:05]" in md
    assert "1. [00:01:05] explicar" in md
    assert md.count("*Nada destacado.*") == 2
