"""Layer 1: query templates copied from the shapes of the official examples. Each recipe fills its holes from
profile.json and returns the shorthand query plus a plain-English `meaning` of what it computes."""
from collections import defaultdict

from common import CFG, NS_NAMES, words

SEX = '(snomed:248153007 "male") (snomed:248152002 "female") (snomed:32570681000036106 "indeterminate")'
NOT_CONDITIONS = {"sphn:AdministrativeSex", "sphn:Unit"}


class Profile:
    def __init__(self, P, rng):
        self.P, self.rng, m = P, rng, CFG["min_patients"]
        self.codes = defaultdict(list)
        for c in P["codes"]:
            self.codes[(c["class"], c["prop"])].append(c)
        prop_n = lambda c, p: P["props"].get(c, {}).get(p, 0)
        self.conditions = [k for k in self.codes if k[1] == "sphn:hasCode" and k[0] not in NOT_CONDITIONS
                           and prop_n(*k) >= m and not self.codes[k][0]["code"].startswith("ucum:")]
        self.categorical = [k for k, v in self.codes.items()
                            if prop_n(*k) >= m and 2 <= sum(not c.get("parent") for c in v) <= 30 and k[0] != "sphn:Unit"]
        self.indiv = defaultdict(list)
        for i in P["individuals"]:
            self.indiv[(i["class"], i["prop"])].append(i["value"])
        self.indiv_attrs = [k for k, v in self.indiv.items() if len(v) >= 2 and prop_n(*k) >= m]
        self.measures = [x for x in P["measures"] if x["patients"] >= m and x["max"] > x["min"]]
        self.props = lambda c: [p for p, n in P["props"].get(c, {}).items() if n >= m]

    def pick(self, seq):
        return seq[self.rng.randrange(len(seq))] if seq else None

    def codes_for(self, key, k=None, absent_ok=True):
        """1-3 codes for a (class, property): mostly common, some rare, a few absent from S (legit zeros)."""
        k = k or self.rng.choice([1, 1, 1, 2, 3])
        pool = sorted(self.codes[key], key=lambda c: -c["patients"])
        common, rare = pool[:max(1, len(pool) // 5)], pool[max(1, len(pool) // 5):] or pool
        absent = self.P["absent"].get(pool[0]["code"].split(":")[0], []) if absent_ok else []
        mix, out = CFG["code_mix"], []
        for _ in range(20 * k):  # bounded: a class may have fewer codes than asked for
            if len(out) == k:
                break
            r = self.rng.random()
            src = common if r < mix["common"] else rare if r < mix["common"] + mix["rare"] or not absent else None
            code = self.pick(src)["code"] if src else self.pick(absent)
            if code not in out:
                out.append(code)
        return out

    def label(self, code):
        return self.P["labels"].get(code, {}).get("en") or code.split(":", 1)[1]

    def describe(self, codes):
        ns = NS_NAMES.get(codes[0].split(":")[0], codes[0].split(":")[0])
        return " or ".join(f"{self.label(c)} ({ns} {c.split(':', 1)[1]}, including sub-codes)" for c in codes)

    def cond(self, v, key, codes):
        """Shorthand pattern: a record ?v of the class whose coded property matches one of the codes."""
        isa = " UNION ".join(f"{{ ?{v}_c isa {c} }}" for c in codes) if len(codes) > 1 else f"?{v}_c isa {codes[0]} ."
        return f"?{v} a {key[0]} ; {key[1]} ?{v}_c . {isa}"

    def condition(self, v="r", keys=None, absent_ok=True):
        key = self.pick(keys or self.conditions)
        if not key:
            return None
        codes = self.codes_for(key, absent_ok=absent_ok)
        absent = any(c not in {x["code"] for x in self.codes[key]} for c in codes)
        text = f"at least one {words(key[0])} coded as {self.describe(codes)}"
        return {"key": key, "codes": codes, "pattern": self.cond(v, key, codes), "text": text, "zero_ok": absent}


def nice(x):
    return float(f"{x:.2g}")


def r01(pf):
    c = pf.condition()
    q = f"SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE {{ GRAPH ?g {{ {c['pattern']} }} }}"
    return c, q, f"Counts distinct patients with {c['text']}.", "one number"


def r02(pf):
    c = pf.condition()
    q = (f"SELECT ?sex (COUNT(DISTINCT ?g) AS ?patients) WHERE {{ GRAPH ?g {{ {c['pattern']} "
         f"?s a sphn:AdministrativeSex ; sphn:hasCode ?sc . ?sc a ?sex_concept . }} "
         f"VALUES (?sex_concept ?sex) {{ {SEX} }} }} GROUP BY ?sex ORDER BY DESC(?patients)")
    return c, q, f"Counts distinct patients with {c['text']}, split by administrative sex.", "one row per sex with a patient count"


def r03(pf):
    a, b = pf.condition("a"), pf.condition("b", absent_ok=False)  # b is the denominator of the share
    if a["codes"] == b["codes"]:
        return None
    q = (f"SELECT (COUNT(DISTINCT ?g) AS ?patients_b) (COUNT(DISTINCT ?ab) AS ?patients_a_and_b) "
         f"(ROUND(1000.0 * COUNT(DISTINCT ?ab) / COUNT(DISTINCT ?g)) / 10 AS ?pct) "
         f"WHERE {{ GRAPH ?g {{ {b['pattern']} }} OPTIONAL {{ GRAPH ?g {{ {a['pattern']} }} BIND(?g AS ?ab) }} }}")
    a["zero_ok"] = a["zero_ok"] or b["zero_ok"]
    a["codes"] = a["codes"] + b["codes"]
    return a, q, (f"Among patients with {b['text']}, counts how many also have {a['text']}, "
                  f"and gives that share as a percentage."), "three numbers: group size, patients with both, percentage"


def r04(pf):
    a, b = pf.condition("a"), pf.condition("b")
    if a["codes"] == b["codes"]:
        return None
    q = f"SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE {{ GRAPH ?g {{ {a['pattern']} {b['pattern']} }} }}"
    a["zero_ok"], a["codes"] = True, a["codes"] + b["codes"]
    return a, q, f"Counts distinct patients with both {a['text']} and {b['text']}.", "one number"


def r05(pf):
    a, b = pf.condition("a"), pf.condition("b")
    if a["codes"] == b["codes"]:
        return None
    q = (f"SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE {{ GRAPH ?g {{ {a['pattern']} }} "
         f"FILTER NOT EXISTS {{ GRAPH ?g {{ {b['pattern']} }} }} }}")
    a["codes"] = a["codes"] + b["codes"]
    return a, q, f"Counts distinct patients with {a['text']} but without {b['text']}.", "one number"


def r06(pf):
    if pf.rng.random() < 0.5 and pf.indiv_attrs:
        cls, prop = pf.pick(pf.indiv_attrs)
        body, value = f"?r a {cls} ; {prop} ?v .", 'BIND(STRAFTER(STR(?v), "#") AS ?value)'
    else:
        cls, prop = pf.pick(pf.categorical)
        body, value = f"?r a {cls} ; {prop} ?v_c . ?v_c a ?v .", 'BIND(REPLACE(STR(?v), "^.*/", "") AS ?value)'
    q = (f"SELECT ?value (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?r) AS ?records) "
         f"WHERE {{ GRAPH ?g {{ {body} }} {value} }} GROUP BY ?value ORDER BY DESC(?records)")
    c = {"key": (cls, prop), "codes": [], "zero_ok": False}
    return c, q, (f"For each value of the {words(prop)} of {words(cls)} records, counts records and distinct patients."), \
        "one row per value with patient and record counts"


def measure(pf):
    m = pf.pick(pf.measures)
    pattern = f"?r a {m['class']} ; sphn:hasQuantity ?q . ?q sphn:hasValue ?v ; sphn:hasUnit/sphn:hasCode ?u . ?u a {m['unit']} ."
    return m, pattern, f"{words(m['class'])} values recorded in {pf.label(m['unit'])}"


def r07(pf):
    m, pattern, what = measure(pf)
    q = (f"SELECT (COUNT(DISTINCT ?g) AS ?patients) (ROUND(10 * AVG(?v)) / 10 AS ?avg) (MIN(?v) AS ?min) (MAX(?v) AS ?max) "
         f"WHERE {{ GRAPH ?g {{ {pattern} }} }}")
    return {"key": (m["class"], m["unit"]), "codes": [], "zero_ok": False}, q, \
        f"Gives the number of patients and the average, minimum and maximum of {what}.", "four numbers"


def r08(pf):
    m, pattern, what = measure(pf)
    t1, t2 = nice(m["min"] + (m["max"] - m["min"]) / 3), nice(m["min"] + 2 * (m["max"] - m["min"]) / 3)
    if not t1 < t2:
        return None
    q = (f"SELECT (SUM(IF(?v < {t1}, 1, 0)) AS ?low) (SUM(IF(?v >= {t1} && ?v < {t2}, 1, 0)) AS ?middle) "
         f"(SUM(IF(?v >= {t2}, 1, 0)) AS ?high) WHERE {{ GRAPH ?g {{ {pattern} }} }}")
    return {"key": (m["class"], m["unit"]), "codes": [], "zero_ok": False}, q, \
        f"Counts {what} in three bands: below {t1}, from {t1} to below {t2}, and {t2} or more.", "three counts"


def r09(pf):
    classes = [c for c in pf.P["props"] if {"sphn:hasStartDateTime", "sphn:hasEndDateTime"} <= set(pf.props(c))]
    cls = pf.pick(classes)
    if not cls:
        return None
    coded = [k for k in pf.conditions if k[0] == cls]
    c = {"key": (cls,), "codes": [], "zero_ok": False, "text": f"{words(cls)} records"}
    head = f"?r a {cls} ."
    if coded and pf.rng.random() < 0.6:
        codes = pf.codes_for(coded[0])
        c.update(codes=codes, text=f"{words(cls)} records coded as {pf.describe(codes)}")
        head = pf.cond("r", coded[0], codes)
    unit = pf.rng.choice(["hour", "day"])
    q = (f"SELECT (COUNT(DISTINCT ?r) AS ?records) (COUNT(DISTINCT ?g) AS ?patients) (ROUND(AVG(?d)) AS ?avg_{unit}s) "
         f"(MIN(?d) AS ?min_{unit}s) (MAX(?d) AS ?max_{unit}s) WHERE {{ GRAPH ?g {{ {head} "
         f"?r sphn:hasStartDateTime ?start ; sphn:hasEndDateTime ?end . }} FILTER(?end >= ?start) "
         f'BIND(bif:datediff("{unit}", ?start, ?end) AS ?d) }}')
    return c, q, f"For {c['text']}, gives the average, minimum and maximum duration from start to end in {unit}s.", \
        "five numbers: records, patients, average, minimum, maximum"


def r12(pf):
    parents = [c for k in pf.conditions for c in pf.codes[k] if c.get("parent")]
    p = pf.pick(parents)
    if not p:
        return None
    key, ns = (p["class"], p["prop"]), p["code"].split(":")[0]
    q = (f"SELECT ?code (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?r) AS ?records) "
         f"WHERE {{ GRAPH ?g {{ ?r a {key[0]} ; {key[1]} ?c . ?c isa {p['code']} . ?c a ?concept . }} "
         f'FILTER(STRSTARTS(STR(?concept), STR({ns}:))) BIND(REPLACE(STR(?concept), "^.*/", "") AS ?code) }} '
         f"GROUP BY ?code ORDER BY DESC(?patients) LIMIT 10")
    return {"key": key, "codes": [p["code"]], "zero_ok": False}, q, \
        (f"Lists the 10 most frequent codes under {pf.describe([p['code']])} on {words(key[0])} records, "
         f"with the number of patients and records for each."), "up to 10 rows: code, patients, records"


def r13(pf):
    keys = [k for k in pf.conditions if "sphn-individual:Principal" in pf.indiv.get((k[0], "sphn:hasRank"), [])]
    if not keys:
        return None
    key = pf.pick(keys)
    codes = pf.codes_for(key, absent_ok=False)
    q = (f"SELECT (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?principal) AS ?patients_principal) "
         f"(ROUND(1000.0 * COUNT(DISTINCT ?principal) / COUNT(DISTINCT ?g)) / 10 AS ?principal_pct) "
         f"WHERE {{ GRAPH ?g {{ {pf.cond('r', key, codes)} }} "
         f"OPTIONAL {{ GRAPH ?g {{ ?r sphn:hasRank sphn-individual:Principal . }} BIND(?g AS ?principal) }} }}")
    return {"key": key, "codes": codes, "zero_ok": False}, q, \
        (f"Counts patients with a {words(key[0])} coded as {pf.describe(codes)}, how many of them have it as the "
         f"principal one, and that share as a percentage."), "three numbers"


def r18(pf):
    keys = [k for k in pf.conditions if "sphn:hasSubjectAge" in pf.props(k[0])]
    if not keys:
        return None
    c = pf.condition(keys=keys)
    q = (f"SELECT (COUNT(DISTINCT ?g) AS ?patients) (ROUND(AVG(?age)) AS ?avg_age) (MIN(?age) AS ?min_age) (MAX(?age) AS ?max_age) "
         f"WHERE {{ GRAPH ?g {{ {c['pattern']} ?r sphn:hasSubjectAge ?a . ?a sphn:hasQuantity/sphn:hasValue ?age . }} }}")
    return c, q, f"For patients with {c['text']}, gives the average, minimum and maximum age recorded with that record.", "four numbers"


def r19(pf):
    c = pf.condition() if pf.rng.random() < 0.6 else None
    cls = c["key"][0] if c else pf.pick(list(pf.P["classes"]))
    dates = [p for p in pf.props(cls) if p.endswith("DateTime")]
    if not dates:
        return None
    d = pf.pick(dates)
    body = c["pattern"] if c else f"?r a {cls} ."
    q = (f"SELECT ?year (COUNT(DISTINCT ?g) AS ?patients) (COUNT(DISTINCT ?r) AS ?records) "
         f"WHERE {{ GRAPH ?g {{ {body} ?r {d} ?date . }} BIND(YEAR(?date) AS ?year) }} GROUP BY ?year ORDER BY ?year")
    what = c["text"] if c else f"at least one {words(cls)} record"
    c = c or {"key": (cls,), "codes": [], "zero_ok": False}
    return c, q, f"Per year of the {words(d)}, counts distinct patients and records with {what}.", "one row per year"


def r20(pf):
    cls = pf.pick([c for c, v in pf.P["classes"].items() if v["patients"] >= CFG["min_patients"]])
    q = (f"SELECT (COUNT(*) AS ?patients) (ROUND(10 * AVG(?n)) / 10 AS ?avg_per_patient) (MIN(?n) AS ?min_per_patient) "
         f"(MAX(?n) AS ?max_per_patient) WHERE {{ {{ SELECT ?g (COUNT(DISTINCT ?r) AS ?n) "
         f"WHERE {{ GRAPH ?g {{ ?r a {cls} . }} }} GROUP BY ?g }} }}")
    return {"key": (cls,), "codes": [], "zero_ok": False}, q, \
        (f"Among patients with at least one {words(cls)} record, gives how many there are and the average, "
         f"minimum and maximum number of such records per patient."), "four numbers"


RECIPES = {"R01_count_coded": r01, "R02_by_sex": r02, "R03_share_a_among_b": r03, "R04_a_and_b": r04,
           "R05_a_not_b": r05, "R06_distribution": r06, "R07_measure_stats": r07, "R08_value_bands": r08,
           "R09_duration": r09, "R12_top_codes": r12, "R13_principal_share": r13, "R18_age_at_record": r18,
           "R19_per_year": r19, "R20_records_per_patient": r20}


RULES_TEXT = """Database rules:
- One named graph per patient. Patterns about a patient go inside GRAPH ?g { ... }; count patients with COUNT(DISTINCT ?g).
- Records: ?r a sphn:SomeClass ; sphn:hasSomething ?x . Codes hang off records: ?r sphn:hasCode ?c . ?c isa icd-10-gm:C91 .
  `?c isa X` matches code X, all its sub-codes and all yearly versions. Use `?c a ?concept` to read the exact code.
- Numbers: ?r sphn:hasQuantity ?q . ?q sphn:hasValue ?v ; sphn:hasUnit/sphn:hasCode ?u . ?u a ucum:X .
- Dates are xsd:dateTime; durations with bif:datediff("day", ?start, ?end); YEAR(?date) for years.
- Code labels and hierarchy live in GRAPH <https://biomedit.ch/rdf/sphn-resource/global> (rdfs:label, rdfs:subClassOf).
- Write no PREFIX lines. Prefixes: sphn, sphn-individual, icd-10-gm, chop, snomed, loinc, ucum, sphn-atc, bif, rdfs.
- SELECT only. Every output column is an aggregate (COUNT, SUM, AVG, MIN, MAX) or a GROUP BY key that is a value,
  a code or a category; never output patient graphs, record IRIs or identifiers."""


def card(pf, cls):
    """Concept card: what a class looks like in S, for prompts."""
    P = pf.P
    lines = [f"{cls} ({words(cls)}), in {P['classes'][cls]['patients']} patients",
             "  properties: " + ", ".join(pf.props(cls))]
    for (c, prop), codes in pf.codes.items():
        if c == cls:
            top = sorted(codes, key=lambda x: -x["patients"])[:5]
            lines.append(f"  {prop} codes, e.g.: " + "; ".join(f'{x["code"]} "{pf.label(x["code"])}"' for x in top))
    for (c, prop), vals in pf.indiv.items():
        if c == cls:
            lines.append(f"  {prop} values: " + ", ".join(vals))
    units = [m["unit"] for m in pf.measures if m["class"] == cls]
    if units:
        lines.append("  sphn:hasQuantity units: " + ", ".join(units))
    return "\n".join(lines)


def cards(pf, k):
    classes = [c for c, v in pf.P["classes"].items() if v["patients"] >= CFG["min_patients"]]
    return "\n".join(card(pf, c) for c in pf.rng.sample(classes, min(k, len(classes))))
