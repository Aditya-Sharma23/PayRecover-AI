# Start PayRecover AI on Windows: the same as `python scripts/payrecover.py` (sets up on the first run).
param([int]$Port = 8000)
python (Join-Path $PSScriptRoot "payrecover.py") --port $Port
