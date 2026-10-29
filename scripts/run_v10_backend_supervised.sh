#!/usr/bin/env bash
# Explicit future V10 entrypoint. Does not alter the existing calibration launch.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export QA_BACKEND_ENTRYPOINT="frontend-v2/backend/v10_app.py"
export QA_BACKEND_HEALTH_URL="http://127.0.0.1:8000/api/v1/v10-runtime/ready"
export QA_BACKEND_INTRADAY_PREFLIGHT_ENABLED=true
export QA_BACKEND_INTRADAY_PREFLIGHT_SCRIPT="${SCRIPT_DIR}/verify_v10_runtime_readiness.py"
export QA_BACKEND_INTRADAY_PREFLIGHT_REPORT_FILE="${SCRIPT_DIR}/../data/results/v10-runtime-readiness-latest.json"
exec /bin/bash "${SCRIPT_DIR}/run_research_shadow_backend_supervised.sh"
