"""Terminology grounding: find codes for clinical concepts mentioned in a question.

Backed by the SQLite FTS5 index built by preparation/build_terminology.py (labels of ICD-10-GM,
CHOP, SNOMED CT, LOINC, UCUM, ORDO, GENO, SO, OBI and SPHN named individuals). Two uses:

* candidate codes for the prompt: n-grams of the question are matched against labels and
  ranked by how completely they cover a label;
* validation: every terminology constant in a generated query must exist.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .textindex import STOPWORDS, fold, stem

SYSTEM_OF_PREFIX = {
    "icd-10-gm": "ICD-10-GM",
    "chop": "CHOP",
    "snomed": "SNOMED CT",
    "loinc": "LOINC",
    "ucum": "UCUM",
    "ordo": "ORDO",
    "geno": "GENO",
    "so": "SO",
    "obi": "OBI",
    "edam": "EDAM",
    "efo": "EFO",
    "sphn-individual": "SPHN individual",
}

# Words that describe the query shape or the SPHN record type rather than a clinical concept.
GENERIC = set(
    """average mean median min minimum max maximum share percent percentage proportion rate ratio most least common
    frequent frequency top often usually typically distribution breakdown split group grouped trend year years yearly
    month months day days hour hours week weeks time times long longer shorter duration first last earliest latest
    record records recorded data database total overall number numbers count counts distinct different kind kinds type
    types category categories value values level levels status code codes coded coding sub subtype subtypes
    billed bill billing hospital stay stays case cases admission admissions admitted discharge discharged
    diagnosis diagnoses diagnosed procedure procedures lab laboratory test tests event events result results
    measurement measurements measured drug drugs administration administrations administered prescription prescribed
    patient patients people person persons someone men women man woman male female sex gender age aged old
    many much more less than least within between above below over under higher lower greater equal exactly
    including include includes also both either neither none any every each other another same whose which
    happen happens happened next follow followed given give gives get show list tell find want need
    our we us have has having had see seen
    """.split()
)


@dataclass
class Candidate:
    curie: str
    system: str
    label: str
    matched: str
    coverage: float
    data_records: int


class Terminology:
    def __init__(self, path: str | Path) -> None:
        path = Path(path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"terminology index not found: {path}")
        # A file URI handles spaces and Windows drive letters; mode=ro never writes to the index.
        self.uri = path.as_uri() + "?mode=ro"
        self._local = threading.local()
        try:
            self.db.execute("SELECT 1 FROM labels WHERE labels MATCH 'cholera' LIMIT 1").fetchall()
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower():
                raise RuntimeError(
                    "This Python's SQLite has no FTS5 full-text search, which the terminology index needs. "
                    "Use a Python build that includes it (the python.org installers do)."
                ) from exc
            raise

    @property
    def db(self) -> sqlite3.Connection:
        """One read-only connection per thread (sqlite3 connections are not thread-safe)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.uri, uri=True, check_same_thread=False)
            self._local.conn = conn
        return conn

    # ---------------------------------------------------------------- lookups
    def get(self, curie: str) -> dict | None:
        row = self.db.execute(
            "SELECT iri, curie, system, code, best_label, data_records FROM codes WHERE curie = ?", (curie,)
        ).fetchone()
        if not row:
            return None
        return dict(zip(("iri", "curie", "system", "code", "label", "data_records"), row))

    def get_iri(self, iri: str) -> dict | None:
        row = self.db.execute(
            "SELECT iri, curie, system, code, best_label, data_records FROM codes WHERE iri = ?", (iri,)
        ).fetchone()
        if not row:
            return None
        return dict(zip(("iri", "curie", "system", "code", "label", "data_records"), row))

    def labels_of(self, curie: str) -> list[str]:
        row = self.get(curie)
        if not row:
            return []
        try:
            return [r[0] for r in self.db.execute("SELECT label FROM code_labels WHERE iri = ?", (row["iri"],))]
        except sqlite3.OperationalError:
            return [row["label"]]

    def search(self, text: str, systems: list[str] | None = None, k: int = 10) -> list[Candidate]:
        toks = [t for t in re.findall(r"[a-z0-9]+", fold(text)) if t not in STOPWORDS]
        if not toks:
            return []
        out: list[Candidate] = []
        for mode in ("and", "or"):
            fts = (" AND " if mode == "and" else " OR ").join(f'"{t}"' for t in toks)
            out = self._query(fts, text, systems, k)
            if out:
                break
        return out

    def _query(self, fts: str, matched: str, systems: list[str] | None, k: int) -> list[Candidate]:
        sql = (
            "SELECT c.curie, c.system, l.label, c.best_label, c.data_records, bm25(labels) AS score "
            "FROM labels l JOIN codes c ON c.iri = l.iri WHERE labels MATCH ? "
        )
        args: list = [fts]
        if systems:
            sql += "AND c.system IN (%s) " % ",".join("?" * len(systems))
            args += systems
        sql += "ORDER BY score LIMIT ?"
        args.append(max(k * 5, 30))
        try:
            rows = self.db.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []
        q_toks = {stem(t) for t in re.findall(r"[a-z0-9]+", fold(matched))} - STOPWORDS
        seen, out = set(), []
        for curie, system, label, best, n, _score in rows:
            if curie in seen:
                continue
            l_toks = {stem(t) for t in re.findall(r"[a-z0-9]+", fold(re.sub(r"\([^)]*\)$", "", label)))} - STOPWORDS
            if not l_toks:
                continue
            coverage = len(q_toks & l_toks) / len(l_toks | q_toks)
            seen.add(curie)
            out.append(Candidate(curie, system, best or label, label, round(coverage, 3), n or 0))
        out.sort(key=lambda c: (-c.coverage, -(c.data_records > 0), len(c.label)))
        return out[:k]

    # ------------------------------------------------------- question mining
    def candidates_for_question(self, question: str, systems: list[str] | None = None, k: int = 8,
                                min_coverage: float = 0.5) -> list[Candidate]:
        """Codes whose label is (almost) fully expressed by a phrase of the question."""
        words = re.findall(r"[A-Za-zÀ-ÿ0-9][\wÀ-ÿ'-]*", question)
        folded = [fold(w).strip("'") for w in words]
        content = [i for i, w in enumerate(folded) if w not in STOPWORDS and w not in GENERIC and len(w) > 1]
        explicit = self._explicit_codes(question)
        found: dict[str, Candidate] = {c.curie: c for c in explicit}
        spans: list[tuple[int, int]] = []
        for n in (5, 4, 3, 2, 1):
            for i in range(len(folded) - n + 1):
                span = list(range(i, i + n))
                if not any(j in content for j in span):
                    continue
                if folded[i] in STOPWORDS or folded[i + n - 1] in STOPWORDS:
                    continue
                if any(a <= i and i + n <= b for a, b in spans):
                    continue  # already covered by a longer matched phrase
                phrase = " ".join(words[i:i + n])
                hits = [c for c in self.search(phrase, systems, k=5) if c.coverage >= min_coverage]
                if hits and hits[0].coverage >= 0.75:
                    spans.append((i, i + n))
                for c in hits[:3]:
                    prev = found.get(c.curie)
                    if prev is None or c.coverage > prev.coverage:
                        found[c.curie] = c
        ranked = sorted(found.values(), key=lambda c: (-c.coverage, -(c.data_records > 0), -len(c.matched), len(c.label)))
        return ranked[:k]

    def _explicit_codes(self, question: str) -> list[Candidate]:
        """Codes written literally in the question (E11, A00.9, 248153007, 00.0, 2345-7, C60-C63)."""
        out = []
        for m in re.finditer(r"\b([A-Z]\d{2}(?:\.\d{1,2})?(?:-[A-Z]\d{2})?|\d{6,18}|\d{2}\.\d{1,3}(?:\.\d+)?|\d{1,6}-\d)\b",
                             question):
            code = m.group(1)
            for prefix in ("icd-10-gm", "snomed", "chop", "loinc"):
                row = self.get(f"{prefix}:{code}")
                if row:
                    out.append(Candidate(row["curie"], row["system"], row["label"], code, 1.0, row["data_records"]))
        return out


