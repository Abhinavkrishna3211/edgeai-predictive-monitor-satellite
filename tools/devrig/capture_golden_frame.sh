#!/usr/bin/env bash
# tools/devrig/capture_golden_frame.sh -- captures one raw section-list
# frame published by a running sim node and writes it to
# tests/golden/sim_reference.hex with a 3-line provenance header.
# Run this while a devrig.sh/devrig.ps1 session is up and a sim node is
# online (its UI shows "online": true at http://127.0.0.1:<ui_port>/state).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${REPO_ROOT}/tests/golden/sim_reference.hex"
TOPIC_FILTER="epm/+/data"
MQTT_HOST="${MQTT_HOST:-localhost}"

echo "Waiting for one frame on ${TOPIC_FILTER}..."
line="$(mosquitto_sub -h "${MQTT_HOST}" -t "${TOPIC_FILTER}" -C 1 -F '%t|%x')"
topic="${line%%|*}"
hexpart="${line#*|}"
node_id="$(echo "${topic}" | cut -d/ -f2)"

read -rp "Capture file being replayed (see the sim node's /state, e.g. healthy.npz): " capture_file

{
    echo "# date: $(date -u +%Y-%m-%d)"
    echo "# sim node id: ${node_id}"
    echo "# capture file replayed: ${capture_file}"
    echo "${hexpart}"
} > "${OUT}"

echo "Wrote ${OUT} ($(( ${#hexpart} / 2 )) bytes)."
