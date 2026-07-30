#!/usr/bin/env bash
# tools/devrig/devrig.sh -- orchestrates the reference-repo maintainer's UNMODIFIED
# base-station/start_desktop_dashboard.sh from a read-only reference clone.
# Never edits anything under the reference repo: only starts a broker,
# snapshots his tree's tracked-file status, and invokes his script verbatim.
#
# Usage: bash tools/devrig/devrig.sh [args passed straight to his script]
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REF_REPO="${EPM_REF_REPO:-/mnt/c/Users/abhin/Documents/edgeai-predictive-monitor-ref}"

if [ ! -f "${REF_REPO}/base-station/start_desktop_dashboard.sh" ]; then
    echo "Reference repo not found at ${REF_REPO} (set EPM_REF_REPO to override)." >&2
    echo "Clone it read-only: git clone --depth 1 https://github.com/rahuljeyaraj/edgeai-predictive-monitor ${REF_REPO}" >&2
    exit 1
fi

REF_REPO_REAL="$(cd "${REF_REPO}" && pwd)"
case "${REF_REPO_REAL}" in
    "${REPO_ROOT}"/*|"${REPO_ROOT}")
        echo "Refusing to run: EPM_REF_REPO (${REF_REPO_REAL}) is inside this repo (${REPO_ROOT})." >&2
        exit 1
        ;;
esac

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

BEFORE="$(git -C "${REF_REPO_REAL}" status --porcelain --untracked-files=no)"

bash "${REF_REPO_REAL}/base-station/start_desktop_dashboard.sh" "$@"
status=$?

AFTER="$(git -C "${REF_REPO_REAL}" status --porcelain --untracked-files=no)"
if [ "${BEFORE}" != "${AFTER}" ]; then
    echo "WARNING: a tracked file in ${REF_REPO_REAL} changed during this run:" >&2
    diff <(echo "${BEFORE}") <(echo "${AFTER}") >&2
fi

exit "${status}"
