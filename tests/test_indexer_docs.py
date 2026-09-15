"""Document readers (DOCX / XLSX / ODT / EPUB / RTF) - all stdlib, no deps."""
import os
import zipfile

from ra.rag import indexer


def _docx_bytes(text: str) -> bytes:
    from io import BytesIO
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>' + text +
            '</w:t></w:r></w:p></w:body></w:document>',
        )
    return buf.getvalue()


def _xlsx_bytes() -> bytes:
    from io import BytesIO
    buf = BytesIO()
    z = zipfile.ZipFile(buf, "w")
    with z:
        z.writestr("xl/sharedStrings.xml",
                   '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/'
                   'spreadsheetml/2006/main"><si><t>Name</t></si><si><t>Ada</t></si>'
                   '</sst>')
        z.writestr("xl/worksheets/sheet1.xml",
                   '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
                   'spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v>'
                   '</c></row><row r="2"><c r="A2" t="s"><v>1</v></c></row></sheetData>'
                   '</worksheet>')
    return buf.getvalue()


def test_read_docx(tmp_path):
    p = tmp_path / "note.docx"
    p.write_bytes(_docx_bytes("Hello from the docx test"))
    text = indexer.read_docx(str(p))
    assert "Hello from the docx test" in text


def test_read_xlsx(tmp_path):
    p = tmp_path / "people.xlsx"
    p.write_bytes(_xlsx_bytes())
    text = indexer.read_xlsx(str(p))
    assert "Name" in text
    assert "Ada" in text


def test_read_odt(tmp_path):
    p = tmp_path / "doc.odt"
    from io import BytesIO
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("content.xml",
                   '<?xml version="1.0"?><office:document-content '
                   'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                   'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
                   '<office:body><office:text><text:p>odt paragraph</text:p>'
                   '</office:text></office:body></office:document-content>')
    p.write_bytes(buf.getvalue())
    assert "odt paragraph" in indexer.read_odt(str(p))


def test_read_epub(tmp_path):
    p = tmp_path / "book.epub"
    from io import BytesIO
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("chapter1.xhtml", "<html><body><p>epub words here</p></body></html>")
    p.write_bytes(buf.getvalue())
    assert "epub words here" in indexer.read_epub(str(p))


def test_read_rtf(tmp_path):
    p = tmp_path / "doc.rtf"
    p.write_text(r"{\rtf1\ansi\b bold text\b0 \( normal\) \par second\_line\par}", encoding="utf-8")
    text = indexer.read_rtf(str(p))
    assert "bold text" in text
    assert "normal" in text


def test_index_file_docx(tmp_path, store, embedder):
    p = tmp_path / "plan.docx"
    p.write_bytes(_docx_bytes("quarterly planning targets for the team"))
    n = indexer.index_file(str(p), store, embedder)
    assert n >= 1
    hits = store.search(embedder.embed("planning"))
    assert hits and "planning" in hits[0]["text"]


def test_index_directory_finds_docs(tmp_path, store, embedder):
    (tmp_path / "sub").mkdir()
    p = tmp_path / "sub" / "budget.xlsx"
    p.write_bytes(_xlsx_bytes())
    stats = indexer.index_directory(str(tmp_path), store, embedder)
    assert stats["files"] == 1
    assert stats["chunks"] >= 1