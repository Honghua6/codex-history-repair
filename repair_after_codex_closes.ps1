param(
  [string]$ProviderMode = "current"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$keeper = Join-Path $scriptDir "codex_history_keeper.py"
$logDir = Join-Path $env:USERPROFILE ".codex_history_keeper"
$log = Join-Path $logDir "repair_after_close.log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Set-Location -LiteralPath $scriptDir

function Write-RepairLog {
  param([string]$Message)
  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Add-Content -Path $log -Value "[$stamp] $Message" -Encoding UTF8
}

function Get-PythonCommand {
  $py = Get-Command py.exe -ErrorAction SilentlyContinue
  if ($py) {
    foreach ($version in @("3.13", "3.12", "3.11")) {
      & $py.Source "-$version" -c "import sys; sys.exit(0)" *> $null
      if ($LASTEXITCODE -eq 0) {
        return @($py.Source, "-$version")
      }
    }
  }

  $python = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($python) {
    & $python.Source -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" *> $null
    if ($LASTEXITCODE -eq 0) {
      return @($python.Source)
    }
  }

  throw "Python 3.11+ was not found. Run open_codex_history_repair_gui.cmd or repair_codex_ui_index.cmd to let the launcher install Python automatically, then try again."
}

function Invoke-Python {
  param([string[]]$Arguments)
  $command = @(Get-PythonCommand)
  $exe = $command[0]
  $prefix = @()
  if ($command.Count -gt 1) {
    $prefix = $command[1..($command.Count - 1)]
  }
  & $exe @prefix @Arguments
}

Write-RepairLog "Watcher started. Waiting for Codex processes to exit."

$sawCodex = $false
while (Get-Process -Name Codex,codex -ErrorAction SilentlyContinue) {
  $sawCodex = $true
  Start-Sleep -Seconds 2
}

if (-not $sawCodex) {
  Write-RepairLog "Watcher exited without action because Codex was not running."
  exit 0
}

try {
  Start-Sleep -Seconds 3
  Write-RepairLog "Codex is closed. Applying repair with provider mode: $ProviderMode."

  $output = Invoke-Python -Arguments @($keeper, "--repair-ui-index", "--apply-repair", "--provider-mode", $ProviderMode) 2>&1
  $exitCode = $LASTEXITCODE
  $output | ForEach-Object { Write-RepairLog $_ }
  Write-RepairLog "Repair exit code: $exitCode"

  if ($exitCode -eq 0) {
    Write-RepairLog "Launching Codex."
    $launchCode = "import codex_history_keeper as keeper; keeper.launch_codex(keeper.load_config())"
    $launchOutput = Invoke-Python -Arguments @("-c", $launchCode) 2>&1
    $launchExitCode = $LASTEXITCODE
    $launchOutput | ForEach-Object { Write-RepairLog $_ }
    Write-RepairLog "Launch exit code: $launchExitCode"
    if ($launchExitCode -ne 0) {
      exit $launchExitCode
    }
  } else {
    Write-RepairLog "Repair failed; Codex will not be launched automatically."
    exit $exitCode
  }
} catch {
  Write-RepairLog "Watcher failed: $($_.Exception.Message)"
  Write-RepairLog "Python is missing. Run open_codex_history_repair_gui.cmd or repair_codex_ui_index.cmd to let the launcher install Python automatically, then rerun the watcher."
  throw
}
