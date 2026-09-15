"""
Local document reading + chunking.

Reads text, markdown, code, JSON, CSV, logs, plain spreadsheets, and office
documents. PDFs are read via the optional pypdf package; DOCX / XLSX / ODT /
EPUB (all OOXML/zip formats) are read with the standard library (zipfile +
XML), and RTF via a lightweight control-word stripper - so no extra pip
wheels are needed to index the user's real documents. Never sends any file
contents anywhere - everything stays local.
"""
import os
import re
import zipfile
from xml.etree import ElementTree as ET

from ra import config
from ra.rag.embedder import HashEmbedder

TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".py", ".js", ".jsx", ".ts", ".tsx", ".json",
    ".csv", ".html", ".htm", ".css", ".xml", ".yaml", ".yml", ".ini",
    ".cfg", ".conf", ".log", ".toml", ".env.example", ".gitignore",
}
# Spreadsheet/office formats handled by the stdlib readers below.
DOC_EXTENSIONS = {
    ".docx", ".xlsx", ".odt", ".ods", ".epub", ".rtf",
}
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".idea", ".vscode", "dist", "build", ".next", "coverage",
}


# ---------------------------------------------------------------------------
# Stdlib office-document readers (zip = OOXML/EPUB container)
# ---------------------------------------------------------------------------

def _zip_read_entries(path: str, *names: str) -> dict:
    """Read named entries out of a zip container. Returns {name: text or None}."""
    out = {n: None for n in names}
    try:
        with zipfile.ZipFile(path) as z:
            for n in names:
                try:
                    out[n] = z.read(n).decode("utf-8", errors="replace")
                except KeyError:
                    out[n] = None
    except (zipfile.BadZipFile, OSError) as e:
        raise ValueError(f"Not a readable zip: {path}: {e}") from e
    return out


def _xml_w_t_text(xml: str) -> str:
    """Extract all <w:t> text runs from a WordprocessingML document.xml."""
    if not xml:
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    texts = []
    for node in root.iter():
        if node.tag.split("}")[-1] == "t" and node.text:
            texts.append(node.text)
    return "\n".join(texts)


def read_docx(path: str) -> str:
    """Read a .docx: zipfile -> word/document.xml -> <w:t> runs (stdlib)."""
    data = _zip_read_entries(path, "word/document.xml")
    xml = data.get("word/document.xml")
    if not xml:
        return ""
    return _xml_w_t_text(xml)


def read_xlsx(path: str) -> str:
    """Read a .xlsx: sharedStrings + each sheet's inline strings (stdlib).
    Cell values are joined so the sheet reads like a table in prose."""
    data = _zip_read_entries(
        path, "xl/sharedStrings.xml",
        "xl/worksheets/sheet1.xml",
        "xl/worksheets/sheet2.xml",
        "xl/worksheets/sheet3.xml",
    )
    shared = []
    ss = data.get("xl/sharedStrings.xml")
    if ss:
        try:
            root = ET.fromstring(ss)
        except ET.ParseError:
            root = None
        if root is not None:
            for si in root.iter():
                if si.tag.split("}")[-1] == "si":
                    shared.append("".join(
                        t.text or "" for t in si.iter()
                        if t.tag.split("}")[-1] == "t" and t.text
                    ))
    sheets = []
    for idx in (1, 2, 3):
        xml = data.get(f"xl/worksheets/sheet{idx}.xml")
        if not xml:
            continue
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            continue
        rows = []
        for row in root.iter():
            if row.tag.split("}")[-1] != "row":
                continue
            cells = []
            for c in row:
                tag = c.tag.split("}")[-1]
                if tag != "c":
                    continue
                val = ""
                for sub in c:
                    st = sub.tag.split("}")[-1]
                    if st == "v" and sub.text:
                        val = sub.text
                    elif st == "t" and sub.text:
                        val = sub.text
                if val.isdigit():
                    k = int(val)
                    if k < len(shared):
                        val = shared[k]
                cells.append(val)
            text = " | ".join(v for v in cells if v)
            if text.strip():
                rows.append(text.strip())
        if rows:
            sheets.append(f"[Sheet {idx}]\n" + "\n".join(rows))
    return "\n\n".join(sheets)


def _read_plaintext_zip(path: str, text_entries: list, title_prefix: str = "") -> str:
    """Generic zip reader for ODT/ODS/EPUB whose content is plain text files."""
    parts = []
    try:
        with zipfile.ZipFile(path) as z:
            for name in text_entries:
                if name in z.namelist():
                    text = z.read(name).decode("utf-8", errors="replace")
                    parts.append(text.strip())
    except (zipfile.BadZipFile, OSError) as e:
        raise ValueError(f"Not a readable zip: {path}: {e}") from e
    return "\n\n".join(p for p in parts if p)


def read_odt(path: str) -> str:
    """Read a .odt / .ods: zipfile -> content.xml -> text runs (stdlib)."""
    data = _zip_read_entries(path, "content.xml")
    xml = data.get("content.xml")
    if not xml:
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    texts = []
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag in ("p", "text:p") and node.text:
            texts.append(node.text.strip())
        elif tag == "table" and node.text:
            texts.append(node.text.strip())
    return "\n".join(t for t in texts if t)


