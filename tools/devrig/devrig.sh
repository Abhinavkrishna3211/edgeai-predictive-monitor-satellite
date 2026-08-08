#!/usr/bin/env bash
# tools/devrig/devrig.sh -- orchestrates the reference repository's UNMODIFIED
# base-station/start_desktop_dashboard.sh from a read-only reference clone.
# Never edits anything under the reference repo: only starts a broker,
# snapshots its tree's tracked-file status, and invokes its script verbatim.
#
# Usage: bash tools/devrig/devrig.sh [args passed straight to the reference script]
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REF_REPO="${EPM_REF_REPO:-/mnt/c/Users/abhin/Documents/edgeai-predictive-monitor-ref}"

ENV_LOCAL="${REPO_ROOT}/tools/devrig/.env.local"
if [ -f "${ENV_LOCAL}" ]; then
    # shellcheck disable=SC1090
    source "${ENV_LOCAL}"
fi

if [ ! -f "${REF_REPO}/base-station/start_desktop_dashboard.sh" ]; then
    echo "Reference repo not found at ${REF_REPO} (set EPM_REF_REPO to override)." >&2
    if [ -z "${REF_REPO_URL:-}" ]; then
        echo "REF_REPO_URL not set: create tools/devrig/.env.local (copy tools/devrig/.env.local.example) or export REF_REPO_URL." >&2
    else
        echo "Clone it read-only: git clone --depth 1 ${REF_REPO_URL} ${REF_REPO}" >&2
    fi
    exit 1
fi

REF_REPO_REAL="$(cd "${REF_REPO}" && pwd)"
case "${REF_REPO_REAL}" in
    "${REPO_ROOT}"/*|"${REPO_ROOT}")
        echo "Refusing to run: EPM_REF_REPO (${REF_REPO_REAL}) is inside this repo (${REPO_ROOT})." >&2
        exit 1
        ;;
esac

# Parse out --mqtt-host/--mqtt-port (also settable via MQTT_HOST/MQTT_PORT env
# vars) so they can steer which invocation runs below, without leaking into
# the args forwarded to whichever script gets invoked -- neither his wrapper
# nor main.py's own --mqtt-host/--mqtt-port would want a second copy of these.
MQTT_HOST="${MQTT_HOST:-localhost}"
MQTT_PORT="${MQTT_PORT:-1883}"
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --mqtt-host)
            MQTT_HOST="$2"
            shift 2
            ;;
        --mqtt-host=*)
            MQTT_HOST="${1#--mqtt-host=}"
            shift
            ;;
        --mqtt-port)
            MQTT_PORT="$2"
            shift 2
            ;;
        --mqtt-port=*)
            MQTT_PORT="${1#--mqtt-port=}"
            shift
            ;;
        *)
            ARGS+=("$1")
            shift
            ;;
    esac
done

# The local WSL mosquitto is only needed for the default localhost path --
# a remote MQTT_HOST means some other broker (e.g. the Windows-hosted one)
# is what's actually being targeted.
if [ "${MQTT_HOST}" = "localhost" ]; then
    if ! ss -tln 2>/dev/null | grep -q ':1883 '; then
        echo "Starting mosquitto..."
        sudo service mosquitto start
        for _ in $(seq 1 10); do
            ss -tln 2>/dev/null | grep -q ':1883 ' && break
            sleep 0.5
        done
    fi
    if ! ss -tln 2>/dev/null | grep -q ':1883 '; then
        echo "mosquitto did not come up on 1883." >&2
        exit 1
    fi
    echo "Broker listening on 1883."
fi

BEFORE="$(git -C "${REF_REPO_REAL}" status --porcelain --untracked-files=no)"

# NOTE (2026-08-06, ADR-026 follow-up, fixed): his start_desktop_dashboard.sh
# hardcodes MQTT_HOST=localhost with no override flag, so it can't point at a
# non-WSL-local broker. When MQTT_HOST/--mqtt-host names a non-localhost broker,
# skip his wrapper and invoke base-station/python/main.py directly instead, the
# same invocation ADR-026's live cross-gateway test proved out.
if [ "${MQTT_HOST}" != "localhost" ]; then
    echo "Targeting non-local broker ${MQTT_HOST}:${MQTT_PORT} -- running main.py directly."
    python3 "${REF_REPO_REAL}/base-station/python/main.py" \
        --mqtt-host "${MQTT_HOST}" --mqtt-port "${MQTT_PORT}" "${ARGS[@]}"
    status=$?
else
    bash "${REF_REPO_REAL}/base-station/start_desktop_dashboard.sh" "${ARGS[@]}"
    status=$?
fi

AFTER="$(git -C "${REF_REPO_REAL}" status --porcelain --untracked-files=no)"
if [ "${BEFORE}" != "${AFTER}" ]; then
    echo "WARNING: a tracked file in ${REF_REPO_REAL} changed during this run:" >&2
    diff <(echo "${BEFORE}") <(echo "${AFTER}") >&2
fi

exit "${status}"
