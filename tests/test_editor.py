"""Pruebas de la integración con Reclaimer (.eia) y de los marcadores para editores (EDL, marcas de tiempo).

Sin red ni GUI. Lee F:\\Reclaimer en solo lectura (si no existe, se omiten las pruebas que lo necesitan)
y genera un video de prueba pequeño con FFmpeg. Nunca abre Reclaimer ni escribe en su carpeta.
"""
from __future__ import annotations

import json
import shutil

import pytest

from cronista.core import ffmpeg
from cronista.core.settings import Settings
from cronista.editor import reclaimer as rc
from cronista.export import editors
from cronista.notes.store import Store

NOTES = [
    {"position_ms": 1000, "duration_ms": 800, "marker": "important", "text": "Este nivel es muy bueno",
     "functions": ["crítica"], "topics": ["diseño de niveles"], "stt_info": {"voice": {"match": True}}},
    {"position_ms": 3000, "duration_ms": 3000, "marker": "problem", "text": "El salto falla | siempre",
     "functions": [], "topics": [], "stt_info": {"voice": {"match": False}}},
    {"position_ms": 4500, "duration_ms": 0, "marker": "", "text": "Ojo al sonido",
     "functions": [], "topics": [], "stt_info": {}},
]
MOMENTS = [{"start_ms": 2000, "end_ms": 5000, "title": "Pelea con el jefe"}]


def make_settings(folder) -> Settings:
    s = Settings()
    s.editor.clip_pre_s = 0.5     # con 5 s (valor por defecto) las notas de los primeros segundos colapsarían
    s.editor.clip_post_s = 0.5
    s.editor.project_dir = str(folder)
    return s


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    try:
        exe = ffmpeg.ffmpeg()
    except FileNotFoundError:
        pytest.skip("FFmpeg no instalado")
    out = tmp_path_factory.mktemp("obs") / "Grabacion 2026-10-09.mp4"
    r = ffmpeg.run([exe, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=6:size=320x240:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-shortest",
                    "-c:v", "libx264", "-c:a", "aac", str(out)], timeout=180)
    assert r.returncode == 0, r.stderr.decode(errors="replace")[-500:]
    return out


@pytest.fixture
def reclaimer_root():
    root = rc.find_reclaimer(Settings())
    if root is None:
        pytest.skip("No hay Reclaimer instalado (F:\\Reclaimer)")
    return root


@pytest.fixture
def session_data(video):
    return {"id": 1, "game": "Halo", "title": "Prueba", "started_at": "2026-10-09T18:30:00",
            "obs_path": str(video)}


# ---- textos y colores (sin Reclaimer) ------------------------------------------------------
def test_note_label_icons_and_voice_flag():
    assert rc.note_label(NOTES[0]) == "⭐ Este nivel es muy bueno (crítica · diseño de niveles)"
    assert rc.note_label(NOTES[1]).endswith(" 🗣️")
    assert rc.note_label(NOTES[2]) == "🎙 Ojo al sonido"


def test_note_label_truncates_to_90_chars():
    label = rc.note_label({"text": "a" * 200, "marker": "liked"})
    assert label == "👍 " + "a" * 89 + "…"


def test_marker_colors_table():
    assert rc.MARKER_COLORS["important"] == "#e5413c"
    assert rc.MARKER_COLORS[""] == "#f2b33d"


def test_load_model_does_not_write_bytecode(tmp_path):
    # Copia del modelo: así se comprueba que no se crea __pycache__ sin tocar la carpeta real de Reclaimer.
    src = rc.find_reclaimer(Settings())
    if src is None:
        pytest.skip("No hay Reclaimer instalado (F:\\Reclaimer)")
    dst = tmp_path / "copia"
    (dst / "editoria" / "core").mkdir(parents=True)
    shutil.copy(src / rc.MODEL_REL, dst / rc.MODEL_REL)
    mod = rc.load_model_module(dst)
    assert hasattr(mod, "Project")
    assert not (dst / "editoria" / "core" / "__pycache__").exists()


def test_find_reclaimer_ignores_invalid_configured_dir(tmp_path):
    s = Settings()
    s.editor.reclaimer_dir = str(tmp_path)          # carpeta vacía: no es Reclaimer
    assert rc.find_reclaimer(s) != tmp_path


def test_build_project_without_reclaimer_model_fails(tmp_path, session_data):
    with pytest.raises(RuntimeError):
        rc.build_project(session_data, NOTES, settings=make_settings(tmp_path), root=tmp_path)


