"""Base de datos local (SQLite, WAL). Todo se guarda en %APPDATA%\\Cronista.

El esquema ya contempla las fases posteriores: dos dimensiones de clasificación
(función y tema), ámbito (juego actual / otro juego / tema general / personal),
referencias externas, relaciones entre notas, correcciones del usuario (para la
personalización opcional) y campos libres en JSON para extender sin migrar.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from cronista.core.paths import db_path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    obs_path TEXT,
    mode TEXT NOT NULL DEFAULT 'manual',
    summary TEXT,
    meta TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    position_ms INTEGER,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    text_raw TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    text_corrected TEXT,
    text_improved TEXT,
    text_script TEXT,
    scope TEXT NOT NULL DEFAULT '',
    functions TEXT NOT NULL DEFAULT '[]',
    topics TEXT NOT NULL DEFAULT '[]',
    refs TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0,
    ambiguous INTEGER NOT NULL DEFAULT 0,
    marker TEXT NOT NULL DEFAULT '',
    categories TEXT NOT NULL DEFAULT '[]',
    context TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'auto',
    forced INTEGER NOT NULL DEFAULT 0,
    user_edited INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    stt_info TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_notes_session ON notes(session_id, position_ms);

CREATE TABLE IF NOT EXISTS note_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_a INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    note_b INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'same_topic',
    weight REAL NOT NULL DEFAULT 1.0,
    UNIQUE(note_a, note_b, kind)
);

CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER REFERENCES notes(id) ON DELETE SET NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    text_snapshot TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER REFERENCES notes(id) ON DELETE SET NULL,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    mode TEXT NOT NULL DEFAULT 'copy',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clips_note ON clips(note_id);

CREATE TABLE IF NOT EXISTS scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    session_ids TEXT NOT NULL DEFAULT '[]',
    params TEXT NOT NULL DEFAULT '{}',
    content TEXT NOT NULL DEFAULT '{}',
    markdown TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'local'
);

CREATE TABLE IF NOT EXISTS note_embeddings (
    note_id INTEGER PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    vector BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS moments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    reasons TEXT NOT NULL DEFAULT '[]',
    note_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_moments_session ON moments(session_id, start_ms);

CREATE TABLE IF NOT EXISTS voice_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

NOTE_JSON_FIELDS = ("functions", "topics", "refs", "categories", "context", "stt_info")
SESSION_JSON_FIELDS = ("summary", "meta")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def _load(v: Any, default: Any) -> Any:
    if v is None or v == "":
        return default
    try:
        return json.loads(v)
    except Exception:
        return default


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or db_path()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            cur = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'")
            row = cur.fetchone()
            if row is None:
                self._conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row[0]) < SCHEMA_VERSION:
                # v2 solo añade tablas (CREATE TABLE IF NOT EXISTS ya las creó): basta con anotar la versión
                self._conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            self._conn.close()

    # ---- clave/valor -------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return _load(row[0], default) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, _dump(value)))

    # ---- sesiones ----------------------------------------------------------
    def create_session(self, s: dict) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions(game, title, started_at, ended_at, duration_ms, obs_path, mode, meta) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (s.get("game", ""), s.get("title", ""), s.get("started_at") or _now(), s.get("ended_at"),
                 int(s.get("duration_ms") or 0), s.get("obs_path"), s.get("mode", "manual"),
                 _dump(s.get("meta") or {})))
            return int(cur.lastrowid)

    def update_session(self, session_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            if k in SESSION_JSON_FIELDS and not isinstance(v, str):
                v = _dump(v)
            cols.append(f"{k}=?")
            vals.append(v)
        vals.append(session_id)
        with self._lock:
            self._conn.execute(f"UPDATE sessions SET {', '.join(cols)} WHERE id=?", vals)

    def get_session(self, session_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return self._session_row(row) if row else None

    def list_sessions(self, limit: int = 200, game: str | None = None) -> list[dict]:
        q = ("SELECT s.*, (SELECT COUNT(*) FROM notes n WHERE n.session_id=s.id AND n.deleted=0) AS notes_count "
             "FROM sessions s")
        args: list[Any] = []
        if game:
            q += " WHERE s.game=?"
            args.append(game)
        q += " ORDER BY s.started_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [self._session_row(r) for r in rows]

    def list_games(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT game FROM sessions ORDER BY game").fetchall()
        return [r[0] for r in rows if r[0]]

    def delete_session(self, session_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def _session_row(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        for f in SESSION_JSON_FIELDS:
            d[f] = _load(d.get(f), {} if f == "meta" else None)
        return d

    # ---- notas -------------------------------------------------------------
    def create_note(self, n: dict) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO notes(session_id, position_ms, duration_ms, created_at, text_raw, text, scope, functions, "
                "topics, refs, confidence, ambiguous, marker, categories, context, source, forced, stt_info) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (n["session_id"], n.get("position_ms"), int(n.get("duration_ms") or 0), n.get("created_at") or _now(),
                 n.get("text_raw", ""), n.get("text") or n.get("text_raw", ""), n.get("scope", ""),
                 _dump(n.get("functions") or []), _dump(n.get("topics") or []), _dump(n.get("refs") or []),
                 float(n.get("confidence") or 0), int(bool(n.get("ambiguous"))), n.get("marker", ""),
                 _dump(n.get("categories") or []), _dump(n.get("context") or {}), n.get("source", "auto"),
                 int(bool(n.get("forced"))), _dump(n.get("stt_info") or {})))
            return int(cur.lastrowid)

    def update_note(self, note_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            if k in NOTE_JSON_FIELDS and not isinstance(v, str):
                v = _dump(v)
            cols.append(f"{k}=?")
            vals.append(v)
        vals.append(note_id)
        with self._lock:
            self._conn.execute(f"UPDATE notes SET {', '.join(cols)} WHERE id=?", vals)

    def get_note(self, note_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        return self._note_row(row) if row else None

    def list_notes(self, session_id: int, include_deleted: bool = False, marker: str | None = None,
                   scope: str | None = None, text_filter: str | None = None) -> list[dict]:
        q = "SELECT * FROM notes WHERE session_id=?"
        args: list[Any] = [session_id]
        if not include_deleted:
            q += " AND deleted=0"
        if marker:
            q += " AND marker=?"
            args.append(marker)
        if scope:
            q += " AND scope=?"
            args.append(scope)
        if text_filter:
            q += " AND (text LIKE ? OR text_raw LIKE ?)"
            args += [f"%{text_filter}%", f"%{text_filter}%"]
        q += " ORDER BY COALESCE(position_ms, 0), id"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [self._note_row(r) for r in rows]

    def search_notes(self, text: str, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT n.*, s.game AS game, s.title AS session_title FROM notes n JOIN sessions s ON s.id=n.session_id "
                "WHERE n.deleted=0 AND (n.text LIKE ? OR n.text_raw LIKE ?) ORDER BY n.id DESC LIMIT ?",
                (f"%{text}%", f"%{text}%", limit)).fetchall()
        return [self._note_row(r) for r in rows]

    def delete_note(self, note_id: int, hard: bool = False) -> None:
        with self._lock:
            if hard:
                self._conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
            else:
                self._conn.execute("UPDATE notes SET deleted=1 WHERE id=?", (note_id,))

    def _note_row(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        for f in NOTE_JSON_FIELDS:
            d[f] = _load(d.get(f), {} if f in ("context", "stt_info") else [])
        d["ambiguous"] = bool(d.get("ambiguous"))
        d["forced"] = bool(d.get("forced"))
        return d

    # ---- relaciones y correcciones (fase 2) ---------------------------------
    def add_relation(self, a: int, b: int, kind: str = "same_topic", weight: float = 1.0) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO note_relations(note_a, note_b, kind, weight) VALUES(?,?,?,?)",
                               (min(a, b), max(a, b), kind, weight))

    def relations_for(self, note_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM note_relations WHERE note_a=? OR note_b=?", (note_id, note_id)).fetchall()
        return [dict(r) for r in rows]

    def replace_relations(self, note_id: int, related: list[tuple[int, str, float]]) -> None:
        """Sustituye las relaciones de una nota por `related` = [(otra_nota, tipo, peso)]."""
        with self._lock:
            self._conn.execute("DELETE FROM note_relations WHERE note_a=? OR note_b=?", (note_id, note_id))
            for other, kind, weight in related:
                if other == note_id:
                    continue
                self._conn.execute(
                    "INSERT OR REPLACE INTO note_relations(note_a, note_b, kind, weight) VALUES(?,?,?,?)",
                    (min(note_id, other), max(note_id, other), kind, float(weight)))

    def related_notes(self, note_id: int) -> list[dict]:
        """Notas relacionadas (no borradas) con `relation_kind` y `relation_weight`, más fuertes primero."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT n.*, r.kind AS relation_kind, r.weight AS relation_weight FROM note_relations r "
                "JOIN notes n ON n.id = CASE WHEN r.note_a=? THEN r.note_b ELSE r.note_a END "
                "WHERE (r.note_a=? OR r.note_b=?) AND n.deleted=0 ORDER BY r.weight DESC",
                (note_id, note_id, note_id)).fetchall()
        out = []
        for r in rows:
            d = self._note_row(r)
            d["relation_kind"] = r["relation_kind"]
            d["relation_weight"] = float(r["relation_weight"])
            out.append(d)
        return out

    def session_relations(self, session_id: int) -> dict[int, list[int]]:
        """Grafo de relaciones de una sesión en UNA consulta: {nota: [notas relacionadas]} (sin borradas)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.note_a, r.note_b FROM note_relations r "
                "JOIN notes a ON a.id=r.note_a JOIN notes b ON b.id=r.note_b "
                "WHERE a.session_id=? AND b.session_id=? AND a.deleted=0 AND b.deleted=0",
                (session_id, session_id)).fetchall()
        graph: dict[int, list[int]] = {}
        for a, b in rows:
            graph.setdefault(a, []).append(b)
            graph.setdefault(b, []).append(a)
        return graph

    def notes_pending_refine(self, limit: int = 1) -> list[dict]:
        """Notas con audio guardado para la segunda pasada (más antiguas primero)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notes WHERE deleted=0 AND stt_info LIKE '%audio_cache%' ORDER BY id LIMIT ?",
                (limit,)).fetchall()
        out = [self._note_row(r) for r in rows]
        return [n for n in out if (n.get("stt_info") or {}).get("audio_cache")]

    def corrected_examples(self, limit: int = 200) -> list[dict]:
        """Notas que el usuario corrigió (con sus valores finales), las más recientes primero.
        Base de la personalización: se usan como ejemplos para el clasificador."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT n.* FROM notes n WHERE n.deleted=0 AND n.id IN (SELECT note_id FROM corrections "
                "WHERE note_id IS NOT NULL) ORDER BY n.id DESC LIMIT ?", (limit,)).fetchall()
        return [self._note_row(r) for r in rows]

    def recent_texts(self, limit: int = 2000) -> list[str]:
        """Textos de notas recientes de todas las sesiones (para aprender vocabulario y estilo)."""
        with self._lock:
            rows = self._conn.execute("SELECT text FROM notes WHERE deleted=0 ORDER BY id DESC LIMIT ?",
                                      (limit,)).fetchall()
        return [r[0] for r in rows if r[0]]

    # ---- clips (fase 3) -------------------------------------------------------
    def add_clip(self, note_id: int | None, session_id: int, path: str, start_ms: int, end_ms: int,
                 mode: str = "copy") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO clips(note_id, session_id, path, start_ms, end_ms, mode, created_at) VALUES(?,?,?,?,?,?,?)",
                (note_id, session_id, path, int(start_ms), int(end_ms), mode, _now()))
            return int(cur.lastrowid)

    def clips_for_note(self, note_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM clips WHERE note_id=? ORDER BY id DESC", (note_id,)).fetchall()
        return [dict(r) for r in rows]

    def clips_for_session(self, session_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM clips WHERE session_id=? ORDER BY start_ms", (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def delete_clip(self, clip_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM clips WHERE id=?", (clip_id,))

    # ---- guiones (fase 3) -------------------------------------------------------
    def save_script(self, script: dict) -> int:
        with self._lock:
            if script.get("id"):
                self._conn.execute(
                    "UPDATE scripts SET game=?, title=?, session_ids=?, params=?, content=?, markdown=?, provider=? WHERE id=?",
                    (script.get("game", ""), script.get("title", ""), _dump(script.get("session_ids") or []),
                     _dump(script.get("params") or {}), _dump(script.get("content") or {}), script.get("markdown", ""),
                     script.get("provider", "local"), script["id"]))
                return int(script["id"])
            cur = self._conn.execute(
                "INSERT INTO scripts(game, title, created_at, session_ids, params, content, markdown, provider) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (script.get("game", ""), script.get("title", ""), script.get("created_at") or _now(),
                 _dump(script.get("session_ids") or []), _dump(script.get("params") or {}),
                 _dump(script.get("content") or {}), script.get("markdown", ""), script.get("provider", "local")))
            return int(cur.lastrowid)

    def list_scripts(self, game: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM scripts", []
        if game:
            q += " WHERE game=?"
            args.append(game)
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY id DESC", args).fetchall()
        return [self._script_row(r) for r in rows]

    def get_script(self, script_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
        return self._script_row(row) if row else None

    def delete_script(self, script_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM scripts WHERE id=?", (script_id,))

    def _script_row(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["session_ids"] = _load(d.get("session_ids"), [])
        d["params"] = _load(d.get("params"), {})
        d["content"] = _load(d.get("content"), {})
        return d

    # ---- embeddings para búsqueda semántica (fase 3) ----------------------------------
    def set_embedding(self, note_id: int, model: str, text_hash: str, vector: bytes) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO note_embeddings(note_id, model, text_hash, vector) VALUES(?,?,?,?)",
                               (note_id, model, text_hash, vector))

    def embeddings(self, model: str) -> dict[int, tuple[str, bytes]]:
        """{note_id: (text_hash, vector)} de notas no borradas para un modelo."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT e.note_id, e.text_hash, e.vector FROM note_embeddings e JOIN notes n ON n.id=e.note_id "
                "WHERE e.model=? AND n.deleted=0", (model,)).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def all_notes(self, game: str | None = None, include_deleted: bool = False) -> list[dict]:
        """Notas de todas las sesiones (con game y session_title), para búsqueda y guiones."""
        q = ("SELECT n.*, s.game AS game, s.title AS session_title, s.started_at AS session_started "
             "FROM notes n JOIN sessions s ON s.id=n.session_id")
        conds, args = [], []
        if not include_deleted:
            conds.append("n.deleted=0")
        if game:
            conds.append("s.game=?")
            args.append(game)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY s.started_at, COALESCE(n.position_ms, 0)"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = self._note_row(r)
            d["game"], d["session_title"], d["session_started"] = r["game"], r["session_title"], r["session_started"]
            out.append(d)
        return out

    # ---- momentos detectados (fase 3) ---------------------------------------------------
    def replace_moments(self, session_id: int, moments: list[dict]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM moments WHERE session_id=?", (session_id,))
            for m in moments:
                self._conn.execute(
                    "INSERT INTO moments(session_id, start_ms, end_ms, score, kind, title, reasons, note_ids, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (session_id, int(m["start_ms"]), int(m["end_ms"]), float(m.get("score", 0)), m.get("kind", ""),
                     m.get("title", ""), _dump(m.get("reasons") or []), _dump(m.get("note_ids") or []), _now()))

    def list_moments(self, session_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM moments WHERE session_id=? ORDER BY start_ms",
                                      (session_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reasons"] = _load(d.get("reasons"), [])
            d["note_ids"] = _load(d.get("note_ids"), [])
            out.append(d)
        return out

    def add_correction(self, note_id: int | None, field: str, old: Any, new: Any, text: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO corrections(note_id, field, old_value, new_value, text_snapshot, created_at) VALUES(?,?,?,?,?,?)",
                (note_id, field, _dump(old), _dump(new), text, _now()))

    def list_corrections(self, limit: int = 1000) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM corrections ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def clear_learning(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM corrections")
            self._conn.execute("DELETE FROM meta WHERE key LIKE 'learn.%'")

    # ---- estadísticas -------------------------------------------------------
    def session_stats(self, session_id: int) -> dict:
        notes = self.list_notes(session_id)
        stats: dict[str, Any] = {"notes": len(notes), "markers": {}, "functions": {}, "topics": {}, "scope": {},
                                 "references": {}}
        for n in notes:
            if n["marker"]:
                stats["markers"][n["marker"]] = stats["markers"].get(n["marker"], 0) + 1
            for f in n["functions"]:
                stats["functions"][f] = stats["functions"].get(f, 0) + 1
            for t in n["topics"]:
                stats["topics"][t] = stats["topics"].get(t, 0) + 1
            sc = n["scope"] or "sin clasificar"
            stats["scope"][sc] = stats["scope"].get(sc, 0) + 1
            for r in n["refs"]:
                name = r.get("name") if isinstance(r, dict) else str(r)
                stats["references"][name] = stats["references"].get(name, 0) + 1
        return stats
