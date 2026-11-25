"""Toma mala, registro de la sesión y posibles errores de lectura (sin micrófono ni ventanas)."""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cronista.core.settings import Settings  # noqa: E402
from cronista.teleprompter import ensayo  # noqa: E402
from cronista.teleprompter.smart import ScriptFollower  # noqa: E402
from cronista.teleprompter.window import TeleprompterWindow  # noqa: E402

GUION = ("Hola a todos y bienvenidos a este análisis tan esperado. "
         "El combate se siente demasiado lento sobre todo al principio del juego. "
         "Las cinemáticas están muy bien trabajadas pero cansan bastante.")


@pytest.fixture(autouse=True)
def _aislado(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from cronista.teleprompter import gramatica
    monkeypatch.setattr(gramatica, "enabled", False)


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


# ---- frases y comparación ---------------------------------------------------------------
def test_frases_del_guion():
    texto = "Primera frase. ¿Segunda?\nTercera sin punto\n\nCuarta… y sigue. "
    frases = [texto[a:b] for a, b in ensayo.sentence_spans(texto)]
    assert frases == ["Primera frase.", "¿Segunda?", "Tercera sin punto", "Cuarta…", "y sigue."]
    spans = ensayo.sentence_spans(texto)
    assert ensayo.sentence_at(spans, texto.index("Segunda")) == 1
    assert ensayo.sentence_spans("   ") == []


def test_una_palabra_de_menos_no_se_marca_dos_seguidas_si():
    frase = "El combate se siente demasiado lento sobre todo al principio del juego."
    assert ensayo.check_sentence(frase, "el combate se siente lento sobre todo al principio del juego".split()) == []
    assert ensayo.check_sentence(frase, "el combate se siente sobre todo al principio del juego".split()) == \
        ["demasiado", "lento"]


def test_muletillas_y_nombres_no_dan_falsas_alarmas():
    assert ensayo.check_sentence("Yo jugué Rise of Iron con Lucas y Ana ayer", "yo jugué con y ayer".split()) == []
    frase = "Las cinemáticas están muy bien trabajadas"
    assert ensayo.check_sentence(frase, "eh las cinemáticas este están muy bien trabajadas".split()) == []


def test_sensibilidad():
    frase = "El combate se siente demasiado lento sobre todo al principio del juego."
    oido = "el combate se siente sobre todo al principio del juego".split()          # faltan dos seguidas
    assert ensayo.check_sentence(frase, oido, "baja") == []
    assert ensayo.check_sentence(frase, oido, "alta") == ["demasiado", "lento"]
    suelta = "el combate se siente demasiado sobre todo al principio del juego".split()  # una sola: nunca molesta
    assert ensayo.check_sentence(frase, suelta, "alta") == []


def test_registro_de_sesion(tmp_path):
    s = ensayo.SessionLog(tmp_path / "sesiones", GUION)
    s.event("toma_mala", frase=1)
    s.event("posible_error", frase=0, faltan=["esperado"])
    s.event("posible_error", frase=2, faltan=["cansan"])
    s.event("frase_bien", frase=0)                     # se volvió a leer bien
    datos = json.loads(s.path.read_text(encoding="utf-8"))
    assert len(datos["frases"]) == 3 and [e["tipo"] for e in datos["eventos"]][0] == "toma_mala"
    assert s.to_review() == [2]


# ---- en la ventana ------------------------------------------------------------------------
def _ventana(tmp_path, nivel="discreto"):
    ajustes = Settings()
    ajustes.teleprompter.review_level = nivel
    ajustes.teleprompter.countdown_s = 0
    ajustes.teleprompter.retake_countdown_s = 0
    ruta = tmp_path / "t.txt"
    ruta.write_text(GUION, encoding="utf-8")
    return TeleprompterWindow(ajustes, text_path=ruta)


def test_toma_mala_vuelve_al_principio_de_la_frase_y_sigue(qapp, tmp_path):
    w = _ventana(tmp_path)
    v = w._view
    v.jump_to_mark(GUION.index("demasiado"))           # a media segunda frase
    w._toma_mala()
    inicio = GUION.index("El combate")
    assert v._line_top(v.reading_pos()) == pytest.approx(v._line_top(inicio))
    assert v.is_playing()                              # (cuenta atrás 0: sigue enseguida)
    eventos = w._sesion.data["eventos"]
    assert {"tipo": "toma_mala", "frase": 1}.items() <= next(e for e in eventos if e["tipo"] == "toma_mala").items()
    v.pause()
    w.close()


def test_errores_se_apuntan_en_silencio_y_salen_en_revisar(qapp, tmp_path):
    w = _ventana(tmp_path)
    w._follower = ScriptFollower(GUION)
    w._view.play()
    for trozo in ("hola a todos y bienvenidos a este análisis tan esperado",
                  "el combate se siente sobre todo al principio del juego",   # faltan «demasiado lento»
                  "las cinemáticas están muy bien trabajadas pero cansan bastante"):
        w._smart_oido_final(trozo.split())
    w._view.pause()                                    # al pausar se juzga también la última frase pendiente
    assert w._sesion.to_review() == [1]
    assert w._view.review_marks() == [GUION.index("El combate")]
    assert w._btn_revisar.isVisibleTo(w) and "1 para revisar" in w._btn_revisar.text()
    w._ir_a_revisar()
    assert w._view._line_top(w._view.reading_pos()) == pytest.approx(w._view._line_top(GUION.index("El combate")))
    w.close()


def test_silencioso_no_muestra_nada_mientras_se_lee(qapp, tmp_path):
    w = _ventana(tmp_path, "silencioso")
    w._follower = ScriptFollower(GUION)
    w._view.play()
    for trozo in ("hola a todos y bienvenidos a este análisis tan esperado",
                  "el combate se siente sobre todo al principio del juego",
                  "las cinemáticas están muy bien trabajadas pero cansan bastante"):
        w._smart_oido_final(trozo.split())
    assert not w._btn_revisar.isVisibleTo(w)           # mientras lee, nada a la vista
    w._view.pause()
    assert w._sesion.to_review() == [1] and w._btn_revisar.isVisibleTo(w)   # al pausar, sí               # al pausar, sí
    w.close()


def test_apagado_no_compara(qapp, tmp_path):
    w = _ventana(tmp_path, "apagado")
    w._follower = ScriptFollower(GUION)
    w._view.play()
    for trozo in ("hola a todos y bienvenidos a este análisis tan esperado",
                  "el combate se siente sobre todo al principio del juego",
                  "las cinemáticas están muy bien trabajadas pero cansan bastante"):
        w._smart_oido_final(trozo.split())
    assert w._sesion.to_review() == [] and not w._btn_revisar.isVisibleTo(w)
    w._view.pause()
    w.close()


def test_palabras_que_smart_no_oye_se_subrayan_y_las_piezas_cuentan(qapp, tmp_path):
    w = _ventana(tmp_path)
    w._btn_smart.blockSignals(True)                    # (sin modelo de voz: no se pregunta si bajarlo)
    w._btn_smart.setChecked(True)
    w._btn_smart.blockSignals(False)
    w._follower = ScriptFollower(GUION)

    class _Oyente:
        unknown_words = {"cinemáticas", "análisis"}
        spoken_forms = {"cinemáticas": [["cine", "máticas"]]}

        def is_running(self):
            return False

    w._oyente = _Oyente()
    w._smart_analizado()
    inicio = GUION.index("análisis")
    assert w._view.hard_words() == [(inicio, inicio + len("análisis"))]   # «cinemáticas» sí se puede oír
    assert "análisis" in w._btn_smart.toolTip()
    w._view.play()
    for trozo in ("hola a todos y bienvenidos a este análisis tan esperado",
                  "el combate se siente demasiado lento sobre todo al principio del juego",
                  "las cine máticas están muy bien trabajadas pero cansan bastante"):
        w._smart_oido_final(trozo.split())
    w._view.pause()
    assert w._sesion.to_review() == []
    w._on_smart(False)
    assert w._view.hard_words() == []
    w.close()
