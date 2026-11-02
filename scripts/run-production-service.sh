#!/usr/bin/env bash
set -euo pipefail
umask 077
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$PROJECT_ROOT/scripts/load-provider-env.sh"
set -a
. "${EMNETS_PRODUCTION_ENV_FILE:-$PROJECT_ROOT/.local/production/services.env}"
set +a
PY="$PROJECT_ROOT/.venv-web/bin/python"

case "${1:-}" in
  research)
    exec "$PY" -u "$PROJECT_ROOT/backend/services/agent_bridge.py" \
      --host 127.0.0.1 --port "$EMNETS_PRODUCTION_AGENT_PORT" --python "$PY" \
      --agent-dir "$PROJECT_ROOT/backend/agent" --runs-dir "$EMNETS_PRODUCTION_RUNS_DIR" \
      --pool-idea 1 --pool-exp 1 --pool-code 1 --pool-exec 1 --pool-write 1 \
      --total-gpus 8 --gpus-per-project 1 --discussion-mode --discussion-rounds 2 \
      --discussion-models "$EMNETS_MODEL"
    ;;
  review)
    exec "$PY" -u "$PROJECT_ROOT/backend/services/review_service.py" \
      --host 127.0.0.1 --port "$EMNETS_PRODUCTION_REVIEW_PORT"
    ;;
  resources)
    exec "$PY" -u "$PROJECT_ROOT/backend/services/resource_monitor.py" \
      --host 127.0.0.1 --port "$EMNETS_PRODUCTION_RESOURCE_PORT"
    ;;
  gateway)
    exec "$EMNETS_CADDY_BIN" run --config "$PROJECT_ROOT/deploy/Caddyfile" --adapter caddyfile
    ;;
  validate)
    exec "$EMNETS_CADDY_BIN" validate --config "$PROJECT_ROOT/deploy/Caddyfile" --adapter caddyfile
    ;;
  tunnel)
    unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
    exec "$EMNETS_CLOUDFLARED_BIN" tunnel --no-autoupdate --protocol http2 \
      --edge-ip-version 4 --url "http://127.0.0.1:$EMNETS_GATEWAY_PORT"
    ;;
  *) printf 'Usage: %s research|review|resources|gateway|validate|tunnel\n' "$0" >&2; exit 2 ;;
esac
