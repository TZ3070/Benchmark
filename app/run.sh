#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$APP_DIR/.." && pwd)"
ENV_FILE="${BENCHMARK_ENV_FILE:-$PROJECT_DIR/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  printf 'Environment file not found: %s\n' "$ENV_FILE" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  printf 'python3 is required but was not found in PATH\n' >&2
  exit 1
fi

# .env must use shell-compatible KEY=value syntax. Export its entries only
# to this process and the app it launches; never print secret values.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

cd "$PROJECT_DIR"
export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m benchmark.app "$@"
