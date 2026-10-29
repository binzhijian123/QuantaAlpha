#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/qa_env.sh"
exec python "${SCRIPT_DIR}/qualify_intraday_v4.py" "$@"
