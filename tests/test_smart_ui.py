"""Modo Smart y modo «junto a la cámara» dentro de la ventana del teleprompter (sin micrófono ni ventanas)."""
from __future__ import annotations

import json
import os
import threading
import time
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cronista.core.settings import Settings  # noqa: E402
from cronista.teleprompter import smart  # noqa: E402
from cronista.teleprompter.window import TeleprompterWindow  # noqa: E402

MODELOS_REALES = Path(os.environ.get("APPDATA", "")) / "Cronista" / "modelos"   # (antes de cambiar APPDATA)
WAV = Path(r"F:\Reclaimer\tests\assets\narracion_prueba.wav")
TRANSCRIPCION = Path(r"F:\Reclaimer\tests\assets\narracion_prueba.transcripcion.json")


@pytest.fixture(autouse=True)
def _appdata_temporal(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def _pump(app, segundos):
    t0 = time.time()
    while time.time() - t0 < segundos:
        app.processEvents()
        time.sleep(0.01)


def _ventana(texto: str, tmp_path) -> TeleprompterWindow:
    ajustes = Settings()
    ajustes.teleprompter.countdown_s = 0
    ruta = tmp_path / "texto.txt"
    ruta.write_text(texto, encoding="utf-8")
    w = TeleprompterWindow(ajustes, ruta)
    w.resize(1000, 600)
    return w


class _OyenteFalso(QObject):
    """Sustituye al micrófono: lo que se le dé con oir() llega como si se hubiera dicho."""
    heard = Signal(list)
    heard_final = Signal(list)
    analysed = Signal()
    error = Signal(str)
    level = Signal(float)
    started = Signal()
    stopped = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.activo = False
        self.idioma = None
        self.unknown_words = set()
        self.spoken_forms = {}

    def start(self, lang, device=None, vocabulary=None, acronyms_=None):
        self.activo, self.idioma, self.micro = True, lang, device

    def stop(self, timeout=2.0):
        self.activo = False

    def is_running(self):
        return self.activo


GUION = ("Hola a todos, bienvenidos a este análisis. Hoy vamos a hablar de un juego que nadie esperaba. "
         "El combate se siente lento y pesado, pero la historia compensa casi todo. ") * 8


def test_smart_sigue_lo_que_se_dice(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(smart, "VoiceListener", _OyenteFalso)
    monkeypatch.setattr(smart, "model_path", lambda lang: tmp_path)        # (como si el modelo estuviera)
    w = _ventana(GUION, tmp_path)
    w._btn_smart.setChecked(True)
    assert w._view.is_following() and w._tp.smart
    w.start_reading()
    assert w._view.is_playing() and w._oyente.is_running() and w._oyente.idioma == "es"
    _pump(qapp, 0.3)
    assert w._view.offset() == 0, "sin voz, el texto no se mueve"
    frases = GUION.split(".")[:12]
    dichas = []
    for frase in frases:
        for palabra in frase.split():
            dichas.append(palabra)
            w._oyente.heard.emit(dichas[-6:])
        _pump(qapp, 0.05)
    _pump(qapp, 1.5)
    assert w._view.offset() > 0, "al leer, el texto avanza"
    meta = w._view._follow_target
    w._oyente.heard.emit(["eh", "bueno", "o", "sea"])      # muletillas: no lo mueven
    _pump(qapp, 0.3)
    assert w._view._follow_target == meta
    w._pulsar_play()                                          # pausa = deja de escuchar
    assert not w._view.is_playing() and not w._oyente.is_running()
    w._btn_smart.setChecked(False)
    assert not w._view.is_following()
    w.close()


def test_modo_camara(qapp, tmp_path):
    w = _ventana(GUION, tmp_path)
    w.show()
    _pump(qapp, 0.1)
    antes = w.geometry()
    w._btn_camara.setChecked(True)
    _pump(qapp, 0.1)
    flags = w.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint and flags & Qt.WindowType.WindowStaysOnTopHint
    area = w.screen().availableGeometry()
    assert w.geometry().top() == area.top() and w.width() < area.width()
    assert w._grip.isVisible()
    w._salir_pantalla_completa()                              # Esc vuelve a la ventana normal
    _pump(qapp, 0.1)
    assert not w._btn_camara.isChecked()
    assert not (w.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
    assert w.geometry() == antes
    w.close()


@pytest.mark.skipif(not (WAV.exists() and TRANSCRIPCION.exists()
                         and (MODELOS_REALES / smart.MODELS["es"][0]).is_dir()),
                    reason="hace falta el modelo de voz en español y la narración de prueba")
def test_smart_con_una_narracion_real(qapp, tmp_path, monkeypatch):
    """Una narración grabada entra por el «micrófono» (a 4× la velocidad real) y el texto la sigue."""
    import vosk
    monkeypatch.setattr(smart, "models_dir", lambda: MODELOS_REALES)

    class _OyenteWav(_OyenteFalso):
        def start(self, lang, device=None, vocabulary=None, acronyms_=None):
            super().start(lang, device)
            self.vocabulario = vocabulary
            threading.Thread(target=self._leer, daemon=True).start()

        def _leer(self):
            vosk.SetLogLevel(-1)
            with wave.open(str(WAV)) as f:
                modelo = vosk.Model(str(smart.model_path("es")))
                rec = (vosk.KaldiRecognizer(modelo, f.getframerate(), json.dumps(self.vocabulario, ensure_ascii=False))
                       if self.vocabulario else vosk.KaldiRecognizer(modelo, f.getframerate()))
                bloque = f.getframerate() // 8
                while self.activo:
                    datos = f.readframes(bloque)
                    if not datos:
                        break
                    if rec.AcceptWaveform(datos):                     # como el oyente real (smart.VoiceListener)
                        palabras = json.loads(rec.Result()).get("text", "").split()
                        if palabras:
                            self.heard_final.emit(palabras)
                    else:
                        palabras = json.loads(rec.PartialResult()).get("partial", "").split()
                        if palabras:
                            self.heard.emit(palabras)
                    time.sleep(0.125 / 4)

    monkeypatch.setattr(smart, "VoiceListener", _OyenteWav)
    palabras = [p[2] for p in json.loads(TRANSCRIPCION.read_text(encoding="utf-8"))["words"]]
    guion = " ".join(palabras)
    w = _ventana(guion, tmp_path)
    w.resize(700, 400)
    w._btn_smart.setChecked(True)
    w.start_reading()
    _pump(qapp, 12)
    llegado = w._view.offset() / max(1.0, w._view.max_offset())
    print(f"\nSmart con narración real: el texto la siguió hasta el {llegado:.0%}")
    assert llegado > 0.6, f"el texto siguió la narración hasta el {llegado:.0%}"
    w.close()


UNICO = ("La mañana empezó con lluvia y nadie quería salir de casa. Marta preparó café mientras revisaba el correo. "
         "En la oficina, el proyecto nuevo parecía imposible de terminar a tiempo. Pedro propuso dividir el trabajo "
         "en partes pequeñas. Al mediodía comieron tacos en el mercado de la esquina. Por la tarde llegó el cliente "
         "con preguntas difíciles sobre el presupuesto. Marta respondió con calma y mostró los números. Al final del "
         "día todos estaban cansados pero contentos. Esa noche la ciudad brillaba bajo la luna llena. Mañana será "
         "otro día lleno de sorpresas y de trabajo duro para todo el equipo.\n") * 1


def test_smart_te_encuentra_aunque_empieces_en_otra_parte(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(smart, "VoiceListener", _OyenteFalso)
    monkeypatch.setattr(smart, "model_path", lambda lang: tmp_path)
    relleno = " ".join(f"Apartado {i}: aquí sigue otra parte distinta del guion." for i in range(120))
    w = _ventana(UNICO + relleno, tmp_path)
    w.resize(700, 300)
    w._view.jump_to_mark(len(UNICO) + len(relleno) // 2)   # el texto está a mitad (como una bandera restaurada)
    lejos = w._view.offset()
    assert lejos > 0
    w._btn_smart.setChecked(True)
    w.start_reading()
    dichas = []
    for palabra in UNICO.split()[:40]:            # pero lee desde el principio
        dichas.append(palabra)
        w._oyente.heard.emit(dichas[-6:])
        _pump(qapp, 0.03)
    _pump(qapp, 2.0)
    assert w._view.offset() < lejos * 0.3, (w._view.offset(), lejos)
    assert "«" in w._lbl_oido.text(), "el indicador enseña lo que oye"
    w.close()


def test_encender_o_apagar_smart_pausa(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(smart, "VoiceListener", _OyenteFalso)
    monkeypatch.setattr(smart, "model_path", lambda lang: tmp_path)
    w = _ventana(UNICO * 3, tmp_path)
    w.start_reading()
    assert w._view.is_playing()
    w._btn_smart.setChecked(True)
    assert not w._view.is_playing() and w._lbl_oido.isVisible() or not w.isVisible()
    w.start_reading()
    assert w._view.is_playing() and w._oyente.is_running()
    w._btn_smart.setChecked(False)
    assert not w._view.is_playing() and not w._oyente.is_running()
    assert w._combo_mic.itemText(0) == "Predeterminado de Windows"
    w.close()


def test_smart_avisa_si_no_te_oye(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(smart, "VoiceListener", _OyenteFalso)
    monkeypatch.setattr(smart, "model_path", lambda lang: tmp_path)
    w = _ventana(UNICO, tmp_path)
    w._btn_smart.setChecked(True)
    w.start_reading()
    w._oido_ultimo -= 10                          # 10 s sin voz
    w._oyente.level.emit(0.0)
    assert "no te oigo" in w._lbl_oido.text()
    w._oyente.level.emit(0.3)
    assert "no te oigo" not in w._lbl_oido.text()
    w.close()
