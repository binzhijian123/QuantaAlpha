#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"

# shellcheck disable=SC1091
source "${PROJECT_ROOT}/scripts/qa_env.sh"

PREFLIGHT_ID="paper_preflight_$(date +%Y%m%d_%H%M%S)"
python scripts/run_paper_validation_preflight.py \
  --config configs/paper_validation_v3.yaml \
  --run-id "${PREFLIGHT_ID}"

if [ "${ALLOW_PAID_PAPER_PILOT:-0}" != "1" ]; then
    echo "Preflight passed, but the paid mechanism pilot is locked."
    echo "Set ALLOW_PAID_PAPER_PILOT=1 only after reviewing model and token cost."
    exit 2
fi

FACTOR_DATA_DIR="${PROJECT_ROOT}/data/research/paper_validation/factor_data/investment_data_20260725"
export FACTOR_CoSTEER_DATA_FOLDER_OVERRIDE="${FACTOR_DATA_DIR}"
export FACTOR_CoSTEER_SYMBOL_LENGTH_THRESHOLD=250
export FACTOR_CoSTEER_BASE_FEATURES_THRESHOLD=6
export FACTOR_CoSTEER_FREE_ARGS_RATIO_THRESHOLD=0.5
export FACTOR_CoSTEER_UNIQUE_VARS_RATIO_THRESHOLD=1.0
export FACTOR_CoSTEER_DUPLICATION_THRESHOLD=5
export CONFIG_PATH="configs/experiment_paper_pilot_v3.yaml"
export EXPERIMENT_ID="paper_v3_pilot_$(date +%Y%m%d_%H%M%S)"

DIRECTION="${1:-CSI 300 low-cost paper-v3 mechanism pilot}"
"${PROJECT_ROOT}/run.sh" "${DIRECTION}" "${EXPERIMENT_ID}"

LIBRARY_PATH="${PROJECT_ROOT}/data/factorlib/all_factors_library_${EXPERIMENT_ID}.json"
POOL_OUTPUT="${PROJECT_ROOT}/data/results/paper_validation/${EXPERIMENT_ID}_factor_pool"
python scripts/build_paper_factor_pool.py \
  --config configs/paper_validation_v3.yaml \
  --library "${LIBRARY_PATH}" \
  --output-dir "${POOL_OUTPUT}"

python -m quantaalpha.backtest.run_backtest \
  --config configs/backtest_paper_v3.yaml \
  --factor-source custom \
  --factor-json "${POOL_OUTPUT}/selected_factor_library.json" \
  --experiment "${EXPERIMENT_ID}" \
  --output-name "${EXPERIMENT_ID}"
