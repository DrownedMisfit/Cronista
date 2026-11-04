"""Detección de momentos destacados de una sesión.

Combina tres tipos de señal en una curva de "interés" con resolución de 1 segundo sobre la grabación:

  1. Lo que dijiste (siempre disponible, sin coste): cada nota suma alrededor de su timestamp; pesan más
     las marcadas (⭐🔥🎬…), las críticas/análisis/ideas para video, las largas y las que forman grupos de
     comentarios relacionados (densidad).
  2. Imagen de la grabación (opcional): cuánto cambia la imagen entre fotogramas clave. Solo se decodifican
     los fotogramas clave (skip_frame=NONKEY) a baja resolución → rápido incluso en grabaciones de horas.
  3. Sonido de la grabación (opcional): picos de volumen respecto al nivel habitual de la sesión
     (explosiones, música de jefe, momentos intensos).

El análisis de la grabación es pesado comparado con lo demás: se hace a petición (después de jugar),
nunca durante la partida. Los momentos se ordenan por puntuación y se guardan en la base de datos.
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)

MARKER_WEIGHT = {"important": 1.6, "use_in_video": 2.0, "video_idea": 1.6, "problem": 1.0, "liked": 1.0,
                 "develop": 0.8, "": 0.0}
FUNCTION_WEIGHT = {"crítica": 0.5, "análisis": 0.5, "opinión": 0.35, "comparación": 0.4, "idea para video": 0.8,
                   "observación": 0.2, "descripción": 0.15, "pensamiento": 0.2}
PRE_S, POST_S = 6.0, 4.0              # contexto alrededor de lo que dijiste
MIN_LEN_S, MAX_LEN_S = 6.0, 45.0
MERGE_GAP_S = 4.0


@dataclass
class MediaSignals:
    """Señales por segundo de la grabación (z-scores robustos; 0 = normal)."""
    duration_s: float
    visual: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    audio: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    keyframes: int = 0
    seconds_analyzed: float = 0.0


def robust_z(x: np.ndarray) -> np.ndarray:
    """(x - mediana) / (1,4826·MAD): insensible a que el juego sea en general ruidoso o tranquilo."""
    if x.size == 0:
        return x
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med))) * 1.4826
    if mad < 1e-9:
        mad = float(np.std(x)) or 1.0
    return ((x - med) / mad).astype(np.float32)


def smooth(x: np.ndarray, win: int) -> np.ndarray:
    win = min(win, x.size)          # grabaciones cortas: la ventana no puede superar la señal
    if win <= 1 or x.size == 0:
        return x
    k = np.ones(win, np.float32) / win
    return np.convolve(x, k, mode="same").astype(np.float32)


# ---- señales de la grabación --------------------------------------------------------------
def analyze_media(path: str, progress=None, cancelled=None, max_side: int = 96) -> MediaSignals:
    """Lee la grabación con PyAV: fotogramas clave (imagen) y audio a 8 kHz (volumen). Cancelable."""
    import av

    progress = progress or (lambda f, t="": None)
    cancelled = cancelled or (lambda: False)
    with av.open(path) as c:
        duration = float(c.duration / 1_000_000) if c.duration else 0.0
    n = max(1, int(math.ceil(duration)))
    visual = np.zeros(n, np.float32)
    audio_rms = np.zeros(n, np.float32)
    keyframes = 0

    # 1) imagen: solo fotogramas clave
    with av.open(path) as c:
        vs = next((s for s in c.streams.video), None)
        if vs is not None:
            vs.codec_context.skip_frame = "NONKEY"
            vs.thread_type = "AUTO"
            prev, prev_t = None, None
            for frame in c.decode(vs):
                if cancelled():
                    return MediaSignals(duration)
                t = float(frame.time or 0.0)
                img = frame.reformat(width=max_side, height=max(2, int(max_side * frame.height / max(1, frame.width))),
                                     format="gray").to_ndarray().astype(np.float32)
                if prev is not None:
                    diff = float(np.mean(np.abs(img - prev))) / 255.0
                    a, b = int(prev_t), min(n - 1, int(t))
                    visual[a:b + 1] = np.maximum(visual[a:b + 1], diff)   # cambio repartido en el intervalo
                prev, prev_t = img, t
                keyframes += 1
                if duration:
                    progress(0.5 * min(1.0, t / duration), f"Analizando imagen… {int(t // 60)} min")

    # 2) sonido: RMS por segundo (mono, 8 kHz)
    with av.open(path) as c:
        ast = next((s for s in c.streams.audio), None)
        if ast is not None:
            res = av.AudioResampler(format="s16", layout="mono", rate=8000)
            acc = np.zeros(n, np.float64)
            cnt = np.zeros(n, np.int64)
            for frame in c.decode(ast):
                if cancelled():
                    return MediaSignals(duration)
                t0 = float(frame.time or 0.0)
                for rf in res.resample(frame):
                    x = rf.to_ndarray().reshape(-1).astype(np.float64) / 32768.0
                    idx = np.minimum(n - 1, (t0 + np.arange(x.size) / 8000.0).astype(np.int64))
                    np.add.at(acc, idx, x * x)
                    np.add.at(cnt, idx, 1)
                    t0 += x.size / 8000.0
                if duration:
                    progress(0.5 + 0.5 * min(1.0, t0 / duration), f"Analizando sonido… {int(t0 // 60)} min")
            with np.errstate(invalid="ignore", divide="ignore"):
                rms = np.sqrt(np.where(cnt > 0, acc / np.maximum(cnt, 1), 0.0))
            audio_rms = (20 * np.log10(np.maximum(rms, 1e-5))).astype(np.float32)   # dBFS

    return MediaSignals(duration, visual=robust_z(smooth(visual, 3)), audio=robust_z(smooth(audio_rms, 3)),
                        keyframes=keyframes, seconds_analyzed=duration)


# ---- curva de interés -----------------------------------------------------------------------
def notes_curve(notes: list[dict], n: int, related: dict[int, list[int]] | None = None) -> tuple[np.ndarray, dict]:
    """Curva de interés por lo que dijiste + qué notas cubren cada segundo."""
    curve = np.zeros(n, np.float32)
    cover: dict[int, list[int]] = {}
    related = related or {}
    for note in notes:
        pos = note.get("position_ms")
        if pos is None:
            continue
        t = pos / 1000.0
        dur = max(1.0, (note.get("duration_ms") or 0) / 1000.0)
        w = 1.0
        w += MARKER_WEIGHT.get(note.get("marker") or "", 0.0)
        w += max((FUNCTION_WEIGHT.get(f, 0.0) for f in note.get("functions") or []), default=0.0)
        w += 0.5 if dur > 20 else 0.0
        w += 0.25 * min(4, len(related.get(note["id"], [])))           # forma parte de un tema recurrente
        voice = ((note.get("stt_info") or {}).get("voice") or {})
        if voice.get("match") is False and not voice.get("confirmed"):
            w *= 0.4                                                    # probablemente no eras tú
        a, b = max(0, int(t - PRE_S)), min(n - 1, int(t + dur + POST_S))
        if a > b:
            continue
        curve[a:b + 1] += w
        for s in range(a, b + 1):
            cover.setdefault(s, []).append(note["id"])
    # densidad: varias notas en pocos minutos refuerzan la zona
    curve = curve + 0.5 * smooth(curve, 120)
    return curve, cover


def detect_moments(notes: list[dict], duration_s: float, media: MediaSignals | None = None,
                   related: dict[int, list[int]] | None = None, max_moments: int | None = None) -> list[dict]:
    n = max(1, int(math.ceil(duration_s or 0)))
    if not notes and media is None:
        return []
    notes_by_id = {x["id"]: x for x in notes}
    curve, cover = notes_curve(notes, n, related)
    vis = aud = np.zeros(n, np.float32)
    if media is not None and media.visual.size:
        vis = np.clip(_fit(media.visual, n), 0, None)
    if media is not None and media.audio.size:
        aud = np.clip(_fit(media.audio, n), 0, None)
    # bordes: el inicio y el final de la grabación generan falsos picos (fundidos, suavizado)
    edge = min(3, n // 4)
    if edge:
        vis = vis.copy()
        aud = aud.copy()
        vis[:edge] = vis[-edge:] = 0
        aud[:edge] = aud[-edge:] = 0
    # lo que dijiste manda; imagen y sonido solo destacan picos claros (z > 2)
    media_part = 0.35 * np.clip(vis - 2.0, 0, 4) + 0.45 * np.clip(aud - 2.0, 0, 4)
    total = curve + media_part
    if not np.any(total > 0):
        return []
    # Dos familias de segmentos: lo comentado (umbral adaptativo sobre tus notas) y la acción sin comentarios
    # (picos claros de imagen/sonido). Así un momento intenso en el que no hablaste también aparece, aunque
    # puntúa menos que lo que comentaste.
    segs: list[tuple[int, int]] = []
    if np.any(curve > 0):
        positive = curve[curve > 0]
        thr = max(0.8, float(np.percentile(positive, 85)) * 0.6)
        segs += _segments(curve >= thr)
    if np.any(media_part > 0):
        segs += [s for s in _segments(media_part >= 0.45) if s[1] - s[0] + 1 >= 2]   # ≥2 s de pico claro
    segs = _merge(sorted(segs), MERGE_GAP_S)
    moments = []
    for a, b in segs:
        if b - a + 1 < 2:
            continue
        a2, b2 = _clamp_len(a, b, total, n)
        ids = sorted({i for s in range(a2, b2 + 1) for i in cover.get(s, [])},
                     key=lambda i: notes_by_id[i].get("position_ms") or 0)
        seg_notes = [notes_by_id[i] for i in ids]
        score = float(total[a2:b2 + 1].max() + 0.1 * total[a2:b2 + 1].mean())
        v_peak = float(vis[a2:b2 + 1].max()) if vis.size else 0.0
        a_peak = float(aud[a2:b2 + 1].max()) if aud.size else 0.0
        reasons, kind = _reasons(seg_notes, v_peak, a_peak, related or {})
        moments.append({"start_ms": a2 * 1000, "end_ms": (b2 + 1) * 1000, "score": round(score, 3), "kind": kind,
                        "title": _title(seg_notes, kind), "reasons": reasons, "note_ids": ids})
    moments.sort(key=lambda m: -m["score"])
    limit = max_moments or max(5, min(40, int(duration_s / 180) + 5))   # ~1 cada 3 min, entre 5 y 40
    top = moments[:limit]
    # puntuación relativa 0-100 para la interfaz
    if top:
        hi = top[0]["score"] or 1.0
        for m in top:
            m["score"] = round(100 * m["score"] / hi, 1)
    return sorted(top, key=lambda m: m["start_ms"])


def _fit(x: np.ndarray, n: int) -> np.ndarray:
    if x.size >= n:
        return x[:n]
    return np.pad(x, (0, n - x.size))


def _segments(mask: np.ndarray) -> list[tuple[int, int]]:
    segs, start = [], None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            segs.append((start, i - 1))
            start = None
    if start is not None:
        segs.append((start, len(mask) - 1))
    return segs


def _merge(segs: list[tuple[int, int]], gap: float) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in segs:
        if out and a - out[-1][1] <= gap:
            out[-1][1] = b
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _clamp_len(a: int, b: int, total: np.ndarray, n: int) -> tuple[int, int]:
    """Entre 6 y 45 s, centrado en el pico."""
    if b - a + 1 > MAX_LEN_S:
        peak = a + int(np.argmax(total[a:b + 1]))
        half = int(MAX_LEN_S // 2)
        a, b = max(a, peak - half), min(b, peak + half)
    if b - a + 1 < MIN_LEN_S:
        extra = int(MIN_LEN_S - (b - a + 1))
        a = max(0, a - extra // 2)
        b = min(n - 1, b + (extra - extra // 2))
    return a, b


def _reasons(notes: list[dict], v_peak: float, a_peak: float, related: dict) -> tuple[list[str], str]:
    reasons: list[str] = []
    if notes:
        marked = [n for n in notes if n.get("marker")]
        reasons.append(f"{len(notes)} comentario{'s' if len(notes) != 1 else ''} tuyo{'s' if len(notes) != 1 else ''}")
        if marked:
            reasons.append(f"{len(marked)} con marcador")
        topics = Counter(t for n in notes for t in (n.get("topics") or []))
        if topics:
            reasons.append("tema: " + ", ".join(t for t, _ in topics.most_common(2)))
        if any(related.get(n["id"]) for n in notes):
            reasons.append("relacionado con otros comentarios")
    if v_peak > 2.0:
        reasons.append("mucho cambio de imagen")
    if a_peak > 2.0:
        reasons.append("pico de sonido")
    has_media = v_peak > 2.0 or a_peak > 2.0
    kind = "comentado+acción" if notes and has_media else ("comentado" if notes else "acción")
    return reasons, kind


def _title(notes: list[dict], kind: str) -> str:
    if not notes:
        return "Momento de acción (sin comentarios)"
    best = max(notes, key=lambda n: (MARKER_WEIGHT.get(n.get("marker") or "", 0.0), len(n.get("text") or "")))
    text = (best.get("text") or "").strip()
    words = text.split()
    return " ".join(words[:12]) + ("…" if len(words) > 12 else "")


def analyze_session(store, session_id: int, *, use_media: bool = True, progress=None, cancelled=None) -> dict:
    """Detecta y guarda los momentos de una sesión. Devuelve {"message", "moments", "media": bool}."""
    import os
    progress = progress or (lambda f, t="": None)
    sess = store.get_session(session_id)
    if not sess:
        raise ValueError("Sesión no encontrada")
    notes = store.list_notes(session_id)
    duration = (sess.get("duration_ms") or 0) / 1000.0
    media = None
    path = sess.get("obs_path")
    if use_media and path and os.path.isfile(path):
        media = analyze_media(path, progress, cancelled)
        duration = media.duration_s or duration
    if cancelled and cancelled():
        return {"message": "Cancelado", "moments": []}
    if not duration:
        duration = max(((n.get("position_ms") or 0) + (n.get("duration_ms") or 0)) / 1000.0 for n in notes) + 30 \
            if notes else 0
    related = store.session_relations(session_id)
    moments = detect_moments(notes, duration, media, related)
    store.replace_moments(session_id, moments)
    progress(1.0, "Listo")
    src = "comentarios, imagen y sonido" if media else "tus comentarios"
    return {"message": f"{len(moments)} momentos detectados (a partir de {src})", "moments": moments,
            "media": media is not None}
