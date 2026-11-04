"""Guiones: validación anti-invención, filtros y map-reduce, con un modelo falso."""
from __future__ import annotations

import json

import pytest

from cronista.ai import scripts


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, *, json_schema=None, max_tokens=512, temperature=0.2, timeout=180.0):
        self.calls.append({"messages": messages, "schema": json_schema})
        return self.responses.pop(0)


def _notes():
    base = [("Combate lento", "important", None), ("Cobertura pegajosa", "", None),
            ("Música genial", "liked", None), ("Otra persona hablando", "", {"voice": {"match": False}})]
    return [{"id": i + 1, "session_id": 1, "session_started": "2026-10-06T20:00:00", "position_ms": (i + 1) * 60000,
             "text": t, "marker": m, "functions": ["crítica"], "topics": ["combate"],
             "stt_info": v or {}} for i, (t, m, v) in enumerate(base)]


def test_prepare_filters_other_voice_and_marked():
    prepared = scripts.prepare_notes(_notes())
    assert [n["ref"] for n in prepared] == ["N1", "N2", "N3"]           # la otra voz queda fuera
    assert len(scripts.prepare_notes(_notes(), include_other_voice=True)) == 4
    assert [n["text"] for n in scripts.prepare_notes(_notes(), only_marked=True)] == ["Combate lento", "Música genial"]


def test_invented_refs_and_unsupported_sections_are_dropped():
    prepared = scripts.prepare_notes(_notes())
    resp = {"titulos": ["T1", "T2", "T3", "T4"],
            "gancho": {"texto": "Gancho", "refs": ["N99"]},
            "tesis": {"texto": "Tesis", "refs": ["N1", "n2"]},
            "secciones": [
                {"titulo": "1. Combate", "objetivo": "o", "puntos": [{"texto": "lento", "refs": ["N1", "N42"]}],
                 "clips": ["N1", "N77"]},
                {"titulo": "Inventada", "objetivo": "o", "puntos": [{"texto": "nada", "refs": ["N50"]}], "clips": []}],
            "conclusion": {"texto": "Fin", "refs": []}, "preguntas": ["¿Qué misión era?"]}
    llm = FakeLLM([json.dumps(resp)])
    sc = scripts.generate_script(llm, "Rise", prepared)
    assert len(sc["titulos"]) == 3
    assert sc["gancho"] == {"texto": "Gancho", "refs": []}               # ref inventada fuera
    assert sc["tesis"]["refs"] == ["N1", "N2"]
    assert [s["titulo"] for s in sc["secciones"]] == ["Combate"]         # numeración quitada; sección sin respaldo fuera
    assert sc["secciones"][0]["puntos"][0]["refs"] == ["N1"] and sc["secciones"][0]["clips"] == ["N1"]
    md = scripts.script_to_markdown(sc, prepared)
    assert "## 1. Combate" in md and "[06/10 · 00:01:00]" in md and "Preguntas abiertas" in md


def test_map_reduce_with_many_notes():
    notes = [{"id": i, "session_id": 1, "session_started": "2026-10-06", "position_ms": i * 1000,
              "text": "una nota bastante larga sobre el diseño de niveles " * 3, "marker": ""} for i in range(1, 60)]
    prepared = scripts.prepare_notes(notes)
    n_chunks = len(scripts._chunks([scripts._line(n) for n in prepared], 1500))
    point = json.dumps({"puntos": [{"tema": "niveles", "texto": "p", "refs": ["N1"]}]})
    final = json.dumps({"titulos": ["T"], "gancho": {"texto": "g", "refs": []}, "tesis": {"texto": "t", "refs": []},
                        "secciones": [{"titulo": "S", "objetivo": "", "puntos": [{"texto": "x", "refs": ["N1"]}],
                                       "clips": []}], "conclusion": {"texto": "c", "refs": []}, "preguntas": []})
    llm = FakeLLM([point] * n_chunks + [final])
    sc = scripts.generate_script(llm, "G", prepared, max_chars=1500)
    assert n_chunks > 1 and len(llm.calls) == n_chunks + 1
    assert "Puntos clave" in llm.calls[-1]["messages"][-1]["content"]
    assert sc["secciones"][0]["puntos"][0]["refs"] == ["N1"]


def test_no_notes_raises_and_record_maps_refs():
    with pytest.raises(ValueError):
        scripts.generate_script(FakeLLM([]), "G", [])
    prepared = scripts.prepare_notes(_notes())
    sc = {"titulos": ["Mi guion"], "secciones": [], "preguntas": [], "meta": {"game": "G"}}
    rec = scripts.script_record(sc, prepared, [1], "local", {"length": "medio"})
    assert rec["title"] == "Mi guion" and rec["content"]["refs"]["N1"]["note_id"] == 1
