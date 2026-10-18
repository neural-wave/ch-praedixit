# Gate B rule examples

One folder per rule of `pppp/src/checker/GateB/rules.py`, named after the rule's value:

```
gate_b_examples/
  small_group/
    block/01-having-one.rq      # must be reported with small_group
    pass/01-having-at-least-ten.rq  # must get no violation at all
```

Each file is a runnable query preceded by one `# why:` line saying what it shows. Keep examples
small: one rule per file, nothing else that could trigger another rule. `pass` examples are the
look-alikes that a too-broad check would reject by mistake; they matter as much as `block` ones.

`pppp/tests/test_gate_b.py` runs every example, and fails when a rule has no `block` example or a
folder is not a rule name.

## How Gate B decides

Gate B gives every variable a label, from the least to the most identifying
(`variable_labels.py`):

| Label | Meaning | Example |
| --- | --- | --- |
| CATEGORY | shared by many patients | `?code a ?class`, a value-set individual, a constant |
| AGGREGATE | a COUNT, SUM or AVG | `COUNT(DISTINCT ?g)` |
| BUCKET | a range of record values | `YEAR(?date)`, `FLOOR(?age / 10)`, `sphn:hasYear` |
| RECORD_VALUE | a raw value of one record | a date-time, a measurement, free text |
| SENSITIVE | a patient graph, record IRI or identifier | `GRAPH ?g`, `?diagnosis`, `sphn:hasIdentifier` |

Then the checks of `checker.CHECKS` run in order, in four layers: query form, triple patterns,
patients selected through values, and what the outer query returns. Most checks are a sentence
about labels: "a SENSITIVE value compared with a constant", "a GROUP BY key that is a
RECORD_VALUE"...

To see both for one query:

```bash
python -m pppp.checker.GateB.explain query.rq
```

## How to add a rule

1. **Write the rule** in `pppp/RULES.md`, in one sentence with an example.
2. **Add it to `rules.py`**: a `Rule` member, with a comment naming the original rule it refines.
3. **Write the examples first**: `gate_b_examples/<rule value>/block/` with the queries it must
   catch, `pass/` with the close ones it must let through. Then run
   ```bash
   python -m pytest pppp/tests -k <rule value>
   ```
   The `block` examples fail: that is the specification of the check.
4. **Write the check**: a function `..._violations(algebra_root, ...)` returning
   `[Violation(Rule.<NEW>, reason)]`, in the module of its layer (`form_checks.py`,
   `pattern_checks.py`, `sensitivity_checks.py`, `output_checks.py`). The reason is a sentence a
   researcher understands: what is wrong and, when possible, what to write instead.
5. **Register it** in `CHECKS` in `checker.py`: its layer, a name, what it looks for, and how to
   call it on the `QueryContext` (`context.algebra_root`, `context.labels`, `context.vocabulary`,
   `context.fixed_values`).
6. **Check it**: `explain` on the examples shows the check and its verdict; then
   ```bash
   python -m pytest pppp/tests
   ```
   runs every example and every labelled set (`data/evaluation_set`, `data/test-queries`,
   `data/evaluation_marzio`), so a new rule cannot silently reject compliant entries.
