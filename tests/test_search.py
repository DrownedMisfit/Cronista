"""Pruebas de la búsqueda semántica con el modelo real (salta si no está en models/embed)."""
from __future__ import annotations

import time

import pytest

from cronista.notes.store import Store
from cronista.search.embedder import MODEL_NAME, SentenceEncoder, model_available
from cronista.search.engine import SearchEngine

GAME_A = "Juego Alfa"
GAME_B = "Juego Beta"

# (clave, juego, texto, tema, función)
NOTES = [
    ("combate_lento", GAME_A, "El combate es muy lento, cada golpe tarda una eternidad en conectar.", "combate", "critica"),
    ("cobertura", GAME_A, "La cobertura es pegajosa, a veces el personaje se queda pegado a la pared.", "combate", "critica"),
    ("musica_tensa", GAME_A, "La música de la fase del puerto es tensa y consigue meter presión.", "música", "elogio"),
    ("ia_tonta", GAME_A, "Los enemigos de IA son tontos, se quedan parados mientras les disparo.", "IA", "critica"),
    ("niveles_lineales", GAME_A, "El diseño de niveles es muy lineal, no hay ninguna decisión que tomar.", "niveles", "critica"),
    ("personajes_bien", GAME_A, "Los personajes secundarios son muy interesantes y tienen buenos diálogos.", "personajes", "elogio"),
    ("tiroteo_seco", GAME_B, "El tiroteo es seco y responde rápido, la sensación de disparo es genial.", "combate", "elogio"),
    ("mira_muro", GAME_B, "Me costó apuntar porque la mira se mueve sola cuando me cubro detrás de un muro.", "combate", "critica"),
    ("banda_sonora", GAME_B, "La banda sonora acompaña perfectamente la exploración, muy relajante.", "música", "elogio"),
    ("rivales_logica", GAME_B, "Los rivales se mueven con lógica, flanquean y te obligan a cambiar de sitio.", "IA", "elogio"),
    ("sin_carisma", GAME_B, "Los personajes no tienen carisma, nadie me importa.", "personajes", "critica"),
]


@pytest.fixture
def needs_model():
    if not model_available():
        pytest.skip("Modelo de embeddings no disponible en models/embed")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "search.db")
    sess_a = s.create_session({"game": GAME_A, "title": "Partida 1", "started_at": "2026-01-01T10:00:00"})
    sess_b = s.create_session({"game": GAME_B, "title": "Partida 2", "started_at": "2026-01-02T10:00:00"})
    s.ids = {}
    for i, (key, game, text, topic, func) in enumerate(NOTES):
        sid = sess_a if game == GAME_A else sess_b
        s.ids[key] = s.create_note({
            "session_id": sid, "position_ms": (i + 1) * 60_000, "text": text, "topics": [topic],
            "functions": [func], "scope": "juego actual" if game == GAME_A else "otro juego",
            "marker": "importante" if key in ("combate_lento", "mira_muro") else "",
        })
    yield s
    s.close()


def test_index_indexa_todo_y_la_segunda_vez_no_hace_nada(needs_model, store):
    engine = SearchEngine(store)
    t0 = time.perf_counter()
    first = engine.index()
    t_index = time.perf_counter() - t0
    assert first["total"] == len(NOTES)
    assert first["indexed"] == len(NOTES)
    assert len(store.embeddings(MODEL_NAME)) == len(NOTES)

    second = engine.index()
    assert second["indexed"] == 0
    assert second["total"] == len(NOTES)
    print(f"\n[tiempo] indexar {len(NOTES)} notas (incluye carga del modelo): {t_index:.2f} s")


def test_cambiar_el_texto_reindexa_solo_esa_nota(needs_model, store):
    engine = SearchEngine(store)
    engine.index()
    nid = store.ids["niveles_lineales"]
    store.update_note(nid, text="Los niveles tienen rutas alternativas y secretos por descubrir.")
    result = engine.index()
    assert result["indexed"] == 1
    assert engine.index()["indexed"] == 0


