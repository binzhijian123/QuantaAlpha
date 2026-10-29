#!/usr/bin/env bash
# Independent calibration process; the existing r3 service keeps its own port/PIDs.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
: "${QA_SHADOW_RECOVERY_R4_DECLARATION:?set the separately frozen r4 declaration}"
export BACKEND_HOST=127.0.0.1
export BACKEND_PORT="${QA_R4_BACKEND_PORT:-8012}"
case "${BACKEND_PORT}" in
    ''|*[!0-9]*) printf 'r4 port must be numeric\n' >&2; exit 1 ;;
esac
if [ "${BACKEND_PORT}" -lt 1024 ] || [ "${BACKEND_PORT}" -gt 65535 ] || [ "${BACKEND_PORT}" -eq 8000 ]; then
    printf 'r4 requires its own unprivileged port, separate from 8000\n' >&2
    exit 1
fi
export QA_BACKEND_ENTRYPOINT="frontend-v2/backend/recovery_r4_app.py"
export QA_BACKEND_HEALTH_URL="http://127.0.0.1:${BACKEND_PORT}/api/health/ready"
export QA_BACKEND_SUPERVISOR_PID_FILE="${PROJECT_ROOT}/data/results/recovery-r4-backend-supervisor.pid"
export QA_BACKEND_PREVENT_IDLE_SLEEP=true
export QA_BACKEND_SLEEP_GUARD_PID_FILE="${PROJECT_ROOT}/data/results/recovery-r4-sleep-guard.pid"
export QA_BACKEND_ALERT_EVENT_LOG="${PROJECT_ROOT}/data/results/recovery-r4-supervisor-events.jsonl"
export QA_BACKEND_INTRADAY_PREFLIGHT_ENABLED=false
exec /bin/bash "${SCRIPT_DIR}/run_research_shadow_backend_supervised.sh"
