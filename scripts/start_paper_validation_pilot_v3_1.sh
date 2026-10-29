#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"

# shellcheck disable=SC1091
source "${PROJECT_ROOT}/scripts/qa_env.sh"

PREFLIGHT_ID="paper_v3_1_preflight_$(date +%Y%m%d_%H%M%S)"
python scripts/run_paper_validation_preflight.py \
  --config configs/paper_validation_pilot_v3_1.yaml \
  --run-id "${PREFLIGHT_ID}"

if [ "${ALLOW_PAID_PAPER_PILOT_V31:-0}" != "1" ]; then
    echo "Preflight passed, but the paid validation-only pilot is locked."
    echo "Set ALLOW_PAID_PAPER_PILOT_V31=1 after reviewing model and token cost."
    exit 2
fi

EXPERIMENT_ID="paper_v3_1_pilot_$(date +%Y%m%d_%H%M%S)"
TRACE_PATH="${PROJECT_ROOT}/log/${EXPERIMENT_ID}"
FACTOR_DATA_DIR="${PROJECT_ROOT}/data/research/paper_validation/factor_data/investment_data_20260725"
QLIB_DATA_DIR="${PROJECT_ROOT}/data/market/a_share/qlib/investment_data_20260725_cn"

export FACTOR_CoSTEER_DATA_FOLDER_OVERRIDE="${FACTOR_DATA_DIR}"
export FACTOR_CoSTEER_DATA_FOLDER_DEBUG_OVERRIDE="${FACTOR_DATA_DIR}"
export QLIB_DATA_DIR_OVERRIDE="${QLIB_DATA_DIR}"
export FACTOR_CoSTEER_SYMBOL_LENGTH_THRESHOLD=250
export FACTOR_CoSTEER_BASE_FEATURES_THRESHOLD=6
export FACTOR_CoSTEER_FREE_ARGS_RATIO_THRESHOLD=0.5
export FACTOR_CoSTEER_UNIQUE_VARS_RATIO_THRESHOLD=1.0
export FACTOR_CoSTEER_DUPLICATION_THRESHOLD=5
export CONFIG_PATH="configs/experiment_paper_pilot_v3_1.yaml"
export EXPERIMENT_ID
export QUANTAALPHA_LOG_TRACE_PATH="${TRACE_PATH}"

DIRECTION="${1:-CSI 300 paper-v3.1 validation-only mechanism pilot. Use only mechanism priors; do not mention or infer any 2022-2025 observations, metrics, winners, or test outcomes.}"
"${PROJECT_ROOT}/run.sh" "${DIRECTION}" "${EXPERIMENT_ID}"

TRAJECTORY_POOL="${TRACE_PATH}/trajectory_pool.json"
LIBRARY_PATH="${PROJECT_ROOT}/data/factorlib/all_factors_library_${EXPERIMENT_ID}.json"
POOL_OUTPUT="${PROJECT_ROOT}/data/results/paper_validation/${EXPERIMENT_ID}_factor_pool"
AUDIT_OUTPUT="${PROJECT_ROOT}/data/results/paper_validation/${EXPERIMENT_ID}_audit"

python scripts/build_paper_factor_pool.py \
  --config configs/paper_validation_pilot_v3_1.yaml \
  --library "${LIBRARY_PATH}" \
  --output-dir "${POOL_OUTPUT}"

python scripts/audit_paper_validation_pilot.py \
  --config configs/paper_validation_pilot_v3_1.yaml \
  --trajectory-pool "${TRAJECTORY_POOL}" \
  --library "${LIBRARY_PATH}" \
  --pool-dir "${POOL_OUTPUT}" \
  --output-dir "${AUDIT_OUTPUT}"

echo "Validation-only pilot complete: ${EXPERIMENT_ID}"
echo "Audit: ${AUDIT_OUTPUT}/report.md"
echo "Historical 2022-2025 test backtest was not run."
