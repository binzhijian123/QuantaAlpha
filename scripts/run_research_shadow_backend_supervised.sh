#!/usr/bin/env bash
set -uo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RESTART_DELAY_SECONDS="${QA_BACKEND_RESTART_DELAY_SECONDS:-5}"
RESTART_MAX_DELAY_SECONDS="${QA_BACKEND_RESTART_MAX_DELAY_SECONDS:-300}"
RESTART_RESET_SECONDS="${QA_BACKEND_RESTART_RESET_SECONDS:-900}"
PID_FILE="${QA_BACKEND_SUPERVISOR_PID_FILE:-${PROJECT_ROOT}/data/results/research-shadow-backend-supervisor.pid}"
BACKEND_ENTRYPOINT="${QA_BACKEND_ENTRYPOINT:-frontend-v2/backend/local_app.py}"
HEALTH_URL="${QA_BACKEND_HEALTH_URL:-http://127.0.0.1:8000/api/health/ready}"
HEALTH_INTERVAL_SECONDS="${QA_BACKEND_HEALTH_INTERVAL_SECONDS:-10}"
HEALTH_TIMEOUT_SECONDS="${QA_BACKEND_HEALTH_TIMEOUT_SECONDS:-3}"
HEALTH_FAILURE_THRESHOLD="${QA_BACKEND_HEALTH_FAILURE_THRESHOLD:-3}"
STARTUP_GRACE_SECONDS="${QA_BACKEND_STARTUP_GRACE_SECONDS:-30}"
TERMINATION_GRACE_SECONDS="${QA_BACKEND_TERMINATION_GRACE_SECONDS:-5}"
ALERT_SCRIPT="${QA_BACKEND_ALERT_SCRIPT:-${PROJECT_ROOT}/scripts/research_shadow_supervisor_alert.py}"
ALERT_REPEAT_SECONDS="${QA_BACKEND_ALERT_REPEAT_SECONDS:-900}"
INTRADAY_PREFLIGHT_ENABLED="${QA_BACKEND_INTRADAY_PREFLIGHT_ENABLED:-false}"
INTRADAY_PREFLIGHT_INTERVAL_SECONDS="${QA_BACKEND_INTRADAY_PREFLIGHT_INTERVAL_SECONDS:-300}"
INTRADAY_PREFLIGHT_SCRIPT="${QA_BACKEND_INTRADAY_PREFLIGHT_SCRIPT:-${PROJECT_ROOT}/scripts/verify_intraday_shadow_readiness.py}"
INTRADAY_PREFLIGHT_REPORT_FILE="${QA_BACKEND_INTRADAY_PREFLIGHT_REPORT_FILE:-${PROJECT_ROOT}/data/results/intraday-shadow-readiness-latest.json}"
PREVENT_IDLE_SLEEP="${QA_BACKEND_PREVENT_IDLE_SLEEP:-false}"
CAFFEINATE_BIN="${QA_BACKEND_CAFFEINATE_BIN:-/usr/bin/caffeinate}"
SLEEP_GUARD_PID_FILE="${QA_BACKEND_SLEEP_GUARD_PID_FILE:-${PROJECT_ROOT}/data/results/research-shadow-sleep-guard.pid}"
child_pid=""
sleep_pid=""
sleep_guard_pid=""
stop_requested=0
current_restart_delay="${RESTART_DELAY_SECONDS}"
recovery_pending=0
last_outage_alert_at=0
sleep_guard_recovery_pending=0
intraday_preflight_recovery_pending=0
last_intraday_preflight_at=0
last_intraday_preflight_alert_at=0

stop_backend() {
    stop_requested=1
    if [ -n "${child_pid}" ] && kill -0 "${child_pid}" 2>/dev/null; then
        kill -TERM "${child_pid}" 2>/dev/null || true
    fi
    if [ -n "${sleep_pid}" ] && kill -0 "${sleep_pid}" 2>/dev/null; then
        kill -TERM "${sleep_pid}" 2>/dev/null || true
    fi
    if [ -n "${sleep_guard_pid}" ] && kill -0 "${sleep_guard_pid}" 2>/dev/null; then
        kill -TERM "${sleep_guard_pid}" 2>/dev/null || true
    fi
}

