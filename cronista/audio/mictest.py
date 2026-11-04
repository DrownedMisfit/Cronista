"""Prueba de micrófono: graba unos segundos, mide ruido de fondo, nivel de voz,
relación señal/ruido y ruido constante, y recomienda una configuración.
Es bloqueante: llamar desde un hilo de trabajo (la UI lo hace así)."""
from __future__ import annotations

import time
from typing import Callable

import numpy as np

from cronista.audio import dsp
from cronista.audio.capture import MicCapture, resolve_device
from cronista.audio.vad import build_vad


def run_mic_test(device_name: str | None, seconds: float = 6.0,
                 progress: Callable[[float, float], None] | None = None) -> dict:
    dev = resolve_device(device_name)
    if dev is None:
        return {"ok": False, "error": "No se encontró ningún micrófono"}
    cap = MicCapture(dev)
    try:
        rate = cap.start()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    res = dsp.StreamResampler(rate, 16000)
    vad = build_vad(16000)
    levels: list[float] = []
    probs: list[float] = []
    acc = np.zeros(0, dtype=np.float32)
    t0 = time.monotonic()
    peak = -120.0
    spectrum_acc = None
    n_spec = 0
    try:
        while time.monotonic() - t0 < seconds:
            try:
                blk = cap.queue.get(timeout=0.5)
            except Exception:
                continue
            x = blk.samples
            lvl = dsp.rms_db(x)
            peak = max(peak, dsp.peak_db(x))
            acc = np.concatenate([acc, res.process(x)])
            while len(acc) >= 512:
                c, acc = acc[:512], acc[512:]
                p = vad.prob(c)
                probs.append(p)
                levels.append(dsp.rms_db(c))
                if p < 0.2 and len(c) == 512:
                    spec = np.abs(np.fft.rfft(c * np.hanning(512))) ** 2
                    spectrum_acc = spec if spectrum_acc is None else spectrum_acc + spec
                    n_spec += 1
            if progress:
                progress(time.monotonic() - t0, lvl)
    finally:
        cap.stop()
    if not levels:
        return {"ok": False, "error": "No se recibió audio del micrófono"}
    lv = np.array(levels)
    pr = np.array(probs)
    noise = float(np.percentile(lv[pr < 0.3], 50)) if np.any(pr < 0.3) else float(np.percentile(lv, 20))
    speech = float(np.percentile(lv[pr > 0.6], 75)) if np.any(pr > 0.6) else float(np.percentile(lv, 95))
    snr = speech - noise
    speech_ratio = float(np.mean(pr > 0.5))
    # ruido constante: energía concentrada en pocas bandas en los silencios (tonal) o nivel alto y estable
    tonal = 0.0
    if spectrum_acc is not None and n_spec > 0:
        s = spectrum_acc / n_spec
        s = s / (s.sum() + 1e-12)
        top = np.sort(s)[-8:].sum()
        tonal = float(top)
    stable = float(np.std(lv[pr < 0.3])) if np.sum(pr < 0.3) > 5 else 0.0
    constant_noise = noise > -55 and stable < 4.0
    rec: dict[str, str] = {}
    notes: list[str] = []
    if snr >= 25:
        rec["noise_suppression"] = "light"
        rec["vad_sensitivity"] = "medium"
        notes.append("Buena relación señal/ruido: filtrado suave para no perder palabras.")
    elif snr >= 12:
        rec["noise_suppression"] = "auto"
        rec["vad_sensitivity"] = "medium"
        notes.append("Ruido moderado: supresión automática.")
    else:
        rec["noise_suppression"] = "strong"
        rec["vad_sensitivity"] = "low"
        notes.append("Mucho ruido respecto a la voz: supresión fuerte y VAD menos sensible. Acerca el micrófono si puedes.")
    if constant_noise:
        notes.append("Se detecta ruido constante (abanico/aire/PC): el filtro estacionario lo aprenderá en los silencios.")
    if speech < -30:
        rec["gain_db"] = "6"
        notes.append("La voz llega baja: se recomienda +6 dB de ganancia o subir el nivel del micrófono en Windows.")
    elif peak > -1.0:
        rec["gain_db"] = "-6"
        notes.append("El micrófono satura: baja el nivel del dispositivo.")
    else:
        rec["gain_db"] = "0"
    if speech_ratio < 0.05:
        notes.append("Casi no se detectó voz: habla durante la prueba para medir el nivel de voz.")
    return {"ok": True, "device": dev.label(), "rate": rate, "noise_db": round(noise, 1), "speech_db": round(speech, 1),
            "snr_db": round(snr, 1), "peak_db": round(peak, 1), "speech_ratio": round(speech_ratio, 2),
            "constant_noise": constant_noise, "tonal": round(tonal, 2), "recommend": rec, "notes": notes}
