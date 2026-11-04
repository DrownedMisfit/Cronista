"""Filtros puros del buscador (sin Qt ni modelo): opinión, ámbito, función y tema."""
from __future__ import annotations

from cronista.ui.search_dialog import _opinion_ok, _passes_filters


def test_filtro_de_opinion_critica_y_problemas():
    critica = {"functions": ["crítica"], "marker": ""}
    problema = {"functions": [], "marker": "problem"}
    me_gusto = {"functions": ["opinión"], "marker": "liked"}
    assert _opinion_ok(critica, "critical")
    assert _opinion_ok(problema, "critical")
    assert not _opinion_ok(me_gusto, "critical")


def test_filtro_de_opinion_me_gusto():
    assert _opinion_ok({"functions": [], "marker": "liked"}, "liked")
    assert not _opinion_ok({"functions": ["crítica"], "marker": ""}, "liked")


def test_sin_filtro_de_opinion_pasa_todo():
    assert _opinion_ok({"functions": [], "marker": ""}, "")


def test_filtros_combinados_ambito_funcion_tema():
    nota = {"scope": "current", "functions": ["crítica"], "topics": ["combate"], "marker": "problem"}
    assert _passes_filters(nota, "current", "crítica", "combate", "critical")
    assert not _passes_filters(nota, "other", "", "", "")
    assert not _passes_filters(nota, "", "descripción", "", "")
    assert not _passes_filters(nota, "", "", "historia", "")
    assert not _passes_filters(nota, "", "", "", "liked")
