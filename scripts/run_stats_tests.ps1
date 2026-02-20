$ErrorActionPreference = 'Stop'

Set-Location (Join-Path $PSScriptRoot '..')

if (-not (Test-Path '.venv/Scripts/python.exe')) {
    Write-Host 'Creating virtual environment (.venv)...'
    python -m venv .venv
}

Write-Host 'Installing requirements into .venv...'
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host 'Running stats tests with .venv python...'
.\.venv\Scripts\python.exe -m pytest -q tests/test_stats_summary.py