def _fuzzy_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", fold(text)) if len(t) >= 4 and t not in STOPWORDS}


def label_matches_question(labels: list[str], question: str) -> bool:
    """True when a label shares a word (or a 5-letter stem, for EN/DE/FR cognates) with the question."""
    q = _fuzzy_tokens(question)
    for label in labels:
        for t in _fuzzy_tokens(re.sub(r"\([^)]*\)$", "", label)):
            for w in q:
                if t == w or (len(t) >= 5 and len(w) >= 5 and t[:5] == w[:5]):
                    return True
    return False


def format_candidates(cands: list[Candidate]) -> str:
    lines = []
    for c in cands:
        data = f"{c.data_records} coded records in sample" if c.data_records else "not in sample data"
        lines.append(f"- {c.curie} = {c.label} [{c.system}; matched '{c.matched}'; {data}]")
    return "\n".join(lines)


def terminology_constants(query: str) -> list[str]:
    """Terminology prefixed names used as constants in a query."""
    pattern = r"\b(" + "|".join(re.escape(p) for p in SYSTEM_OF_PREFIX) + r"):([\w.\-%]*[\w%])"
    out = []
    clean = re.sub(r'"(?:[^"\\]|\\.)*"', '""', query)
    clean = re.sub(r"^\s*PREFIX[^\n]*$", "", clean, flags=re.MULTILINE | re.IGNORECASE)
    for m in re.finditer(pattern, clean):
        curie = f"{m.group(1)}:{m.group(2)}"
        if curie not in out:
            out.append(curie)
    return out


def exists_path(path: str | Path) -> bool:
    return Path(path).exists()
