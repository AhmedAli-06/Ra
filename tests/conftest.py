"""Shared pytest setup: hermetic env + index path before any ra import."""
import os
import sys
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="ra-pytest-"))
os.environ.setdefault("RA_INDEX_PATH", str(_TEST_ROOT / "index.sqlite"))
os.environ.setdefault("RA_LLM_PROVIDER", "groq")  # deterministic provider for tests
os.environ.setdefault("RA_GROQ_API_KEY", "test-key-not-real")
os.environ.setdefault("RA_GROQ_MODEL", "test/model")
# The real .env in the repo may hold the multi-brain keys (RA_NIM_API_KEY,
# RA_GEMINI_API_KEY, ...) which would auto-enable the race - and every existing
# single-brain test would start hitting real providers. Pin it OFF here;
# test_multibrain.py flips it ON per-test with RA_MULTI_BRAIN=1.
os.environ.setdefault("RA_MULTI_BRAIN", "0")

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    from ra.rag.store import SemanticStore
    return SemanticStore(str(tmp_path / "idx.sqlite"))


@pytest.fixture()
def embedder():
    from ra.rag.embedder import HashEmbedder
    return HashEmbedder(dim=512)