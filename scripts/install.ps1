# Thin entry point; shared installation policy lives in install.py.
$ErrorActionPreference = 'Stop'
if (-not (Get-Command uv -CommandType Application -ErrorAction SilentlyContinue)) {
    Write-Error 'uv is required. Install uv and reopen PowerShell.'
    exit 1
}
& uv run --isolated --no-project --python 3.12 (Join-Path $PSScriptRoot 'install.py') @args
exit $LASTEXITCODE