stop_sleep_guard() {
    local pid="${sleep_guard_pid}"
    if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
        kill -TERM "${pid}" 2>/dev/null || true
        wait "${pid}" 2>/dev/null || true
    fi
    sleep_guard_pid=""
    rm -f "${SLEEP_GUARD_PID_FILE}"
    rm -f "${SLEEP_GUARD_PID_FILE}.tmp.$$"
}

terminate_child() {
    local pid="${child_pid}"
    local attempt=0
    if [ -z "${pid}" ] || ! kill -0 "${pid}" 2>/dev/null; then
        return
    fi
    kill -TERM "${pid}" 2>/dev/null || true
    while [ "${attempt}" -lt "${TERMINATION_GRACE_SECONDS}" ]; do
        if ! kill -0 "${pid}" 2>/dev/null; then
            return
        fi
        sleep 1
        attempt=$((attempt + 1))
    done
    if kill -0 "${pid}" 2>/dev/null; then
        kill -KILL "${pid}" 2>/dev/null || true
    fi
}

health_ok() {
    local payload
    payload="$(
        curl --noproxy '*' --fail --silent --show-error \
            --max-time "${HEALTH_TIMEOUT_SECONDS}" "${HEALTH_URL}" 2>/dev/null
    )" || return 1
    printf '%s' "${payload}" \
        | grep -Eq '"status"[[:space:]]*:[[:space:]]*"healthy"' || return 1
    printf '%s' "${payload}" \
        | grep -Eq '"pid"[[:space:]]*:[[:space:]]*'"${child_pid}"'[[:space:]]*([,}])'
}

alert_event() {
    local event_name="$1"
    shift
    if [ ! -f "${ALERT_SCRIPT}" ]; then
        printf '%s supervisor alert script is missing\n' \
            "$(date '+%Y-%m-%dT%H:%M:%S%z')" >&2
        return 0
    fi
    python "${ALERT_SCRIPT}" "${event_name}" \
        --supervisor-pid "$$" "$@" || {
        printf '%s supervisor alert command failed\n' \
            "$(date '+%Y-%m-%dT%H:%M:%S%z')" >&2
        return 0
    }
}

alert_outage_if_due() {
    local event_name="$1"
    local now_seconds
    shift
    now_seconds="$(date +%s)"
    if [ "${last_outage_alert_at}" -ne 0 ] && \
       [ $((now_seconds - last_outage_alert_at)) -lt "${ALERT_REPEAT_SECONDS}" ]; then
        return 0
    fi
    alert_event "${event_name}" "$@"
    last_outage_alert_at="${now_seconds}"
}

valid_preflight_report() {
    python -c \
        'import json, sys; payload = json.load(open(sys.argv[1], encoding="utf-8")); raise SystemExit(0 if isinstance(payload, dict) else 1)' \
        "$1" >/dev/null 2>&1
}

ready_preflight_report() {
    python -c \
        'import json, sys; payload = json.load(open(sys.argv[1], encoding="utf-8")); raise SystemExit(0 if payload.get("ready") is True and payload.get("status") == "READY" else 1)' \
        "$1" >/dev/null 2>&1
}

