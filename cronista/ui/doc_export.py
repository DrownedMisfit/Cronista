"""Diálogo común "Guardar como documento": Word, PDF, OpenDocument, HTML o Markdown."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QMessageBox

from cronista import APP_NAME
from cronista.export.documents import DOC_FORMATS, save_document

log = logging.getLogger(__name__)

_FILTERS = [f"{label} (*.{ext})" for ext, label in DOC_FORMATS.items()]


def safe_stem(text: str, fallback: str = "documento") -> str:
    stem = "".join(c if c.isalnum() or c in " -_" else "_" for c in (text or "")).strip()
    return stem[:80] or fallback


def save_markdown_as(parent, markdown: str, default_stem: str, base_dir: Path, title: str | None = None,
                     default_ext: str = "docx") -> Path | None:
    """Pregunta dónde y en qué formato guardar, lo guarda y ofrece abrirlo. Devuelve la ruta o None."""
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    default_filter = next((f for f in _FILTERS if f"*.{default_ext})" in f), _FILTERS[0])
    path, chosen = QFileDialog.getSaveFileName(parent, "Guardar documento",
                                               str(base_dir / f"{safe_stem(default_stem)}.{default_ext}"),
                                               ";;".join(_FILTERS), default_filter)
    if not path:
        return None
    p = Path(path)
    if p.suffix.lower().lstrip(".") not in DOC_FORMATS:      # sin extensión: la del filtro elegido
        ext = next((e for e, label in DOC_FORMATS.items() if chosen.startswith(label)), default_ext)
        p = p.with_suffix(f".{ext}")
    try:
        out = save_document(markdown, p, title=title)
    except Exception as e:
        log.exception("No se pudo guardar el documento")
        QMessageBox.warning(parent, APP_NAME, f"No se pudo guardar el documento:\n{e}")
        return None
    hint = ""
    if out.suffix.lower() in (".docx", ".odt"):
        hint = "\n\nPara Google Docs: súbelo a Google Drive y ábrelo con Documentos de Google."
    if QMessageBox.question(parent, APP_NAME, f"Guardado en:\n{out}{hint}\n\n¿Abrirlo ahora?") == QMessageBox.Yes:
        try:
            os.startfile(str(out))  # type: ignore[attr-defined]
        except OSError as e:
            QMessageBox.warning(parent, APP_NAME, f"No se pudo abrir: {e}")
    return out
