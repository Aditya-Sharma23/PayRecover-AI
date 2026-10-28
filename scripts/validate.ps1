$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
Set-Location backend
& .\.venv\Scripts\python.exe -m ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Set-Location ../frontend
npm run lint
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
npm test
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
npm run build
exit $LASTEXITCODE