run_intraday_preflight_if_due() {
    local now_seconds
    local temporary_file
    local command_succeeded=0
    local report_ready=0
    local report_written=0

    if [ "${INTRADAY_PREFLIGHT_ENABLED}" != "true" ]; then
        return 0
    fi
    now_seconds="$(date +%s)"
    if [ "${last_intraday_preflight_at}" -ne 0 ] && \
       [ $((now_seconds - last_intraday_preflight_at)) -lt "${INTRADAY_PREFLIGHT_INTERVAL_SECONDS}" ]; then
        return 0
    fi
    last_intraday_preflight_at="${now_seconds}"
    temporary_file="${INTRADAY_PREFLIGHT_REPORT_FILE}.tmp.$$"
    mkdir -p "$(dirname "${INTRADAY_PREFLIGHT_REPORT_FILE}")"

    if [ -f "${INTRADAY_PREFLIGHT_SCRIPT}" ] && \
       python "${INTRADAY_PREFLIGHT_SCRIPT}" > "${temporary_file}" 2>/dev/null; then
        command_succeeded=1
    fi
    if ! valid_preflight_report "${temporary_file}"; then
        printf '%s\n' \
            '{"fatalError":"preflight command produced no valid JSON","ready":false,"status":"NOT_READY","version":1}' \
            > "${temporary_file}"
        command_succeeded=0
    fi
    if [ "${command_succeeded}" -eq 1 ] && ready_preflight_report "${temporary_file}"; then
        report_ready=1
    fi
    if chmod 600 "${temporary_file}" && \
       mv -f "${temporary_file}" "${INTRADAY_PREFLIGHT_REPORT_FILE}"; then
        report_written=1
    else
        rm -f "${temporary_file}"
    fi

    if [ "${report_ready}" -eq 1 ] && [ "${report_written}" -eq 1 ]; then
        if [ "${intraday_preflight_recovery_pending}" -ne 0 ]; then
            alert_event INTRADAY_PREFLIGHT_RECOVERED
        fi
        intraday_preflight_recovery_pending=0
        last_intraday_preflight_alert_at=0
        return 0
    fi

    if [ "${intraday_preflight_recovery_pending}" -eq 0 ] || \
       [ $((now_seconds - last_intraday_preflight_alert_at)) -ge "${ALERT_REPEAT_SECONDS}" ]; then
        alert_event INTRADAY_PREFLIGHT_FAILED
        last_intraday_preflight_alert_at="${now_seconds}"
    fi
    intraday_preflight_recovery_pending=1
    return 0
}

write_sleep_guard_pid_file() {
    local temporary_file="${SLEEP_GUARD_PID_FILE}.tmp.$$"
    local existing_pid=""
    if [ -f "${SLEEP_GUARD_PID_FILE}" ]; then
        existing_pid="$(cat "${SLEEP_GUARD_PID_FILE}" 2>/dev/null || true)"
    fi
    if [ "${existing_pid}" = "${sleep_guard_pid}" ]; then
        chmod 600 "${SLEEP_GUARD_PID_FILE}"
        return 0
    fi
    mkdir -p "$(dirname "${SLEEP_GUARD_PID_FILE}")"
    if ! (umask 077 && printf '%s\n' "${sleep_guard_pid}" > "${temporary_file}"); then
        rm -f "${temporary_file}"
        return 1
    fi
    if ! chmod 600 "${temporary_file}"; then
        rm -f "${temporary_file}"
        return 1
    fi
    if ! mv -f "${temporary_file}" "${SLEEP_GUARD_PID_FILE}"; then
        rm -f "${temporary_file}"
        return 1
    fi
}

start_sleep_guard() {
    if [ "${PREVENT_IDLE_SLEEP}" != "true" ]; then
        return 0
    fi
    if [ ! -x "${CAFFEINATE_BIN}" ]; then
        return 1
    fi
    "${CAFFEINATE_BIN}" -i -w "$$" &
    sleep_guard_pid=$!
    sleep 0.1
    if ! kill -0 "${sleep_guard_pid}" 2>/dev/null; then
        wait "${sleep_guard_pid}" 2>/dev/null || true
        sleep_guard_pid=""
        return 1
    fi
    if ! write_sleep_guard_pid_file; then
        kill -TERM "${sleep_guard_pid}" 2>/dev/null || true
        wait "${sleep_guard_pid}" 2>/dev/null || true
        sleep_guard_pid=""
        rm -f "${SLEEP_GUARD_PID_FILE}.tmp.$$"
        return 1
    fi
}