def test_busqueda_semantica_sin_palabras_exactas(needs_model, store):
    engine = SearchEngine(store)
    engine.index()  # libera el codificador al terminar
    query = "el sistema de disparos se siente torpe"
    t0 = time.perf_counter()
    results = engine.search(query)  # primera búsqueda: incluye carga del modelo
    t_cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    engine.search(query)
    t_warm = time.perf_counter() - t0
    print(f"\n[tiempo] busqueda con carga del modelo: {t_cold * 1000:.0f} ms; busqueda con modelo cargado: {t_warm * 1000:.0f} ms")
    for r in results[:6]:
        print(f"[ranking] {r['score']:.3f} {r['match']:<10} {r['text'][:70]}")
    assert results, "la búsqueda debe devolver resultados"
    combat_ids = {store.ids[k] for k in ("combate_lento", "cobertura", "tiroteo_seco", "mira_muro")}
    assert results[0]["id"] in combat_ids
    assert all(r["score"] >= 0.2 for r in results)
    assert results[0]["match"] in ("semántica", "ambas", "palabras")


def test_filtro_por_juego(needs_model, store):
    engine = SearchEngine(store)
    engine.index()
    results = engine.search("personajes interesantes", game=GAME_A)
    assert results
    assert all(r["game"] == GAME_A for r in results)


def test_filtro_por_tema_y_marcador(needs_model, store):
    engine = SearchEngine(store)
    engine.index()
    by_topic = engine.search("enemigos", topic="IA")
    assert by_topic and all("IA" in r["topics"] for r in by_topic)
    by_marker = engine.search("combate", marker="importante")
    assert by_marker and all(r["marker"] == "importante" for r in by_marker)


def test_consulta_vacia_con_filtro_no_puntua(store):
    # No necesita el modelo: solo listado filtrado.
    engine = SearchEngine(store)
    rows = engine.search("", game=GAME_B, limit=100)
    assert len(rows) == 5
    assert all(r["game"] == GAME_B for r in rows)
    assert all("score" not in r for r in rows)
    assert [r["position_ms"] for r in rows] == sorted(r["position_ms"] for r in rows)
    topic_rows = engine.search("   ", topic="combate", limit=100)
    assert len(topic_rows) == 4 and all("combate" in r["topics"] for r in topic_rows)


def test_compare_games_devuelve_resultados_de_ambos_juegos(needs_model, store):
    engine = SearchEngine(store)
    engine.index()
    out = engine.compare_games("la inteligencia artificial de los enemigos", [GAME_A, GAME_B], per_game=3)
    assert set(out) == {GAME_A, GAME_B}
    assert out[GAME_A] and out[GAME_B]
    assert all(r["game"] == GAME_A for r in out[GAME_A])
    assert all(r["game"] == GAME_B for r in out[GAME_B])
    assert all(len(v) <= 3 for v in out.values())


def test_similar_notes_excluye_la_propia_y_filtra_juego(needs_model, store):
    engine = SearchEngine(store)
    engine.index()
    nid = store.ids["ia_tonta"]
    rows = engine.similar_notes(nid, limit=8)
    assert rows
    assert all(r["id"] != nid for r in rows)
    other = engine.similar_notes(nid, limit=8, other_games_only=True)
    assert other and all(r["game"] != GAME_A for r in other)
    assert engine.similar_notes(999_999) == []


def test_encoder_produce_vectores_normalizados(needs_model):
    enc = SentenceEncoder()
    vecs = enc.encode(["Prueba de codificación", "", "otra frase"], batch=2)
    assert vecs.shape == (3, 384)
    assert vecs.dtype.name == "float32"
    norms = (vecs ** 2).sum(axis=1) ** 0.5
    assert all(abs(n - 1.0) < 1e-3 for n in norms)
    enc.unload()
