#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${LITELLM_COMPOSE_DIR:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
COMPOSE_FILE="${LITELLM_COMPOSE_FILE:-$COMPOSE_DIR/compose.yaml}"
POSTGRES_SERVICE="${LITELLM_POSTGRES_SERVICE:-postgres}"
OUTPUT_DIR="${SPENDLOGS_OUTPUT_DIR:-$SCRIPT_DIR/../data/spendlogs}"

mkdir -p "$OUTPUT_DIR"
chmod 700 "$OUTPUT_DIR"

WEEK="$(date -u +%G-W%V)"
EXPORTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BASENAME="$WEEK"
FINAL_JSONL="$OUTPUT_DIR/${BASENAME}.jsonl"
FINAL_MANIFEST="$OUTPUT_DIR/${BASENAME}.manifest.json"
TEMP_JSONL="$OUTPUT_DIR/.${BASENAME}.jsonl.tmp.$$"
TEMP_MANIFEST="$OUTPUT_DIR/.${BASENAME}.manifest.json.tmp.$$"

cleanup() {
  rm -f "$TEMP_JSONL" "$TEMP_MANIFEST"
}
trap cleanup EXIT

docker compose \
  --project-directory "$COMPOSE_DIR" \
  -f "$COMPOSE_FILE" \
  exec -T "$POSTGRES_SERVICE" sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -X -U litellm -d litellm -Atc "SELECT row_to_json(t)::text FROM \"LiteLLM_SpendLogs\" AS t ORDER BY \"startTime\";"' \
  > "$TEMP_JSONL"

ROW_COUNT="$(wc -l < "$TEMP_JSONL" | tr -d '[:space:]')"
SHA256="$(sha256sum "$TEMP_JSONL" | awk '{print $1}')"

cat > "$TEMP_MANIFEST" <<EOF
{
  "source_table": "LiteLLM_SpendLogs",
  "exported_at_utc": "$EXPORTED_AT",
  "iso_week_utc": "$WEEK",
  "format": "jsonl",
  "scope": "full_table_snapshot",
  "row_count": $ROW_COUNT,
  "sha256": "$SHA256"
}
EOF

mv -f "$TEMP_JSONL" "$FINAL_JSONL"
mv -f "$TEMP_MANIFEST" "$FINAL_MANIFEST"
trap - EXIT

printf 'Exported %s rows to %s\n' "$ROW_COUNT" "$FINAL_JSONL"
printf 'Manifest: %s\n' "$FINAL_MANIFEST"
