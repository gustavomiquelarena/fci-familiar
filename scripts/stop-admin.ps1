$ErrorActionPreference = 'Stop'
$ProjectPath = Split-Path -Parent $PSScriptRoot
$Listeners = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
foreach ($Listener in $Listeners) {
    $OwnedProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($Listener.OwningProcess)"
    if ($OwnedProcess.CommandLine -and $OwnedProcess.CommandLine.Contains($ProjectPath) -and $OwnedProcess.CommandLine.Contains('fci_familiar')) {
        Stop-Process -Id $Listener.OwningProcess -ErrorAction Stop
        Write-Output 'Administrador local cerrado.'
    }
}
