import os

from ra.rag import indexer
from ra.rag.embedder import HashEmbedder


def test_chunk_text_honors_size_and_overlap():
    text = "word " * 1000
    chunks = indexer.chunk_text(text, chunk_size=200, overlap=50)
    assert len(chunks) > 5
    assert all(len(c) <= 205 for c in chunks)
    assert all(abs(len(a) - len(b)) < 200 for a, b in zip(chunks, chunks[1:]))


def test_chunk_text_edge_cases():
    assert indexer.chunk_text("") == []
    assert indexer.chunk_text("   ") == []
    assert indexer.chunk_text("short") == ["short"]


def test_index_file(store, tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("Grocery list: milk, eggs, bread, turmeric\n" * 20, encoding="utf-8")
    n = indexer.index_file(str(p), store, HashEmbedder(dim=512), source_type="file")
    assert n >= 1
    assert store.stats()["total_chunks"] == n


def test_index_directory_walks_and_skips(store, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / ".venv").mkdir()
    (tmp_path / "README.md").write_text("ra is a virtual assistant", encoding="utf-8")
    (tmp_path / "src" / "app.py").write_text("print('hello rag')", encoding="utf-8")
    (tmp_path / ".venv" / "skip.py").write_text("junk data", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")

    st = indexer.index_directory(str(tmp_path), store, HashEmbedder(dim=64))
    assert st["files"] == 2
    assert st["chunks"] >= 2
    assert st["errors"] == []


def test_pdf_graceful_without_pypdf(tmp_path):
    fake = tmp_path / "doc.pdf"
    fake.write_bytes(b"%PDF-1.4 something")
    assert indexer.read_text(str(fake)) == ""


def test_read_text_encodings(tmp_path):
    p = tmp_path / "utf8.txt"
    p.write_text("héllo café", encoding="utf-8")
    assert indexer.read_text(str(p)) == "héllo café"