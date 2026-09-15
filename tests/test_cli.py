"""CLI behaviour tests (pure, offline — no LLM, no screen)."""
from ra import config
from ra.rag import cli
from ra.rag.embedder import HashEmbedder


def test_grant_then_revoke(monkeypatch):
    monkeypatch.setitem(config.GRANTED_ACCESS, "screen", False)
    cli.main(["rag", "grant", "screen"])
    assert config.GRANTED_ACCESS["screen"] is True
    cli.main(["rag", "revoke", "screen"])
    assert config.GRANTED_ACCESS["screen"] is False
    monkeypatch.setitem(config.GRANTED_ACCESS, "screen", True)


def test_index_deindex_clear(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "RAG_INDEX_PATH", str(tmp_path / "cli.sqlite"))
    (tmp_path / "cli.md").write_text("standing desk settings ergonomics posture", encoding="utf-8")

    cli.main(["rag", "index", "--file", str(tmp_path / "cli.md")])
    cli.main(["rag", "search", "ergonomics", "-k", "1"])
    out = capsys.readouterr().out
    assert "cli.md" in out

    cli.main(["rag", "deindex", "--source", str(tmp_path / "cli.md")])
    cli.main(["rag", "stats"])
    assert "0" in capsys.readouterr().out


def test_sources_lists_indexed_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "RAG_INDEX_PATH", str(tmp_path / "cli.sqlite"))
    from ra.rag.store import SemanticStore
    store = SemanticStore(str(tmp_path / "cli.sqlite"))
    name = str(tmp_path / "doc.md")
    (tmp_path / "doc.md").write_text("some content words here", encoding="utf-8")
    store.add_chunks([{"source_type": "file", "source": name, "path": name,
                       "chunk_index": 0, "text": "some content words here"}],
                     HashEmbedder(dim=64))
    cli.main(["rag", "sources"])
    assert name in capsys.readouterr().out


def test_ask_reports_missing_llm_cleanly(monkeypatch, capsys):
    import ra.brain as brain

    monkeypatch.setattr(
        brain,
        "ask",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no key")),
    )
    cli.main(["ask", "hello"])
    out = capsys.readouterr().out
    assert "can't answer right now" in out
    assert "no key" in out
    assert "Traceback" not in out