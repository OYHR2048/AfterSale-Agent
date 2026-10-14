param([string]$PythonExe='python',[string]$RuntimeDir='../aftersale-runtime',[int]$Port=8018)
$ErrorActionPreference='Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:PYTHONDONTWRITEBYTECODE='1'
$env:AFTERSALE_RUNTIME_DIR=[IO.Path]::GetFullPath((Join-Path (Get-Location) $RuntimeDir))
$env:AFTERSALE_MODE='demo'
Write-Host "Development credentials will be written to $env:AFTERSALE_RUNTIME_DIR/auth.json"
Write-Host "Open http://127.0.0.1:$Port after startup. Use one worker."
& $PythonExe -m uvicorn aftersale.api:app --host 127.0.0.1 --port $Port
