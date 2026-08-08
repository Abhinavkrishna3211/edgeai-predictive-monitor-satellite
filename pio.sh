#!/usr/bin/env bash
# pio.sh -- thin wrapper around `pio` that applies a dev-bench-local
# EPM_MQTT_BROKER_HOST/PORT override from .env.local (gitignored, see
# .env.local.example), the same pattern tools/devrig/devrig.sh uses for
# REF_REPO_URL. Falls back to link_mqtt.c/wifi_task.c's compiled
# "10.42.0.1":1883 default if .env.local is absent or a value is unset.
#
# Usage: bash pio.sh run --target upload --environment xiao_esp32s3
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_LOCAL="${REPO_ROOT}/.env.local"
if [ -f "${ENV_LOCAL}" ]; then
    # shellcheck disable=SC1090
    source "${ENV_LOCAL}"
fi

EXTRA_FLAGS=""
if [ -n "${EPM_MQTT_BROKER_HOST:-}" ]; then
    EXTRA_FLAGS="${EXTRA_FLAGS} -DEPM_MQTT_BROKER_HOST=\\\"${EPM_MQTT_BROKER_HOST}\\\""
fi
if [ -n "${EPM_MQTT_BROKER_PORT:-}" ]; then
    EXTRA_FLAGS="${EXTRA_FLAGS} -DEPM_MQTT_BROKER_PORT=${EPM_MQTT_BROKER_PORT}"
fi

export PLATFORMIO_BUILD_FLAGS="${PLATFORMIO_BUILD_FLAGS:-}${EXTRA_FLAGS}"
exec pio "$@"