ensure_sleep_guard() {
    if [ "${PREVENT_IDLE_SLEEP}" != "true" ]; then
        return 0
    fi
    if [ -n "${sleep_guard_pid}" ] && kill -0 "${sleep_guard_pid}" 2>/dev/null; then
        write_sleep_guard_pid_file
        return $?
    fi
    if [ -n "${sleep_guard_pid}" ]; then
        wait "${sleep_guard_pid}" 2>/dev/null || true
        sleep_guard_pid=""
    fi
    rm -f "${SLEEP_GUARD_PID_FILE}"
    if start_sleep_guard; then
        if [ "${sleep_guard_recovery_pending}" -ne 0 ]; then
            alert_event SLEEP_GUARD_RECOVERED
            sleep_guard_recovery_pending=0
        fi
        return 0
    fi
    if [ "${sleep_guard_recovery_pending}" -eq 0 ]; then
        alert_event SLEEP_GUARD_FAILED
        sleep_guard_recovery_pending=1
    fi
    return 1
}

cleanup_pid_file() {
    if [ -f "${PID_FILE}" ] && [ "$(cat "${PID_FILE}" 2>/dev/null || true)" = "$$" ]; then
        rm -f "${PID_FILE}"
    fi
}

cleanup_supervisor() {
    stop_sleep_guard
    cleanup_pid_file
}

trap stop_backend INT TERM HUP
trap cleanup_supervisor EXIT

# shellcheck disable=SC1091
source "${PROJECT_ROOT}/scripts/qa_env.sh" || exit 1
cd "${PROJECT_ROOT}" || exit 1

for value in \
    "${RESTART_DELAY_SECONDS}" \
    "${RESTART_MAX_DELAY_SECONDS}" \
    "${RESTART_RESET_SECONDS}" \
    "${HEALTH_INTERVAL_SECONDS}" \
    "${HEALTH_TIMEOUT_SECONDS}" \
    "${HEALTH_FAILURE_THRESHOLD}" \
    "${STARTUP_GRACE_SECONDS}" \
    "${TERMINATION_GRACE_SECONDS}" \
    "${ALERT_REPEAT_SECONDS}" \
    "${INTRADAY_PREFLIGHT_INTERVAL_SECONDS}"; do
    case "${value}" in
        ''|*[!0-9]*)
            printf 'backend supervisor timing values must be non-negative integers\n' >&2
            exit 1
            ;;
    esac
done
if [ "${HEALTH_INTERVAL_SECONDS}" -eq 0 ] || \
   [ "${HEALTH_TIMEOUT_SECONDS}" -eq 0 ] || \
   [ "${HEALTH_FAILURE_THRESHOLD}" -eq 0 ] || \
   [ "${INTRADAY_PREFLIGHT_INTERVAL_SECONDS}" -eq 0 ]; then
    printf 'health and preflight intervals, timeout, and failure threshold must be greater than zero\n' >&2
    exit 1
fi
if [ "${RESTART_MAX_DELAY_SECONDS}" -lt "${RESTART_DELAY_SECONDS}" ]; then
    printf 'maximum restart delay must be greater than or equal to the initial delay\n' >&2
    exit 1
fi
case "${PREVENT_IDLE_SLEEP}" in
    true|false) ;;
    *)
        printf 'QA_BACKEND_PREVENT_IDLE_SLEEP must be true or false\n' >&2
        exit 1
        ;;
esac
case "${INTRADAY_PREFLIGHT_ENABLED}" in
    true|false) ;;
    *)
        printf 'QA_BACKEND_INTRADAY_PREFLIGHT_ENABLED must be true or false\n' >&2
        exit 1
        ;;
esac

mkdir -p "$(dirname "${PID_FILE}")"
if [ -f "${PID_FILE}" ]; then
    existing_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [ -n "${existing_pid}" ] && kill -0 "${existing_pid}" 2>/dev/null; then
        printf 'backend supervisor already running with pid %s\n' "${existing_pid}" >&2
        exit 1
    fi
fi
printf '%s\n' "$$" > "${PID_FILE}"
ensure_sleep_guard || true

