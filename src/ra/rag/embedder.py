"""
Dependency-free hashed-word embedding.

Each term is hashed into one of `dim` buckets (signed random projection using
md5), weighted by term frequency so documents sharing many terms land closer
in cosine space. Deterministic across machines and Python versions, so an
index built anywhere works with any retriever built anywhere.
"""
import hashlib
import math
import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class HashEmbedder:
    def __init__(self, dim=512):
        self.dim = dim

    def tokenize(self, text: str):
        return _TOKEN_RE.findall((text or "").lower())

    def embed(self, text: str) -> list:
        vec = [0.0] * self.dim
        tokens = self.tokenize(text)
        if not tokens:
            return vec
        weight = 1.0 / len(tokens)
        for tok in tokens:
            h = hashlib.md5(tok.encode("utf-8")).digest()
            idx = int.from_bytes(h[:2], "little") % self.dim
            sign = 1.0 if (h[2] & 1) else -1.0
            vec[idx] += sign * weight
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    @staticmethod
    def similarity(a: list, b: list) -> float:
        return sum(x * y for x, y in zip(a, b)) if a and b else 0.0