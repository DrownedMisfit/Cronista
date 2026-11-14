"""Configuración persistente (JSON) con valores por defecto seguros."""
from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from cronista.core.paths import settings_path

log = logging.getLogger(__name__)


@dataclass
class AudioSettings:
    device: str | None = None          # nombre del micrófono (None = predeterminado)
    noise_suppression: str = "auto"    # off | light | auto | strong
    denoise_for_stt: bool = False      # False = Whisper recibe audio casi original (prioridad: no perder palabras)
    vad_sensitivity: str = "medium"    # low | medium | high
    pre_roll_ms: int = 700             # margen antes del inicio de voz
    post_roll_ms: int = 400            # margen después del fin de voz
    end_silence_ms: int = 1500         # pausa máxima tolerada dentro de una frase
    min_speech_ms: int = 300           # frases más cortas se descartan
    max_utterance_s: int = 75          # corte suave para comentarios largos
    partial_every_s: float = 2.5       # frecuencia de transcripción parcial mientras hablo
    gain_db: float = 0.0


@dataclass
class STTSettings:
    # Motor en vivo: auto = Parakeet si está instalado (rápido en CPU, ~15x más que Whisper) | parakeet |
    # faster-whisper. Medido: Whisper tarda 1,3-2 s por segundo hablado en un Ryzen 7 5700X; Parakeet ~0,1 s.
    backend: str = "auto"
    # Segunda pasada: tras jugar (sin juego en primer plano), Whisper + glosario repasan las frases dichas en vivo
    # con Parakeet para afinar títulos en inglés. Requiere guardar el audio de cada frase hasta repasarla.
    second_pass: bool = True
    model: str = "auto"                # auto | tiny | base | small | medium | large-v3 | large-v3-turbo
    partial_model: str = "small"
    partials: str = "auto"             # auto | on | off
    device: str = "auto"               # auto | cpu | cuda
    compute_type: str = "auto"         # auto | int8 | int8_float16 | float16 | float32
    cpu_threads: int = 0               # 0 = automático (mitad de los núcleos físicos)
    language: str = "es"
    beam_size: int = 5
    idle_unload_min: int = 10          # descargar modelos tras N minutos sin usarlos (0 = nunca)
    # Glosario: títulos y términos (sobre todo en inglés) tal como deben escribirse. Formato de cada línea:
    #   Término correcto [= variante mal entendida, otra variante]
    # Además se añaden solos: el juego de la sesión, los juegos que ya registraste y términos de videojuegos.
    vocabulary: list[str] = field(default_factory=lambda: [
        "Rise of Iron: D-Day = Rise of Iron Di Day, Rise of Iron didey",
        "Rise of Iron", "Resident Evil", "Resident Evil 4", "Dark Souls", "Elden Ring", "Metal Gear Solid",
        "Halo", "Zelda", "Mario", "God of War", "Silent Hill", "The Last of Us", "Red Dead Redemption",
        "Grand Theft Auto", "Call of Duty", "Uncharted", "Lucas Dravek",
    ])
    fix_terms: bool = True             # corregir títulos/términos en inglés mal entendidos tras transcribir


@dataclass
class OBSSettings:
    auto_discover: bool = True         # leer host/puerto/contraseña del config de OBS
    host: str = "localhost"
    port: int = 4455
    password: str = ""
    sync_interval_s: float = 5.0
    reconnect_interval_s: float = 5.0
    auto_session: bool = False         # (modo en vivo) iniciar/detener sesión con la grabación de OBS


@dataclass
class HotkeySettings:
    note_key: str = "f8"               # mantener pulsado = captura forzada
    mode: str = "hybrid"               # manual | auto | hybrid
    toggle_mode: bool = False          # True = pulsar para empezar, pulsar para terminar


@dataclass
class PerformanceSettings:
    prioritize_game: bool = True       # bajar prioridad del proceso cuando hay un juego en primer plano
    low_priority_always: bool = False


