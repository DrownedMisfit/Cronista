"""Pruebas del recorte de clips y del vínculo nota↔clip. Sin red; genera un vídeo de prueba con FFmpeg."""
from __future__ import annotations

from pathlib import Path

import pytest

from cronista.clips import exporter
from cronista.clips.exporter import (clip_filename, clip_range, cut_clip, export_note_clips, merge_ranges,
                                     safe_name)
from cronista.core.ffmpeg import ffmpeg, probe, run
from cronista.core.settings import Settings
from cronista.notes.store import Store


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    """Grabación de prueba de 20 s (vídeo + audio, MKV como OBS)."""
    path = tmp_path_factory.mktemp("rec") / "grabacion.mkv"
    r = run([ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=duration=20:size=320x240:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
             "-shortest", "-c:v", "libx264", "-g", "30", "-c:a", "aac", str(path)], timeout=120)
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    return path


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def _settings(tmp_path, mode="copy", pre=1.0, post=1.0) -> Settings:
    st = Settings()
    st.editor.clip_dir = str(tmp_path / "clips")
    st.editor.clip_mode = mode
    st.editor.clip_pre_s = pre
    st.editor.clip_post_s = post
    return st


# ---- tramos y nombres ------------------------------------------------------------
def test_clip_range_bordes():
    # nota en medio: pre 5 s, duración 2 s, post 3 s
    assert clip_range({"position_ms": 1000, "duration_ms": 2000}, 20, 5, 3) == (0, 6000)
    # nota al final: el fin se acota a la duración de la grabación
    assert clip_range({"position_ms": 18000, "duration_ms": 1000}, 20, 5, 3) == (13000, 20000)
    # sin posición → error
    with pytest.raises(ValueError):
        clip_range({"position_ms": None, "duration_ms": 1000}, 20, 5, 3)
    # nota posterior al final de la grabación → error
    with pytest.raises(ValueError):
        clip_range({"position_ms": 25000, "duration_ms": 1000}, 20, 5, 3)


def test_merge_ranges():
    ranges = [(0, 1000), (2000, 3000), (6000, 7000)]
    assert merge_ranges(ranges, gap_ms=1500) == [(0, 3000, [0, 1]), (6000, 7000, [2])]
    # solapados siempre se unen; sin gap no se unen tramos separados
    assert merge_ranges([(0, 2000), (1000, 3000)]) == [(0, 3000, [0, 1])]
    assert merge_ranges([(0, 1000), (3000, 4000)], gap_ms=1500) == [(0, 1000, [0]), (3000, 4000, [1])]
    assert merge_ranges([]) == []


def test_safe_name():
    assert safe_name('a/b:c*d?"<>|e') == "a b c d e"
    assert safe_name("   ") == "nota"
    assert len(safe_name("x" * 100)) == 48
    assert safe_name("  hola   mundo  ") == "hola mundo"


def test_clip_filename():
    session = {"obs_path": "C:/rec/video.mkv"}
    note = {"text": "Esto es una prueba muy larga de nota"}
    assert clip_filename(session, note, 65_000) == "00-01-05 Esto es una prueba muy larga.mkv"
    assert clip_filename(session, note, 65_000, mode="exact") == "00-01-05 Esto es una prueba muy larga.mp4"


# ---- corte -----------------------------------------------------------------------
def test_cut_copy_reproducible(video, tmp_path):
    dst = tmp_path / "copy.mkv"
    assert cut_clip(video, dst, 5000, 10000, "copy") is True
    assert dst.is_file()
    assert probe(str(dst))["duration"] == pytest.approx(5.0, abs=2.0)


def test_cut_exact_duracion_precisa(video, tmp_path):
    dst = tmp_path / "exact.mp4"
    assert cut_clip(video, dst, 5000, 10000, "exact") is True
    info = probe(str(dst))
    assert info["duration"] == pytest.approx(5.0, abs=0.3)
    assert info["video_codec"] == "h264"


def test_cut_exact_cae_a_libx264(video, tmp_path, monkeypatch):
    """Si el codificador de GPU no existe o falla, se reintenta con libx264."""
    monkeypatch.setattr(exporter, "HW_ENCODER", "encoder_que_no_existe")
    dst = tmp_path / "fallback.mp4"
    assert cut_clip(video, dst, 2000, 4000, "exact") is True
    assert probe(str(dst))["duration"] == pytest.approx(2.0, abs=0.3)


# ---- exportación ------------------------------------------------------------------
def _session_with_notes(store, video, positions, marker=""):
    sid = store.create_session({"game": "Juego Prueba", "obs_path": str(video),
                                "started_at": "2026-10-09T10:00:00", "mode": "manual"})
    ids = []
    for i, pos in enumerate(positions):
        ids.append(store.create_note({"session_id": sid, "position_ms": pos, "duration_ms": 1000,
                                      "text": f"nota número {i}", "marker": marker if i == 0 else ""}))
    return sid, ids


def test_export_tres_notas_y_no_duplica(video, store, tmp_path):
    sid, ids = _session_with_notes(store, video, [2000, 8000, 14000])
    st = _settings(tmp_path)
    res = export_note_clips(store, sid, st)
    assert len(res["clips"]) == 3
    assert res["skipped"] == 0
    assert all(_exists(c["path"]) for c in res["clips"])
    assert len(store.clips_for_session(sid)) == 3
    for nid in ids:
        assert len(store.clips_for_note(nid)) == 1

    again = export_note_clips(store, sid, st)
    assert again["skipped"] == 3
    assert len(store.clips_for_session(sid)) == 3


def test_export_solo_marcadas(video, store, tmp_path):
    sid, ids = _session_with_notes(store, video, [2000, 8000, 14000], marker="favorito")
    res = export_note_clips(store, sid, _settings(tmp_path), only_marked=True)
    assert len(res["clips"]) == 1
    assert res["clips"][0]["note_ids"] == [ids[0]]
    assert len(store.clips_for_session(sid)) == 1


def test_export_sin_grabacion_da_error(store, tmp_path):
    sid = store.create_session({"game": "X", "obs_path": None, "started_at": "2026-10-09T10:00:00"})
    with pytest.raises(ValueError):
        export_note_clips(store, sid, _settings(tmp_path))


def test_cancelacion_no_deja_archivos_a_medias(video, store, tmp_path):
    sid, _ = _session_with_notes(store, video, [2000, 8000, 14000])
    st = _settings(tmp_path, mode="exact")
    flag = {"stop": False}

    def progress(fraction, text=""):
        if fraction > 0:
            flag["stop"] = True          # cancelar en cuanto empieza el primer clip

    res = export_note_clips(store, sid, st, progress=progress, cancelled=lambda: flag["stop"])
    assert res["clips"] == []
    assert store.clips_for_session(sid) == []
    folder = tmp_path / "clips"
    leftovers = [p for p in folder.iterdir() if p.is_file()] if folder.exists() else []
    assert leftovers == []


def _exists(path: str) -> bool:
    return Path(path).is_file()
