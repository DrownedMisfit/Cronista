"""Transcriptor de grabaciones: el flujo principal de Cronista.

Mientras juegas, Cronista no hace nada: OBS graba tu voz en una pista de audio aparte (pista 2 = micrófono D1,
pista 3 = juego, pista 1 = mezcla). Después le das el archivo y:

  1. detecta la pista de tu voz (nombre de pista o, en grabaciones de OBS con varias pistas, la segunda);
  2. la extrae a 16 kHz mono con ffmpeg;
  3. separa las frases con Silero VAD (sherpa-onnx, C++), uniendo pausas cortas para no cortar ideas;
  4. transcribe cada frase con Parakeet + glosario bilingüe (modo «precisa»: además Whisper, aceptado solo si
     coincide razonablemente con Parakeet);
  5. crea una sesión con las notas en su segundo exacto de la grabación (mismo archivo = misma línea de tiempo).

También acepta un audio suelto de tu voz (.wav, .mp3, .m4a, .flac…).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from cronista.core import ffmpeg as ff

log = logging.getLogger(__name__)

SR = 16000
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
VIDEO_EXT = {".mkv", ".mp4", ".mov", ".flv", ".ts", ".avi", ".webm", ".m4v"}
MERGE_GAP_S = 1.2        # pausas más cortas que esto quedan dentro de la misma nota
MAX_NOTE_S = 45.0        # una nota no se alarga más (se corta en la siguiente pausa)
PAD_S = 0.25             # margen antes/después de cada frase (no comerse sílabas)


@dataclass
class AudioTrack:
    index: int            # índice entre las pistas de audio (0 = primera)
    title: str
    channels: int
    codec: str

    def label(self) -> str:
        name = self.title or f"Pista {self.index + 1}"
        return f"{name} ({self.channels} canal{'es' if self.channels != 1 else ''}, {self.codec})"


def audio_tracks(path: str) -> list[AudioTrack]:
    r = ff.run([ff.ffprobe(), "-v", "error", "-print_format", "json", "-show_streams", "-select_streams", "a", path],
               timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-300:] or "ffprobe falló")
    streams = json.loads(r.stdout or b"{}").get("streams", [])
    out = []
    for i, s in enumerate(streams):
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        title = tags.get("title") or tags.get("handler_name", "")
        if title.lower() in ("soundhandler", "sound handler", "audio", "obs audio handler"):   # nombres genéricos
            title = ""
        out.append(AudioTrack(i, title, int(s.get("channels") or 0), s.get("codec_name", "")))
    return out


def pick_voice_track(tracks: list[AudioTrack]) -> tuple[int, str]:
    """(índice, explicación). Prioridad: pista con nombre de voz/micrófono; si hay 2+ pistas, la 2.ª (OBS)."""
    if not tracks:
        raise ValueError("El archivo no tiene audio.")
    for t in tracks:
        if any(k in t.title.lower() for k in ("voz", "mic", "voice", "d1")):
            return t.index, f"pista «{t.title}»"
    if len(tracks) >= 2:
        return 1, "pista 2 (en tus grabaciones de OBS, el micrófono D1)"
    return 0, "única pista (tu voz va mezclada con el juego: la transcripción puede ser peor)"


def extract_voice(path: str, track: int, dst: str, progress=None, cancelled=None) -> float:
    """Extrae una pista de audio a WAV 16 kHz mono. Devuelve la duración en segundos."""
    duration = ff.probe(path).get("duration", 0.0) or 0.0
    args = [ff.ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-y", "-i", path,
            "-map", f"0:a:{track}", "-vn", "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le",
            "-progress", "pipe:1", dst]
    proc = ff.popen(args)
    for line in proc.stdout:
        if cancelled and cancelled():
            proc.kill()
            proc.wait()
            raise InterruptedError
        if line.startswith("out_time_us=") and duration and progress:
            try:
                progress(min(1.0, int(line.split("=")[1]) / 1e6 / duration))
            except ValueError:
                pass
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.read()[-400:] or "ffmpeg falló al extraer el audio")
    return duration


def speech_segments(audio: np.ndarray, vad_model: str, threshold: float = 0.4, progress=None,
                    cancelled=None) -> list[tuple[float, float]]:
    """Tramos con voz (segundos), con pausas cortas unidas y margen a los lados."""
    import sherpa_onnx
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = vad_model
    cfg.silero_vad.threshold = threshold              # sensible: mejor incluir ruido que perder palabras
    cfg.silero_vad.min_silence_duration = 0.35
    cfg.silero_vad.min_speech_duration = 0.25
    cfg.silero_vad.max_speech_duration = 30.0
    cfg.silero_vad.window_size = 512
    cfg.sample_rate = SR
    cfg.num_threads = 1
    vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=120)
    raw: list[tuple[float, float]] = []
    # Ventana a ventana (512 muestras): con trozos grandes, sherpa-onnx devuelve posiciones desplazadas varios
    # segundos (medido). A esta granularidad sigue siendo rápido: ~27 s para 2 h de audio.
    step = 512
    report_every = 512 * 1000
    for i in range(0, len(audio), step):
        vad.accept_waveform(audio[i:i + step])
        while not vad.empty():
            seg = vad.front
            raw.append((seg.start / SR, (seg.start + len(seg.samples)) / SR))
            vad.pop()
        if i % report_every == 0:
            if cancelled and cancelled():
                raise InterruptedError
            if progress:
                progress(min(1.0, (i + step) / max(1, len(audio))))
    vad.flush()
    while not vad.empty():
        seg = vad.front
        raw.append((seg.start / SR, (seg.start + len(seg.samples)) / SR))
        vad.pop()
    return merge_segments(raw, len(audio) / SR)


def merge_segments(raw: list[tuple[float, float]], total_s: float, gap: float = MERGE_GAP_S,
                   max_len: float = MAX_NOTE_S, pad: float = PAD_S) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(raw):
        if out and a - out[-1][1] <= gap and b - out[-1][0] <= max_len:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(max(0.0, a - pad), min(total_s, b + pad)) for a, b in out if b - a >= 0.3]


def _dur(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    h, rem = divmod(seconds, 3600)
    return f"{h} h {rem // 60} min" if h else f"{rem // 60} min {rem % 60} s"


def transcribe_recording(app, path: str, *, game: str, track: int | None = None, quality: str = "rápida",
                         progress=None, cancelled=None) -> dict:
    """Trabajo completo (para JobRunner). Devuelve {"message", "session_id", "notes", "track_info"}."""
    import soundfile as sf
    from cronista.core.paths import models_dir
    from cronista.notes.manager import is_relevant
    from cronista.stt.engine import looks_like_hallucination
    from cronista.stt.parakeet import ParakeetBackend, available as parakeet_ok
    from cronista.stt.refiner import MIN_SIMILARITY, similarity

    progress = progress or (lambda f, t="": None)
    cancelled = cancelled or (lambda: False)
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"No existe: {path}")
    is_video = src.suffix.lower() in VIDEO_EXT
    tracks = audio_tracks(str(src))
    if track is None:
        track, why = pick_voice_track(tracks)
    else:
        why = f"pista {track + 1} elegida por ti"
    t_start = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="cronista_tr_") as tmp:
        wav = os.path.join(tmp, "voz.wav")
        progress(0.0, f"Extrayendo tu voz ({why})…")
        duration = extract_voice(str(src), track, wav, lambda f: progress(0.08 * f, "Extrayendo tu voz…"), cancelled)
        audio, _ = sf.read(wav, dtype="float32")

    progress(0.08, "Buscando las frases…")
    vad_model = str(models_dir() / "silero_vad.onnx")
    segs = speech_segments(audio, vad_model, progress=lambda f: progress(0.08 + 0.07 * f, "Buscando las frases…"),
                           cancelled=cancelled)
    speech_s = sum(b - a for a, b in segs)
    if not segs:
        raise ValueError("No se encontró voz en la pista elegida. ¿Es la pista del micrófono?")

    stt = app.stt
    glossary = stt.glossary()
    if not parakeet_ok() and quality == "rápida":
        quality = "whisper"
    live = ParakeetBackend(cpu_threads=max(2, stt._threads())) if parakeet_ok() else None
    whisper = None
    if quality in ("precisa", "whisper"):
        from cronista.stt.engine import FasterWhisperBackend
        s = app.settings.stt
        whisper = FasterWhisperBackend(stt.model_name, s.device, s.compute_type, max(2, stt._threads()), s.language)
    prompt, _ = glossary.prompt(game)

    # sesión: misma línea de tiempo que el archivo
    try:
        started = datetime.fromtimestamp(src.stat().st_mtime - duration).isoformat(timespec="seconds")
    except Exception:
        started = datetime.now().isoformat(timespec="seconds")
    sess = {"game": game or "Sin título", "title": f"{game or 'Sin título'} — {src.name}", "started_at": started,
            "ended_at": datetime.now().isoformat(timespec="seconds"), "duration_ms": int(duration * 1000),
            "obs_path": str(src) if is_video else None, "mode": "file",
            "meta": {"source_file": str(src), "voice_track": track, "track_info": why, "quality": quality}}
    sess["id"] = app.store.create_session(sess)
    app.context.reset(sess["game"])

    created, skipped = 0, 0
    for i, (a, b) in enumerate(segs):
        if cancelled():
            break
        chunk = audio[int(a * SR):int(b * SR)]
        info: dict = {"file": True, "segment": [round(a, 2), round(b, 2)]}
        text = ""
        if live is not None:
            text, linfo = live.transcribe(chunk)
            info["backend"] = "parakeet"
        if whisper is not None:
            wtext, _ = whisper.transcribe(chunk, initial_prompt=prompt)
            wtext = "" if looks_like_hallucination(wtext) else wtext
            if quality == "whisper" or not text:
                text, info["backend"] = wtext, "whisper"
            elif wtext and similarity(text, wtext) >= MIN_SIMILARITY:
                info["live_text"], info["backend"] = text, "parakeet+whisper"
                text = wtext
        fixed, changes = glossary.fix(text, game=game)
        if changes:
            info["glossary"], info["text_before_glossary"] = changes, text
        ok, _reason = is_relevant(fixed)
        if ok and not looks_like_hallucination(fixed):
            app.notes.create_from_transcript(sess, int(a * 1000), int((b - a) * 1000), fixed, info)
            created += 1
        else:
            skipped += 1
        progress(0.15 + 0.85 * (i + 1) / len(segs), f"Transcribiendo frase {i + 1} de {len(segs)}…")

    elapsed = time.monotonic() - t_start
    app.store.update_session(sess["id"], meta=dict(sess["meta"], notes=created, skipped=skipped,
                                                   speech_seconds=round(speech_s), seconds=round(elapsed)))
    msg = (f"{created} notas de {src.name} ({_dur(duration)} de grabación, {_dur(speech_s)} hablando) "
           f"en {_dur(elapsed)}")
    return {"message": "Cancelado" if cancelled() else msg, "session_id": sess["id"], "notes": created,
            "skipped": skipped, "track_info": why, "duration_s": duration, "speech_s": speech_s}