@dataclass
class VoiceSettings:
    use_voice_id: bool = False         # requiere el modelo de voz y un perfil creado en "Configurar mi voz"
    profile_path: str = ""             # (reservado) el perfil vive en %APPDATA%\Cronista\voice\profile.json
    threshold: float = 0.0             # 0 = automático (calibrado con tu grabación); >0 = umbral manual
    non_match_action: str = "mark"     # mark = transcribir y marcar "otra voz" | skip = descartar sin transcribir


@dataclass
class LearningSettings:
    learn_style: bool = False
    learn_corrections: bool = False


@dataclass
class LLMSettings:
    """IA local (llama.cpp llama-server + modelo GGUF). Todo en este equipo; nada sale a internet."""
    enabled: bool = True
    runtime_path: str = ""             # vacío = tools/llama/llama-server.exe
    model_path: str = ""               # vacío = primer .gguf de models/llm
    device: str = "gpu"                # gpu (Vulkan) | cpu
    gpu_layers: int = 99               # capas en GPU cuando device=gpu
    context_size: int = 8192
    threads: int = 0                   # 0 = automático (mitad de núcleos físicos)
    during_game: str = "defer"         # defer = esperar a que el juego no esté en primer plano | cpu | gpu
    idle_unload_min: int = 5           # cerrar el servidor tras N minutos sin trabajo (0 = nunca)
    auto_classify: bool = True         # refinar con IA la clasificación de cada nota nueva
    port: int = 0                      # 0 = puerto libre automático (solo 127.0.0.1)


@dataclass
class UISettings:
    start_minimized: bool = False
    close_to_tray: bool = False        # False = cerrar la ventana cierra la aplicación por completo
    show_context_panel: bool = True
    show_sessions_panel: bool = True
    theme: str = "dark"


@dataclass
class GeneralSettings:
    live_mode: bool = False            # escucha en vivo (micrófono + OBS). Por defecto apagada: el flujo principal es
                                       # transcribir la grabación después (OBS graba tu voz en la pista 2)
    listening_on_start: bool = False
    listen_without_session: bool = False   # False = el micrófono solo se abre durante una sesión
    auto_manual_session: bool = False      # hotkey sin sesión activa → crea una sesión manual (evita capturas accidentales al pulsar F8 en otra app)
    default_game: str = ""
    player_command: str = ""           # ruta a mpv/vlc (vacío = autodetectar)
    export_dir: str = ""
    preload_model: bool = False        # cargar el modelo de transcripción al arrancar


@dataclass
class EditorSettings:
    """Fase 3: integración con el editor (Reclaimer) y clips."""
    reclaimer_dir: str = ""            # vacío = autodetectar (F:\Reclaimer, D:\EditorIA…)
    project_dir: str = ""              # dónde guardar los proyectos .eia generados (vacío = junto a la grabación)
    clip_dir: str = ""                 # dónde guardar los clips (vacío = carpeta "Clips Cronista" junto a la grabación)
    clip_pre_s: float = 5.0            # segundos antes de la nota (contexto de lo que estabas viendo)
    clip_post_s: float = 3.0           # segundos después del final de lo que dijiste
    clip_mode: str = "copy"            # copy = rápido, sin recodificar (puede empezar unos segundos antes) | exact
    marker_notes: bool = True          # poner un marcador por nota en el proyecto de Reclaimer
    favorites_notes: bool = True       # guardar cada nota como "momento favorito" en Reclaimer
    include_moments: bool = True       # incluir los momentos detectados (marcadores de otro color)


@dataclass
class AISettings:
    provider: str = "none"             # none | openai | anthropic
    base_url: str = ""                 # para compatibles con OpenAI (LM Studio, Ollama, OpenRouter...)
    api_key: str = ""
    model: str = ""
    chatbot_site: str = "ChatGPT"      # sitio a abrir al pulsar "Abrir chatbot"


