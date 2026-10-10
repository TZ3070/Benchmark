#!/usr/bin/env bash
# Export a full LiteLLM SpendLogs table snapshot as UTC ISO-week JSONL.
# Configure LITELLM_COMPOSE_DIR to the existing LiteLLM Compose project.
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
LITELLM_COMPOSE_DIR="${LITELLM_COMPOSE_DIR:?Set LITELLM_COMPOSE_DIR to the existing LiteLLM Compose project directory}"
COMPOSE_FILE="${LITELLM_COMPOSE_FILE:-$LITELLM_COMPOSE_DIR/compose.yaml}"
POSTGRES_SERVICE="${LITELLM_POSTGRES_SERVICE:-postgres}"
OUTPUT_DIR="${SPENDLOGS_OUTPUT_DIR:-$PROJECT_DIR/data/spendlogs}"

mkdir -p "$OUTPUT_DIR"
chmod 700 "$OUTPUT_DIR"

WEEK="$(date -u +%G-W%V)"
EXPORTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
FINAL_JSONL="$OUTPUT_DIR/${WEEK}.jsonl"
FINAL_MANIFEST="$OUTPUT_DIR/${WEEK}.manifest.json"
TEMP_JSONL="$OUTPUT_DIR/.${WEEK}.jsonl.tmp.$$"
TEMP_MANIFEST="$OUTPUT_DIR/.${WEEK}.manifest.json.tmp.$$"

cleanup() {
  rm -f "$TEMP_JSONL" "$TEMP_MANIFEST"
}
trap cleanup EXIT

# Each weekly file is a full-table snapshot. Replay filters rows by startTime.
docker compose \
  --project-directory "$LITELLM_COMPOSE_DIR" \
  -f "$COMPOSE_FILE" \
  exec -T "$POSTGRES_SERVICE" sh -lc \
  'psql -X -U "${POSTGRES_USER:-litellm}" -d "${POSTGRES_DB:-litellm}" -Atc "SELECT row_to_json(t)::text FROM \"LiteLLM_SpendLogs\" AS t ORDER BY \"startTime\";"' \
  > "$TEMP_JSONL"

ROW_COUNT="$(wc -l < "$TEMP_JSONL" | tr -d '[:space:]')"
if command -v sha256sum >/dev/null 2>&1; then
  SHA256="$(sha256sum "$TEMP_JSONL" | awk '{print $1}')"
else
  SHA256="$(shasum -a 256 "$TEMP_JSONL" | awk '{print $1}')"
fi

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
