"""Superficie de lectura del teleprompter: el texto sube a velocidad constante y una línea marca dónde leer.

No tiene controles: la ventana (window.py) los coloca alrededor. Aquí hay maquetación, desplazamiento y pintado.
"""
from __future__ import annotations

import logging
import math
import time

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QImage, QOpenGLContext, QPainter, QPen,
                           QPixmap, QPolygonF, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument,
                           QTransform)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

from cronista.teleprompter import gramatica
from cronista.teleprompter.fonts import DEFAULT_FAMILY
from cronista.teleprompter.text import (ANGLE_MAX, ANGLE_MIN, FONT_MAX, FONT_MIN, GUIDE_CENTER_FRACTION,
                                        GUIDE_FRACTION, MARGIN_MAX, MARGIN_MIN, SPACING_MAX, SPACING_MIN, SPEED_MAX,
                                        SPEED_MIN, VIEW_DISTANCE, PHRASE_MAX_CHARS, clamp, phrase_breaks,
                                        speed_to_px_per_s)

log = logging.getLogger(__name__)

PHRASE_CAP_MIN = 24            # líneas por frases: caracteres por línea, como mínimo y como máximo
PHRASE_CAP_MAX = 70            # (aunque quepan más, una frase tan larga ya no se lee de un tirón)
_MUESTRA = "el combate se siente pesado y cada disparo cuenta en la campaña "   # para medir el ancho medio de letra

FONT_FAMILY = "Segoe UI"
TEXT_COLOR = "#f2f2f2"
GUIDE_COLOR = "#3b5bdb"
EMPTY_COLOR = "#8b93a3"
LINE_FACTOR = 1.3              # alto de línea = interlineado natural de la fuente × 1,3
LINE_SPACING_PCT = 130         # el mismo 130 % aplicado al documento
PARAGRAPH_GAP = 0.1            # espacio entre párrafos, como fracción del tamaño de letra
TICK_MS = 16                   # cadencia inicial del temporizador; luego la fija la fluidez (set_fps)
FPS_MIN = 30
FPS_MAX = 240
PAINT_BUDGET = 0.5             # en automático, pintar no ocupa más de la mitad del tiempo de cada fotograma
SUBPIXEL_SCALE = 1.0000001     # escala inapreciable que obliga a Qt a interpolar la posición con decimales
CPU_BUDGET = 1.0              # ...ni el programa pasa de un núcleo de CPU (en total, sumando hilos)
AUTO_CHECK_TICKS = 60          # cada cuántos fotogramas se reajusta el modo automático
MAX_DT_S = 0.1                 # tope del tiempo entre fotogramas: tras un bloqueo no hay salto
MIN_TEXT_WIDTH = 40.0
WHEEL_FONT_STEP = 4            # puntos por muesca de rueda con Ctrl
GUIDE_HALF_H = 10.0            # semialto de los triángulos de la línea de lectura (px)
GUIDE_W = 14.0                 # ancho de los triángulos (px)
FLAG_COLOR = "#f85149"         # bandera roja: dónde se pausó (para seguir leyendo otro día)
HARD_WORD_COLOR = "#6e7681"    # gris: subrayado de puntos bajo las palabras que Smart no puede oír
REVIEW_COLOR = "#d29922"       # ámbar: frase con un posible error de lectura (modo Smart)
TRACK_W = 6.0                  # barra de avance a la derecha: ancho, separación del borde y márgenes (px)
TRACK_RIGHT = 26.0
TRACK_PAD = 16.0
TRACK_HIT = 36.0               # ancho de la zona donde se puede pulsar o arrastrar la barra
MARK_ZONE_MIN = 70.0           # ancho mínimo del margen izquierdo donde un clic pone o quita una bandera
GHOST_ALPHA = 90               # bandera tenue al pasar el ratón por el margen (clic = ponerla)
_END_EPS = 0.01                # px de tolerancia para dar por alcanzado el final
FOLLOW_TAU_S = 0.35            # modo Smart: cuánto tarda (constante de tiempo) en llegar a la línea que dices
FOLLOW_RATE_WINDOW_S = 4.0     # modo Smart: el ritmo de habla se mide con lo oído en los últimos segundos
FOLLOW_COAST_S = 0.9           # ...y se sigue a ese ritmo como mucho este tiempo tras la última palabra oída
FOLLOW_MAX_LEAD = 0.5          # ...sin pasar de media línea por delante de ella
FOLLOW_MAX_RATE_LINES = 2.0    # ...ni de 2 líneas por segundo
# Caché del texto ya pintado. Por encima de ~64 px Qt dibuja cada letra como un contorno: a 56 pt y 1080p eso
# costaba ~10 ms por fotograma. Ahora el texto se pinta en una imagen de dos pantallas de alto que cada
# fotograma solo copia (<1 ms); se vuelve a pintar tras avanzar unas tres cuartas partes de pantalla.
CACHE_SCREENS = 2.0
CACHE_BACK = 0.25              # la caché empieza un cuarto de pantalla por encima de lo visible (rueda atrás)