while [ "${stop_requested}" -eq 0 ]; do
    python "${BACKEND_ENTRYPOINT}" &
    child_pid=$!
    child_started_at="$(date +%s)"
    health_failures=0
    unhealthy_termination=0

    while kill -0 "${child_pid}" 2>/dev/null; do
        sleep "${HEALTH_INTERVAL_SECONDS}" &
        sleep_pid=$!
        wait "${sleep_pid}" 2>/dev/null || true
        sleep_pid=""

        if [ "${stop_requested}" -ne 0 ] || ! kill -0 "${child_pid}" 2>/dev/null; then
            break
        fi
        ensure_sleep_guard || true
        elapsed_seconds=$(( $(date +%s) - child_started_at ))
        if [ "${elapsed_seconds}" -lt "${STARTUP_GRACE_SECONDS}" ]; then
            continue
        fi
        if health_ok; then
            health_failures=0
            if [ "${elapsed_seconds}" -ge "${RESTART_RESET_SECONDS}" ]; then
                current_restart_delay="${RESTART_DELAY_SECONDS}"
            fi
            if [ "${recovery_pending}" -ne 0 ]; then
                alert_event BACKEND_RECOVERED --child-pid "${child_pid}"
                recovery_pending=0
                last_outage_alert_at=0
            fi
            run_intraday_preflight_if_due
            continue
        fi

        health_failures=$((health_failures + 1))
        printf '%s backend health check failed (%s/%s): %s\n' \
            "$(date '+%Y-%m-%dT%H:%M:%S%z')" \
            "${health_failures}" "${HEALTH_FAILURE_THRESHOLD}" "${HEALTH_URL}" >&2
        if [ "${health_failures}" -ge "${HEALTH_FAILURE_THRESHOLD}" ]; then
            printf '%s backend remained unhealthy; terminating child %s\n' \
                "$(date '+%Y-%m-%dT%H:%M:%S%z')" "${child_pid}" >&2
            if [ "${elapsed_seconds}" -ge "${RESTART_RESET_SECONDS}" ]; then
                current_restart_delay="${RESTART_DELAY_SECONDS}"
            fi
            alert_outage_if_due BACKEND_UNHEALTHY \
                --child-pid "${child_pid}" \
                --child-runtime-seconds "${elapsed_seconds}" \
                --restart-delay-seconds "${current_restart_delay}"
            unhealthy_termination=1
            terminate_child
            break
        fi
    done

    if [ "${stop_requested}" -ne 0 ]; then
        terminate_child
    fi
    exited_child_pid="${child_pid}"
    wait "${child_pid}"
    exit_code=$?
    child_runtime_seconds=$(( $(date +%s) - child_started_at ))
    child_pid=""

    if [ "${stop_requested}" -ne 0 ]; then
        break
    fi
    if [ "${child_runtime_seconds}" -ge "${RESTART_RESET_SECONDS}" ]; then
        current_restart_delay="${RESTART_DELAY_SECONDS}"
    fi
    if [ "${unhealthy_termination}" -eq 0 ]; then
        alert_outage_if_due BACKEND_EXITED \
            --child-pid "${exited_child_pid}" \
            --exit-code "${exit_code}" \
            --child-runtime-seconds "${child_runtime_seconds}" \
            --restart-delay-seconds "${current_restart_delay}"
    fi
    recovery_pending=1
    printf '%s backend exited with code %s after %ss; restarting in %ss\n' \
        "$(date '+%Y-%m-%dT%H:%M:%S%z')" "${exit_code}" \
        "${child_runtime_seconds}" "${current_restart_delay}" >&2
    sleep "${current_restart_delay}" &
    sleep_pid=$!
    wait "${sleep_pid}" 2>/dev/null || true
    sleep_pid=""

    if [ "${child_runtime_seconds}" -lt "${RESTART_RESET_SECONDS}" ]; then
        next_restart_delay=$((current_restart_delay * 2))
        if [ "${next_restart_delay}" -lt "${RESTART_DELAY_SECONDS}" ]; then
            next_restart_delay="${RESTART_DELAY_SECONDS}"
        fi
        if [ "${next_restart_delay}" -gt "${RESTART_MAX_DELAY_SECONDS}" ]; then
            next_restart_delay="${RESTART_MAX_DELAY_SECONDS}"
        fi
        current_restart_delay="${next_restart_delay}"
    fi
done

exit 0
