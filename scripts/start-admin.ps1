param([string]$DataDir = "$env:LOCALAPPDATA\FCIFamiliar", [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$ProjectPath = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $ProjectPath '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $PythonPath)) { $PythonPath = (Get-Command python).Source }
$env:PYTHONPATH = Join-Path $ProjectPath 'local'
if (!$NoBrowser) { Start-Process 'http://127.0.0.1:8765' }
& $PythonPath -m fci_familiar --data-dir $DataDir serve
