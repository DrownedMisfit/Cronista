"""Detección de momentos: señales sintéticas y un video generado con ffmpeg."""
from __future__ import annotations

import numpy as np
import pytest

from cronista.core.ffmpeg import ffmpeg, run
from cronista.moments.detector import MediaSignals, analyze_media, analyze_session, detect_moments, robust_z


def _note(i, sec, marker="", text="comentario de prueba sobre el combate", functions=("opinión",), dur=4000):
    return {"id": i, "position_ms": sec * 1000, "duration_ms": dur, "text": text, "marker": marker,
            "functions": list(functions), "topics": ["combate"]}


def test_robust_z_ignores_overall_level():
    quiet = np.full(100, -40.0, np.float32)
    loud = quiet + 30
    quiet[50] = loud[50] = 0.0 + quiet[0] + 20
    assert robust_z(quiet)[50] > 5
    assert abs(robust_z(np.full(10, 3.0, np.float32))).max() == 0


def test_notes_only_marked_ranks_first_and_lengths_are_bounded():
    notes = [_note(1, 60), _note(2, 300, marker="important"), _note(3, 305), _note(4, 900)]
    moms = detect_moments(notes, 1200, None, related={2: [3], 3: [2]})
    assert moms and all(6000 <= m["end_ms"] - m["start_ms"] <= 46000 for m in moms)
    best = max(moms, key=lambda m: m["score"])
    assert 2 in best["note_ids"] and best["score"] == 100.0
    assert best["kind"] == "comentado" and any("marcador" in r for r in best["reasons"])


def test_other_voice_notes_weigh_less():
    me = _note(1, 100)
    other = _note(2, 600)
    other["stt_info"] = {"voice": {"match": False}}
    moms = {m["note_ids"][0]: m["score"] for m in detect_moments([me, other], 900, None)}
    assert moms[1] > moms.get(2, 0)


def test_media_only_peaks_become_action_moments():
    n = 600
    vis = np.zeros(n, np.float32)
    aud = np.zeros(n, np.float32)
    aud[200:208] = 6.0          # explosión
    vis[400:404] = 5.0          # cambio de escena
    aud[:3] = 9.0               # borde: debe ignorarse
    media = MediaSignals(n, visual=vis, audio=aud)
    moms = detect_moments([_note(1, 100, marker="important")], n, media)
    kinds = {m["kind"] for m in moms}
    assert "acción" in kinds and "comentado" in kinds
    starts = [m["start_ms"] // 1000 for m in moms if m["kind"] == "acción"]
    assert any(190 <= s <= 205 for s in starts) and any(390 <= s <= 404 for s in starts)
    assert not any(s < 3 for s in starts)
    commented = next(m for m in moms if m["kind"] == "comentado")
    assert commented["score"] == 100.0           # lo que dijiste pesa más que la acción sin comentar


def test_analyze_media_and_session_on_generated_video(tmp_path):
    video = tmp_path / "rec.mkv"
    r = run([ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "color=c=0x202020:s=320x180:d=60:r=30",
             "-f", "lavfi", "-i", "testsrc2=s=320x180:d=8:r=30",
             "-f", "lavfi", "-i", "anoisesrc=d=60:c=pink:a=0.02:r=48000",
             "-f", "lavfi", "-i", "sine=frequency=120:d=6:sample_rate=48000",
             "-filter_complex", "[0:v][1:v]overlay=enable='between(t,40,48)'[v];"
                                "[3:a]volume=6,adelay=15000|15000[b];[2:a][b]amix=inputs=2:duration=first:normalize=0[a]",
             "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-g", "30", "-preset", "veryfast", "-c:a", "aac",
             str(video)], timeout=120)
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    m = analyze_media(str(video))
    assert 58 <= m.duration_s <= 61 and m.keyframes >= 30
    assert np.any(m.audio[14:22] > 2) and np.any(m.visual[38:50] > 2)

    from cronista.notes.store import Store
    st = Store(tmp_path / "t.db")
    sid = st.create_session({"game": "G", "started_at": "2026-10-09T10:00:00", "duration_ms": 60000,
                             "obs_path": str(video)})
    st.create_note({"session_id": sid, "position_ms": 30000, "duration_ms": 3000, "text": "esto está genial",
                    "marker": "liked"})
    res = analyze_session(st, sid)
    assert res["media"] and len(st.list_moments(sid)) == len(res["moments"]) >= 2
    cancelled = analyze_media(str(video), cancelled=lambda: True)
    assert cancelled.keyframes == 0
    st.close()
