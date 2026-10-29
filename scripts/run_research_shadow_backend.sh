#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Keep Screen and LaunchAgent on the same health-checking supervisor path.
exec "${PROJECT_ROOT}/scripts/run_research_shadow_backend_supervised.sh"
