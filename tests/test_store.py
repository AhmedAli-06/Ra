import pytest

from ra.rag.embedder import HashEmbedder
from ra.rag.store import SemanticStore


def _store(tmp_path):
    return SemanticStore(str(tmp_path / "dx.sqlite"))


def test_add_search_delete(tmp_path):
    s = _store(tmp_path)
    e = HashEmbedder(dim=64)
    s.add_chunks([{"source_type": "file", "source": "/a/neural.txt", "path": "/a/neural.txt",
                   "chunk_index": 0, "text": "machine learning neural networks backpropagation"}], e)
    s.add_chunks([{"source_type": "file", "source": "/b/pasta.txt", "path": "/b/pasta.txt",
                   "chunk_index": 0, "text": "pasta recipes italian tomato basil"}], e)
    assert s.stats()["total_chunks"] == 2

    hits = s.search(e.embed("neural networks"), k=1)
    assert hits[0]["source"] == "/a/neural.txt"
    assert hits[0]["score"] > 0.0

    hits = s.search(e.embed("tomato pasta recipe"), k=1)
    assert hits[0]["source"] == "/b/pasta.txt"

    assert s.delete_source("/a/neural.txt") == 1
    assert s.stats()["total_chunks"] == 1


def test_search_respects_source_filter(tmp_path):
    s = _store(tmp_path)
    e = HashEmbedder(dim=64)
    s.add_chunks([{"source_type": "file", "source": "/f", "text": "server outage incident report"}], e)
    s.add_chunks([{"source_type": "device", "source": "device", "text": "server outage incident report"}], e)
    hits = s.search(e.embed("server outage"), k=4, source_types=["device"])
    assert len(hits) == 1
    assert hits[0]["source_type"] == "device"


def test_persistence_across_instances(tmp_path):
    path = str(tmp_path / "persist.sqlite")
    e = HashEmbedder(dim=64)
    SemanticStore(path).add_chunks([{"source_type": "file", "source": "/keep", "text": "persisted chunk data"}], e)
    reopened = SemanticStore(path)
    hits = reopened.search(e.embed("persisted data"), k=1)
    assert hits and hits[0]["source"] == "/keep"


def test_clear(tmp_path):
    s = _store(tmp_path)
    e = HashEmbedder(dim=64)
    s.add_chunks([{"source_type": "file", "source": "/x", "text": "one"}], e)
    s.clear()
    assert s.stats()["total_chunks"] == 0


def test_empty_add_is_noop(tmp_path):
    s = _store(tmp_path)
    e = HashEmbedder(dim=64)
    s.add_chunks([{"source_type": "file", "source": "/blank", "text": "   "}], e)
    assert s.stats()["total_chunks"] == 0