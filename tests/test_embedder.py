import pytest

from ra.rag.embedder import HashEmbedder


def test_dim_and_unit_norm():
    e = HashEmbedder(dim=512)
    v = e.embed("hello world")
    assert len(v) == 512
    norm = sum(x * x for x in v) ** 0.5
    assert norm == pytest.approx(1.0)


def test_deterministic():
    e = HashEmbedder(dim=512)
    assert e.embed("rag web") == e.embed("rag web")


def test_similar_docs_are_closer():
    e = HashEmbedder(dim=512)
    a = e.embed("the quick brown fox jumps over the lazy dog near a river")
    b = e.embed("the quick brown fox is running through the forest")
    c = e.embed("quantum computing research on neural tissue")
    assert HashEmbedder.similarity(a, b) > HashEmbedder.similarity(a, c)


def test_empty_text_zeros():
    v = HashEmbedder(dim=64).embed("")
    assert sum(v) == 0.0


def test_tf_weighting_matters():
    e = HashEmbedder(dim=512)
    doc1 = e.embed("budget budget budget committee meeting")
    doc2 = e.embed("the committee scheduled a budget review")
    doc3 = e.embed("xylophone zebra arkansas")
    assert HashEmbedder.similarity(doc1, doc2) > HashEmbedder.similarity(doc1, doc3)