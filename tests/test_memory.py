"""Persistent memory tests - offline, tmp data dir."""
import pytest

from ra import memory


@pytest.fixture
def mem_file(tmp_path, monkeypatch):
    memory._MEMORY_FILE = str(tmp_path / "memory.jsonl")
    return tmp_path


def test_remember_and_recall(mem_file):
    memory.remember("the user prefers decaf coffee", source="test")
    got = memory.recall_text("coffee")
    assert "decaf" in got


def test_recall_empty(mem_file):
    assert memory.recall_text("anything") == "No relevant memories yet."


def test_duplicate_entries_kept(mem_file):
    memory.remember("favourite colour is green")
    memory.remember("favourite colour is green")
    assert len(memory.recall("colour")) == 2


def test_unrelated_query_returns_nothing(mem_file):
    memory.remember("the office wifi password is pw123")
    assert memory.recall_text("astronomy lecture") == "No relevant memories yet."


def test_inject_context_only_for_relevant(mem_file):
    memory.remember("painting the garage next saturday")
    assert memory.inject_context("garage painting") is not None
    assert memory.inject_context("recipe for lasagna") is None


def test_summarize_session(mem_file):
    turns = [
        {"role": "user", "content": "please archive the q3 files"},
        {"role": "assistant", "content": "done"},
    ]
    top = memory.summarize_session(turns)
    assert "archive" in top
    got = memory.recall_text("q3 archive")
    assert "q3" in got