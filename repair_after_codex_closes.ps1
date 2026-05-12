param(
  [string]$ProviderMode = "current"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$keeper = Join-Path $scriptDir "codex_history_keeper.py"
$logDir = Join-Path $env:USERPROFILE ".codex_history_keeper"
$log = Join-Path $logDir "repair_after_close.log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-RepairLog {
  param([string]$Message)
  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Add-Content -Path $log -Value "[$stamp] $Message" -Encoding UTF8
}

Write-RepairLog "Watcher started. Waiting for Codex processes to exit."

while (Get-Process -Name Codex,codex -ErrorAction SilentlyContinue) {
  Start-Sleep -Seconds 2
}

Start-Sleep -Seconds 3
Write-RepairLog "Codex is closed. Applying repair with provider mode: $ProviderMode."

$output = & python $keeper --repair-ui-index --apply-repair --provider-mode $ProviderMode 2>&1
$exitCode = $LASTEXITCODE
$output | ForEach-Object { Write-RepairLog $_ }
Write-RepairLog "Repair exit code: $exitCode"

if ($exitCode -eq 0) {
  Write-RepairLog "Launching Codex."
  Start-Process "explorer.exe" "shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"
} else {
  Write-RepairLog "Repair failed; Codex will not be launched automatically."
}
