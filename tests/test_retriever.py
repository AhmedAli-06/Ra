from ra.rag import indexer
from ra.rag.retriever import format_context, retrieve


def test_retrieve_end_to_end(store, embedder, tmp_path):
    (tmp_path / "meeting.txt").write_text(
        "standup summary: API key rotation completed for the staging cluster", encoding="utf-8")
    (tmp_path / "todo.md").write_text(
        "buy groceries: milk, eggs, turmeric and chapatis", encoding="utf-8")
    indexer.index_directory(str(tmp_path), store, embedder)

    res = retrieve(store, embedder, "what happened with the API key rotation?", k=2)
    assert res
    assert res[0].score > 0.0
    assert "meeting" in res[0].source.lower()

    res2 = retrieve(store, embedder, "buy milk and eggs", k=2)
    assert "todo" in res2[0].source.lower()

    ctx = format_context(res)
    assert "meeting" in ctx.lower()
    assert "source" in ctx or res[0].source in ctx


def test_retrieve_unknown_query_scores_zero(store, embedder, tmp_path):
    (tmp_path / "a.md").write_text("quantum physics notes", encoding="utf-8")
    indexer.index_directory(str(tmp_path), store, embedder)
    res = retrieve(store, embedder, "zzzz nonexistent word qqqqq", k=2)
    assert res and res[0].score == 0.0
    assert retrieve(store, embedder, "zzzz nonexistent word qqqqq", k=2, min_score=0.01) == []


def test_format_context_truncates(store, embedder, tmp_path):
    big = " ".join(["the quick brown fox"] * 800)
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")
    indexer.index_directory(str(tmp_path), store, embedder)
    res = retrieve(store, embedder, "quick brown fox", k=4)
    ctx = format_context(res, max_chars=1500)
    assert len(ctx) <= 1500 + 120