"""Documentos para compartir: Word (.docx), PDF, OpenDocument (.odt), HTML y Markdown.

Todo lo que Cronista exporta como texto (notas de una sesión, guiones, resúmenes, análisis) se genera
primero en Markdown y aquí se convierte al formato elegido:

- .docx → python-docx (estilos reales de Word: Título 1-3, listas, cita). Google Docs lo abre directamente.
- .pdf  → Qt (QTextDocument + QPdfWriter), A4, sin dependencias externas.
- .odt  → Qt (QTextDocumentWriter "odf"): OpenDocument, se importa en Google Docs y LibreOffice.
- .html / .md → texto.

PDF y ODT necesitan una QGuiApplication creada (la interfaz siempre la tiene).
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path

DOC_FORMATS = {
    "docx": "Word (.docx) — también se abre en Google Docs",
    "pdf": "PDF (.pdf)",
    "odt": "OpenDocument (.odt) — Google Docs y LibreOffice",
    "md": "Markdown (.md)",
    "html": "Página web (.html)",
}
FILE_FILTER = ";;".join(f"{label} (*.{ext})" for ext, label in DOC_FORMATS.items())


# ---- Markdown sencillo → bloques -------------------------------------------------------------------
@dataclass
class Block:
    kind: str                     # heading | para | bullet | number | quote | rule
    text: str = ""
    level: int = 0                # nivel del título (1-3)
    items: list[str] = field(default_factory=list)


_INLINE = re.compile(r"(\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*|_[^_\s][^_]*_|`[^`]+`)")


def parse_markdown(md: str) -> list[Block]:
    blocks: list[Block] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(Block("para", " ".join(s.strip() for s in para)))
            para.clear()

    for raw in (md or "").splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not s:
            flush()
            continue
        if m := re.match(r"^(#{1,6})\s+(.*)$", s):
            flush()
            blocks.append(Block("heading", m.group(2).strip(), level=min(3, len(m.group(1)))))
        elif re.match(r"^(-{3,}|\*{3,}|_{3,})$", s):
            flush()
            blocks.append(Block("rule"))
        elif m := re.match(r"^[-*+]\s+(.*)$", s):
            flush()
            if blocks and blocks[-1].kind == "bullet":
                blocks[-1].items.append(m.group(1))
            else:
                blocks.append(Block("bullet", items=[m.group(1)]))
        elif m := re.match(r"^\d+[.)]\s+(.*)$", s):
            flush()
            if blocks and blocks[-1].kind == "number":
                blocks[-1].items.append(m.group(1))
            else:
                blocks.append(Block("number", items=[m.group(1)]))
        elif s.startswith(">"):
            flush()
            text = s.lstrip(">").strip()
            if blocks and blocks[-1].kind == "quote":
                blocks[-1].text += " " + text
            else:
                blocks.append(Block("quote", text))
        else:
            para.append(s)
    flush()
    return blocks


def inline_runs(text: str) -> list[tuple[str, bool, bool, bool]]:
    """Trozos (texto, negrita, cursiva, código) de una línea con **negrita**, _cursiva_ y `código`."""
    runs = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if (part.startswith("**") and part.endswith("**")) or (part.startswith("__") and part.endswith("__")):
            runs.append((part[2:-2], True, False, False))
        elif part.startswith("`") and part.endswith("`"):
            runs.append((part[1:-1], False, False, True))
        elif len(part) > 2 and part[0] in "*_" and part[-1] == part[0]:
            runs.append((part[1:-1], False, True, False))
        else:
            runs.append((part, False, False, False))
    return runs


# ---- Word -------------------------------------------------------------------------------------------
def to_docx(md: str, path: Path, title: str | None = None) -> Path:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    if title:
        doc.core_properties.title = title
    doc.core_properties.author = "Cronista"

    def add_runs(par, text: str) -> None:
        for t, bold, italic, code in inline_runs(text):
            r = par.add_run(t)
            r.bold, r.italic = bold, italic
            if code:
                r.font.name = "Consolas"

    for b in parse_markdown(md):
        if b.kind == "heading":
            h = doc.add_heading(level=b.level if b.level > 1 else 1)
            if b.level == 1 and not doc.paragraphs[:-1]:
                h.style = doc.styles["Title"]
            add_runs(h, b.text)
        elif b.kind == "para":
            add_runs(doc.add_paragraph(), b.text)
        elif b.kind in ("bullet", "number"):
            style = "List Bullet" if b.kind == "bullet" else "List Number"
            for it in b.items:
                add_runs(doc.add_paragraph(style=style), it)
        elif b.kind == "quote":
            p = doc.add_paragraph(style="Quote") if "Quote" in [s.name for s in doc.styles] else doc.add_paragraph()
            add_runs(p, b.text)
        elif b.kind == "rule":
            p = doc.add_paragraph("")
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = p.add_run("· · ·")
            r.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
    doc.save(str(path))
    return path


# ---- HTML (también base para PDF/ODT) ----------------------------------------------------------------
def to_html(md: str, title: str | None = None) -> str:
    def inline(text: str) -> str:
        out = []
        for t, bold, italic, code in inline_runs(text):
            t = html.escape(t)
            if code:
                t = f"<code>{t}</code>"
            if bold:
                t = f"<b>{t}</b>"
            if italic:
                t = f"<i>{t}</i>"
            out.append(t)
        return "".join(out)

    parts = []
    for b in parse_markdown(md):
        if b.kind == "heading":
            parts.append(f"<h{b.level}>{inline(b.text)}</h{b.level}>")
        elif b.kind == "para":
            parts.append(f"<p>{inline(b.text)}</p>")
        elif b.kind in ("bullet", "number"):
            tag = "ul" if b.kind == "bullet" else "ol"
            parts.append(f"<{tag}>" + "".join(f"<li>{inline(i)}</li>" for i in b.items) + f"</{tag}>")
        elif b.kind == "quote":
            parts.append(f"<blockquote>{inline(b.text)}</blockquote>")
        elif b.kind == "rule":
            parts.append("<hr/>")
    head = f"<title>{html.escape(title)}</title>" if title else ""
    css = ("body{font-family:'Segoe UI',Calibri,sans-serif;font-size:11pt;line-height:1.4;color:#111}"
           "h1{font-size:20pt}h2{font-size:15pt;margin-top:14pt}h3{font-size:12.5pt}"
           "blockquote{color:#444;border-left:3px solid #bbb;margin-left:0;padding-left:10px}"
           "code{font-family:Consolas,monospace}")
    return (f"<!DOCTYPE html><html lang='es'><head><meta charset='utf-8'>{head}<style>{css}</style></head>"
            f"<body>{''.join(parts)}</body></html>")


def _qt_document(md: str, title: str | None):
    from PySide6.QtGui import QFont, QTextDocument
    doc = QTextDocument()
    doc.setDefaultFont(QFont("Segoe UI", 11))
    doc.setHtml(to_html(md, title))
    if title:
        doc.setMetaInformation(QTextDocument.DocumentTitle, title)
    return doc


def to_pdf(md: str, path: Path, title: str | None = None) -> Path:
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter
    doc = _qt_document(md, title)
    writer = QPdfWriter(str(path))
    writer.setPageLayout(QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait, QMarginsF(18, 18, 18, 18),
                                     QPageLayout.Millimeter))
    writer.setResolution(300)
    writer.setTitle(title or "Cronista")
    writer.setCreator("Cronista")
    doc.print_(writer)      # print_ maqueta en puntos y escala al DPI del PDF (tamaño de letra real)
    return path


def to_odt(md: str, path: Path, title: str | None = None) -> Path:
    from PySide6.QtGui import QTextDocumentWriter
    w = QTextDocumentWriter(str(path), b"odf")
    if not w.write(_qt_document(md, title)):
        raise RuntimeError(f"No se pudo escribir {path.name}")
    return path


def save_document(md: str, path: str | Path, title: str | None = None) -> Path:
    """Guarda `md` en el formato que indique la extensión de `path` (.docx, .pdf, .odt, .html, .md)."""
    path = Path(path)
    ext = path.suffix.lower().lstrip(".")
    if ext not in DOC_FORMATS:
        path = path.with_suffix(".docx")
        ext = "docx"
    path.parent.mkdir(parents=True, exist_ok=True)
    if ext == "docx":
        return to_docx(md, path, title)
    if ext == "pdf":
        return to_pdf(md, path, title)
    if ext == "odt":
        return to_odt(md, path, title)
    if ext == "html":
        path.write_text(to_html(md, title), encoding="utf-8")
        return path
    path.write_text(md, encoding="utf-8")
    return path
