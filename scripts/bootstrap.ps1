param([string]$PythonPath = 'python')
$ErrorActionPreference = 'Stop'
$ProjectPath = Split-Path -Parent $PSScriptRoot
$VenvPath = Join-Path $ProjectPath '.venv'
if (!(Test-Path -LiteralPath $VenvPath)) { & $PythonPath -m venv $VenvPath }
& (Join-Path $VenvPath 'Scripts\python.exe') -m pip install -r (Join-Path $ProjectPath 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'No se pudieron instalar las dependencias del entorno aislado' }
Write-Output 'Entorno preparado. Usá scripts/start-admin.ps1 para abrir el administrador.'
