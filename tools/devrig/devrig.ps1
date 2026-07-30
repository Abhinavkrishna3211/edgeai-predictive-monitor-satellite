# tools/devrig/devrig.ps1 -- Windows entry point. Runs devrig.sh inside the
# WSL Ubuntu-24.04 distro that hosts the reference-repo maintainer's unmodified dashboard rig
# (native Windows can't run his script as-is: Windows venvs use Scripts/
# not bin/, and ai-edge-litert has no Windows wheel).
#
# Usage: tools\devrig\devrig.ps1 --nodes 1 --port 8180 --captures-dir "" --auto-online
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$drive = $repoRoot.Substring(0, 1).ToLower()
$rest = $repoRoot.Substring(2).Replace('\', '/')
$wslRepoRoot = "/mnt/$drive$rest"

# Each arg must be individually single-quoted before joining -- a plain
# space-join silently drops empty-string args (e.g. --captures-dir "") once
# bash -lc re-parses the flattened string, shifting every flag after it.
$quotedArgs = $Args | ForEach-Object { "'" + $_.Replace("'", "'\''") + "'" }
$argString = ($quotedArgs -join ' ')
wsl -d Ubuntu-24.04 -- bash -lc "cd '$wslRepoRoot' && bash tools/devrig/devrig.sh $argString"