@dataclass
class TeleprompterSettings:
    """Teleprompter (función principal desde la 0.2). El texto se guarda aparte, en teleprompter.txt."""
    font_size: int = 56                # puntos
    font_family: str = "Segoe UI"      # una de teleprompter.fonts.FONT_OPTIONS
    letter_spacing: int = 0            # % extra de separación entre letras (0-50)
    speed: int = 10                    # 1-30; 10 ≈ un tercio de línea por segundo (ritmo de habla normal)
    mirror: bool = False               # reflejar en horizontal para el cristal de un teleprompter
    center_text: bool = False          # líneas centradas a lo ancho (False = alineadas a la izquierda)
    phrase_breaks: bool = False        # mostrar las líneas cortadas por frases (no cambia el texto guardado)
    guide_center: bool = False         # línea de lectura a media altura (False = a un tercio)
    show_progress_bar: bool = True     # barra de avance a la derecha
    view_angle: int = 0                # ángulo de visión (grados): + = miras desde arriba, − = desde abajo
    fps: int = 0                       # fluidez al desplazar: 0 = automático (los Hz del monitor si el equipo da)
    use_gpu: bool = True               # dibujar con la tarjeta gráfica (OpenGL) si se puede; False = procesador
    bookmark_pos: int = -1             # bandera de la última pausa: posición en el texto (-1 = ninguna)
    bookmarks: list[int] = field(default_factory=list)   # banderas puestas con un clic en el margen
    bookmark_crc: int = 0              # huella (zlib.crc32) del texto al que pertenecen las banderas
    margin_pct: int = 10               # margen a cada lado (% del ancho)
    smart_mic: str = ""                # micrófono del modo Smart ("" = el predeterminado de Windows)
    smart: bool = False                # modo Smart: el texto sigue la voz (reconocimiento local con Vosk)
    countdown_s: int = 3               # cuenta atrás (segundos) antes de empezar a leer; 0 = empieza enseguida
    last_file: str = ""                # último archivo abierto (para empezar el diálogo en su carpeta)


@dataclass
class Settings:
    audio: AudioSettings = field(default_factory=AudioSettings)
    stt: STTSettings = field(default_factory=STTSettings)
    obs: OBSSettings = field(default_factory=OBSSettings)
    hotkey: HotkeySettings = field(default_factory=HotkeySettings)
    performance: PerformanceSettings = field(default_factory=PerformanceSettings)
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    learning: LearningSettings = field(default_factory=LearningSettings)
    ui: UISettings = field(default_factory=UISettings)
    general: GeneralSettings = field(default_factory=GeneralSettings)
    ai: AISettings = field(default_factory=AISettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    editor: EditorSettings = field(default_factory=EditorSettings)
    teleprompter: TeleprompterSettings = field(default_factory=TeleprompterSettings)
    version: int = 2

    # ---- persistencia -------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or settings_path()
        s = cls()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                _migrate(raw)
                _apply(s, raw)
            except Exception:
                log.exception("No se pudo leer settings.json; se usan valores por defecto")
        return s

    def save(self, path: Path | None = None) -> None:
        path = path or settings_path()
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def copy(self) -> "Settings":
        s = Settings()
        _apply(s, self.to_dict())
        return s


def _apply(obj: Any, raw: dict[str, Any]) -> None:
    """Copia recursivamente solo las claves conocidas (tolerante a versiones viejas)."""
    for f in fields(obj):
        if f.name not in raw:
            continue
        cur = getattr(obj, f.name)
        val = raw[f.name]
        if is_dataclass(cur) and isinstance(val, dict):
            _apply(cur, val)
        else:
            setattr(obj, f.name, val)


def _migrate(raw: dict[str, Any]) -> None:
    """Adapta configuraciones guardadas por versiones anteriores."""
    version = int(raw.get("version", 1) or 1)
    if version < 2:
        # v1 tenía un umbral fijo de voz de 0,55 (demasiado estricto); v2 lo calibra con tu grabación.
        voice = raw.get("voice") or {}
        if voice.get("threshold") == 0.55:
            voice["threshold"] = 0.0
        raw["version"] = 2
