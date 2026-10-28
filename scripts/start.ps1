# Start TestPilot AI locally: one server for the API and the built dashboard.
param([int]$Port = 8010)
$root = Join-Path $PSScriptRoot ".."
if (-not (Test-Path (Join-Path $root "frontend\dist\index.html"))) {
    Push-Location (Join-Path $root "frontend"); npm run build; Pop-Location
}
Set-Location (Join-Path $root "backend")
Write-Host "TestPilot AI on http://127.0.0.1:$Port"
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port $Port
