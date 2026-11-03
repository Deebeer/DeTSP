# DeTSP Windows launcher: create venv on first run, install deps, start.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = $null
foreach ($c in @("python", "py")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
}
if (-not $py) {
    Write-Host "Python 3.11+ not found. Install it from https://www.python.org and retry."
    exit 1
}
if (-not (Test-Path ".venv")) {
    Write-Host "First run: creating virtual environment..."
    & $py -m venv .venv
}
Write-Host "Installing dependencies..."
& .\.venv\Scripts\python -m pip install --upgrade pip
& .\.venv\Scripts\pip install -e ".[live]"
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
if (-not (Test-Path "config.yaml")) { Copy-Item "config.example.yaml" "config.yaml" }
Write-Host "Starting DeTSP (panel: http://127.0.0.1:8320/) ..."
& .\.venv\Scripts\python -m detsp
