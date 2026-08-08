# pio.ps1 -- thin wrapper around `pio` that applies a dev-bench-local
# EPM_MQTT_BROKER_HOST/PORT override from .env.local (gitignored, see
# .env.local.example), the same pattern tools/devrig/devrig.sh uses for
# REF_REPO_URL. Falls back to link_mqtt.c/wifi_task.c's compiled
# "10.42.0.1":1883 default if .env.local is absent or a value is unset.
#
# Usage: .\pio.ps1 run --target upload --environment xiao_esp32s3
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$envLocal = Join-Path $PSScriptRoot ".env.local"

$brokerHost = $null
$brokerPort = $null
if (Test-Path $envLocal) {
    Get-Content $envLocal | ForEach-Object {
        if ($_ -match '^\s*EPM_MQTT_BROKER_HOST\s*=\s*(.+)$') { $brokerHost = $matches[1].Trim() }
        if ($_ -match '^\s*EPM_MQTT_BROKER_PORT\s*=\s*(.+)$') { $brokerPort = $matches[1].Trim() }
    }
}

$extraFlags = ""
if ($brokerHost) { $extraFlags += " -DEPM_MQTT_BROKER_HOST=\`"$brokerHost\`"" }
if ($brokerPort) { $extraFlags += " -DEPM_MQTT_BROKER_PORT=$brokerPort" }

$env:PLATFORMIO_BUILD_FLAGS = "$($env:PLATFORMIO_BUILD_FLAGS)$extraFlags"

& pio @Args
