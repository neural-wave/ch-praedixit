"""Build the terminology label index (SQLite FTS5) used to ground clinical concepts to codes.

Labels are read from the global graph of the Virtuoso store (the release ontologies), so the
index contains exactly the codes the inference rule set knows:

  ICD-10-GM (unversioned codes and blocks, de/fr), CHOP (de/fr/it), SNOMED CT (label,
  preferred and alternative labels), LOINC, UCUM, ORDO, GENO, SO, OBI, EDAM/EFO when present,
  and the SPHN named individuals (sphn-individual:Principal, ...).

Each code also gets ``data_records``: how many code nodes in the loaded patient sample are
typed with it (for ICD/CHOP the versioned classes are folded onto the unversioned code). It
only breaks ties between equally good label matches; it never filters a code out.

Output: runtime/resources/terminology.sqlite
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # repository root
RUNTIME = ROOT / "runtime"
WORK = ROOT / "preparation" / "work"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(ROOT))

from nl2sparql.prefixes import GLOBAL_GRAPH, compact_iri  # noqa: E402
from nl2sparql.sparql_client import SparqlClient  # noqa: E402

SYSTEMS = [
    ("ICD-10-GM", "https://biomedit.ch/rdf/sphn-resource/icd-10-gm/"),
    ("CHOP", "https://biomedit.ch/rdf/sphn-resource/chop/"),
    ("SNOMED CT", "http://snomed.info/id/"),
    ("LOINC", "https://loinc.org/rdf/"),
    ("UCUM", "https://biomedit.ch/rdf/sphn-resource/ucum/"),
    ("ORDO", "http://www.orpha.net/ORDO/"),
    ("GENO", "http://purl.obolibrary.org/obo/GENO_"),
    ("SO", "http://purl.obolibrary.org/obo/SO_"),
    ("OBI", "http://purl.obolibrary.org/obo/OBI_"),
    ("EDAM", "http://edamontology.org/"),
    ("EFO", "http://www.ebi.ac.uk/efo/EFO_"),
    ("SPHN individual", "https://biomedit.ch/rdf/sphn-schema/sphn/individual#"),
]
LABEL_PREDICATES = {
    "http://www.w3.org/2000/01/rdf-schema#label": "label",
    "http://www.w3.org/2004/02/skos/core#prefLabel": "pref",
    "http://www.w3.org/2004/02/skos/core#altLabel": "alt",
    # UCUM labels are symbols ("Cel", "mm[Hg]"); their names ("degree Celsius") are in rdfs:comment
    "http://www.w3.org/2000/01/rdf-schema#comment": "comment",
}
COMMENT_SYSTEMS = {"UCUM"}
VERSIONED = re.compile(r"^(https://biomedit\.ch/rdf/sphn-resource/(?:icd-10-gm|chop)/)\d{4}/(.+)$")


def fetch_labels(client: SparqlClient, ns: str, with_comments: bool = False):
    """All labels of one namespace, one request per label predicate.

    Virtuoso refuses sorted paging beyond 10000 rows, so each request returns everything at
    once (well below ResultSetMaxRows); versioned ICD-10-GM/CHOP classes are filtered out
    server-side because their labels duplicate those of the unversioned codes.
    """
    for pred in LABEL_PREDICATES:
        if LABEL_PREDICATES[pred] == "comment" and not with_comments:
            continue
        q = f"""
        SELECT ?s ?l WHERE {{
          GRAPH <{GLOBAL_GRAPH}> {{
            ?s <{pred}> ?l .
            FILTER(STRSTARTS(STR(?s), "{ns}"))
            FILTER(!REGEX(STR(?s), "/[0-9][0-9][0-9][0-9]/"))
          }}
        }}"""
        res = client.query(q, timeout=1800)
        if not res.ok or res.truncated:
            raise RuntimeError(res.error or "label query truncated")
        for b in res.rows:
            yield b["s"]["value"], pred, b["l"]["value"], b["l"].get("xml:lang", "")


def data_code_counts(client: SparqlClient) -> Counter:
    """Count code nodes per class over the patient graphs (direct types only)."""
    q = """
    SELECT ?c (COUNT(*) AS ?n) WHERE {
      GRAPH ?g { ?code a ?c }
      FILTER(STRSTARTS(STR(?g), "http://example.org/graph/"))
      FILTER(!STRSTARTS(STR(?c), "https://biomedit.ch/rdf/sphn-schema/sphn#"))
    } GROUP BY ?c"""
    res = client.query(q, timeout=1800)
    if not res.ok:
        raise RuntimeError(res.error)
    counts: Counter = Counter()
    for b in res.rows:
        iri, n = b["c"]["value"], int(b["n"]["value"])
        m = VERSIONED.match(iri)
        if m:
            iri = m.group(1) + m.group(2)
        counts[iri] += n
    return counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="http://localhost:8890/sparql")
    ap.add_argument("--out", default=str(RUNTIME / "resources" / "terminology.sqlite"))
    args = ap.parse_args()
    client = SparqlClient(args.endpoint, timeout=900, max_bytes=2_000_000_000)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    db = sqlite3.connect(out)
    db.execute(
        "CREATE TABLE codes (iri TEXT PRIMARY KEY, curie TEXT, system TEXT, code TEXT, "
        "best_label TEXT, data_records INTEGER DEFAULT 0)"
    )
    db.execute("CREATE TABLE code_labels (iri TEXT, label TEXT)")
    db.execute(
        "CREATE VIRTUAL TABLE labels USING fts5(iri UNINDEXED, label, lang UNINDEXED, kind UNINDEXED, "
        "tokenize='porter unicode61 remove_diacritics 2')"
    )
    t0 = time.time()
    counts = data_code_counts(client)
    print(f"data code classes: {len(counts)} ({time.time() - t0:.0f}s)")
    for system, ns in SYSTEMS:
        t1 = time.time()
        n_labels = 0
        codes: dict[str, str] = {}
        rows = []
        for s, p, label, lang in fetch_labels(client, ns, with_comments=system in COMMENT_SYSTEMS):
            if VERSIONED.match(s):
                continue  # versioned duplicates: the unversioned code carries the same labels
            kind = LABEL_PREDICATES[p]
            rows.append((s, label, lang, kind))
            n_labels += 1
            # Prefer an English label, then German, then anything, as the display label.
            prev = codes.get(s)
            rank = {"en": 0, "": 1, "de": 2, "fr": 3, "it": 4}.get(lang, 5) + {"alt": 10, "comment": 20}.get(kind, 0)
            if prev is None or rank < int(prev.split("\t", 1)[0]):
                codes[s] = f"{rank}\t{label}"
            if len(rows) >= 50000:
                db.executemany("INSERT INTO labels VALUES (?,?,?,?)", rows)
                db.executemany("INSERT INTO code_labels VALUES (?,?)", [(r[0], r[1]) for r in rows])
                rows = []
        if rows:
            db.executemany("INSERT INTO labels VALUES (?,?,?,?)", rows)
            db.executemany("INSERT INTO code_labels VALUES (?,?)", [(r[0], r[1]) for r in rows])
        db.executemany(
            "INSERT OR REPLACE INTO codes VALUES (?,?,?,?,?,?)",
            [
                (iri, compact_iri(iri), system, iri[len(ns):], lab.split("\t", 1)[1], counts.get(iri, 0))
                for iri, lab in codes.items()
            ],
        )
        db.commit()
        print(f"{system}: {len(codes)} codes, {n_labels} labels ({time.time() - t1:.0f}s)")
    db.execute("CREATE INDEX codes_curie ON codes(curie)")
    db.execute("CREATE INDEX code_labels_iri ON code_labels(iri)")
    db.execute("CREATE INDEX codes_code ON codes(code)")
    db.commit()
    db.execute("INSERT INTO labels(labels) VALUES('optimize')")
    db.commit()
    db.close()
    print(f"wrote {out} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
