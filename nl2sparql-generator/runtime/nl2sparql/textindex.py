"""Small dependency-free BM25 index used for example retrieval and terminology lookup."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict

STOPWORDS = set(
    """a an the of and or in on at to for by with without from as is are was were be been being do does did
    how many much what which who whom whose when where why there their our we us you your they them it its this
    that these those have has had any all each per than then so such not no nor into over under also can could
    would should may might will shall give show list tell me please number count patients patient
    der die das und oder ein eine einer eines mit von zu im in den dem des ist sind wie viele welche für bei nicht
    le la les un une des du de et ou en au aux avec sans pour par est sont combien quels quelles qui patients
    """.split()
)


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().replace("ß", "ss")


def stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es") and token[-3] in "sxz":
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str, keep_stopwords: bool = False) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+(?:\.[0-9]+)*", fold(text))
    out = []
    for t in tokens:
        if not keep_stopwords and t in STOPWORDS:
            continue
        out.append(stem(t))
    return out


class BM25:
    """Okapi BM25 over pre-tokenised documents."""

    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.2, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.n_docs = len(docs_tokens)
        self.doc_len = [len(t) for t in docs_tokens]
        self.avg_len = (sum(self.doc_len) / self.n_docs) if self.n_docs else 0.0
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, toks in enumerate(docs_tokens):
            for term, tf in Counter(toks).items():
                postings[term].append((i, tf))
        self.postings = dict(postings)
        self.idf = {
            term: math.log(1 + (self.n_docs - len(p) + 0.5) / (len(p) + 0.5)) for term, p in self.postings.items()
        }

    def scores(self, query_tokens: list[str], max_postings: int | None = None) -> dict[int, float]:
        acc: dict[int, float] = defaultdict(float)
        for term in set(query_tokens):
            plist = self.postings.get(term)
            if not plist:
                continue
            if max_postings and len(plist) > max_postings:
                # Extremely common terms carry almost no signal and are slow to score.
                continue
            idf = self.idf[term]
            for doc, tf in plist:
                norm = tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * self.doc_len[doc] / self.avg_len))
                acc[doc] += idf * norm
        return acc

    def top(self, query_tokens: list[str], k: int = 10, max_postings: int | None = None) -> list[tuple[int, float]]:
        acc = self.scores(query_tokens, max_postings=max_postings)
        return sorted(acc.items(), key=lambda kv: -kv[1])[:k]
