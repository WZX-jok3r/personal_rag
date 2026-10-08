# check.ps1 - Verification gate for each incremental change.
#
# NOTE: This file is intentionally ASCII-ONLY.
#   Windows PowerShell 5.1 reads BOM-less script files using the system ANSI code
#   page (936/GBK on this machine), so UTF-8 Chinese text inside a .ps1 without a
#   BOM is mis-decoded and causes ParserError. Same root cause as the documented
#   alembic.ini pitfall. Keeping scripts ASCII-only is the established project
#   convention and works regardless of encoding/BOM.
#
# Three levels, progressively stricter:
#   L1 static : byte-compile / import self-check (seconds, no infra needed)
#   L2 unit   : full pytest suite (seconds, hermetic, no external services)
#   L3 e2e    : retrieval eval + PER-QUESTION baseline comparison
#               (minutes, needs full Docker stack + indexed Qdrant)
#
# Usage:
#   .\scripts\check.ps1                    # L1 + L2  (fast, use while coding)
#   .\scripts\check.ps1 -Full              # L1 + L2 + L3 (REQUIRED after any
#                                          #   change to retrieval/chunking/ingest/eval)
#
# Exit code: 0 = all passed; non-zero = failure (do NOT proceed to next step)

param(
    [switch]$Full
)

# NOTE: ErrorActionPreference is deliberately "Continue", NOT "Stop".
#   Windows PowerShell 5.1 wraps a native command's stderr output into an
#   ErrorRecord; with ErrorActionPreference="Stop" that turns into a TERMINATING
#   error and aborts this script even when the command succeeded. Our Python
#   tools legitimately write progress to stderr (logging), so "Stop" would make
#   the gate unusable. We therefore rely on $LASTEXITCODE for pass/fail, which is
#   the correct signal for native commands anyway.
$ErrorActionPreference = "Continue"

# Repo root = parent of the directory containing this script (scripts/)
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Backend = Join-Path $Root "backend"
$Py = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Py)) {
    Write-Host "[FAIL] venv python not found: $Py" -ForegroundColor Red
    exit 1
}

$script:Failed = @()

function Invoke-Gate {
    param([string]$Name, [scriptblock]$Action)
    Write-Host ""
    Write-Host ("=" * 64) -ForegroundColor DarkGray
    Write-Host ">> $Name" -ForegroundColor Cyan
    Write-Host ("=" * 64) -ForegroundColor DarkGray
    $global:LASTEXITCODE = 0
    & $Action
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] $Name (exit=$LASTEXITCODE)" -ForegroundColor Red
        $script:Failed += $Name
        return
    }
    Write-Host "[PASS] $Name" -ForegroundColor Green
}

Push-Location $Backend
try {
    # ---------- L1: syntax / compile self-check ----------
    Invoke-Gate "L1 compileall (syntax self-check)" {
        & $Py -m compileall -q app eval tests
        if ($LASTEXITCODE -eq 0) { Write-Host "  all modules compiled" }
    }

    # ---------- L2: full unit test suite ----------
    Invoke-Gate "L2 pytest (full suite)" {
        & $Py -m pytest -q --no-header 2>&1 | Select-Object -Last 6 | Out-String | Write-Host
    }

    # ---------- L3: retrieval eval + per-question baseline diff ----------
    if ($Full) {
        # eval exits non-zero when any case fails; one baseline case is a known
        # miss, so a non-zero exit here is EXPECTED and not itself a gate failure.
        # Do NOT use `exit 0` inside the gate block -- that would terminate this
        # whole script. We simply run the eval and ignore its exit code, then let
        # the real gate be the per-question comparison below.
        Invoke-Gate "L3a retrieval eval (retrieval-only)" {
            & $Py -m eval --retrieval-only *>&1 | Select-Object -Last 18 | Out-String | Write-Host
            $global:LASTEXITCODE = 0
        }

        Write-Host ""
        Write-Host ("=" * 64) -ForegroundColor DarkGray
        Write-Host ">> L3b per-question baseline comparison" -ForegroundColor Cyan
        Write-Host ("=" * 64) -ForegroundColor DarkGray
        Invoke-Gate "L3b baseline comparison" {
            & $Py -m eval.compare --baseline baseline_v1 --current (Join-Path $Root "output\eval_result.json") *>&1 | Out-String | Write-Host
        }
    }
    else {
        Write-Host ""
        Write-Host "[SKIP] L3 end-to-end eval (run with -Full)" -ForegroundColor Yellow
        Write-Host "       Required after changing retrieval/chunking/ingest/eval." -ForegroundColor Yellow
    }
}
finally {
    Pop-Location
}

# ---------- summary ----------
Write-Host ""
Write-Host ("=" * 64) -ForegroundColor DarkGray
if ($script:Failed.Count -eq 0) {
    Write-Host "OK - all gates passed, safe to continue" -ForegroundColor Green
    Write-Host ("=" * 64) -ForegroundColor DarkGray
    exit 0
}
else {
    Write-Host "FAILED ($($script:Failed.Count)): $($script:Failed -join ', ')" -ForegroundColor Red
    Write-Host "  -> Do NOT proceed. Fix, or roll back with git checkout." -ForegroundColor Red
    Write-Host ("=" * 64) -ForegroundColor DarkGray
    exit 1
}
