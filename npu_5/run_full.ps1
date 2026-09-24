# Full-scale run for npu_5 (100 cases, cores 2-5).
# Usage:
#   .\npu_5\run_full.ps1                        # start a fresh full run
#   .\npu_5\run_full.ps1 -Workers 8             # more parallelism
#   .\npu_5\run_full.ps1 -Resume <run_dir>      # resume an interrupted run
#   .\npu_5\run_full.ps1 -Resume <run_dir> -Verify   # replay-verify a finished run
#   .\npu_5\run_full.ps1 -ProblemOneOnly -Workers 12 # Problem 1 only
param(
    [int]$Workers = 6,
    [int]$TimeoutMinutes = 120,
    [int]$SmallBudget = 72,
    [int]$LargeBudget = 44,
    [string]$Resume = "",
    [switch]$Verify,
    [switch]$ProblemOneOnly
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$CondaExe    = "C:\Users\chris\miniconda3\Scripts\conda.exe"
$EnvName     = "npu312"
$Python      = "C:\Users\chris\miniconda3\envs\$EnvName\python.exe"

if (-not (Test-Path $Python)) {
    Write-Host "ERROR: env not found: $Python" -ForegroundColor Red
    exit 1
}

Set-Location $ProjectRoot
Write-Host "cwd    = $ProjectRoot" -ForegroundColor Cyan
Write-Host "python = $Python" -ForegroundColor Cyan
& $Python -V

$logDir = Join-Path $ProjectRoot "npu_5\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if ($Verify) {
    if (-not $Resume) { Write-Host "ERROR: -Verify needs -Resume <run_dir>" -ForegroundColor Red; exit 1 }
    Write-Host "== replay verification ==" -ForegroundColor Yellow
    & $Python ".\npu_5\verify.py" $Resume --replay
    exit $LASTEXITCODE
}

# ---- build argument list ----
$args = @(".\npu_5\run.py", "--workers", $Workers,
          "--timeout-minutes", $TimeoutMinutes,
          "--small-budget", $SmallBudget,
          "--large-budget", $LargeBudget)

if ($ProblemOneOnly) {
    $args += "--scene-a-only"
}

if ($Resume) {
    $args += @("--run", $Resume)
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logPrefix = if ($ProblemOneOnly) { "problem1" } else { "full" }
$log   = Join-Path $logDir "${logPrefix}_$stamp.log"

Write-Host "== launching ==" -ForegroundColor Yellow
Write-Host "args: $($args -join ' ')" -ForegroundColor Gray
Write-Host "log : $log" -ForegroundColor Gray
Write-Host "`nNOTE: the runner only prints a few lines (RUN=, progress, STATUS=)."
Write-Host "Silence between lines is NORMAL. Watch the run dir for progress:"
Write-Host "  Get-ChildItem <run_dir> -Recurse -File | Measure-Object`n" -ForegroundColor DarkGray

# -u disables output buffering so the log updates in real time
& $Python -u @args 2>&1 | Tee-Object -FilePath $log
$code = $LASTEXITCODE

Write-Host "`n==== exit code: $code ====" -ForegroundColor $(if ($code -eq 0) { "Green" } else { "Red" })
if ($code -eq 0) {
    Write-Host "Full run complete. Results under npu_5\runs\" -ForegroundColor Green
} else {
    Write-Host "Run incomplete (exit $code). Re-run with -Resume <run_dir> to continue." -ForegroundColor Yellow
}
exit $code