# ---- proyecto .eia (requiere Reclaimer) ----------------------------------------------------
def test_build_project_structure(reclaimer_root, session_data, tmp_path):
    d = rc.build_project(session_data, NOTES, settings=make_settings(tmp_path), moments=MOMENTS, root=reclaimer_root)
    M = rc.load_model_module(reclaimer_root)
    M.Project.from_dict(d)                              # debe pasar la validación de Reclaimer
    assert d["format"] == "editoria-project"
    assert "Halo — Cronista 09/10/2026" == d["name"]

    assert len(d["media"]) == 1
    media = d["media"][0]
    assert media["kind"] == "video" and media["has_audio"] is True and media["probed"] is True
    assert media["path"].lower().endswith("grabacion 2026-10-09.mp4")

    tracks = d["tracks"]
    v1 = next(t for t in tracks if t["name"] == "V1")
    av = next(t for t in tracks if t["name"] == "Audio de video")
    assert tracks.index(av) == tracks.index(v1) + 1      # justo debajo de las de video, como Reclaimer

    clips = d["clips"]
    vclip = next(c for c in clips if c["track_id"] == v1["id"])
    aclip = next(c for c in clips if c["track_id"] == av["id"])
    assert vclip["start"] == 0 and vclip["source_in"] == 0
    assert vclip["duration"] == pytest.approx(media["duration"])
    assert vclip["link"] and vclip["link"] == aclip["link"]
    assert aclip["media_id"] == vclip["media_id"] == media["id"]
    assert aclip["duration"] == vclip["duration"]

    markers = d["markers"]
    assert len(markers) == 4                             # 3 notas + 1 momento
    assert [m["color"] for m in markers[:3]] == ["#e5413c", "#b54ae0", "#f2b33d"]
    assert markers[0]["end"] is None                     # 0,8 s: no es un tramo
    assert markers[1]["end"] == pytest.approx(6.0)       # 3 s + 3 s de nota
    assert markers[2]["end"] is None
    assert markers[3]["color"] == "#8b93a3" and markers[3]["label"] == "◆ Pelea con el jefe"
    assert markers[3]["origin"] == "ai" and markers[3]["kind"] == "marker"
    assert markers[3]["time"] == pytest.approx(2.0) and markers[3]["end"] == pytest.approx(5.0)

    favs = d["favorites"]
    assert [f["start"] for f in favs] == [0.5, 2.5, 4.0]
    assert favs[0]["end"] == pytest.approx(2.3)
    assert favs[1]["end"] == pytest.approx(6.0, abs=0.05)  # acotado a la duración del video
    assert favs[2]["end"] == pytest.approx(5.0)
    assert all(f["media_id"] == media["id"] for f in favs)


def test_build_project_without_audio_track_options(reclaimer_root, session_data, tmp_path):
    s = make_settings(tmp_path)
    s.editor.marker_notes = False
    s.editor.favorites_notes = False
    s.editor.include_moments = False
    d = rc.build_project(session_data, NOTES, settings=s, moments=MOMENTS, root=reclaimer_root)
    assert d["markers"] == [] and d["favorites"] == []


def test_favorites_are_not_duplicated(reclaimer_root, session_data, tmp_path):
    near = dict(NOTES[0], position_ms=1020)              # mismo inicio de clip (±0,05 s)
    d = rc.build_project(session_data, [NOTES[0], near], settings=make_settings(tmp_path), root=reclaimer_root)
    assert len(d["favorites"]) == 1


def test_build_project_rejects_missing_recording(reclaimer_root, session_data, tmp_path):
    session_data["obs_path"] = str(tmp_path / "no_existe.mp4")
    with pytest.raises(ValueError):
        rc.build_project(session_data, NOTES, settings=make_settings(tmp_path), root=reclaimer_root)


def test_export_session_never_overwrites(reclaimer_root, video, tmp_path):
    store = Store(tmp_path / "cronista.db")
    try:
        sid = store.create_session({"game": "Halo", "title": "Prueba", "started_at": "2026-10-09T18:30:00",
                                    "obs_path": str(video)})
        for n in NOTES:
            store.create_note(dict(n, session_id=sid, text_raw=n["text"]))
        store.replace_moments(sid, [{"start_ms": 2000, "end_ms": 5000, "title": "Pelea con el jefe"}])
        settings = make_settings(tmp_path / "proyectos")
        p1 = rc.export_session(store, sid, settings)
        p2 = rc.export_session(store, sid, settings)
    finally:
        store.close()
    assert p1 != p2
    assert p1.name == "Halo - Cronista - 2026-10-09_1830.eia"
    assert p2.name == "Halo - Cronista - 2026-10-09_1830 (2).eia"
    data = json.loads(p1.read_text(encoding="utf-8"))
    assert data["format"] == "editoria-project" and len(data["markers"]) == 4
    assert not list((tmp_path / "proyectos").glob("*.tmp"))


# ---- marcadores para editores estándar (sin Reclaimer) ----------------------------------------
def test_edl_entries_timecodes_and_colors():
    session = {"game": "Halo"}
    notes = NOTES + [{"position_ms": 4354500, "marker": "liked", "text": "Final  \n del  nivel"}]
    edl = editors.to_marker_edl(session, notes, fps=30.0)
    lines = edl.splitlines()
    assert lines[0] == "TITLE: Halo - Cronista"   # ASCII simple (CMX3600)
    assert lines[1] == "FCM: NON-DROP FRAME"
    assert edl.count("|D:1") == 4
    assert "001  001      V     C        01:00:01:00 01:00:01:01 01:00:01:00 01:00:01:01  \n" in edl
    assert "003  003      V     C        01:00:04:15 01:00:04:16 01:00:04:15 01:00:04:16  \n" in edl
    assert " |C:ResolveColorRed |M:Este nivel es muy bueno |D:1\n" in edl
    assert " |C:ResolveColorPurple |M:El salto falla / siempre |D:1\n" in edl
    assert " |C:ResolveColorYellow |M:Ojo al sonido |D:1\n" in edl
    assert " |C:ResolveColorCyan |M:Final del nivel |D:1\n" in edl
    assert "02:12:34:15" in edl                          # 1 h 12 min 34,5 s + la hora de base


def test_edl_skips_notes_without_position_and_cuts_text():
    notes = [{"position_ms": None, "text": "sin sitio"}, {"position_ms": 0, "marker": "develop", "text": "x" * 300}]
    edl = editors.to_marker_edl({"game": ""}, notes, fps=25.0)
    assert "sin sitio" not in edl
    assert " |C:ResolveColorBlue |M:" + "x" * 120 + " |D:1\n" in edl
    assert edl.count("|D:1") == 1


def test_youtube_timestamps():
    notes = [{"position_ms": 65000, "text": "Primera   idea"}, {"position_ms": 3725000, "text": "Final"},
             {"position_ms": None, "text": "omitida"}]
    assert editors.to_youtube_timestamps(notes) == "01:05 Primera idea\n01:02:05 Final\n"
    assert editors.to_youtube_timestamps([]) == ""
