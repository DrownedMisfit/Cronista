"""Pruebas de relaciones entre notas y personalización local (sin IA ni hardware)."""
from __future__ import annotations

import pytest

from cronista.core.settings import Settings
from cronista.learning.personalizer import Personalizer
from cronista.notes.relations import compute_relations, tokenize, topic_clusters
from cronista.notes.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def _note(nid, text, topics=(), refs=(), position_ms=None, subtopic=""):
    return {"id": nid, "text": text, "topics": list(topics), "functions": [], "scope": "current",
            "refs": list(refs), "position_ms": position_ms,
            "context": {"subtopic": subtopic} if subtopic else {}}


def test_tokenize_quita_tildes_y_stopwords():
    assert tokenize("La MÚSICA del Jefe final, ¡sí!") == ["musica", "jefe", "final"]
    assert "4" in tokenize("Resident Evil 4")


def test_notas_del_mismo_espacio_quedan_relacionadas():
    n1 = _note(1, "la distribución de los espacios del escenario", topics=["diseño de niveles"])
    n2 = _note(2, "los espacios del escenario tienen una distribución confusa", topics=["diseño de niveles"])
    n3 = _note(3, "la música del jefe final", topics=["música"])
    rels = compute_relations(n1, [n2, n3])
    ids = {oid for oid, _, _ in rels}
    assert 2 in ids
    assert 3 not in ids
    assert [kind for oid, kind, _ in rels if oid == 2] == ["same_topic"]


def test_referencia_compartida_es_same_reference():
    n1 = _note(1, "hoy vuelvo a este juego", refs=[{"name": "Resident Évil", "relation": "comparación"}])
    n4 = _note(4, "el combate del jefe", refs=[{"name": "resident  evil", "relation": "referencia"}])
    assert compute_relations(n1, [n4]) == [(4, "same_reference", 0.9)]


def test_topic_clusters_agrupa_cadena_de_notas():
    topics = {1: "diseño de niveles", 2: "diseño de niveles", 3: "diseño de niveles", 4: "diseño de niveles",
              5: "música"}
    notes = [_note(i, f"nota {i}", topics=[topics[i]], position_ms=i * 60_000) for i in range(1, 6)]
    relations = {1: [2], 2: [1, 3], 3: [2, 4], 4: [3], 5: []}
    clusters = topic_clusters(notes, relations)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["note_ids"] == [1, 2, 3, 4]
    assert c["count"] == 4
    assert c["topic"] == "diseño de niveles"
    assert c["first_ms"] == 60_000
    assert c["last_ms"] == 4 * 60_000


def test_examples_for_depende_del_ajuste(store):
    sid = store.create_session({"game": "Rise of Iron"})
    n1 = store.create_note({"session_id": sid, "text": "el escenario tiene demasiada niebla",
                            "scope": "current", "functions": ["crítica"], "topics": ["diseño de niveles"]})
    store.add_correction(n1, "scope", "", "current", "el escenario tiene demasiada niebla")

    s = Settings()
    s.learning.learn_corrections = False
    assert Personalizer(store, s).examples_for("niebla en el escenario") == []

    s.learning.learn_corrections = True
    p = Personalizer(store, s)
    similares = p.examples_for("niebla en el escenario")
    assert similares and similares[0]["text"] == "el escenario tiene demasiada niebla"
    assert set(similares[0]) == {"text", "scope", "functions", "topics"}

    # sin ejemplos parecidos devuelve los más recientes
    recientes = p.examples_for("zzz qqq")
    assert len(recientes) == 1


def test_style_vocabulary_encuentra_nombres_repetidos(store):
    sid = store.create_session({"game": "Resident Evil 4"})
    textos = [
        "Ayer estuve jugando a Resident Evil 4 con mi hermano.",
        "Lo que más me gusta de Resident Evil 4 es la atmósfera.",
        "Lucas Dravek es un protagonista muy carismático.",
        "Creo que Lucas Dravek tiene mucho carisma.",
    ]
    for t in textos:
        store.create_note({"session_id": sid, "text": t})

    s = Settings()
    s.learning.learn_style = True
    vocab = Personalizer(store, s).style_vocabulary()
    assert "Resident Evil 4" in vocab
    assert "Lucas Dravek" in vocab
    assert "Ayer" not in vocab and "Creo" not in vocab
    assert store.kv_get("learn.vocab") == vocab

    s.learning.learn_style = False
    assert Personalizer(store, s).style_vocabulary() == []


def test_known_games_y_remember_game(store):
    sid = store.create_session({"game": "Rise of Iron"})
    store.create_note({"session_id": sid, "text": "x"})
    s = Settings()
    s.learning.learn_style = True
    p = Personalizer(store, s)
    p.remember_game("Halo")
    p.remember_game("halo")
    assert p.known_games() == ["Rise of Iron", "Halo"]


def test_clear_borra_correcciones(store):
    sid = store.create_session({"game": ""})
    n = store.create_note({"session_id": sid, "text": "algo"})
    store.add_correction(n, "scope", "", "current", "algo")
    assert store.list_corrections()

    s = Settings()
    s.learning.learn_corrections = True
    Personalizer(store, s).clear()
    assert store.list_corrections() == []
