"""Orquestador: crea los servicios, los conecta por el bus y aplica las políticas
(cuándo se abre el micrófono, qué hace el hotkey, cómo se aplican cambios de
configuración). La UI solo habla con esta clase y con el bus.
"""
from __future__ import annotations

import logging
import threading

from cronista.ai.worker import AnalysisWorker
from cronista.audio.pipeline import AudioPipeline
from cronista.context.engine import ContextEngine
from cronista.core import hardware
from cronista.core.events import EventBus, Topics
from cronista.core.settings import Settings
from cronista.hotkey.manager import HotkeyManager
from cronista.jobs import JobRunner
from cronista.learning.personalizer import Personalizer
from cronista.notes.manager import NoteManager
from cronista.notes.store import Store
from cronista.obs.client import OBSClient
from cronista.perf.monitor import PerfMonitor
from cronista.session.manager import SessionManager
from cronista.stt.engine import STTEngine
from cronista.voice.speaker import VoiceGate
from cronista.stt.refiner import SecondPass, cleanup_orphans

log = logging.getLogger(__name__)


class App:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bus = EventBus()
        self.hw = hardware.detect()
        log.info("Hardware: %s", self.hw.describe())
        self.store = Store()
        self.personalizer = Personalizer(self.store, settings)
        self.context = ContextEngine(known_games=self._known_games())
        self.sessions = SessionManager(self.bus, settings, self.store)
        self.notes = NoteManager(self.bus, settings, self.store, self.sessions, self.context)
        self.stt = STTEngine(self.bus, settings, self.hw)
        self.stt.extra_vocab = self.personalizer.style_vocabulary   # vacío salvo "Aprender de mi estilo"
        self.voice_gate = VoiceGate(settings)
        self.stt.voice_gate = self.voice_gate      # no hace nada hasta que actives la identificación de voz
        # glosario bilingüe: el juego de la sesión y los juegos/referencias que ya conoces
        self.stt.context_game = lambda: (self.sessions.current or {}).get("game") or self.sessions.game_hint or ""
        self.stt.context_terms = self._known_games
        self.jobs = JobRunner(self.bus)     # tareas largas pedidas por el usuario (fase 3)
        self.second_pass = SecondPass(self.bus, settings, self.store, self.stt, self.hw)
        self.ai = AnalysisWorker(self.bus, settings, self.store, self.sessions, self.context, self.personalizer)
        self.audio = AudioPipeline(self.bus, settings)
        self.obs = OBSClient(self.bus, settings)
        self.hotkey = HotkeyManager(self.bus, settings)
        self.perf = PerfMonitor(self.bus, settings)
        self.listening_enabled = bool(settings.general.listening_on_start)
        self._lock = threading.RLock()
        self._started = False
        self.bus.subscribe(Topics.HOTKEY_DOWN, self._on_hotkey_down)
        self.bus.subscribe(Topics.HOTKEY_UP, self._on_hotkey_up)
        self.bus.subscribe(Topics.SESSION_STARTED, lambda e: self._refresh_audio())
        self.bus.subscribe(Topics.SESSION_STOPPED, lambda e: self._refresh_audio())
        self.bus.subscribe(Topics.SESSION_STARTED, self._on_session_started)
        self.bus.subscribe(Topics.SESSION_STOPPED, lambda e: self._refresh_learning())
        # al terminar una sesión: indexar sus notas para la búsqueda (rápido, en segundo plano)
        self.bus.subscribe(Topics.SESSION_STOPPED, lambda e: self._index_quietly())
        self.bus.subscribe(Topics.AUDIO_ERROR, lambda e: self.bus.publish(Topics.STATUS, level="error", message=e.message))

    # ---- ciclo de vida ----------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        self.audio.auto_detect = self.settings.hotkey.mode != "manual"
        self.stt.start()
        self.ai.start()
        self.jobs.start()
        self.second_pass.start()
        try:
            cleanup_orphans(self.store)
        except Exception:
            log.debug("Limpieza de audio pendiente omitida", exc_info=True)
        if self.settings.general.preload_model:
            self.stt.preload()
        self.perf.start()                   # detecta juegos: la IA local espera mientras juegas
        if self.settings.general.live_mode:
            # modo en vivo (opcional): OBS + micrófono + hotkey. Apagado por defecto: Cronista no toca el
            # micrófono ni OBS mientras juegas; transcribe la grabación después.
            self.obs.start()
            self.hotkey.start()
            self._refresh_audio()
        try:
            from cronista.core import autostart
            autostart.remove_legacy_entries()   # Cronista solo se abre a mano: limpiar restos de versiones viejas
        except Exception as e:
            log.debug("Revisión del inicio de Windows omitida: %s", e)
        self.bus.publish(Topics.MODE_CHANGED, mode=self.settings.hotkey.mode)
        self.bus.publish(Topics.LISTENING_CHANGED, enabled=self.listening_enabled)

    def shutdown(self) -> None:
        log.info("Cerrando servicios…")
        try:
            self.hotkey.stop()
            self.audio.stop()
            if self.sessions.active and self.sessions.current and self.sessions.current["mode"] == "manual":
                self.sessions.stop()
            self.obs.stop()
            self.perf.stop()
            self.ai.stop()      # cierra también el servidor de IA local si estaba cargado
            self.jobs.stop()
            self.second_pass.stop()
            self.stt.stop()
        finally:
            self.settings.save()
            self.store.close()

    # ---- aprendizaje y contexto -------------------------------------------------
    def _known_games(self) -> list[str]:
        try:
            return self.personalizer.known_games()
        except Exception:
            log.debug("known_games falló", exc_info=True)
            return self.store.list_games()

    def _on_session_started(self, ev) -> None:
        self.context.set_known_games(self._known_games())
        self.context.reset(ev.session.get("game", ""))

    def _refresh_learning(self) -> None:
        """Al terminar una sesión: recalcular vocabulario personal (solo si el aprendizaje está activo)."""
        try:
            self.personalizer.invalidate()
            if self.settings.learning.learn_style:
                self.personalizer.style_vocabulary(refresh=True)
        except Exception:
            log.debug("No se pudo actualizar el aprendizaje", exc_info=True)

    def _index_quietly(self) -> None:
        try:
            from cronista.search.embedder import model_available
            if model_available():
                self.index_search()
        except Exception:
            log.debug("Indexado omitido", exc_info=True)

    def clear_learning(self) -> None:
        self.personalizer.clear()

    # ---- fase 3: editor, clips, momentos, búsqueda y guiones (tareas en segundo plano) ----------------
    # Todas se ejecutan en JobRunner (un hilo, una a la vez) y publican Topics.JOB_PROGRESS / JOB_DONE
    # con job = "reclaimer" | "clips" | "moments" | "index" | "script".
    def transcribe_recording(self, path: str, game: str, track: int | None = None, quality: str = "rápida") -> None:
        """Flujo principal: transcribir la pista de tu voz de una grabación (o un audio suelto)."""
        from cronista.transcribe.recording import transcribe_recording

        def job(progress, cancelled):
            return transcribe_recording(self, path, game=game, track=track, quality=quality,
                                        progress=progress, cancelled=cancelled)

        self.jobs.submit("transcribe", job, "Transcribir grabación")

    def export_reclaimer(self, session_id: int, path: str | None = None, open_after: bool = False) -> None:
        from cronista.editor import reclaimer

        def job(progress, cancelled):
            progress(0.1, "Creando el proyecto de Reclaimer…")
            out = reclaimer.export_session(self.store, session_id, self.settings, path=path)
            if open_after:
                progress(0.9, "Abriendo Reclaimer…")
                reclaimer.open_in_reclaimer(out, self.settings)
            return {"message": f"Proyecto creado: {out}", "path": str(out), "opened": open_after}

        self.jobs.submit("reclaimer", job, "Proyecto de Reclaimer")

    def export_clips(self, session_id: int, note_ids: list[int] | None = None, only_marked: bool = False,
                     merge: bool = False) -> None:
        from cronista.clips.exporter import export_note_clips

        def job(progress, cancelled):
            return export_note_clips(self.store, session_id, self.settings, note_ids=note_ids,
                                     only_marked=only_marked, merge=merge, progress=progress, cancelled=cancelled)

        self.jobs.submit("clips", job, "Clips de notas")

    def analyze_moments(self, session_id: int, use_media: bool = True) -> None:
        from cronista.moments.detector import analyze_session

        def job(progress, cancelled):
            return analyze_session(self.store, session_id, use_media=use_media, progress=progress,
                                   cancelled=cancelled)

        self.jobs.submit("moments", job, "Momentos destacados")

    @property
    def search(self):
        """Motor de búsqueda semántica (se crea al primer uso)."""
        if getattr(self, "_search", None) is None:
            from cronista.search.engine import SearchEngine
            self._search = SearchEngine(self.store)
        return self._search

    def index_search(self) -> None:
        """Indexa las notas nuevas o cambiadas (incremental; ~0,1 s por nota en CPU)."""
        def job(progress, cancelled):
            return self.search.index(progress=progress, cancelled=cancelled)

        if not self.jobs.busy("index"):
            self.jobs.submit("index", job, "Índice de búsqueda")

    def generate_script(self, game: str, session_ids: list[int] | None = None, *, length: str = "medio",
                        approach: str = "análisis", extra: str = "", only_marked: bool = False,
                        which: str = "local") -> None:
        """Genera un esqueleto de guion con las notas del juego (todas las sesiones o las indicadas)."""
        from cronista.ai import scripts

        def job(progress, cancelled):
            notes = self.store.all_notes(game)
            if session_ids:
                notes = [n for n in notes if n["session_id"] in session_ids]
            prepared = scripts.prepare_notes(notes, only_marked=only_marked)
            if not prepared:
                raise ValueError("No hay notas que usar con esos filtros.")
            progress(0.05, "Preparando la IA…")
            llm = self.ai._prepare_llm(which)
            script = scripts.generate_script(llm, game, prepared, length=length, approach=approach, extra=extra,
                                             progress=progress)
            sids = sorted({n["session_id"] for n in prepared})
            rec = scripts.script_record(script, prepared, sids, which,
                                        {"length": length, "approach": approach, "extra": extra,
                                         "only_marked": only_marked})
            rec["id"] = self.store.save_script(rec)
            return {"message": f"Guion listo: {rec['title']}", "script_id": rec["id"], "markdown": rec["markdown"]}

        self.jobs.submit("script", job, "Guion")

    # ---- políticas -------------------------------------------------------------
    def _should_listen(self) -> bool:
        return (self.settings.general.live_mode and self.listening_enabled
                and (self.settings.general.listen_without_session or self.sessions.active))

    def _refresh_audio(self) -> None:
        want = self._should_listen()
        if want and not self.audio.listening:
            self.audio.start()
        elif not want and self.audio.listening:
            self.audio.stop()

    def set_listening(self, enabled: bool) -> None:
        """Interruptor del micrófono. OFF = el dispositivo se cierra de inmediato."""
        self.listening_enabled = bool(enabled)
        self.settings.general.listening_on_start = self.listening_enabled
        self._refresh_audio()
        self.bus.publish(Topics.LISTENING_CHANGED, enabled=self.listening_enabled)

    def stop_listening_completely(self) -> None:
        self.set_listening(False)
        self.audio.stop()
        self.stt.unload()

    def set_mode(self, mode: str) -> None:
        self.settings.hotkey.mode = mode
        self.audio.auto_detect = mode != "manual"
        self.settings.save()
        self.bus.publish(Topics.MODE_CHANGED, mode=mode)

    # ---- sesiones -----------------------------------------------------------------
    def start_manual_session(self, game: str = "") -> dict:
        return self.sessions.start(mode="manual", game=game)

    def stop_session(self) -> dict | None:
        return self.sessions.stop()

    def toggle_session(self) -> None:
        if self.sessions.active:
            self.stop_session()
        else:
            self.start_manual_session()

    # ---- hotkey -------------------------------------------------------------------
    def _on_hotkey_down(self, ev) -> None:
        if self.settings.hotkey.mode == "auto":
            return
        if not self.sessions.active:
            if not self.settings.general.auto_manual_session:
                self.bus.publish(Topics.STATUS, level="warn", message="No hay sesión activa.")
                return
            self.sessions.start(mode="manual")
        if not self.audio.listening:
            if not self.listening_enabled:
                self.bus.publish(Topics.STATUS, level="warn", message="El micrófono está apagado.")
                return
            self.audio.start()
        self.audio.set_forced(True)

    def _on_hotkey_up(self, ev) -> None:
        self.audio.set_forced(False)

    # ---- configuración -----------------------------------------------------------
    def apply_settings(self, new: Settings) -> None:
        old = self.settings.copy()
        # Copiamos campo a campo para que todos los servicios (que guardan la referencia) vean los cambios.
        for name in ("audio", "stt", "obs", "hotkey", "performance", "voice", "learning", "ui", "general", "ai", "llm", "editor"):
            setattr(self.settings, name, getattr(new, name))
        self.settings.save()
        if _d(old.audio) != _d(self.settings.audio) and self.audio.listening:
            self.audio.restart()
        if _d(old.stt) != _d(self.settings.stt):
            self.stt.reload_models()
        if _d(old.obs) != _d(self.settings.obs):
            self.obs.reconnect_now()
        if _d(old.hotkey) != _d(self.settings.hotkey):
            self.hotkey.start()
            self.audio.auto_detect = self.settings.hotkey.mode != "manual"
            self.bus.publish(Topics.MODE_CHANGED, mode=self.settings.hotkey.mode)
        if _d(old.general) != _d(self.settings.general):
            self._refresh_audio()
        self.bus.publish(Topics.SETTINGS_CHANGED, settings=self.settings)


def _d(obj) -> dict:
    import dataclasses
    return dataclasses.asdict(obj)
