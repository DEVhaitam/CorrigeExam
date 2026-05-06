#!/usr/bin/env bash
# run_scenario.sh
#
# Convenience wrapper for the layered compose deploy.
#
# Usage:
#   ./run_scenario.sh <scenario>                # bring up the stack with that scenario
#   ./run_scenario.sh <scenario> down           # tear down (preserves volumes)
#   ./run_scenario.sh <scenario> reset          # tear down AND remove volumes
#   ./run_scenario.sh <scenario> logs back      # tail logs of one service
#
# Examples:
#   ./run_scenario.sh S0-baseline
#   ./run_scenario.sh S1-heap-256m
#   ./run_scenario.sh S7-cpu-cap-half down
#
# After bring-up, give the stack ~60s to warm before running locust.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCENARIO="${1:-}"
ACTION="${2:-up}"

if [ -z "$SCENARIO" ]; then
  echo "Usage: $0 <scenario-name> [up|down|reset|logs <service>]"
  echo "Available scenarios:"
  ls "$REPO_ROOT/iac/stage1-compose/scenarios/" | sed 's/\.env$//' | sed 's/^/  /'
  exit 1
fi

ENV_FILE="$REPO_ROOT/iac/stage1-compose/scenarios/${SCENARIO}.env"
COMPOSE_BASE="$REPO_ROOT/docker-compose.yml"
COMPOSE_OVERRIDE="$REPO_ROOT/iac/stage1-compose/research-overrides.yml"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: scenario file not found: $ENV_FILE"
  exit 1
fi

# Capture iac_sha at deploy time. Surfaced via container labels so it's in
# every log line and queryable from the Prometheus pushgateway later.
export IAC_SHA="$(cd "$REPO_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo "untracked")"

COMPOSE_CMD=(
  docker compose
  --env-file "$ENV_FILE"
  -f "$COMPOSE_BASE"
  -f "$COMPOSE_OVERRIDE"
  --project-name "correctexam-${SCENARIO}"
)

case "$ACTION" in
  up)
    echo "[run_scenario] Deploying scenario: $SCENARIO"
    echo "[run_scenario] iac_sha: $IAC_SHA"
    "${COMPOSE_CMD[@]}" up -d --remove-orphans
    echo
    echo "[run_scenario] Stack starting. Useful endpoints:"
    echo "  Front:      http://localhost:8080"
    echo "  Back API:   http://localhost:8082"
    echo "  Quarkus metrics: http://localhost:8082/q/metrics"
    echo "  Prometheus: http://localhost:9092"
    echo "  Grafana:    http://localhost:3000  (admin/admin)"
    echo "  MySQL exporter: http://localhost:9104/metrics"
    echo
    echo "[run_scenario] Wait ~60s for full warmup before driving load."
    ;;
  down)
    "${COMPOSE_CMD[@]}" down --remove-orphans
    ;;
  reset)
    echo "[run_scenario] Tearing down scenario AND removing volumes"
    "${COMPOSE_CMD[@]}" down -v --remove-orphans
    ;;
  logs)
    SERVICE="${3:-}"
    if [ -z "$SERVICE" ]; then
      "${COMPOSE_CMD[@]}" logs -f --tail=100
    else
      "${COMPOSE_CMD[@]}" logs -f --tail=100 "$SERVICE"
    fi
    ;;
  *)
    echo "Unknown action: $ACTION"
    exit 1
    ;;
esac
