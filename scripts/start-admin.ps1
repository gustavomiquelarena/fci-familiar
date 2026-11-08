param([string]$DataDir = '', [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$ProjectPath = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $ProjectPath '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $PythonPath)) { $PythonPath = (Get-Command python).Source }
$env:PYTHONPATH = Join-Path $ProjectPath 'local'
try {
    $Health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 2
    if ($Health.app -eq 'fci-familiar' -and !$DataDir) {
        if (!$NoBrowser) { Start-Process 'http://127.0.0.1:8765' }
        exit 0
    }
} catch { }
$Arguments = @('-m','fci_familiar')
if ($DataDir) {
    if ($DataDir.Contains('"')) { throw 'La ruta privada no puede contener comillas' }
    $Arguments += @('--data-dir',('"{0}"' -f $DataDir))
}
$Arguments += 'serve'
$PrivateRoot = if ($DataDir) { $DataDir } else { & $PythonPath -c 'from fci_familiar.config import Config; print(Config.load().root)' }
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw 'No se pudo cargar la configuración privada' }
$LogPath = Join-Path $PrivateRoot 'logs'
New-Item -ItemType Directory -Force -Path $LogPath | Out-Null
$AdminProcess = Start-Process -FilePath $PythonPath -ArgumentList $Arguments -WorkingDirectory $ProjectPath -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogPath 'admin.out.log') -RedirectStandardError (Join-Path $LogPath 'admin.err.log') -PassThru
for ($Attempt=0; $Attempt -lt 30; $Attempt++) {
    try {
        $Health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 1
        if ($Health.app -eq 'fci-familiar') {
            if (!$NoBrowser) { Start-Process 'http://127.0.0.1:8765' }
            exit 0
        }
    } catch { }
    Start-Sleep -Milliseconds 300
}
throw 'El administrador no inició. Revisá logs/admin.err.log en la carpeta privada.'
