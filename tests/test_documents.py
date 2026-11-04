"""Exportación de documentos: Word, PDF, OpenDocument, HTML y Markdown."""
from __future__ import annotations

import os
import zipfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cronista.export.documents import DOC_FORMATS, inline_runs, parse_markdown, save_document, to_html

MD = """# Rise of Iron D-Day — resumen de la sesión

_Basado en 12 notas tuyas_

## Principales críticas

1. El combate se siente **demasiado lento** [00:12:34]
2. Enemigos que aparecen detrás [00:45:17]

## Aspectos positivos

- La música del jefe está genial 👍
- El escenario *respira*

> **Corregida:** Creo que la cobertura se siente pegajosa.

---

Texto final con `código`.
"""


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_parse_markdown_blocks():
    kinds = [b.kind for b in parse_markdown(MD)]
    assert kinds == ["heading", "para", "heading", "number", "heading", "bullet", "quote", "rule", "para"]
    nums = next(b for b in parse_markdown(MD) if b.kind == "number")
    assert len(nums.items) == 2
    assert inline_runs("a **b** _c_ `d`") == [("a ", False, False, False), ("b", True, False, False),
                                               (" ", False, False, False), ("c", False, True, False),
                                               (" ", False, False, False), ("d", False, False, True)]


def test_docx_has_real_word_structure(tmp_path):
    from docx import Document
    p = save_document(MD, tmp_path / "resumen.docx", title="Resumen")
    assert zipfile.is_zipfile(p)
    d = Document(str(p))
    styles = [par.style.name for par in d.paragraphs if par.text.strip()]
    texts = [par.text for par in d.paragraphs]
    assert "Title" in styles and "Heading 2" in styles
    assert styles.count("List Number") == 2 and styles.count("List Bullet") == 2
    bold = [r.text for par in d.paragraphs for r in par.runs if r.bold]
    assert "demasiado lento" in bold and any("👍" in t for t in texts)
    assert d.core_properties.title == "Resumen"


def test_pdf_and_odt(tmp_path, qapp):
    pdf = save_document(MD, tmp_path / "resumen.pdf", title="Resumen")
    assert pdf.read_bytes()[:5] == b"%PDF-" and pdf.stat().st_size > 2000
    odt = save_document(MD, tmp_path / "resumen.odt", title="Resumen")
    with zipfile.ZipFile(odt) as z:
        assert z.read("mimetype") == b"application/vnd.oasis.opendocument.text"
        assert "Principales críticas" in z.read("content.xml").decode("utf-8")


def test_html_md_and_unknown_extension(tmp_path):
    h = to_html(MD, "T")
    assert "<h2>Principales críticas</h2>" in h and "<ol>" in h and "<b>demasiado lento</b>" in h
    assert save_document(MD, tmp_path / "a.md").read_text(encoding="utf-8") == MD
    assert save_document(MD, tmp_path / "a.xyz").suffix == ".docx"       # extensión desconocida → Word
    assert set(DOC_FORMATS) == {"docx", "pdf", "odt", "md", "html"}
