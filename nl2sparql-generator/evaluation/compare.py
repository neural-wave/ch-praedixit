"""Result-set comparison for execution-based evaluation.

Two queries are *execution-equivalent* on the store when their result tables are equal as
multisets of rows, up to a renaming/reordering of columns (variable names never matter) and
small numeric tolerance. Row order is ignored unless both queries use ORDER BY with LIMIT
(then the selected rows matter, which the multiset check already covers).

The relaxed variant accepts a prediction that returns every reference column (same rows)
plus extra columns, which is how a question answered with additional context looks.
"""

from __future__ import annotations

import itertools
import math
import re
from collections import Counter

NUM_RE = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


def cell_value(binding: dict | None, iri_local: bool = False) -> object:
    if binding is None:
        return None
    v = binding.get("value")
    t = binding.get("type")
    dt = binding.get("datatype", "")
    if t in ("literal", "typed-literal") and (
        dt.endswith(("#integer", "#int", "#long", "#decimal", "#double", "#float", "#short", "#byte",
                     "#nonNegativeInteger", "#positiveInteger"))
        or (not dt and NUM_RE.match(v or ""))
    ):
        try:
            f = float(v)
            return round(f, 4) if not math.isnan(f) else "nan"
        except ValueError:
            return v
    if t == "uri" and iri_local:
        return re.split(r"[/#]", v)[-1]
    if t == "bnode":
        return "_:b"
    return v


def table(result, iri_local: bool = False) -> tuple[list[str], list[tuple]]:
    vars_ = result.vars
    rows = [tuple(cell_value(r.get(v), iri_local) for v in vars_) for r in result.rows]
    return vars_, rows


def _close(a: object, b: object) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= 1e-3 * max(1.0, abs(a), abs(b))
    return a == b


def _norm_row(row: tuple) -> tuple:
    return tuple(round(x, 2) if isinstance(x, float) else x for x in row)


def columns_match(ref_rows: list[tuple], pred_rows: list[tuple], ref_cols: list[int], pred_cols: list[int]) -> bool:
    a = Counter(_norm_row(tuple(r[i] for i in ref_cols)) for r in ref_rows)
    b = Counter(_norm_row(tuple(r[i] for i in pred_cols)) for r in pred_rows)
    return a == b


def find_alignment(ref_rows: list[tuple], pred_rows: list[tuple], n_ref: int, n_pred: int,
                   allow_extra: bool) -> bool:
    """Is there an injective map of reference columns onto prediction columns making the tables equal?"""
    if len(ref_rows) != len(pred_rows):
        return False
    if not allow_extra and n_ref != n_pred:
        return False
    if n_ref > n_pred:
        return False
    if n_ref == 0:
        return True
    # Candidate prediction columns for each reference column: same multiset of values.
    ref_col_vals = [Counter(_norm_row((r[i],)) for r in ref_rows) for i in range(n_ref)]
    pred_col_vals = [Counter(_norm_row((r[j],)) for r in pred_rows) for j in range(n_pred)]
    candidates = [[j for j in range(n_pred) if pred_col_vals[j] == ref_col_vals[i]] for i in range(n_ref)]
    if any(not c for c in candidates):
        return False
    n_combos = 1
    for c in candidates:
        n_combos *= len(c)
    if n_combos > 20000:
        candidates = [c[:3] for c in candidates]
    for combo in itertools.product(*candidates):
        if len(set(combo)) != len(combo):
            continue
        if columns_match(ref_rows, pred_rows, list(range(n_ref)), list(combo)):
            return True
    return False


def _numeric_projection(cols: list[str], rows: list[tuple]) -> tuple[int, list[tuple]]:
    idx = [i for i in range(len(cols)) if rows and all(isinstance(r[i], float) or r[i] is None for r in rows)]
    return len(idx), [tuple(r[i] for i in idx) for r in rows]


def compare(ref, pred) -> dict:
    """Return strict/relaxed execution equivalence for two QueryResult objects.

    projection: the prediction returns a subset of the reference columns with the same rows
    (it answers the question without some of the reference's extra context columns), or a superset.
    numeric: same number of rows and the reference's numeric columns (counts, averages, ...)
    are reproduced, while non-numeric columns (group labels) may be presented differently,
    e.g. a sex shown as snomed:248153007 in one query and "male" in the other.
    """
    out = {"strict": False, "relaxed": False, "iri_relaxed": False, "numeric": False, "projection": False}
    if not (ref.ok and pred.ok):
        return out
    if ref.boolean is not None or pred.boolean is not None:
        same = ref.boolean == pred.boolean
        return {"strict": same, "relaxed": same, "iri_relaxed": same, "numeric": same, "projection": same}
    if ref.truncated or pred.truncated:
        return out
    rv, rr = table(ref)
    pv, pr = table(pred)
    out["strict"] = find_alignment(rr, pr, len(rv), len(pv), allow_extra=False)
    out["relaxed"] = out["strict"] or find_alignment(rr, pr, len(rv), len(pv), allow_extra=True)
    if not out["relaxed"]:
        rv, rr = table(ref, iri_local=True)
        pv, pr = table(pred, iri_local=True)
        out["iri_relaxed"] = find_alignment(rr, pr, len(rv), len(pv), allow_extra=True)
    else:
        out["iri_relaxed"] = True
    # projection: one table is a column subset of the other (same rows), in either direction
    rv, rr = table(ref, iri_local=True)
    pv, pr = table(pred, iri_local=True)
    out["projection"] = out["iri_relaxed"] or find_alignment(pr, rr, len(pv), len(rv), allow_extra=True)
    if out["iri_relaxed"]:
        out["numeric"] = True
    else:
        rv, rr = table(ref)
        pv, pr = table(pred)
        n_rn, rnum = _numeric_projection(rv, rr)
        n_pn, pnum = _numeric_projection(pv, pr)
        if n_rn and len(rr) == len(pr):
            out["numeric"] = find_alignment(rnum, pnum, n_rn, n_pn, allow_extra=True)
    return out