def read_epub(path: str) -> str:
    """Read an .epub: concatenate the HTML chapter files (stdlib)."""
    entries = []
    try:
        with zipfile.ZipFile(path) as z:
            entries = [n for n in z.namelist()
                       if n.endswith((".xhtml", ".html", ".htm"))]
    except (zipfile.BadZipFile, OSError):
        return ""
    body = _read_plaintext_zip(path, sorted(entries)[:40])
    body = re.sub(r"<[^>]+>", " ", body)
    body = " ".join(body.split())
    return body


def read_rtf(path: str) -> str:
    """Read an .rtf: strip control words/braces, keep plain text (stdlib)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return ""
    if not raw.lstrip().startswith("{"):
        return ""
    # Drop control words like \pard \b0, skip grouped destinations we don't
    # want (\fonttbl, \colortbl, \stylesheet, \info), unescape \par.
    text = re.sub(r"\\f[0-9]+tbl\b.*?(\}|$)", " ", raw, flags=re.S)
    text = re.sub(r"\\(colortbl|stylesheet|info|listtable|listoverridetable|themedata)\b[^}]*(\}|$)", " ", text, flags=re.S)
    text = re.sub(r"\\[a-zA-Z]+-?[0-9]* ?", " ", text)
    text = text.replace("{", " ").replace("}", " ")
    text = " ".join(text.split())
    text = re.sub(r"(\w)- (\w)", r"\1\2", text)
    return text


# ---------------------------------------------------------------------------
# Public read entrypoint
# ---------------------------------------------------------------------------

def read_text_file(path: str) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            with open(path, "r", encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Could not decode {path}")


def read_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return ""
        try:
            with open(path, "rb") as f:
                reader = PdfReader(f)
                return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as e:
            print(f"[pdf skip: {path}: {e}]")
            return ""
    if ext in TEXT_EXTENSIONS:
        return read_text_file(path)
    if ext == ".docx":
        return read_docx(path)
    if ext == ".xlsx":
        return read_xlsx(path)
    if ext in (".odt", ".ods"):
        return read_odt(path)
    if ext == ".epub":
        return read_epub(path)
    if ext == ".rtf":
        return read_rtf(path)
    return ""


def chunk_text(text: str, chunk_size: int = None, overlap: int = None) -> list:
    """Split a document into overlapping fixed-size chunks; where possible the
    chunk end is snapped back to a paragraph/line boundary for readability."""
    chunk_size = chunk_size or config.RAG_CHUNK_SIZE
    overlap = overlap or config.RAG_CHUNK_OVERLAP
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks, pos = [], 0
    snap_window = max(120, chunk_size // 10)
    while pos < len(text):
        end = min(pos + chunk_size, len(text))
        if end < len(text):
            tail = text[pos:end]
            snap = max(tail.rfind("\n"), tail.rfind("\r"), tail.rfind(". "), tail.rfind("; "))
            if snap >= chunk_size - snap_window and tail[snap:snap + 1] in ("\n", "\r"):
                end = pos + snap + 1
            elif snap >= chunk_size - snap_window:
                end = pos + snap + 2 if tail.startswith(". ", snap) else pos + snap + 1
        chunk = text[pos:end].strip()
        if chunk:
            chunks.append(chunk)
        next_pos = end - overlap
        if next_pos <= pos:
            next_pos = pos + chunk_size
        if next_pos >= len(text):
            break
        pos = next_pos

    tail_chunk = text[pos:].strip()
    if tail_chunk and (not chunks or chunks[-1] != tail_chunk):
        chunks.append(tail_chunk)
    return chunks


def index_file(path: str, store, embedder=None, source_type: str = "file"):
    """Index one file. Returns chunks added (0 if the file has no readable text)."""
    text = read_text(path)
    if not text:
        return 0
    embedder = embedder or HashEmbedder(dim=config.RAG_EMBED_DIM)
    chunks = chunk_text(text)
    payload = [
        {
            "source_type": source_type,
            "source": os.path.abspath(path),
            "path": os.path.abspath(path),
            "chunk_index": i,
            "text": chunk,
        }
        for i, chunk in enumerate(chunks)
    ]
    store.add_chunks(payload, embedder)
    return len(chunks)


def index_directory(root: str, store, embedder=None, exts: set = None) -> dict:
    """Walk `root`, index every supported file. Returns stats."""
    embedder = embedder or HashEmbedder(dim=config.RAG_EMBED_DIM)
    stats = {"files": 0, "chunks": 0, "skipped": 0, "errors": []}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = os.path.join(dirpath, name)
            ext = os.path.splitext(name)[1].lower()
            if exts is not None and ext not in exts:
                stats["skipped"] += 1
                continue
            if ext not in TEXT_EXTENSIONS and ext not in DOC_EXTENSIONS and ext != ".pdf":
                stats["skipped"] += 1
                continue
            stats["files"] += 1
            try:
                stats["chunks"] += index_file(path, store, embedder)
            except Exception as e:
                stats["errors"].append(f"{path}: {e}")
    return stats