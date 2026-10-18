#!/usr/bin/env bash
# Spins up a Virtuoso 7.2.16 store that mirrors the hackathon release setup, for
# execution-based evaluation of generated queries:
#   - ontologies loaded into <https://biomedit.ch/rdf/sphn-resource/global>
#   - RDFS rule set of the same name (OPTION (inference "...global"))
#   - one named graph per patient file: http://example.org/graph/<file name>
#
# The release's own start.sh depends on config.sh/lib.sh, which are not shipped in the
# release folder, so this script reproduces the same container settings directly.
# It loads a deterministic SAMPLE of patients (the full 55 GB cohort does not fit on disk).
#
# Usage: preparation/setup_virtuoso.sh [N_PATIENTS] [MAX_FILE_BYTES]
#   N_PATIENTS      number of patient files to load (default 2000)
#   MAX_FILE_BYTES  skip patient files larger than this (default 5000000)
# Environment: RELEASE_DIR (default ../hackathon-virtuoso-release), CONTAINER_NAME,
#              HTTP_PORT (8890), ISQL_PORT (1111), VIRTUOSO_BUFFERS (default 300000 x 8 KB)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_DIR="${RELEASE_DIR:-$HERE/../../hackathon-virtuoso-release}"
RELEASE_DIR="$(cd "$RELEASE_DIR" && pwd)"
CONTAINER_NAME="${CONTAINER_NAME:-nl2sparql-virtuoso}"
HTTP_PORT="${HTTP_PORT:-8890}"
ISQL_PORT="${ISQL_PORT:-1111}"
BUFFERS="${VIRTUOSO_BUFFERS:-300000}"
N_PATIENTS="${1:-2000}"
MAX_BYTES="${2:-5000000}"
RULE_SET="https://biomedit.ch/rdf/sphn-resource/global"
GRAPH_PREFIX="http://example.org/graph/"

isql() { docker exec "$CONTAINER_NAME" isql 1111 dba dba "$@" </dev/null; }

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
  if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
    echo "resuming $CONTAINER_NAME"; docker start "$CONTAINER_NAME" >/dev/null
  else
    echo "creating $CONTAINER_NAME"
    docker run -d --name "$CONTAINER_NAME" \
      -p "${ISQL_PORT}:1111" -p "${HTTP_PORT}:8890" \
      -v "$RELEASE_DIR/ontologies:/ontologies:ro" \
      -v "$RELEASE_DIR/patients:/patients:ro" \
      -v "${CONTAINER_NAME}-db:/database" \
      -e DBA_PASSWORD=dba \
      -e "VIRT_Parameters_DirsAllowed=.,/ontologies,/patients,/database,/usr/share/proj" \
      -e VIRT_Parameters_MaxQueryCostEstimationTime=0 \
      -e VIRT_SPARQL_MaxQueryCostEstimationTime=0 \
      -e VIRT_SPARQL_ResultSetMaxRows=1000000 \
      -e VIRT_SPARQL_MaxQueryExecutionTime=600 \
      -e "VIRT_Parameters_NumberOfBuffers=${BUFFERS}" \
      -e "VIRT_Parameters_MaxDirtyBuffers=$((BUFFERS * 3 / 4))" \
      openlink/virtuoso-opensource-7:7.2.16 >/dev/null
  fi
fi

echo "waiting for Virtuoso"
for _ in $(seq 1 120); do isql "exec=select 1;" >/dev/null 2>&1 && break; sleep 2; done

loaded="$(isql "exec=SPARQL SELECT COUNT(*) WHERE { GRAPH <${RULE_SET}> { ?s ?p ?o } };" | grep -Eo '^[0-9]+$' | head -1 || true)"
if [ "${loaded:-0}" -lt 1000000 ]; then
  echo "loading ontologies into <${RULE_SET}> (several minutes)"
  isql "exec=DELETE FROM DB.DBA.load_list; ld_dir('/ontologies', '*.ttl', '${RULE_SET}');"
  isql "exec=log_enable(2,1); checkpoint_interval(-1);"
  for _ in 1 2 3 4; do isql "exec=rdf_loader_run();" >/dev/null & done
  wait
  isql "exec=checkpoint; checkpoint_interval(60);"
  isql "exec=rdfs_rule_set('${RULE_SET}', '${RULE_SET}');"
  isql "exec=SELECT ll_file, ll_error FROM DB.DBA.load_list WHERE ll_error IS NOT NULL;"
fi

n_graphs="$(isql "exec=SPARQL SELECT COUNT(DISTINCT ?g) WHERE { GRAPH ?g { ?s a <https://biomedit.ch/rdf/sphn-schema/sphn#SubjectPseudoIdentifier> } FILTER(STRSTARTS(STR(?g), '${GRAPH_PREFIX}')) };" | grep -Eo '^[0-9]+$' | head -1 || true)"
if [ "${n_graphs:-0}" -lt "$N_PATIENTS" ]; then
  echo "selecting a deterministic sample of ${N_PATIENTS} patient files (<= ${MAX_BYTES} bytes)"
  sample_file="$HERE/work/patient_sample.txt"
  python3 - "$RELEASE_DIR/patients" "$N_PATIENTS" "$MAX_BYTES" "$sample_file" <<'EOF'
import os, sys, random
d, n, max_bytes, out = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
names = sorted(f for f in os.listdir(d) if f.endswith('.ttl'))
rng = random.Random(20261018)
rng.shuffle(names)
picked = []
for f in names:
    if os.path.getsize(os.path.join(d, f)) <= max_bytes:
        picked.append(f)
    if len(picked) == n:
        break
open(out, 'w').write('\n'.join(sorted(picked)) + '\n')
print(len(picked), 'files')
EOF
  echo "queueing patient files"
  isql "exec=DELETE FROM DB.DBA.load_list;"
  batch=""
  i=0
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    batch+="ld_add('/patients/${f}', '${GRAPH_PREFIX}${f%.ttl}'); "
    i=$((i + 1))
    if [ $((i % 40)) -eq 0 ]; then isql "exec=${batch}" >/dev/null; batch=""; fi
  done <"$sample_file"
  [ -n "$batch" ] && isql "exec=${batch}" >/dev/null
  echo "loading ${i} patient files"
  isql "exec=log_enable(2,1); checkpoint_interval(-1);"
  for _ in 1 2 3 4 5 6; do isql "exec=rdf_loader_run();" >/dev/null & done
  wait
  isql "exec=checkpoint; checkpoint_interval(60);"
  isql "exec=SELECT COUNT(*) FROM DB.DBA.load_list WHERE ll_error IS NOT NULL;"
fi

echo "ready: http://localhost:${HTTP_PORT}/sparql"
