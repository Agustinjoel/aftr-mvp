$ErrorActionPreference = 'Stop'

param(
    [switch]$SkipRefresh,
    [switch]$SkipTests,
    [int]$Port = 8000
)

Set-Location (Join-Path $PSScriptRoot '..')

if (-not (Test-Path '.venv/Scripts/python.exe')) {
    Write-Host 'Creating virtual environment (.venv)...'
    python -m venv .venv
}

Write-Host 'Installing requirements into .venv...'
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not $SkipRefresh) {
    Write-Host 'Refreshing daily data (matches + picks)...'
    .\.venv\Scripts\python.exe -m app.cli refresh
}

if (-not $SkipTests) {
    Write-Host 'Running tests...'
    .\.venv\Scripts\python.exe -m pytest -q tests/test_stats_summary.py
}

Write-Host "Starting app on http://127.0.0.1:$Port ..."
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port $Port