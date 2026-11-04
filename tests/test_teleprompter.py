"""Pruebas del teleprompter sin hardware: texto, vista, ventana y ajustes. Todo va a carpetas temporales."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import zipfile  # noqa: E402
import zlib  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QImage, QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from cronista.core.paths import settings_path  # noqa: E402
from cronista.core.settings import Settings  # noqa: E402
from cronista.teleprompter import fonts  # noqa: E402
from cronista.teleprompter import text as tt  # noqa: E402
from cronista.teleprompter.view import PrompterView  # noqa: E402
from cronista.teleprompter.window import TeleprompterWindow  # noqa: E402

TEXTO_LARGO = "\n".join(f"Línea {i}: texto de prueba para el teleprompter." for i in range(200))
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


@pytest.fixture(autouse=True)
def _appdata_temporal(tmp_path, monkeypatch):
    """Nunca escribir en el %APPDATA% real: los ajustes y el texto van a tmp_path."""
    monkeypatch.setenv("APPDATA", str(tmp_path))


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def _crear_docx(ruta: Path, cuerpo: str) -> None:
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document xmlns:w="{W_NS}"><w:body>{cuerpo}</w:body></w:document>')
    with zipfile.ZipFile(ruta, "w") as zf:
        zf.writestr("word/document.xml", xml)


# ---- text.py -------------------------------------------------------------------
def test_txt_utf8_con_bom(tmp_path):
    ruta = tmp_path / "a.txt"
    ruta.write_bytes("\ufeffHola\nmundo\r\n".encode("utf-8"))
    assert tt.load_text(ruta) == "Hola\nmundo"


def test_txt_cp1252_y_extension_en_mayusculas(tmp_path):
    ruta = tmp_path / "b.TXT"
    ruta.write_bytes("canción".encode("cp1252"))
    assert tt.load_text(ruta) == "canción"


def test_md_quita_formato_sin_tocar_guiones_bajos(tmp_path):
    ruta = tmp_path / "c.md"
    ruta.write_text("# Título\n\nEsto es **negrita** y un [enlace](http://x.org).\n- viñeta\n"
                    "El archivo nombre_archivo queda.\n---\n", encoding="utf-8")
    assert tt.load_text(ruta).splitlines() == [
        "Título", "", "Esto es negrita y un enlace.", "• viñeta", "El archivo nombre_archivo queda."]


@pytest.mark.parametrize("entrada, esperado", [
    ("*cursiva* y _otra_", "cursiva y otra"),
    ("![foto](a.png)", ""),
    ("> cita", "cita"),
    ("+ punto", "• punto"),
    ("`codigo`", "codigo"),
    ("***", ""),
    ("2*3 y C# y mi_var", "2*3 y C# y mi_var"),
])
def test_strip_markdown(entrada, esperado):
    assert tt.strip_markdown(entrada) == esperado


def test_docx_parrafos_pestanas_y_pestanas_de_definicion(tmp_path):
    cuerpo = (
        '<w:p><w:pPr><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs></w:pPr>'
        '<w:r><w:t>Primera</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Antes</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t>después</w:t></w:r></w:p>'
    )
    ruta = tmp_path / "d.docx"
    _crear_docx(ruta, cuerpo)
    # La pestaña de definición (w:pPr/w:tabs) no es un tabulador escrito: no debe aparecer
    assert tt.load_text(ruta) == "Primera\nAntes\tdespués"


def test_docx_corrupto_da_valueerror(tmp_path):
    ruta = tmp_path / "roto.docx"
    ruta.write_bytes(b"esto no es un zip")
    with pytest.raises(ValueError, match="dañado"):
        tt.load_text(ruta)


def test_docx_sin_document_xml_da_valueerror(tmp_path):
    ruta = tmp_path / "vacio.docx"
    with zipfile.ZipFile(ruta, "w") as zf:
        zf.writestr("otra/cosa.txt", "x")
    with pytest.raises(ValueError):
        tt.load_text(ruta)


def test_docx_xml_malformado_da_valueerror(tmp_path):
    ruta = tmp_path / "xml.docx"
    with zipfile.ZipFile(ruta, "w") as zf:
        zf.writestr("word/document.xml", "<w:document><w:body>")
    with pytest.raises(ValueError):
        tt.load_text(ruta)


def test_extension_no_admitida(tmp_path):
    ruta = tmp_path / "x.pdf"
    ruta.write_bytes(b"%PDF-1.4")
    with pytest.raises(ValueError, match="Formato no admitido"):
        tt.load_text(ruta)


def test_velocidad_en_pixeles_por_segundo():
    valores = [tt.speed_to_px_per_s(n, 90) for n in range(tt.SPEED_MIN, tt.SPEED_MAX + 1)]
    assert valores == sorted(valores)
    assert len(set(valores)) == len(valores)
    assert tt.speed_to_px_per_s(10, 90) == pytest.approx(30.0)
    assert tt.speed_to_px_per_s(30, 90) == pytest.approx(90.0)
    assert tt.speed_to_px_per_s(0, 90) == tt.speed_to_px_per_s(tt.SPEED_MIN, 90)
    assert tt.speed_to_px_per_s(500, 90) == tt.speed_to_px_per_s(tt.SPEED_MAX, 90)


# ---- ajustes -------------------------------------------------------------------
def test_ajustes_del_teleprompter_se_guardan_y_cargan(tmp_path):
    ruta = tmp_path / "settings.json"
    s = Settings()
    tp = s.teleprompter
    tp.font_size, tp.speed, tp.mirror, tp.margin_pct = 72, 17, True, 12
    tp.last_file = "C:/textos/guion.md"
    s.save(ruta)
    assert Settings.load(ruta).teleprompter == tp


# ---- PrompterView --------------------------------------------------------------
def _vista(texto: str = TEXTO_LARGO) -> PrompterView:
    v = PrompterView()
    v.resize(800, 600)
    v.set_text(texto)
    return v


def test_avance_respeta_la_velocidad(qapp):
    v = _vista()
    v.set_speed(10)
    v.play()
    v.advance(1.0)
    assert v.offset() == pytest.approx(tt.speed_to_px_per_s(10, v.line_height()))


def test_pausa_no_mueve_el_texto(qapp):
    v = _vista()
    v.play()
    v.pause()
    v.advance(1.0)
    assert v.offset() == 0.0
    assert not v.is_playing()


def test_rebobinar_vuelve_al_principio_y_pausa(qapp):
    v = _vista()
    v.play()
    v.advance(0.5)
    v.rewind()
    assert v.offset() == 0.0
    assert not v.is_playing()


def test_al_llegar_al_final_se_para_y_emite_finished(qapp):
    v = _vista()
    fines: list[str] = []
    estados: list[bool] = []
    v.finished.connect(lambda: fines.append("fin"))
    v.playingChanged.connect(estados.append)
    v.play()
    v.advance(100_000.0)
    assert v.offset() == pytest.approx(v.max_offset())
    assert not v.is_playing()
    assert fines == ["fin"]
    assert estados == [True, False]
    v.advance(1.0)
    assert fines == ["fin"]


def test_iniciar_al_final_vuelve_a_empezar(qapp):
    v = _vista()
    v.play()
    v.advance(100_000.0)
    assert not v.is_playing()
    v.play()
    assert v.is_playing()
    assert v.offset() == 0.0


def test_espejo_no_cambia_la_posicion(qapp):
    v = _vista()
    v.play()
    v.advance(1.0)
    antes = v.offset()
    v.set_mirror(True)
    assert v.offset() == antes
    assert v.is_mirrored()


def _sigue_en_la_misma_frase(v: PrompterView, ancla: int) -> bool:
    """La línea que está en la línea de lectura es la que contiene la posición ancla del texto."""
    en_guia = v._pos_at_y(v.offset() + v.line_height() / 2)
    return v._line_top(en_guia) == pytest.approx(v._line_top(ancla))


def test_cambiar_tamano_conserva_la_frase_que_se_lee(qapp):
    v = _vista()
    v.play()
    v.advance(30.0)
    ancla = v._pos_at_y(v.offset() + v.line_height() / 2)
    progreso = v.progress()
    v.set_font_size(80)
    assert v.font_size() == 80
    assert _sigue_en_la_misma_frase(v, ancla)
    assert v.progress() == pytest.approx(progreso, abs=0.05)


def test_desplazamiento_manual_limitado(qapp):
    v = _vista()
    v.scroll_by_lines(-5)
    assert v.offset() == 0.0
    v.scroll_by_lines(1e9)
    assert v.offset() == pytest.approx(v.max_offset())
    v.scroll_by_lines(-1e9)
    assert v.offset() == 0.0


def test_texto_vacio_no_reproduce(qapp):
    v = PrompterView()
    v.resize(800, 600)
    v.set_text("")
    v.play()
    assert not v.is_playing()
    v.set_text("   \n  ")
    v.play()
    assert not v.is_playing()


def test_temporizador_solo_activo_al_reproducir(qapp):
    v = _vista()
    assert not v._timer.isActive()
    v.play()
    assert v._timer.isActive()
    v.pause()
    assert not v._timer.isActive()


def test_el_temporizador_mueve_el_texto(qapp):
    v = _vista()
    v.play()
    QTest.qWait(150)
    v.pause()
    assert v.offset() > 0.0


def test_pintado_con_y_sin_espejo(qapp):
    v = _vista()
    v.play()
    v.advance(2.0)
    assert v.grab().toImage().size() == v.size()
    v.set_mirror(True)
    assert v.grab().toImage().size() == v.size()
    vacia = PrompterView()
    vacia.resize(800, 600)
    assert vacia.grab().toImage().size() == vacia.size()


def test_cache_de_pintado_se_reutiliza_y_se_renueva(qapp):
    v = _vista()
    v.grab()
    primera = v._cache
    assert primera is not None
    v.play()
    v.advance(0.5)                    # unos píxeles: se reutiliza lo ya pintado
    v.grab()
    assert v._cache is primera
    v.scroll_by_lines(40)             # más de una pantalla: se pinta otro tramo
    v.grab()
    assert v._cache is not primera
    v.set_mirror(True)                # el espejo y la letra invalidan la caché
    assert v._cache is None
    v.grab()
    v.set_font_size(v.font_size() + 8)
    assert v._cache is None


def _pixeles_claros(img: QImage, izquierda: bool) -> int:
    """Píxeles claros en una mitad de la imagen (el suavizado impide comparar píxel a píxel)."""
    img = img.convertToFormat(QImage.Format.Format_RGB32)
    datos = bytes(img.constBits())
    bpl, mitad = img.bytesPerLine(), img.width() // 2 * 4
    total = 0
    for fila in range(img.height()):
        ini = fila * bpl + (0 if izquierda else mitad)
        total += sum(1 for rojo in datos[ini + 2:ini + mitad:4] if rojo > 40)   # también bordes suavizados
    return total


def test_espejo_refleja_el_texto(qapp):
    v = _vista("\n".join(["Hola"] * 30))    # líneas cortas: todo el texto cae en la mitad izquierda
    v.set_progress_bar(False)               # la barra de avance (a un lado) contaría como texto
    normal = v.grab().toImage()
    v.set_mirror(True)
    reflejada = v.grab().toImage()
    # Texto alineado a la izquierda: sin espejo cae a la izquierda; con espejo, a la derecha. (Sin fuentes, la
    # plataforma de pruebas dibuja cuadros de líneas finas que el reflejo atenúa: no se comparan cantidades.)
    assert _pixeles_claros(normal, True) > 3 * _pixeles_claros(normal, False)
    assert _pixeles_claros(reflejada, False) > 3 * _pixeles_claros(reflejada, True)


# ---- TeleprompterWindow --------------------------------------------------------
def test_sin_texto_arranca_en_edicion(qapp, tmp_path):
    w = TeleprompterWindow(Settings(), text_path=tmp_path / "t.txt")
    assert w._stack.currentIndex() == 1
    assert not w._btn_play.isEnabled()
    w.close()


def test_con_texto_guardado_arranca_leyendo(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text("Hola a todos", encoding="utf-8")
    w = TeleprompterWindow(Settings(), text_path=ruta)
    assert w._stack.currentIndex() == 0
    assert w._view.text() == "Hola a todos"
    w.close()


def test_editar_y_volver_aplica_el_texto_y_se_guarda_al_cerrar(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    w = TeleprompterWindow(Settings(), text_path=ruta)
    w._editor.setPlainText("Primera línea\nSegunda línea")
    w._btn_editar.setChecked(False)
    assert w._view.text() == "Primera línea\nSegunda línea"
    assert w._stack.currentIndex() == 0
    w.close()
    assert ruta.read_text(encoding="utf-8") == "Primera línea\nSegunda línea"


def test_velocidad_espejo_y_letra_actualizan_vista_y_ajustes(qapp, tmp_path):
    ajustes = Settings()
    w = TeleprompterWindow(ajustes, text_path=tmp_path / "t.txt")
    w._slider.setValue(22)
    assert w._view.speed() == 22
    assert ajustes.teleprompter.speed == 22
    w._btn_espejo.setChecked(True)
    assert w._view.is_mirrored()
    assert ajustes.teleprompter.mirror
    w._spin.setValue(64)
    assert w._view.font_size() == 64
    assert ajustes.teleprompter.font_size == 64
    w.close()
    guardados = Settings.load(settings_path())
    assert guardados.teleprompter.speed == 22
    assert guardados.teleprompter.mirror is True
    assert guardados.teleprompter.font_size == 64


def test_ajustes_guardados_se_aplican_al_arrancar(qapp, tmp_path):
    ajustes = Settings()
    ajustes.teleprompter.font_size = 72
    ajustes.teleprompter.speed = 17
    ajustes.teleprompter.mirror = True
    ruta = tmp_path / "t.txt"
    ruta.write_text("Texto", encoding="utf-8")
    w = TeleprompterWindow(ajustes, text_path=ruta)
    assert (w._view.font_size(), w._view.speed(), w._view.is_mirrored()) == (72, 17, True)
    assert w._slider.value() == 17
    assert w._spin.value() == 72
    assert w._btn_espejo.isChecked()
    w.close()


def test_espacio_inicia_y_pausa_la_lectura(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    w = TeleprompterWindow(Settings(), text_path=ruta)
    w.show()
    w.activateWindow()
    QTest.qWaitForWindowActive(w)   # los atajos solo se disparan con la ventana activa
    w._view.setFocus()
    QTest.keyClick(w._view, Qt.Key.Key_Space)
    assert w._view.is_playing()
    QTest.keyClick(w._view, Qt.Key.Key_Space)
    assert not w._view.is_playing()
    QTest.keyClick(w._view, Qt.Key.Key_M)
    assert w._view.is_mirrored()
    w.close()


def test_espacio_escribe_en_el_editor(qapp, tmp_path):
    w = TeleprompterWindow(Settings(), text_path=tmp_path / "t.txt")
    w.show()
    w._editor.setFocus()
    QTest.keyClick(w._editor, Qt.Key.Key_Space)
    assert w._editor.toPlainText() == " "
    w.close()


# ---- tipos de letra (fonts.py) y espaciado -------------------------------------
def test_lista_de_letras_incluye_las_recomendadas(qapp):
    familias = [opt.family for opt in fonts.FONT_OPTIONS]
    assert {"Atkinson Hyperlegible Next", "Lexend", "Luciole", "Verdana"} <= set(familias)
    assert fonts.DEFAULT_FAMILY == "Segoe UI"


def test_letras_de_fonts_se_cargan_aunque_no_haya_letras_de_windows(qapp):
    # En la plataforma offscreen Qt no ve ninguna letra de Windows; las de la carpeta fonts/ sí se cargan.
    familias = [opt.family for opt in fonts.available_options()]
    assert {"Atkinson Hyperlegible Next", "Lexend", "Luciole"} <= set(familias)


def test_con_todas_las_letras_instaladas_salen_las_nueve_en_orden(qapp, monkeypatch):
    fonts.load_bundled_fonts()          # antes del parche: así la carga real no queda marcada como hecha con el falso

    class _BaseFalsa:                   # se simula que Windows tiene todas las letras de la lista
        @staticmethod
        def hasFamily(_familia):
            return True

        @staticmethod
        def addApplicationFont(_ruta):
            return 0

    monkeypatch.setattr(fonts, "QFontDatabase", _BaseFalsa)
    assert [opt.family for opt in fonts.available_options()] == [opt.family for opt in fonts.FONT_OPTIONS]
    assert fonts.resolve_family("Verdana") == "Verdana"


def test_letra_no_disponible_cae_en_la_de_windows(qapp):
    assert fonts.resolve_family("No existe") == fonts.DEFAULT_FAMILY
    assert fonts.resolve_family("Lexend") == "Lexend"


def test_vista_letra_y_espaciado_por_defecto(qapp):
    v = PrompterView()
    assert v.font_family() == fonts.DEFAULT_FAMILY
    assert v.letter_spacing() == 0


def test_cambiar_letra_de_la_vista_conserva_la_frase_que_se_lee(qapp):
    v = _vista()
    v.play()
    v.advance(30.0)
    ancla = v._pos_at_y(v.offset() + v.line_height() / 2)
    v.set_font_family("Verdana")
    assert v.font_family() == "Verdana"
    assert _sigue_en_la_misma_frase(v, ancla)


def test_espaciado_se_limita_engorda_el_texto_y_vuelve_a_0(qapp):
    normal = _vista()
    alto_normal, linea = normal.max_offset(), normal.line_height()
    separada = _vista()
    separada.set_letter_spacing(tt.SPACING_MAX)
    assert separada.letter_spacing() == tt.SPACING_MAX
    assert separada.max_offset() > alto_normal          # más líneas por ajuste de línea
    assert separada.line_height() == pytest.approx(linea)  # el espaciado entre letras no cambia el alto de línea
    separada.set_letter_spacing(999)
    assert separada.letter_spacing() == tt.SPACING_MAX
    separada.set_letter_spacing(-5)
    assert separada.letter_spacing() == 0
    assert separada.max_offset() == pytest.approx(alto_normal)   # al quitarlo, el documento vuelve a ser el mismo


def test_cambiar_espaciado_o_ancho_conserva_la_frase_que_se_lee(qapp):
    v = _vista()
    v.play()
    v.advance(30.0)
    ancla = v._pos_at_y(v.offset() + v.line_height() / 2)
    v.set_letter_spacing(30)
    assert _sigue_en_la_misma_frase(v, ancla)
    ancla = v._pos_at_y(v.offset() + v.line_height() / 2)   # lo que se lee ahora (la línea empieza en otro sitio)
    v.resize(500, 600)                      # ventana más estrecha: cambian las líneas, no la frase
    v.grab()                                # entrega el cambio de tamaño pendiente (vista sin mostrar)
    assert _sigue_en_la_misma_frase(v, ancla)


# ---- letra y espaciado en la ventana -------------------------------------------
def test_desplegable_de_letra_tiene_una_entrada_por_letra(qapp, tmp_path):
    w = TeleprompterWindow(Settings(), text_path=tmp_path / "t.txt")
    opciones = fonts.available_options()
    assert w._combo_fuente.count() == len(opciones)
    for i, opt in enumerate(opciones):
        assert w._combo_fuente.itemText(i) == opt.label
        assert w._combo_fuente.itemData(i, Qt.ItemDataRole.UserRole) == opt.family
        assert w._combo_fuente.itemData(i, Qt.ItemDataRole.ToolTipRole) == opt.note
        assert w._combo_fuente.itemData(i, Qt.ItemDataRole.FontRole).family() == opt.family
    w.close()


def test_elegir_letra_en_el_desplegable_cambia_vista_y_ajustes(qapp, tmp_path):
    ajustes = Settings()
    w = TeleprompterWindow(ajustes, text_path=tmp_path / "t.txt")
    w._combo_fuente.setCurrentIndex(w._combo_fuente.findData("Lexend"))
    assert w._view.font_family() == "Lexend"
    assert ajustes.teleprompter.font_family == "Lexend"
    w.close()
    assert Settings.load(settings_path()).teleprompter.font_family == "Lexend"


def test_cycle_font_avanza_y_da_la_vuelta(qapp, tmp_path):
    w = TeleprompterWindow(Settings(), text_path=tmp_path / "t.txt")
    n = w._combo_fuente.count()
    assert n >= 2
    w._combo_fuente.setCurrentIndex(0)
    w._cycle_font(1)
    assert w._combo_fuente.currentIndex() == 1
    assert w._view.font_family() == w._combo_fuente.currentData()
    w._combo_fuente.setCurrentIndex(n - 1)
    w._cycle_font(1)
    assert w._combo_fuente.currentIndex() == 0
    w._cycle_font(-1)
    assert w._combo_fuente.currentIndex() == n - 1
    w.close()


def test_teclas_L_y_mayusculas_L_cambian_la_letra(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    w = TeleprompterWindow(Settings(), text_path=ruta)
    w.show()
    w.activateWindow()
    QTest.qWaitForWindowActive(w)
    w._view.setFocus()
    w._combo_fuente.setCurrentIndex(0)
    QTest.keyClick(w._view, Qt.Key.Key_L)
    assert w._combo_fuente.currentIndex() == 1
    QTest.keyClick(w._view, Qt.Key.Key_L, Qt.KeyboardModifier.ShiftModifier)
    assert w._combo_fuente.currentIndex() == 0
    w.close()


def test_letra_y_espaciado_guardados_se_aplican_al_arrancar(qapp, tmp_path):
    ajustes = Settings()
    ajustes.teleprompter.font_family = "Luciole"
    ajustes.teleprompter.letter_spacing = 20
    ruta = tmp_path / "t.txt"
    ruta.write_text("Texto", encoding="utf-8")
    w = TeleprompterWindow(ajustes, text_path=ruta)
    assert (w._view.font_family(), w._view.letter_spacing()) == ("Luciole", 20)
    assert w._combo_fuente.currentData() == "Luciole"
    assert w._spin_espaciado.value() == 20
    assert not w._timer_ajustes.isActive()      # aplicar los ajustes no los vuelve a guardar
    w.close()


def test_letra_no_instalada_en_los_ajustes_arranca_con_la_de_windows(qapp, tmp_path):
    ajustes = Settings()
    ajustes.teleprompter.font_family = "No existe"
    ruta = tmp_path / "t.txt"
    ruta.write_text("Texto", encoding="utf-8")
    w = TeleprompterWindow(ajustes, text_path=ruta)
    assert w._view.font_family() == fonts.DEFAULT_FAMILY
    assert ajustes.teleprompter.font_family == fonts.DEFAULT_FAMILY
    w.close()


def test_espaciado_de_la_ventana_actualiza_vista_y_ajustes(qapp, tmp_path):
    ajustes = Settings()
    w = TeleprompterWindow(ajustes, text_path=tmp_path / "t.txt")
    assert w._spin_espaciado.maximum() == tt.SPACING_MAX
    assert w._spin_espaciado.singleStep() == tt.SPACING_STEP
    w._spin_espaciado.setValue(30)
    assert w._view.letter_spacing() == 30
    assert ajustes.teleprompter.letter_spacing == 30
    w.close()
    assert Settings.load(settings_path()).teleprompter.letter_spacing == 30


# ---- centrado ------------------------------------------------------------------
def test_centrar_lineas_cambia_la_alineacion_y_conserva_el_progreso(qapp):
    v = _vista()
    v.scroll_by_lines(50)
    antes = v.progress()
    v.set_centered(True)
    assert v.is_centered()
    assert v._doc.firstBlock().blockFormat().alignment() & Qt.AlignmentFlag.AlignHCenter
    assert v.progress() == pytest.approx(antes, abs=0.02)
    v.set_centered(False)
    assert v._doc.firstBlock().blockFormat().alignment() & Qt.AlignmentFlag.AlignLeft


def test_lectura_al_centro_mueve_la_linea_de_lectura_sin_mover_el_texto(qapp):
    v = _vista()
    v.scroll_by_lines(10)
    offset = v.offset()
    assert v.guide_y() == pytest.approx(600 / 3)
    v.set_guide_centered(True)
    assert v.guide_y() == pytest.approx(300)
    assert v.offset() == offset
    assert v.grab().toImage().size() == v.size()


def test_botones_de_centrado_actualizan_vista_y_ajustes(qapp, tmp_path):
    ajustes = Settings()
    w = TeleprompterWindow(ajustes, text_path=tmp_path / "t.txt")
    w._btn_centrar.setChecked(True)
    w._btn_guia.setChecked(True)
    assert w._view.is_centered() and w._view.is_guide_centered()
    assert ajustes.teleprompter.center_text and ajustes.teleprompter.guide_center
    w.close()


def test_centrado_guardado_se_aplica_al_arrancar(qapp, tmp_path):
    ajustes = Settings()
    ajustes.teleprompter.center_text = True
    ajustes.teleprompter.guide_center = True
    ruta = tmp_path / "t.txt"
    ruta.write_text("Texto", encoding="utf-8")
    w = TeleprompterWindow(ajustes, text_path=ruta)
    assert w._view.is_centered() and w._view.is_guide_centered()
    assert w._btn_centrar.isChecked() and w._btn_guia.isChecked()
    w.close()


# ---- bandera (dónde se pausó) y barra de avance: vista -------------------------
def test_al_pausar_queda_la_bandera_en_la_linea_de_lectura(qapp):
    v = _vista()
    marcas: list[int] = []
    v.marksChanged.connect(lambda: marcas.append(v.pause_mark()))
    v.scroll_by_lines(37)
    v.play()
    v.toggle()
    esperada = v._pos_at_y(v.offset() + v.line_height() / 2)
    assert v.has_marks()
    assert v.pause_mark() == esperada
    assert marcas == [esperada]


def test_la_bandera_sobrevive_a_cambiar_la_letra(qapp):
    v = _vista()
    v.scroll_by_lines(37)
    v.play()
    v.toggle()
    pos = v.pause_mark()
    v.set_font_size(90)
    v.rewind()
    v.jump_to_mark(v.pause_mark())
    assert not v.is_playing()
    assert v._pos_at_y(v.offset() + v.line_height() / 2) == pos


def test_cambiar_el_texto_quita_la_bandera(qapp):
    v = _vista()
    v.scroll_by_lines(37)
    v.play()
    v.toggle()
    marcas: list[int] = []
    v.marksChanged.connect(lambda: marcas.append(v.pause_mark()))
    v.set_text("Otro texto\nsegunda línea")
    assert not v.has_marks()
    assert marcas == [-1]


def test_llegar_al_final_quita_la_bandera(qapp):
    v = _vista()
    v.scroll_by_lines(37)
    v.play()
    v.toggle()
    v.play()
    v.advance(100_000.0)
    assert not v.has_marks()


def test_seek_lleva_a_la_proporcion_indicada(qapp):
    v = _vista()
    v.seek(0.5)
    assert v.progress() == pytest.approx(0.5, abs=0.01)


def test_pulsar_en_la_barra_de_avance_salta(qapp):
    v = _vista()
    centro = v._track_rect().center()
    QTest.mouseClick(v, Qt.MouseButton.LeftButton, pos=QPoint(int(centro.x()), int(centro.y())))
    assert v.progress() == pytest.approx(0.5, abs=0.05)


def test_con_espejo_la_barra_de_avance_se_pulsa_a_la_izquierda(qapp):
    v = _vista()
    v.set_mirror(True)
    centro = v._track_rect().center()
    QTest.mouseClick(v, Qt.MouseButton.LeftButton, pos=QPoint(int(v.width() - centro.x()), int(centro.y())))
    assert v.progress() == pytest.approx(0.5, abs=0.05)
    sin_barra_en_su_sitio = _vista()
    sin_barra_en_su_sitio.set_mirror(True)
    QTest.mouseClick(sin_barra_en_su_sitio, Qt.MouseButton.LeftButton,
                     pos=QPoint(int(centro.x()), int(centro.y())))
    assert sin_barra_en_su_sitio.progress() == 0.0


def test_sin_barra_de_avance_pulsar_no_salta(qapp):
    v = _vista()
    v.set_progress_bar(False)
    centro = v._track_rect().center()
    QTest.mouseClick(v, Qt.MouseButton.LeftButton, pos=QPoint(int(centro.x()), int(centro.y())))
    assert v.progress() == 0.0


def test_margen_inferior_reduce_el_alto_de_la_barra_de_avance(qapp):
    v = _vista()
    alto = v._track_rect().height()
    v.set_bottom_inset(100)
    assert v._track_rect().height() == pytest.approx(alto - 100)


# ---- bandera y barra de avance: ventana -----------------------------------------
def test_pausar_con_el_boton_guarda_la_marca(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    ajustes = Settings()
    w = TeleprompterWindow(ajustes, text_path=ruta)
    assert not w._btn_marca.isEnabled()
    w._btn_play.click()                 # leer un rato
    w._view.advance(2.0)
    w._btn_play.click()                 # pausar: aquí queda la bandera
    texto = w._view.text()
    assert ajustes.teleprompter.bookmark_pos >= 0
    assert ajustes.teleprompter.bookmark_crc == zlib.crc32(texto.encode("utf-8"))
    assert w._btn_marca.isEnabled()
    w.close()


def test_al_reabrir_sigue_desde_la_marca(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    w1 = TeleprompterWindow(Settings(), text_path=ruta)
    w1.show()                           # como en la app: la bandera se pone con el ancho real de la ventana
    w1._btn_play.click()
    w1._view.advance(2.0)
    w1._btn_play.click()
    pos = w1._view.pause_mark()
    w1.close()
    w2 = TeleprompterWindow(Settings.load(settings_path()), text_path=ruta)
    w2.show()
    assert w2._view.has_marks()
    assert w2._view.offset() > 0
    assert w2._view._pos_at_y(w2._view.offset() + w2._view.line_height() / 2) == pos
    assert w2._btn_marca.isEnabled()
    w2.close()


def test_con_otro_texto_no_hay_marca(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    w1 = TeleprompterWindow(Settings(), text_path=ruta)
    w1._btn_play.click()
    w1._view.advance(2.0)
    w1._btn_play.click()
    assert w1._view.has_marks()
    w1.close()
    ruta.write_text(TEXTO_LARGO + "\nUna línea nueva.", encoding="utf-8")
    ajustes = Settings.load(settings_path())
    w2 = TeleprompterWindow(ajustes, text_path=ruta)
    assert not w2._view.has_marks()
    assert ajustes.teleprompter.bookmark_pos == -1
    assert ajustes.teleprompter.bookmark_crc == 0
    assert not w2._btn_marca.isEnabled()
    w2.close()


def test_ir_a_la_marca_tras_rebobinar_vuelve_a_la_bandera(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    w = TeleprompterWindow(Settings(), text_path=ruta)
    w._btn_play.click()
    w._view.advance(2.0)
    w._btn_play.click()
    pos = w._view.pause_mark()
    w._btn_inicio.click()
    assert w._view.offset() == 0.0
    assert w._btn_marca.isEnabled()
    w._btn_marca.click()
    assert not w._view.is_playing()
    assert w._view._pos_at_y(w._view.offset() + w._view.line_height() / 2) == pos
    w.close()


def test_tecla_B_va_a_la_marca(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    w = TeleprompterWindow(Settings(), text_path=ruta)
    w.show()
    w.activateWindow()
    QTest.qWaitForWindowActive(w)
    w._btn_play.click()
    w._view.advance(2.0)
    w._btn_play.click()
    pos = w._view.pause_mark()
    w._view.rewind()
    w._view.setFocus()
    QTest.keyClick(w._view, Qt.Key.Key_B)
    assert not w._view.is_playing()
    assert w._view._pos_at_y(w._view.offset() + w._view.line_height() / 2) == pos
    w.close()


def test_boton_barra_de_avance_cambia_vista_y_ajustes(qapp, tmp_path):
    ajustes = Settings()
    w = TeleprompterWindow(ajustes, text_path=tmp_path / "t.txt")
    assert w._view.has_progress_bar() and w._btn_barra.isChecked()
    w._btn_barra.click()
    assert not w._view.has_progress_bar()
    assert ajustes.teleprompter.show_progress_bar is False
    w._btn_barra.click()
    assert w._view.has_progress_bar()
    assert ajustes.teleprompter.show_progress_bar is True
    w._btn_barra.click()
    w.close()
    assert Settings.load(settings_path()).teleprompter.show_progress_bar is False


def test_barra_de_avance_guardada_arranca_oculta(qapp, tmp_path):
    ajustes = Settings()
    ajustes.teleprompter.show_progress_bar = False
    ruta = tmp_path / "t.txt"
    ruta.write_text("Texto", encoding="utf-8")
    w = TeleprompterWindow(ajustes, text_path=ruta)
    assert not w._view.has_progress_bar()
    assert not w._btn_barra.isChecked()
    assert not w._timer_ajustes.isActive()      # aplicar los ajustes no los vuelve a guardar
    w.close()


def test_cerrar_mientras_se_lee_guarda_la_marca(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    w = TeleprompterWindow(Settings(), text_path=ruta)
    w._btn_play.click()
    w._view.advance(2.0)
    assert w._view.is_playing()
    texto = w._view.text()
    w.close()
    guardados = Settings.load(settings_path()).teleprompter
    assert guardados.bookmark_pos >= 0
    assert guardados.bookmark_crc == zlib.crc32(texto.encode("utf-8"))


def test_margen_inferior_de_la_vista_es_el_alto_de_la_barra_de_controles(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text("Texto", encoding="utf-8")
    w = TeleprompterWindow(Settings(), text_path=ruta)
    w.show()
    w.resize(900, 650)
    qapp.processEvents()
    assert w._view._bottom_inset == w._bar.sizeHint().height()
    assert w._view._bottom_inset > 0
    assert w._view._track_rect().bottom() <= w._bar.y()     # la barra de avance no queda debajo de los controles
    w.close()


# ---- banderas rojas en el margen: vista ----------------------------------------
def _clic_margen(v: PrompterView, x: int = 40) -> None:
    """Clic izquierdo en el margen, a la altura de la línea de lectura."""
    QTest.mouseClick(v, Qt.MouseButton.LeftButton, pos=QPoint(x, int(v.guide_y())))


def _linea_de_lectura(v: PrompterView) -> int:
    """Posición del texto que está en la línea de lectura."""
    return v._pos_at_y(v.offset() + v.line_height() / 2)


def test_clic_en_el_margen_pone_y_quita_una_bandera_del_usuario(qapp):
    v = _vista()
    v.scroll_by_lines(37)
    linea = _linea_de_lectura(v)
    _clic_margen(v)
    assert v.manual_marks() == [linea]
    assert v.marks() == [linea]
    _clic_margen(v)
    assert v.manual_marks() == []
    assert not v.has_marks()


def test_clic_en_medio_del_texto_no_pone_bandera(qapp):
    v = _vista()
    QTest.mouseClick(v, Qt.MouseButton.LeftButton, pos=QPoint(400, int(v.guide_y())))
    assert not v.has_marks()


def test_con_espejo_el_margen_es_el_de_la_derecha(qapp):
    v = _vista()
    v.set_progress_bar(False)     # con espejo, la barra de avance queda a la izquierda (x ≈ 29) y taparía el margen
    v.set_mirror(True)
    _clic_margen(v, 40)
    assert not v.has_marks()
    _clic_margen(v, v.width() - 40)
    assert v.manual_marks() == [_linea_de_lectura(v)]


def test_clic_sobre_la_bandera_de_pausa_la_quita(qapp):
    v = _vista()
    v.play()
    v.advance(10.0)
    v.toggle()                    # pausa: bandera de pausa en la línea de lectura
    assert v.pause_mark() >= 0
    _clic_margen(v)
    assert v.pause_mark() == -1
    assert not v.has_marks()


def test_dos_pausas_dejan_una_sola_bandera_de_pausa_y_conservan_las_del_usuario(qapp):
    v = _vista()
    _clic_margen(v)               # bandera del usuario en la primera línea
    manuales = v.manual_marks()
    v.play()
    v.advance(10.0)
    v.toggle()                    # primera pausa
    primera = v.pause_mark()
    v.toggle()
    v.advance(10.0)
    v.toggle()                    # segunda pausa, más adelante: sustituye a la primera
    assert v.pause_mark() > primera
    assert v.manual_marks() == manuales
    assert v.marks() == [manuales[0], v.pause_mark()]


def test_jump_to_next_mark_recorre_las_banderas_y_vuelve_a_la_primera(qapp):
    v = _vista()
    p1, p2, p3 = (TEXTO_LARGO.index(f"Línea {n}:") for n in (10, 30, 60))
    for p in (p3, p1, p2):        # puestas en desorden: el salto va en orden de texto
        v.toggle_mark_at(p)
    recorrido = []
    for _ in range(4):
        v.jump_to_next_mark()
        recorrido.append(_linea_de_lectura(v))
    assert recorrido == [p1, p2, p3, p1]
    assert not v.is_playing()


def test_llegar_al_final_quita_la_de_pausa_pero_no_las_del_usuario(qapp):
    v = _vista()
    _clic_margen(v)
    manuales = v.manual_marks()
    v.toggle()
    v.advance(10.0)
    v.toggle()                    # pausa
    v.toggle()                    # sigue leyendo hasta el final
    v.advance(100_000.0)
    assert v.pause_mark() == -1
    assert v.manual_marks() == manuales
    assert v.has_marks()


def test_set_text_quita_todas_las_banderas(qapp):
    v = _vista()
    _clic_margen(v)
    v.toggle()
    v.advance(10.0)
    v.toggle()
    assert v.has_marks()
    v.set_text("Otro texto\nsegunda línea")
    assert not v.has_marks()
    assert v.marks() == [] and v.manual_marks() == [] and v.pause_mark() == -1


def test_raton_sobre_el_margen_muestra_la_mano_y_fuera_no(qapp):
    v = _vista()

    def mover(x: float) -> None:      # movimiento sin botones, como el de un ratón parado sobre la vista
        QApplication.sendEvent(v, QMouseEvent(
            QEvent.Type.MouseMove, QPointF(x, v.guide_y()), QPointF(x, v.guide_y()),
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))

    mover(40)
    assert v._hover_mark >= 0
    assert v.cursor().shape() == Qt.CursorShape.PointingHandCursor
    mover(400)
    assert v._hover_mark == -1
    assert v.cursor().shape() != Qt.CursorShape.PointingHandCursor
    v.set_cursor_hidden(True)
    assert v.cursor().shape() == Qt.CursorShape.BlankCursor
    v.set_cursor_hidden(False)
    assert v.cursor().shape() != Qt.CursorShape.BlankCursor


def test_tras_cambiar_la_letra_jump_to_mark_lleva_la_marca_a_la_linea_de_lectura(qapp):
    v = _vista()
    p = TEXTO_LARGO.index("Línea 120:")
    v.toggle_mark_at(p)
    v.set_font_size(90)
    assert v.marks() == [p]
    v.jump_to_mark(p)
    assert not v.is_playing()
    assert _sigue_en_la_misma_frase(v, p)


# ---- banderas rojas en el margen: ventana --------------------------------------
def _responder_borrar(monkeypatch, respuesta) -> None:
    """Sustituye la pregunta de «Borrar texto» por una respuesta fija: así nunca sale una ventana modal."""
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: respuesta)


def test_bandera_del_usuario_se_guarda_con_la_huella_del_texto(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    ajustes = Settings()
    w = TeleprompterWindow(ajustes, text_path=ruta)
    w.show()
    _clic_margen(w._view)
    texto = w._view.text()
    assert len(ajustes.teleprompter.bookmarks) == 1
    assert ajustes.teleprompter.bookmarks == w._view.manual_marks()
    assert ajustes.teleprompter.bookmark_crc == zlib.crc32(texto.encode("utf-8"))
    assert ajustes.teleprompter.bookmark_pos == -1
    assert w.statusBar().currentMessage() == ""          # poner una con clic no avisa
    w.close()


def test_aviso_de_marca_solo_al_cambiar_la_de_pausa(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    w = TeleprompterWindow(Settings(), text_path=ruta)
    w.show()
    _clic_margen(w._view)
    assert w.statusBar().currentMessage() == ""
    w._btn_play.click()
    w._view.advance(10.0)
    w._btn_play.click()
    assert "Marca puesta" in w.statusBar().currentMessage()
    w.close()


def test_al_reabrir_se_restauran_las_banderas_del_usuario_y_la_de_pausa(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    w1 = TeleprompterWindow(Settings(), text_path=ruta)
    w1.show()
    _clic_margen(w1._view)                 # bandera del usuario al principio
    w1._btn_play.click()
    w1._view.advance(10.0)
    w1._btn_play.click()                   # pausa: bandera de pausa más adelante
    manuales, pausa = w1._view.manual_marks(), w1._view.pause_mark()
    assert manuales and pausa > manuales[0]
    w1.close()
    w2 = TeleprompterWindow(Settings.load(settings_path()), text_path=ruta)
    w2.show()
    assert w2._view.manual_marks() == manuales
    assert w2._view.pause_mark() == pausa
    assert w2._view.offset() > 0
    assert _linea_de_lectura(w2._view) == pausa
    assert w2._btn_marca.isEnabled() and w2._btn_quitar_marcas.isEnabled()
    w2.close()


def test_quitar_marcas_quita_todas_y_guarda_listas_vacias(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    ajustes = Settings()
    w = TeleprompterWindow(ajustes, text_path=ruta)
    w.show()
    _clic_margen(w._view)
    w._btn_play.click()
    w._view.advance(10.0)
    w._btn_play.click()
    assert w._btn_quitar_marcas.isEnabled()
    w._btn_quitar_marcas.click()
    assert not w._view.has_marks()
    assert ajustes.teleprompter.bookmarks == []
    assert ajustes.teleprompter.bookmark_pos == -1
    assert ajustes.teleprompter.bookmark_crc == 0
    assert not w._btn_quitar_marcas.isEnabled() and not w._btn_marca.isEnabled()
    w.close()
    guardados = Settings.load(settings_path()).teleprompter
    assert guardados.bookmarks == [] and guardados.bookmark_pos == -1 and guardados.bookmark_crc == 0


def test_botones_de_marcas_se_habilitan_con_marcas_y_se_apagan_al_editar(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    w = TeleprompterWindow(Settings(), text_path=ruta)
    assert not w._btn_marca.isEnabled() and not w._btn_quitar_marcas.isEnabled()
    w._view.toggle_mark_at(TEXTO_LARGO.index("Línea 5:"))
    assert w._btn_marca.isEnabled() and w._btn_quitar_marcas.isEnabled()
    w._btn_editar.setChecked(True)
    assert not w._btn_marca.isEnabled() and not w._btn_quitar_marcas.isEnabled()
    w._btn_editar.setChecked(False)
    assert w._btn_marca.isEnabled() and w._btn_quitar_marcas.isEnabled()
    w.close()


def test_borrar_texto_con_si_vacia_todo_y_ctrl_z_lo_recupera(qapp, tmp_path, monkeypatch):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    w = TeleprompterWindow(Settings(), text_path=ruta)
    w.show()
    _clic_margen(w._view)
    texto = w._editor.toPlainText()
    assert w._btn_borrar.isEnabled()
    _responder_borrar(monkeypatch, QMessageBox.StandardButton.Yes)
    w._btn_borrar.click()
    assert w._editor.toPlainText() == ""
    assert w._view.text() == ""
    assert not w._view.has_marks()
    assert w._stack.currentIndex() == 1                  # página de edición
    assert not w._btn_borrar.isEnabled()
    assert w._timer_texto.isActive()                     # el guardado del texto lo escribirá
    QTest.keyClick(w._editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert w._editor.toPlainText() == texto
    assert w._btn_borrar.isEnabled()
    w._btn_editar.setChecked(False)
    assert w._view.text() == texto
    w.close()


def test_borrar_texto_con_no_no_cambia_nada(qapp, tmp_path, monkeypatch):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    w = TeleprompterWindow(Settings(), text_path=ruta)
    w.show()
    _clic_margen(w._view)
    texto, marcas = w._view.text(), w._view.manual_marks()
    _responder_borrar(monkeypatch, QMessageBox.StandardButton.No)
    w._btn_borrar.click()
    assert w._editor.toPlainText() == texto
    assert w._view.text() == texto
    assert w._view.manual_marks() == marcas
    assert w._stack.currentIndex() == 0
    w.close()


def test_boton_borrar_texto_apagado_con_el_editor_vacio(qapp, tmp_path):
    w = TeleprompterWindow(Settings(), text_path=tmp_path / "t.txt")
    assert not w._btn_borrar.isEnabled()
    w._editor.setPlainText("Algo")
    assert w._btn_borrar.isEnabled()
    w._editor.setPlainText("")
    assert not w._btn_borrar.isEnabled()
    w.close()


def test_tecla_B_va_a_la_siguiente_bandera_del_usuario(qapp, tmp_path):
    ruta = tmp_path / "t.txt"
    ruta.write_text(TEXTO_LARGO, encoding="utf-8")
    w = TeleprompterWindow(Settings(), text_path=ruta)
    w.show()
    w.activateWindow()
    QTest.qWaitForWindowActive(w)
    p1, p2 = TEXTO_LARGO.index("Línea 10:"), TEXTO_LARGO.index("Línea 30:")
    w._view.toggle_mark_at(p2)
    w._view.toggle_mark_at(p1)
    w._view.rewind()
    w._view.setFocus()
    QTest.keyClick(w._view, Qt.Key.Key_B)
    assert _linea_de_lectura(w._view) == p1
    QTest.keyClick(w._view, Qt.Key.Key_B)
    assert _linea_de_lectura(w._view) == p2
    assert not w._view.is_playing()
    w.close()

# ---- ángulo de visión ------------------------------------------------------------
def test_angulo_se_limita_y_solo_deforma_si_no_es_cero(qapp):
    v = _vista()
    assert v.view_angle() == 0 and v._persp is None
    v.set_view_angle(99)
    assert v.view_angle() == tt.ANGLE_MAX and v._persp is not None
    v.set_view_angle(-99)
    assert v.view_angle() == tt.ANGLE_MIN
    v.set_view_angle(0)
    assert v._persp is None


def test_desde_arriba_dibuja_mas_ancho_abajo_y_desde_abajo_al_reves(qapp):
    from PySide6.QtCore import QPointF
    v = _vista()
    v.set_view_angle(25)
    arriba_izq = v._persp.map(QPointF(0, 0)).x()
    abajo_izq = v._persp.map(QPointF(0, 600)).x()
    assert abajo_izq < arriba_izq                       # visto desde arriba, abajo queda lejos: se dibuja más ancho
    for esquina in (QPointF(0, 0), QPointF(800, 0), QPointF(800, 600), QPointF(0, 600)):
        p = v._persp.map(esquina)                         # el trapecio cabe dentro de la vista
        assert -0.5 <= p.x() <= 800.5 and -0.5 <= p.y() <= 600.5
    v.set_view_angle(-25)
    assert v._persp.map(QPointF(0, 600)).x() > v._persp.map(QPointF(0, 0)).x()
    assert v.grab().toImage().size() == v.size()


def test_con_angulo_el_clic_en_el_margen_sigue_poniendo_la_bandera(qapp):
    from PySide6.QtCore import QPointF
    v = _vista()
    v.set_progress_bar(False)
    v.set_view_angle(30)
    en_pantalla = v._persp.map(QPointF(40, v.guide_y()))   # dónde se ve el margen de la línea de lectura
    QTest.mouseClick(v, Qt.MouseButton.LeftButton, pos=QPoint(round(en_pantalla.x()), round(en_pantalla.y())))
    assert v.manual_marks() == [v._pos_at_y(v.offset() + v.line_height() / 2)]


def test_control_de_angulo_actualiza_vista_ajustes_y_vuelve_a_cero(qapp, tmp_path):
    ajustes = Settings()
    w = TeleprompterWindow(ajustes, text_path=tmp_path / "t.txt")
    w._slider_angulo.setValue(20)
    assert w._view.view_angle() == 20 and ajustes.teleprompter.view_angle == 20
    assert "arriba" in w._lbl_angulo.text()
    w._slider_angulo.setValue(-15)
    assert "abajo" in w._lbl_angulo.text()
    QTest.mouseDClick(w._lbl_angulo, Qt.MouseButton.LeftButton)
    assert w._view.view_angle() == 0 and ajustes.teleprompter.view_angle == 0
    w.close()


def test_angulo_guardado_se_aplica_al_arrancar(qapp, tmp_path):
    ajustes = Settings()
    ajustes.teleprompter.view_angle = 12
    ruta = tmp_path / "t.txt"
    ruta.write_text("Texto", encoding="utf-8")
    w = TeleprompterWindow(ajustes, text_path=ruta)
    assert w._view.view_angle() == 12 and w._slider_angulo.value() == 12
    w.close()

# ---- fluidez (fps) ---------------------------------------------------------------
def test_fps_fijos_ajustan_el_temporizador(qapp):
    v = _vista()
    v.set_fps(240)
    v.play()
    assert v.effective_fps() == 240 and v._timer.interval() == 4
    v.set_fps(60)
    assert v._timer.interval() == 17
    v.set_fps(999)
    assert v.fps() == 240
    v.set_fps(5)
    assert v.fps() == 30
    v.pause()


def test_automatico_usa_el_monitor_y_baja_si_pintar_es_caro(qapp, monkeypatch):
    v = _vista()
    v.set_fps(0)
    monkeypatch.setattr(v, "monitor_hz", lambda: 240)
    v._paint_ms = 0.5                       # pintar es barato: los 240 Hz del monitor
    assert v.effective_fps() == 240
    v._paint_ms = 6.0                       # caro (p. ej. con ángulo): que pintar no pase de la mitad del fotograma
    assert v.effective_fps() == 83
    v._paint_ms = 100.0                     # nunca por debajo del mínimo
    assert v.effective_fps() == 30
    v._paint_ms = 0.5
    v._cpu_ms = 15.0                        # mucha CPU por fotograma (varios hilos): no pasar de un núcleo
    assert v.effective_fps() == 66
    v.set_fps(240)                          # con fps fijos se respeta lo elegido
    assert v.effective_fps() == 240


def test_selector_de_fluidez_actualiza_vista_y_ajustes(qapp, tmp_path):
    ajustes = Settings()
    w = TeleprompterWindow(ajustes, text_path=tmp_path / "t.txt")
    assert w._combo_fps.currentData() == 0 and "fps" in w._lbl_fps.text()
    w._combo_fps.setCurrentIndex(w._combo_fps.findData(144))
    assert w._view.fps() == 144 and ajustes.teleprompter.fps == 144
    assert w._lbl_fps.text().startswith("≈ 144")
    w.close()


def test_fluidez_guardada_se_aplica_al_arrancar(qapp, tmp_path):
    ajustes = Settings()
    ajustes.teleprompter.fps = 120
    w = TeleprompterWindow(ajustes, text_path=tmp_path / "t.txt")
    assert w._view.fps() == 120 and w._combo_fps.currentData() == 120
    w.close()

# ---- tarjeta gráfica ---------------------------------------------------------------
def test_sin_pantalla_real_se_usa_la_vista_del_procesador(qapp):
    from cronista.teleprompter.view import PrompterView as Vista, create_view, gpu_available
    assert not gpu_available()               # plataforma de pruebas: sin OpenGL
    assert isinstance(create_view(True), Vista)
    assert isinstance(create_view(False), Vista)


def test_la_ventana_respeta_usar_procesador(qapp, tmp_path):
    ajustes = Settings()
    ajustes.teleprompter.use_gpu = False
    w = TeleprompterWindow(ajustes, text_path=tmp_path / "t.txt")
    assert type(w._view).__name__ == "PrompterView"
    w.close()

def test_con_angulo_la_banda_y_la_barra_quedan_rectas(qapp):
    from PySide6.QtCore import QPointF
    v = _vista()
    v.set_view_angle(30)
    centro, alto = v._screen_band()
    # la banda está donde queda la línea de lectura del texto deformado
    assert centro == pytest.approx(v._persp.map(QPointF(400, v.guide_y())).y(), abs=3.0)   # la perspectiva no es lineal: ~1 px
    assert alto > 0
    # la barra de avance se pulsa en su sitio de pantalla (recta, a la derecha), no en el deformado
    pista = v._track_rect()
    QTest.mouseClick(v, Qt.MouseButton.LeftButton, pos=QPoint(round(pista.center().x()), round(pista.center().y())))
    assert v.progress() == pytest.approx(0.5, abs=0.05)
    assert v.grab().toImage().size() == v.size()