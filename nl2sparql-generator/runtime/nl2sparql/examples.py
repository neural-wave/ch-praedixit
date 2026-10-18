"""Retrieval of validated question/query examples for dynamic few-shot prompting."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .textindex import BM25, tokenize

CODE_RE = re.compile(
    r"\b(?:snomed|icd-10-gm|chop|loinc|ucum|geno|so|obi|efo|ordo|sphn-atc|atc):[\w.\-]+|<https?://[^>]+>|\"[^\"]*\"|\b\d+(?:\.\d+)?\b"
)


def skeleton(query: str) -> str:
    """Query structure with codes, literals and numbers abstracted away (for diversity)."""
    s = CODE_RE.sub("C", query)
    s = re.sub(r"\?\w+", "?v", s)
    return " ".join(s.split())


class ExampleBank:
    def __init__(self, path: str | Path, exclude_ids: set[str] | None = None,
                 exclude_gold_sections: set[int] | None = None, exclude_fn=None) -> None:
        self.examples: list[dict] = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ex = json.loads(line)
            if exclude_ids and ex["id"] in exclude_ids:
                continue
            if exclude_gold_sections and ex.get("gold_section") in exclude_gold_sections:
                continue
            if exclude_fn and exclude_fn(ex):
                continue
            self.examples.append(ex)
        docs = []
        for ex in self.examples:
            text = ex["question"]
            if ex.get("template_question"):
                text += " " + ex["template_question"]
            docs.append(tokenize(text))
        self.bm25 = BM25(docs)
        self._skeletons = [ex.get("skeleton") or skeleton(ex["compact"]) for ex in self.examples]

    def __len__(self) -> int:
        return len(self.examples)

    def search(self, question: str, k: int = 6, pool: int = 60, max_per_skeleton: int = 1,
               min_score_ratio: float = 0.0) -> list[dict]:
        hits = self.bm25.top(tokenize(question), k=pool)
        if not hits:
            return []
        best = hits[0][1]
        chosen, per_skel = [], {}
        for idx, score in hits:
            if min_score_ratio and score < best * min_score_ratio:
                break
            sk = self._skeletons[idx]
            if per_skel.get(sk, 0) >= max_per_skeleton:
                continue
            per_skel[sk] = per_skel.get(sk, 0) + 1
            ex = dict(self.examples[idx])
            ex["_score"] = round(score, 3)
            chosen.append(ex)
            if len(chosen) == k:
                break
        return chosen