class _PrompterLogic:
    """Todo el teleprompter salvo con qué se dibuja: texto, desplazamiento, banderas, barra de avance, ángulo y
    fluidez. Se combina con QWidget (PrompterView: dibuja el procesador) o con QOpenGLWidget (PrompterViewGL:
    dibuja la tarjeta gráfica, sincronizado con el monitor). Las señales las declara cada clase.
    """

    _gpu = False

    def _init_prompter(self) -> None:
        self._text = ""
        self._phrase_breaks = False   # mostrar las líneas cortadas por frases
        self._phrase_cap = PHRASE_MAX_CHARS   # caracteres que caben en una línea (se ajusta al maquetar)
        self._grammar_worker = None           # análisis gramatical: dónde haría una pausa una persona
        self._grammar_costs: dict | None = None
        self._grammar_text: str | None = None     # texto al que corresponde ese análisis
        self._grammar_pending: str | None = None  # texto que se está analizando
        self._font_size = 56
        self._font_family = DEFAULT_FAMILY   # letra del texto: la ventana elige una de las instaladas
        self._letter_spacing = 0             # % extra entre letras (0 = normal)
        self._speed = 10
        self._mirror = False
        self._centered = False        # líneas centradas a lo ancho
        self._guide_centered = False  # línea de lectura a media altura en vez de a un tercio
        self._margin_pct = 10
        self._offset = 0.0            # px que ha subido el texto
        self._playing = False
        self._wheel_font_acc = 0.0    # fracción de muesca acumulada con Ctrl + rueda (touchpads)
        # Modo Smart: en vez de subir a velocidad fija, el texto va (suave) a la línea que la voz acaba de decir
        self._follow = False
        self._follow_target = 0.0
        self._follow_heard: list[tuple[float, float]] = []   # (momento, meta) de las últimas palabras oídas
        self._review_marks: list[int] = []     # frases con un posible error de lectura (dónde empiezan)
        self._review_level = "discreto"
        self._hard_words: list[tuple[int, int]] = []   # (inicio, fin) de las palabras que Smart no puede oír
        self.on_manual_move = None    # modo Smart: la ventana se entera de que el usuario movió el texto a mano

        self._doc = QTextDocument(self)
        self._doc.setDocumentMargin(0)
        self._doc.setUndoRedoEnabled(False)
        self._font = QFont(self._font_family)
        self._line_h = 1.0
        self._margin_px = 0.0
        self._text_width = MIN_TEXT_WIDTH
        self._doc_height = 0.0
        self._cache: QPixmap | None = None
        self._cache_top = 0.0         # coordenada del documento que corresponde al borde superior de la caché
        self._layout_size = self.size()
        # Banderas rojas, guardadas como posiciones en el texto (no en píxeles): siguen en su sitio aunque cambien
        # la letra, el tamaño o el ancho. Hay dos clases: las que pone el usuario con un clic en el margen (se
        # quedan hasta que las quite) y la de la última pausa (cada pausa la mueve, para no acumular banderas).
        self._manual_marks: set[int] = set()
        self._pause_mark = -1
        self._mark_tops: dict[int, float] = {}   # posición → altura de su línea en el documento (al maquetar)
        self._hover_mark = -1         # línea (posición) bajo el ratón en el margen izquierdo; -1 = ninguna
        self._cursor_hidden = False   # la ventana oculta el cursor en pantalla completa
        self._show_track = True
        self._bottom_inset = 0.0      # alto de la barra de controles que tapa la parte de abajo (la pone la ventana)
        self._dragging_track = False
        # Ángulo de visión: con un ángulo distinto de 0, cada fotograma se pinta en una imagen aparte y se dibuja
        # deformado en trapecio (perspectiva), para que visto desde arriba o desde abajo el texto se vea recto.
        self._view_angle = 0
        self._persp: QTransform | None = None       # imagen lógica → pantalla
        self._persp_inv: QTransform | None = None   # pantalla → imagen lógica (para los clics)
        self._frame: QImage | None = None
        self._layout_doc()

        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._on_tick)
        self._fps_setting = 0          # 0 = automático
        self._paint_ms = 0.0           # media móvil de lo que tarda en pintarse un fotograma
        self._ticks = 0
        self._paint_clock = QElapsedTimer()
        self._paint_count = 0
        self._cpu_ms = 0.0             # CPU del programa por fotograma (media), para el modo automático
        self._cpu_mark = 0.0
        self._paints_mark = 0

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)

    # ---- texto y ajustes ----------------------------------------------------
    def set_text(self, text: str) -> None:
        """Cambia el texto. Se pausa, vuelve al principio y quita las banderas (ya no apuntarían al mismo sitio)."""
        self.pause()
        self._text = text
        self._doc.setPlainText(self._display_text())
        self._offset = 0.0
        self._follow_target = 0.0
        self._review_marks = []                     # las frases para revisar eran del texto anterior
        self._hard_words = []
        self._hover_mark = -1
        self._layout_doc()
        self.clear_marks()
        self._after_move()
        self._request_grammar()

    def text(self) -> str:
        return self._text

    def _display_text(self) -> str:
        if not self._phrase_breaks:
            return self._text
        costes = self._grammar_costs if self._grammar_text == self._text else None
        return phrase_breaks(self._text, self._phrase_cap, costes)

    def _request_grammar(self) -> None:
        """Pide el análisis gramatical (en otro hilo) del texto actual; mientras llega se usan las reglas."""
        if (not self._phrase_breaks or not gramatica.enabled or not self._text.strip()
                or self._text in (self._grammar_text, self._grammar_pending)):
            return
        if self._grammar_worker is None:
            self._grammar_worker = gramatica.GrammarAnalyzer(self)
            self._grammar_worker.ready.connect(self._on_grammar, Qt.ConnectionType.QueuedConnection)
        self._grammar_pending = self._text
        self._grammar_worker.analyze(self._text)

    def _on_grammar(self, text: str, costes: dict) -> None:
        """Llegó el análisis: se vuelven a cortar las líneas sin mover la frase que se está leyendo."""
        self._grammar_pending = None
        if text == self._text:
            self._grammar_text, self._grammar_costs = text, costes
            if self._phrase_breaks:
                self._reapply_text()

    def _chars_per_line(self) -> int:
        """Caracteres de texto normal que caben en una línea con la letra y el ancho actuales (con un margen)."""
        por_letra = QFontMetricsF(self._font).horizontalAdvance(_MUESTRA) / len(_MUESTRA)
        return int(clamp(0.95 * self._text_width / max(por_letra, 1.0), PHRASE_CAP_MIN, PHRASE_CAP_MAX))

    def set_phrase_breaks(self, on: bool) -> None:
        """Corta las líneas por frases. Solo cambia cómo se muestra: la misma frase sigue en la línea de lectura."""
        if bool(on) == self._phrase_breaks:
            return
        self._phrase_breaks = bool(on)
        self._reapply_text()
        self._request_grammar()

    def _reapply_text(self) -> None:
        """Vuelve a poner el texto con los cortes actuales, con la misma frase en la línea de lectura."""
        ancla = self._pos_at_y(self._offset + self._line_h / 2) if self._text.strip() else 0
        self._doc.setPlainText(self._display_text())
        self._layout_doc()
        self._offset = clamp(self._line_top(ancla), 0.0, self.max_offset()) if self._text.strip() else 0.0
        self._follow_target = self._offset
        self._after_move()

    def has_phrase_breaks(self) -> bool:
        return self._phrase_breaks

    def set_font_size(self, pt: int) -> None:
        pt = int(clamp(int(pt), FONT_MIN, FONT_MAX))
        if pt != self._font_size:
            self._font_size = pt
            self._relayout_keep_position()

    def font_size(self) -> int:
        return self._font_size

    def set_font_family(self, family: str) -> None:
        """Cambia la letra del texto. La ventana solo pasa letras instaladas (fonts.resolve_family)."""
        family = family or DEFAULT_FAMILY
        if family != self._font_family:
            self._font_family = family
            self._relayout_keep_position()

    def font_family(self) -> str:
        return self._font_family

    def set_letter_spacing(self, pct: int) -> None:
        """Separación extra entre letras, en % (0 = normal; el máximo lo fija SPACING_MAX)."""
        pct = int(clamp(int(pct), SPACING_MIN, SPACING_MAX))
        if pct != self._letter_spacing:
            self._letter_spacing = pct
            self._relayout_keep_position()

    def letter_spacing(self) -> int:
        return self._letter_spacing

    def set_centered(self, on: bool) -> None:
        """Líneas centradas a lo ancho (False = alineadas a la izquierda)."""
        if bool(on) != self._centered:
            self._centered = bool(on)
            self._relayout_keep_position()

    def is_centered(self) -> bool:
        return self._centered

    def set_guide_centered(self, on: bool) -> None:
        """Línea de lectura a media altura (False = a un tercio). Solo cambia dónde se pinta: el texto que está
        en la línea de lectura sigue siendo el mismo."""
        self._guide_centered = bool(on)
        self.update()

    def is_guide_centered(self) -> bool:
        return self._guide_centered

    def set_view_angle(self, degrees: int) -> None:
        """Ángulo desde el que miras la pantalla: + = desde arriba (miras hacia abajo), − = desde abajo, 0 = de
        frente. El texto se deforma en trapecio para compensarlo."""
        degrees = int(clamp(int(degrees), ANGLE_MIN, ANGLE_MAX))
        if degrees != self._view_angle:
            self._view_angle = degrees
            self._update_perspective()

    def view_angle(self) -> int:
        return self._view_angle

    def _update_perspective(self) -> None:
        """Calcula el trapecio: proyección de un ojo a VIEW_DISTANCE alturas de pantalla, inclinado el ángulo
        elegido, invertida (lo que se verá lejos se dibuja más grande) y encajada dentro de la vista."""
        w, h = float(self.width()), float(self.height())
        self._persp = self._persp_inv = None
        self._frame = None
        if self._view_angle and w > 2 and h > 2:
            t = math.radians(self._view_angle)
            s, c = math.sin(t), math.cos(t)
            d = VIEW_DISTANCE * h
            y_arr = (h / 2) * d / (d * c + (h / 2) * s)         # alturas aparentes (hacia arriba, desde el centro)
            y_ab = -(h / 2) * d / (d * c - (h / 2) * s)
            x_arr = (w / 2) * (d - y_arr * s) / d               # semianchos que hay que dibujar arriba y abajo
            x_ab = (w / 2) * (d - y_ab * s) / d
            ajuste = min((w / 2) / max(x_arr, x_ab), h / (y_arr - y_ab))
            centro = (y_arr + y_ab) / 2

            def punto(x: float, y: float) -> QPointF:
                return QPointF(w / 2 + x * ajuste, h / 2 - (y - centro) * ajuste)

            trapecio = QPolygonF([punto(-x_arr, y_arr), punto(x_arr, y_arr), punto(x_ab, y_ab), punto(-x_ab, y_ab)])
            rect = QPolygonF([QPointF(0, 0), QPointF(w, 0), QPointF(w, h), QPointF(0, h)])
            tr = QTransform.quadToQuad(rect, trapecio)
            if isinstance(tr, QTransform):
                inv, ok = tr.inverted()
                if ok:
                    self._persp, self._persp_inv = tr, inv
        self.update()

    def _to_logical(self, pos: QPointF) -> QPointF:
        """Punto de la pantalla → punto de la imagen sin deformar (los clics con ángulo de visión)."""
        return self._persp_inv.map(pos) if self._persp_inv is not None else pos

    def guide_y(self) -> float:
        """Altura de la línea de lectura dentro de la vista, en px."""
        return self.height() * (GUIDE_CENTER_FRACTION if self._guide_centered else GUIDE_FRACTION)

    def set_speed(self, level: int) -> None:
        self._speed = int(clamp(int(level), SPEED_MIN, SPEED_MAX))

    def speed(self) -> int:
        return self._speed

    def set_mirror(self, on: bool) -> None:
        self._mirror = bool(on)
        self._cache = None            # la caché se pinta ya reflejada
        self.update()

    def is_mirrored(self) -> bool:
        return self._mirror

    def set_margin_pct(self, pct: int) -> None:
        pct = int(clamp(int(pct), MARGIN_MIN, MARGIN_MAX))
        if pct != self._margin_pct:
            self._margin_pct = pct
            self._relayout_keep_position()

    # ---- reproducción -------------------------------------------------------
    def play(self) -> None:
        """Empieza a desplazar. Con texto vacío no hace nada; si ya está al final, vuelve a empezar."""
        if self._playing or not self._text.strip() or self.max_offset() <= _END_EPS:
            return
        if self._offset >= self.max_offset() - _END_EPS:
            self._offset = 0.0
            self._after_move()
        self._playing = True
        # Las medidas empiezan de cero: el primer fotograma tras parar (o el primero con la tarjeta gráfica, que
        # prepara texturas) es lento y no debe hundir los fps del modo automático.
        self._paint_ms = self._cpu_ms = 0.0
        self._cpu_mark, self._paints_mark = time.process_time(), self._paint_count
        self._apply_tick()
        self._clock.start()
        self._timer.start()
        self.playingChanged.emit(True)

    def pause(self) -> None:
        if self._playing:
            self._stop()

    def toggle(self) -> None:
        """Iniciar / pausar (botón y Espacio). Al pausar deja la bandera roja en la línea donde se paró."""
        if self._playing:
            self.pause()
            self.mark_pause_here()
        else:
            self.play()

    def rewind(self) -> None:
        """Vuelve al principio y se pausa; el usuario pulsa Iniciar para volver a leer."""
        self.pause()
        self._offset = 0.0
        self._after_move()
        self._manual_moved()

    def is_playing(self) -> bool:
        return self._playing

    def advance(self, dt_s: float) -> None:
        """Avanza dt_s segundos a la velocidad actual. Lo llama el temporizador; es pública para las pruebas."""
        if not self._playing or dt_s <= 0:
            return
        top = self.max_offset()
        if self._follow:                   # modo Smart: se acerca (suave) a la palabra dicha
            self._extrapolate_follow(dt_s)
            meta = clamp(self._follow_target, 0.0, top)
            falta = meta - self._offset
            if abs(falta) <= 0.05:
                return
            paso = falta * (1.0 - math.exp(-dt_s / FOLLOW_TAU_S))
            paso = max(paso, 0.05) if falta > 0 else min(paso, -0.05)
            self._offset = min(self._offset + paso, meta) if falta > 0 else max(self._offset + paso, meta)
        else:
            self._offset = min(self._offset + speed_to_px_per_s(self._speed, self._line_h) * dt_s, top)
        self._after_move()
        if self._offset >= top - _END_EPS:
            self._stop()
            self.clear_pause_mark()        # texto terminado: ya no hay dónde continuar (las del usuario se quedan)
            self.finished.emit()

    # ---- modo Smart (el texto sigue la voz) ---------------------------------
    def set_follow(self, on: bool) -> None:
        """Modo Smart: mientras se lee, el texto no sube a velocidad fija sino que va a donde diga follow_to()."""
        self._follow = bool(on)
        self._follow_target = self._offset
        self._follow_heard = []

    def is_following(self) -> bool:
        return self._follow

    # ---- frases para revisar (posibles errores de lectura) -------------------
    def set_review(self, positions: list[int], level: str) -> None:
        """Frases con un posible error (posición donde empiezan) y cómo mostrarlas: «discreto» = rayita ámbar en la
        barra de avance; «atento» = además un punto junto a la frase; «silencioso»/«apagado» = nada mientras se lee."""
        self._review_marks = sorted(int(p) for p in positions)
        self._review_level = level
        self.update()

    def review_marks(self) -> list[int]:
        return list(self._review_marks)

    # ---- palabras que Smart no puede oír ------------------------------------
    def set_hard_words(self, spans: list[tuple[int, int]]) -> None:
        """Subraya con puntos grises, finos, las palabras (inicio, fin) que el reconocedor no entenderá: ahí el texto
        no se guía por la voz. Sin avisos ni colores fuertes; [] las quita."""
        spans = sorted((int(a), int(b)) for a, b in spans if 0 <= a < b <= len(self._text))
        if spans == self._hard_words:
            return
        self._hard_words = spans
        self._format_doc()
        self._cache = None
        self.update()

    def hard_words(self) -> list[tuple[int, int]]:
        return list(self._hard_words)

    def follow_to(self, pos: int, back: bool = False) -> None:
        """La voz acaba de decir la palabra que acaba en pos: el texto va (suave) a dejarla en la línea de lectura.

        No va al principio de su línea sino en proporción a lo que lleva dicho de ella: así sube de forma continua
        mientras se lee la línea (antes se quedaba quieto toda la línea y luego subía de golpe). Solo hacia adelante,
        salvo back=True (la voz se encontró con seguridad más atrás: empezó en otra parte)."""
        if not self._text.strip() or pos < 0:
            return
        meta = clamp(self._line_top(pos) + self._line_fraction(pos) * self._line_h, 0.0, self.max_offset())
        ahora = time.monotonic()
        if back:
            self._follow_target, self._follow_heard = meta, [(ahora, meta)]
            return
        self._follow_target = max(self._follow_target, meta)
        self._follow_heard = [(t, m) for t, m in self._follow_heard if ahora - t <= FOLLOW_RATE_WINDOW_S]
        self._follow_heard.append((ahora, max(meta, self._follow_heard[-1][1] if self._follow_heard else meta)))

    def _line_fraction(self, pos: int) -> float:
        """Qué parte de su línea (0..1) queda antes del final de la palabra que acaba en pos."""
        bloque = self._doc.findBlock(max(0, pos - 1))
        if not bloque.isValid():
            return 0.0
        rel = pos - bloque.position()
        linea = bloque.layout().lineForTextPosition(max(0, rel - 1))
        if not linea.isValid() or linea.naturalTextWidth() <= 0:
            return 0.0
        x = linea.cursorToX(rel)
        x = x[0] if isinstance(x, tuple) else x
        return clamp((x - linea.x()) / linea.naturalTextWidth(), 0.0, 1.0)

    def _extrapolate_follow(self, dt_s: float) -> None:
        """Entre dos palabras reconocidas, el texto sigue un poco al ritmo con que se viene hablando: como mucho
        FOLLOW_MAX_LEAD líneas por delante de la última palabra oída y durante FOLLOW_COAST_S tras ella (si se calla,
        se para)."""
        if len(self._follow_heard) < 2:
            return
        (t0, m0), (t1, m1) = self._follow_heard[0], self._follow_heard[-1]
        if time.monotonic() - t1 > FOLLOW_COAST_S or t1 - t0 < 0.5:
            return
        ritmo = min((m1 - m0) / (t1 - t0), FOLLOW_MAX_RATE_LINES * self._line_h)
        if ritmo > 0:
            self._follow_target = min(self._follow_target + ritmo * dt_s, m1 + FOLLOW_MAX_LEAD * self._line_h)

    def _manual_moved(self) -> None:
        """El usuario movió el texto (rueda, barra, teclas, bandera): el modo Smart sigue desde ahí."""
        self._follow_target = self._offset
        self._follow_heard = []                     # el ritmo medido era de otro sitio
        if self.on_manual_move is not None:
            self.on_manual_move()

    def reading_pos(self) -> int:
        """Posición del texto al principio de la línea que está en la línea de lectura."""
        return self._pos_at_y(self._offset + self._line_h / 2) if self._text.strip() else 0

    def _stop(self) -> None:
        self._playing = False
        self._timer.stop()
        self.playingChanged.emit(False)

    def _on_tick(self) -> None:
        dt = self._clock.nsecsElapsed() / 1e9      # en nanosegundos: a 240 fps un milisegundo es un 24 % del paso
        self._clock.restart()
        self.advance(min(dt, MAX_DT_S))
        self._ticks += 1
        if self._ticks % AUTO_CHECK_TICKS == 0:
            self._measure_cpu()
            if self._fps_setting == 0:
                self._apply_tick()                   # automático: se reajusta según lo que cuesta pintar

    def _measure_cpu(self) -> None:
        """CPU del programa por fotograma pintado (todos los hilos: con ángulo, Qt deforma en varios núcleos).
        Se mide por tandas porque el reloj de CPU de Windows solo avanza de ~16 en ~16 ms."""
        ahora, pintados = time.process_time(), self._paint_count
        if pintados > self._paints_mark:
            ms = (ahora - self._cpu_mark) * 1000.0 / (pintados - self._paints_mark)
            self._cpu_ms = ms if self._cpu_ms <= 0 else 0.7 * self._cpu_ms + 0.3 * ms
        self._cpu_mark, self._paints_mark = ahora, pintados

    # ---- fluidez (fotogramas por segundo) ---------------------------------------
    def set_fps(self, fps: int) -> None:
        """Fotogramas por segundo al desplazar: 0 = automático (los del monitor, si el equipo da para ello)."""
        self._fps_setting = 0 if int(fps) <= 0 else int(clamp(int(fps), FPS_MIN, FPS_MAX))
        self._apply_tick()

    def fps(self) -> int:
        return self._fps_setting

    def monitor_hz(self) -> int:
        """Frecuencia del monitor en el que está la vista (60 si no se sabe)."""
        pantalla = self.screen()
        hz = pantalla.refreshRate() if pantalla is not None else 0.0
        return int(round(hz)) if hz and hz > 1 else 60

    def effective_fps(self) -> int:
        """Los fotogramas por segundo que se usan de verdad."""
        if self._fps_setting:
            return self._fps_setting
        fps = min(self.monitor_hz(), FPS_MAX)
        if self._paint_ms > 0:                       # que pintar no ocupe más de PAINT_BUDGET de cada fotograma
            fps = min(fps, int(1000.0 * PAINT_BUDGET / self._paint_ms))
        if self._cpu_ms > 0:                         # y que el programa no pase de CPU_BUDGET núcleos
            fps = min(fps, int(1000.0 * CPU_BUDGET / self._cpu_ms))
        return int(clamp(fps, FPS_MIN, FPS_MAX))

    def paint_ms(self) -> float:
        """Lo que tarda de media en pintarse un fotograma (ms)."""
        return self._paint_ms

    def _apply_tick(self) -> None:
        self._timer.setInterval(max(1, round(1000.0 / self.effective_fps())))

    # ---- posición -----------------------------------------------------------
    def offset(self) -> float:
        return self._offset

    def max_offset(self) -> float:
        """Desplazamiento máximo: la última línea llega a la línea de lectura."""
        return max(0.0, self._doc_height - self._line_h)

    def progress(self) -> float:
        top = self.max_offset()
        return clamp(self._offset / top, 0.0, 1.0) if top > 0 else 0.0

    def line_height(self) -> float:
        return self._line_h

    def scroll_by_lines(self, n: float) -> None:
        """Desplaza n líneas (negativo = hacia atrás). Siempre dentro de [0, max_offset]."""
        self._offset = clamp(self._offset + n * self._line_h, 0.0, self.max_offset())
        self._after_move()
        self._manual_moved()

    def seek(self, fraction: float) -> None:
        """Salta a una proporción del texto (0 = principio, 1 = final). Si estaba leyendo, sigue leyendo."""
        self._offset = clamp(fraction, 0.0, 1.0) * self.max_offset()
        self._after_move()
        self._manual_moved()

    # ---- banderas (marcas) --------------------------------------------------
    def marks(self) -> list[int]:
        """Todas las banderas (las del usuario y la de la pausa), en orden de aparición en el texto."""
        return sorted(self._all_marks(), key=lambda p: (self._mark_tops.get(p, 0.0), p))

    def manual_marks(self) -> list[int]:
        return sorted(self._manual_marks)

    def pause_mark(self) -> int:
        """Bandera de la última pausa (donde seguir leyendo otro día); -1 = ninguna."""
        return self._pause_mark

    def has_marks(self) -> bool:
        return bool(self._manual_marks) or self._pause_mark >= 0

    def set_marks(self, manual: list[int], pause: int = -1) -> None:
        """Restaura las banderas guardadas (posiciones en el texto)."""
        tope = max(0, self._doc.characterCount() - 1)
        valida = (lambda p: 0 <= int(p) <= tope) if self._text.strip() else (lambda p: False)
        self._manual_marks = {int(p) for p in manual if valida(p)}
        self._pause_mark = int(pause) if valida(pause) else -1
        self._marks_changed()

    def mark_pause_here(self) -> None:
        """Bandera de pausa en la línea que está en la línea de lectura (sustituye a la de la pausa anterior)."""
        if self._text.strip():
            self._pause_mark = self._pos_at_y(self._offset + self._line_h / 2)
            self._marks_changed()

    def toggle_mark_at(self, pos: int) -> None:
        """Clic en el margen: quita las banderas de esa línea o, si no hay, pone una del usuario."""
        if not self._text.strip() or pos < 0:
            return
        top = self._line_top(pos)
        en_linea = [p for p in self.marks() if abs(self._mark_tops.get(p, -1e9) - top) < 0.5]
        if en_linea:
            for p in en_linea:
                self._manual_marks.discard(p)
                if p == self._pause_mark:
                    self._pause_mark = -1
        else:
            self._manual_marks.add(self._line_start(pos))
        self._marks_changed()

    def clear_marks(self) -> None:
        if self.has_marks():
            self._manual_marks.clear()
            self._pause_mark = -1
            self._marks_changed()

    def clear_pause_mark(self) -> None:
        if self._pause_mark >= 0:
            self._pause_mark = -1
            self._marks_changed()

    def jump_to_mark(self, pos: int) -> None:
        """Lleva la línea de la bandera (o de cualquier posición) a la línea de lectura y se queda en pausa."""
        if pos < 0 or not self._text.strip():
            return
        self.pause()
        self._offset = clamp(self._line_top(pos), 0.0, self.max_offset())
        self._after_move()
        self._manual_moved()

    def jump_to_next_mark(self) -> None:
        """Va a la siguiente bandera por debajo de la línea de lectura; tras la última, vuelve a la primera."""
        marcas = self.marks()
        if not marcas:
            return
        actual = self._line_top(self._pos_at_y(self._offset + self._line_h / 2))
        siguientes = [p for p in marcas if self._mark_tops.get(p, 0.0) > actual + 0.5]
        self.jump_to_mark(siguientes[0] if siguientes else marcas[0])

    def mark_progresses(self) -> list[float]:
        """Dónde están las banderas, como proporción del texto (0..1), para la barra de avance."""
        top = self.max_offset()
        if top <= 0:
            return [0.0 for _ in self.marks()]
        return [clamp(self._mark_tops.get(p, 0.0) / top, 0.0, 1.0) for p in self.marks()]

    def _marks_changed(self) -> None:
        self._update_mark_tops()
        self.marksChanged.emit()
        self.update()

    def _update_mark_tops(self) -> None:
        self._mark_tops = {p: self._line_top(p) for p in self._all_marks()}

    def _all_marks(self) -> set[int]:
        todas = set(self._manual_marks)
        if self._pause_mark >= 0:
            todas.add(self._pause_mark)
        return todas

    def _line_start(self, pos: int) -> int:
        """Posición donde empieza la línea (visual) que contiene pos."""
        bloque = self._doc.findBlock(pos)
        if not bloque.isValid():
            return max(0, pos)
        linea = bloque.layout().lineForTextPosition(pos - bloque.position())
        return bloque.position() + (linea.textStart() if linea.isValid() else 0)

    def _line_top(self, pos: int) -> float:
        """Altura (en el documento) de la línea que contiene la posición pos."""
        bloque = self._doc.findBlock(pos)
        if not bloque.isValid():
            return 0.0
        tl = bloque.layout()
        linea = tl.lineForTextPosition(pos - bloque.position())
        return tl.position().y() + (linea.y() if linea.isValid() else 0.0)

    def _pos_at_y(self, y: float) -> int:
        """Posición del texto al principio de la línea que ocupa la altura y del documento. Recorre los párrafos:
        solo se usa al pausar, no en cada fotograma."""
        bloque = elegido = self._doc.begin()
        while bloque.isValid() and bloque.layout().position().y() <= y:
            elegido = bloque
            bloque = bloque.next()
        tl = elegido.layout()
        rel = y - tl.position().y()
        n = tl.lineCount()
        for i in range(n):
            linea = tl.lineAt(i)
            if rel < linea.y() + linea.height():
                return elegido.position() + linea.textStart()
        return elegido.position() + (tl.lineAt(n - 1).textStart() if n else 0)

    # ---- margen izquierdo: poner y quitar banderas con el ratón -------------
    def _in_mark_zone(self, x: float) -> bool:
        """El margen a la izquierda del texto (a la derecha en la pantalla si hay espejo)."""
        if self._mirror:
            x = self.width() - x
        return 0 <= x <= max(self._margin_px, MARK_ZONE_MIN)

    def _line_under(self, y: float) -> int:
        """Posición del principio de la línea que se ve a la altura y de la vista; -1 si ahí no hay texto."""
        doc_y = y - self.guide_y() + self._offset + self._line_h / 2
        if not self._text.strip() or doc_y < 0 or doc_y >= self._doc_height:
            return -1
        return self._pos_at_y(doc_y)

    def _set_hover(self, pos: int) -> None:
        if pos != self._hover_mark:
            self._hover_mark = pos
            self._update_cursor()
            self.update()

    def set_cursor_hidden(self, hidden: bool) -> None:
        """La ventana oculta el cursor en pantalla completa tras unos segundos sin mover el ratón."""
        self._cursor_hidden = bool(hidden)
        self._update_cursor()

    def _update_cursor(self) -> None:
        if self._cursor_hidden:
            self.setCursor(Qt.CursorShape.BlankCursor)
        elif self._hover_mark >= 0:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.unsetCursor()

    # ---- barra de avance ----------------------------------------------------
    def set_progress_bar(self, on: bool) -> None:
        self._show_track = bool(on)
        self.update()

    def has_progress_bar(self) -> bool:
        return self._show_track

    def set_bottom_inset(self, px: float) -> None:
        """La ventana avisa de cuánto tapa su barra de controles, para que la barra de avance no quede debajo."""
        px = max(0.0, float(px))
        if px != self._bottom_inset:
            self._bottom_inset = px
            self.update()

    def _track_rect(self) -> QRectF:
        """Barra de avance, en coordenadas sin reflejar (a la derecha; con espejo se pinta reflejada)."""
        alto = max(0.0, self.height() - self._bottom_inset - 2 * TRACK_PAD)
        return QRectF(self.width() - TRACK_RIGHT - TRACK_W, TRACK_PAD, TRACK_W, alto)

    def _in_track_zone(self, x: float) -> bool:
        centro = self._track_rect().center().x()
        if self._mirror:
            centro = self.width() - centro
        return abs(x - centro) <= TRACK_HIT / 2

    # ---- maquetación --------------------------------------------------------
    def _layout_doc(self) -> None:
        """Maqueta el documento con la fuente, el margen y el ancho actuales. No se llama en cada fotograma."""
        self._layout_size = self.size()
        self._font = QFont(self._font_family)
        self._font.setPointSize(self._font_size)
        if self._letter_spacing > 0:    # en Qt, 100 % es el espaciado normal de la letra
            self._font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100 + self._letter_spacing)
        self._line_h = QFontMetricsF(self._font).lineSpacing() * LINE_FACTOR
        self._margin_px = self.width() * self._margin_pct / 100.0
        self._text_width = max(MIN_TEXT_WIDTH, self.width() * (1 - 2 * self._margin_pct / 100.0))
        self._doc.setDefaultFont(self._font)
        if self._phrase_breaks:                # los trozos se parten según lo que quepa con esta letra y este ancho
            tope = self._chars_per_line()
            if tope != self._phrase_cap:
                self._phrase_cap = tope
                self._doc.setPlainText(self._display_text())
        self._format_doc()
        self._doc.setTextWidth(self._text_width)
        self._doc_height = self._doc.size().height() if self._text.strip() else 0.0
        self._cache = None
        self._update_mark_tops()

    def _format_doc(self) -> None:
        """Aplica a todo el documento: alineación izquierda, interlineado 130 %, espacio entre párrafos y color."""
        cursor = QTextCursor(self._doc)
        cursor.select(QTextCursor.SelectionType.Document)
        bloque = QTextBlockFormat()
        bloque.setAlignment(Qt.AlignmentFlag.AlignHCenter if self._centered else Qt.AlignmentFlag.AlignLeft)
        bloque.setLineHeight(LINE_SPACING_PCT, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        bloque.setBottomMargin(max(2.0, self._font_size * PARAGRAPH_GAP))
        cursor.mergeBlockFormat(bloque)
        letra = QTextCharFormat()
        letra.setFont(self._font)
        letra.setForeground(QColor(TEXT_COLOR))
        letra.setUnderlineStyle(QTextCharFormat.UnderlineStyle.NoUnderline)
        cursor.mergeCharFormat(letra)
        if self._hard_words:
            dificil = QTextCharFormat()
            dificil.setUnderlineStyle(QTextCharFormat.UnderlineStyle.DotLine)
            dificil.setUnderlineColor(QColor(HARD_WORD_COLOR))
            n = self._doc.characterCount() - 1
            for a, b in self._hard_words:
                if b <= n:
                    cursor.setPosition(a)
                    cursor.setPosition(b, QTextCursor.MoveMode.KeepAnchor)
                    cursor.mergeCharFormat(dificil)

    def _relayout_keep_position(self) -> None:
        """Vuelve a maquetar (letra, tamaño, espaciado, margen, ancho) sin perder lo que se está leyendo: la misma
        frase sigue en la línea de lectura aunque cambie cuántas palabras caben en cada línea. (Conservar solo el
        porcentaje recorrido dejaba otra frase delante al cambiar la letra o el ancho de la ventana.)"""
        if not self._text.strip() or self._line_h <= 0:
            self._layout_doc()
            self._offset = 0.0
            self._after_move()
            return
        ancla = self._pos_at_y(self._offset + self._line_h / 2)       # frase que está en la línea de lectura
        dentro = clamp((self._offset - self._line_top(ancla)) / self._line_h, 0.0, 1.0)   # cuánto de esa línea
        self._layout_doc()
        self._offset = clamp(self._line_top(ancla) + dentro * self._line_h, 0.0, self.max_offset())
        self._follow_target = self._offset        # (en píxeles ya no valía; la voz lo vuelve a fijar)
        self._after_move()

    def _after_move(self) -> None:
        self.progressChanged.emit(self.progress())
        self.update()

    # ---- eventos ------------------------------------------------------------
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.size() != self._layout_size:   # Qt también avisa sin cambio real (p. ej. al capturar oculta)
            self._relayout_keep_position()
            self._update_perspective()

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if not delta:
            event.ignore()
            return
        muescas = delta / 120.0
        if bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._wheel_font_acc += muescas
            pasos = int(self._wheel_font_acc)
            if pasos:
                self._wheel_font_acc -= pasos
                antes = self._font_size
                self.set_font_size(antes + pasos * WHEEL_FONT_STEP)
                if self._font_size != antes:
                    self.fontSizeChanged.emit(self._font_size)
        else:
            self.scroll_by_lines(-muescas)
        event.accept()

    def mousePressEvent(self, event) -> None:
        pantalla = event.position()                   # la barra de avance nunca se deforma: coordenadas de pantalla
        pos = self._to_logical(pantalla)              # el texto y su margen sí: coordenadas sin deformar
        if event.button() == Qt.MouseButton.LeftButton and self._text.strip():
            if self._show_track and self._in_track_zone(pantalla.x()):
                self._dragging_track = True
                self._seek_to_y(pantalla.y())
                event.accept()
                return
            if self._in_mark_zone(pos.x()):
                linea = self._line_under(pos.y())     # la de ahora: el texto puede haberse movido bajo el ratón
                if linea >= 0:
                    self.toggle_mark_at(linea)
                    self._set_hover(linea)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pos = self._to_logical(event.position())
        if self._dragging_track:
            self._seek_to_y(event.position().y())
            event.accept()
            return
        self._set_hover(self._line_under(pos.y()) if self._in_mark_zone(pos.x()) else -1)
        event.ignore()          # que también llegue a la ventana (muestra la barra en pantalla completa)

    def leaveEvent(self, event) -> None:
        self._set_hover(-1)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging_track:
            self._dragging_track = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _seek_to_y(self, y: float) -> None:
        pista = self._track_rect()
        if pista.height() > 0:
            self.seek((y - pista.top()) / pista.height())

    def _paint_timed(self) -> None:
        """Pinta un fotograma y mide lo que tarda (para el modo automático de fluidez)."""
        self._paint_clock.start()
        self._paint()
        ms = self._paint_clock.nsecsElapsed() / 1e6
        self._paint_ms = ms if self._paint_ms <= 0 else 0.9 * self._paint_ms + 0.1 * ms
        self._paint_count += 1

    def _paint(self) -> None:
        painter = QPainter(self)
        if not self._text.strip():
            painter.fillRect(self.rect(), QColor("#000000"))   # WA_OpaquePaintEvent: el fondo lo pintamos aquí
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            painter.setFont(QFont(FONT_FAMILY, 16))
            painter.setPen(QColor(EMPTY_COLOR))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "No hay texto. Pulsa «Editar texto» o abre un archivo.")
            painter.end()
            return
        if self._persp is None:
            self._paint_frame(painter)
        elif self._gpu:
            # Tarjeta gráfica: el trapecio es solo otra transformación al dibujar (casi gratis).
            painter.fillRect(self.rect(), QColor("#000000"))
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setTransform(self._persp)
            self._paint_frame(painter, overlay=False)
            painter.resetTransform()
            self._paint_overlay(painter, *self._screen_band())
        else:
            # Con ángulo de visión: el fotograma se pinta normal en una imagen y se dibuja deformado (~5 ms a 1080p
            # medido; sin ángulo no se paga nada de esto).
            marco = self._frame_image()
            p = QPainter(marco)
            self._paint_frame(p, overlay=False)
            p.end()
            painter.fillRect(self.rect(), QColor("#000000"))
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.setTransform(self._persp)
            painter.drawImage(QPointF(0, 0), marco)
            painter.resetTransform()
            self._paint_overlay(painter, *self._screen_band())
        painter.end()

    def _screen_band(self) -> tuple[float, float]:
        """Centro y alto, en pantalla, de la banda de lectura del texto deformado. Las líneas horizontales siguen
        siendo horizontales en el trapecio, así que basta con llevar sus bordes de arriba y abajo."""
        guia_y, lh, x = self.guide_y(), self._line_h, self.width() / 2
        arriba = self._persp.map(QPointF(x, guia_y - lh / 2)).y()
        abajo = self._persp.map(QPointF(x, guia_y + lh / 2)).y()
        return (arriba + abajo) / 2, abajo - arriba

    def _frame_image(self) -> QImage:
        dpr = self.devicePixelRatioF()
        ancho, alto = max(1, math.ceil(self.width() * dpr)), max(1, math.ceil(self.height() * dpr))
        if self._frame is None or self._frame.width() != ancho or self._frame.height() != alto \
                or self._frame.devicePixelRatio() != dpr:
            self._frame = QImage(ancho, alto, QImage.Format.Format_RGB32)
            self._frame.setDevicePixelRatio(dpr)
        return self._frame

    def _paint_frame(self, painter: QPainter, overlay: bool = True) -> None:
        """Un fotograma sin deformar: texto (desde la caché) y banderas; con overlay, también la línea de lectura
        y la barra de avance (con ángulo de visión esas van aparte, rectas)."""
        ancho, alto = self.width(), self.height()
        guia_y = self.guide_y()
        lh = self._line_h
        # Con offset 0, la primera línea queda centrada en la línea de lectura; con offset = max, la última.
        # «arriba» es la coordenada del documento que cae en el borde superior de la vista.
        arriba = self._offset + lh / 2 - guia_y
        dpr = self.devicePixelRatioF()
        if not self._cache_covers(arriba, alto, dpr):
            self._paint_cache(arriba, ancho, alto, dpr)
        # Copia en posición con decimales (por fracciones de píxel). Con píxeles enteros, a velocidades normales el
        # texto se quedaba quieto varias imágenes y luego saltaba 1 px («sube, pausa, sube»). Qt redondea las
        # posiciones decimales de una traslación pura; una escala inapreciable le obliga a mezclar las dos filas
        # vecinas (interpolación bilineal): movimiento continuo por ~1 ms a 1080p.
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setTransform(QTransform(1, 0, 0, SUBPIXEL_SCALE, 0, self._cache_top - arriba), True)
        # Solo la franja visible de la caché (que mide dos pantallas): interpolar la caché entera costaba el doble.
        desde = max(0.0, math.floor(arriba - self._cache_top) - 1.0)
        alto_franja = min(alto + 3.0, self._cache.height() / dpr - desde)
        painter.drawPixmap(QRectF(0, desde, ancho, alto_franja), self._cache,
                           QRectF(0, desde * dpr, ancho * dpr, alto_franja * dpr))
        painter.restore()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if overlay:
            self._paint_overlay(painter, guia_y, lh)
        # Banderas: se pintan reflejadas con el espejo, para que en el cristal se vean a la izquierda del texto.
        painter.save()
        if self._mirror:
            painter.translate(ancho, 0)
            painter.scale(-1, 1)
        hover_top = self._line_top(self._hover_mark) if self._hover_mark >= 0 else None
        hay_en_hover = False
        for top in set(self._mark_tops.values()):
            bajo_raton = hover_top is not None and abs(top - hover_top) < 0.5
            hay_en_hover = hay_en_hover or bajo_raton
            self._paint_flag(painter, guia_y + top - self._offset, cross=bajo_raton)
        if hover_top is not None and not hay_en_hover:
            self._paint_flag(painter, guia_y + hover_top - self._offset, GHOST_ALPHA)
        if self._review_level == "atento":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(REVIEW_COLOR))
            radio = clamp(self._line_h * 0.08, 3.0, 7.0)
            for pos in self._review_marks:                  # punto ámbar junto a la frase con un posible error
                y = guia_y + self._line_top(pos) - self._offset
                if -radio <= y <= self.height() + radio:
                    painter.drawEllipse(QPointF(max(radio + 2.0, self._margin_px - 8.0), y), radio, radio)
        painter.restore()

    def _paint_overlay(self, painter: QPainter, centro_y: float, alto_banda: float) -> None:
        """Banda de la línea de lectura, triángulos y barra de avance. Con ángulo de visión se pintan rectos (sin
        deformar), a la altura donde queda la línea de lectura del texto deformado."""
        ancho = self.width()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(QRectF(0, centro_y - alto_banda / 2, ancho, alto_banda), QColor(255, 255, 255, 18))
        self._paint_guide(painter, ancho, centro_y)        # simétricos: valen igual con espejo
        if self._show_track:
            painter.save()
            if self._mirror:                                # en el cristal, a la derecha del texto
                painter.translate(ancho, 0)
                painter.scale(-1, 1)
            self._paint_track(painter)
            painter.restore()

    def _paint_flag(self, painter: QPainter, y: float, alpha: int = 255, cross: bool = False) -> None:
        """Bandera roja a la izquierda de una línea (y = centro de esa línea en la vista). Con alpha < 255 se ve
        tenue (ratón por el margen: un clic la pone); con cross, una ✕ blanca encima (un clic la quita)."""
        alto = clamp(self._line_h * 0.7, 18.0, 64.0)
        if y < -alto or y > self.height() + alto:
            return
        ancho = alto * 0.75
        x = max(6.0, self._margin_px - ancho - 18.0)
        rojo = QColor(FLAG_COLOR)
        rojo.setAlpha(alpha)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(rojo)
        painter.drawRect(QRectF(x, y - alto / 2, max(2.0, alto / 14), alto))                   # mástil
        painter.drawPolygon(QPolygonF([QPointF(x, y - alto / 2), QPointF(x + ancho, y - alto / 4),
                                       QPointF(x, y)]))                                        # tela
        if cross:
            r = alto * 0.16
            cx, cy = x + ancho * 0.36, y - alto / 4
            painter.setPen(QPen(QColor("#ffffff"), max(2.0, alto / 18), Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            painter.drawLine(QPointF(cx - r, cy - r), QPointF(cx + r, cy + r))
            painter.drawLine(QPointF(cx - r, cy + r), QPointF(cx + r, cy - r))
            painter.setPen(Qt.PenStyle.NoPen)

    def _paint_track(self, painter: QPainter) -> None:
        """Barra de avance: en azul lo ya leído, punto blanco donde vas y rayas rojas donde hay banderas."""
        pista = self._track_rect()
        if pista.height() <= 0:
            return
        radio = TRACK_W / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 45))
        painter.drawRoundedRect(pista, radio, radio)
        y = pista.top() + pista.height() * self.progress()
        painter.setBrush(QColor(GUIDE_COLOR))
        painter.drawRoundedRect(QRectF(pista.left(), pista.top(), TRACK_W, y - pista.top()), radio, radio)
        if self._review_level in ("discreto", "atento") and self._review_marks:
            painter.setBrush(QColor(REVIEW_COLOR))         # frases con un posible error: rayitas ámbar más cortas
            top = self.max_offset()
            for pos in self._review_marks:
                p = clamp(self._line_top(pos) / top, 0.0, 1.0) if top > 0 else 0.0
                painter.drawRect(QRectF(pista.left() - 2, pista.top() + pista.height() * p - 1.5, TRACK_W + 4, 3))
        painter.setBrush(QColor(FLAG_COLOR))
        for marca in self.mark_progresses():
            painter.drawRect(QRectF(pista.left() - 5, pista.top() + pista.height() * marca - 2, TRACK_W + 10, 4))
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QPointF(pista.center().x(), y), TRACK_W, TRACK_W)

    def _cache_covers(self, arriba: float, alto: float, dpr: float) -> bool:
        c = self._cache
        if c is None or c.devicePixelRatio() != dpr:
            return False
        fondo = self._cache_top + c.height() / dpr
        return self._cache_top <= arriba - 1 and arriba + alto + 1 <= fondo

    def _paint_cache(self, arriba: float, ancho: float, alto: float, dpr: float) -> None:
        """Pinta (ya reflejado si hay espejo) el tramo del texto que rodea lo visible."""
        alto_cache = math.ceil(alto * CACHE_SCREENS) + 2
        self._cache_top = math.floor(arriba - alto * CACHE_BACK)
        pm = QPixmap(max(1, math.ceil(ancho * dpr)), max(1, math.ceil(alto_cache * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(QColor("#000000"))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        if self._mirror:
            p.translate(ancho, 0)
            p.scale(-1, 1)
        p.translate(self._margin_px, -self._cache_top)
        self._doc.drawContents(p, QRectF(0, self._cache_top, self._text_width, alto_cache))
        p.end()
        self._cache = pm

    def _paint_guide(self, painter: QPainter, ancho: float, y: float) -> None:
        """Dos triángulos azules, uno en cada borde, a la altura de la línea de lectura."""
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(GUIDE_COLOR))
        izquierda = QPolygonF([QPointF(0, y - GUIDE_HALF_H), QPointF(0, y + GUIDE_HALF_H),
                               QPointF(GUIDE_W, y)])
        derecha = QPolygonF([QPointF(ancho, y - GUIDE_HALF_H), QPointF(ancho, y + GUIDE_HALF_H),
                             QPointF(ancho - GUIDE_W, y)])
        painter.drawPolygon(izquierda)
        painter.drawPolygon(derecha)


class PrompterView(_PrompterLogic, QWidget):
    """Vista dibujada por el procesador. Es el respaldo si no hay OpenGL (y la que usan las pruebas).

    Señales: playingChanged(bool), progressChanged(float 0..1), finished() al llegar al final,
    fontSizeChanged(int) cuando Ctrl + rueda cambia la letra, marksChanged() al poner o quitar banderas.
    """

    playingChanged = Signal(bool)
    progressChanged = Signal(float)
    finished = Signal()
    fontSizeChanged = Signal(int)
    marksChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        QWidget.__init__(self, parent)
        self._init_prompter()

    def paintEvent(self, event) -> None:
        self._paint_timed()


class PrompterViewGL(_PrompterLogic, QOpenGLWidget):
    """Vista dibujada por la tarjeta gráfica (OpenGL). El texto ya maquetado se sube como textura cuando cambia y
    en cada fotograma la tarjeta lo coloca con fracciones de píxel (movimiento continuo, sin «sube, pausa») y lo
    presenta sincronizado con el refresco del monitor. El procesador apenas trabaja, también con ángulo."""

    _gpu = True

    playingChanged = Signal(bool)
    progressChanged = Signal(float)
    finished = Signal()
    fontSizeChanged = Signal(int)
    marksChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        QOpenGLWidget.__init__(self, parent)
        self._init_prompter()

    def paintGL(self) -> None:
        self._paint_timed()


def gpu_available() -> bool:
    """¿Se puede dibujar con OpenGL? (No en la plataforma de pruebas sin pantalla, ni sin controlador.)"""
    if QGuiApplication.platformName() in ("offscreen", "minimal"):
        return False
    try:
        return QOpenGLContext().create()
    except Exception:
        log.exception("No se pudo comprobar OpenGL")
        return False


def create_view(prefer_gpu: bool = True) -> PrompterView | PrompterViewGL:
    """La vista con tarjeta gráfica si se puede; si no, la del procesador."""
    if prefer_gpu and gpu_available():
        log.info("Teleprompter: dibujo con la tarjeta gráfica (OpenGL)")
        return PrompterViewGL()
    log.info("Teleprompter: dibujo con el procesador")
    return PrompterView()