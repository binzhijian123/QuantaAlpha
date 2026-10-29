#!/usr/bin/env bash
# Shared QuantaAlpha environment bootstrap.
#
# Source this file from project scripts before running Python/Node commands:
#   source "${PROJECT_ROOT}/scripts/qa_env.sh"

QA_ENV_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QA_PROJECT_ROOT="$(cd "${QA_ENV_SCRIPT_DIR}/.." && pwd)"
QA_CONDA_HOME="${QA_CONDA_HOME:-${HOME}/miniforge3}"
QA_CONDA_ENV="${CONDA_ENV_NAME:-quantaalpha}"

qa_fail() {
    echo "Error: $*" >&2
    return 1
}

qa_activate_conda_env() {
    if [ -f "${QA_CONDA_HOME}/etc/profile.d/conda.sh" ]; then
        # shellcheck disable=SC1091
        source "${QA_CONDA_HOME}/etc/profile.d/conda.sh"
    elif [ -x "${QA_CONDA_HOME}/bin/conda" ]; then
        eval "$("${QA_CONDA_HOME}/bin/conda" shell.bash hook)"
    elif command -v conda >/dev/null 2>&1; then
        eval "$(conda shell.bash hook)"
    else
        qa_fail "conda not found. Set QA_CONDA_HOME to your Miniforge/Conda path."
        return 1
    fi

    conda activate "${QA_CONDA_ENV}" || {
        qa_fail "failed to activate conda env '${QA_CONDA_ENV}'"
        return 1
    }

    case ":${PYTHONPATH:-}:" in
        *":${QA_PROJECT_ROOT}:"*) ;;
        *) export PYTHONPATH="${QA_PROJECT_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" ;;
    esac

    export QA_PROJECT_ROOT QA_CONDA_HOME QA_CONDA_ENV
}

qa_print_env_summary() {
    echo "Conda env: ${QA_CONDA_ENV}"
    echo "Python: $(python --version 2>&1)"
    echo "Python path: $(command -v python)"
    if command -v node >/dev/null 2>&1; then
        echo "Node: $(node --version)"
    fi
    if command -v npm >/dev/null 2>&1; then
        echo "npm: $(npm --version)"
    fi
}

qa_activate_conda_env
