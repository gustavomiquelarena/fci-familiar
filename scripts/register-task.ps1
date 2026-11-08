param(
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$DataDir,
    [Parameter(Mandatory=$true)][string]$ProjectPath
)
$ErrorActionPreference = 'Stop'
foreach ($TaskPath in @($PythonPath,$DataDir,$ProjectPath)) {
    if ($TaskPath.Contains('"') -or ![IO.Path]::IsPathRooted($TaskPath)) { throw 'Las rutas deben ser absolutas y no contener comillas' }
}
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Launcher = Join-Path $ProjectPath 'scripts\weekly.py'
$Arguments = '"{0}" --data-dir "{1}" weekly --if-due' -f $Launcher,$DataDir
$Action = New-ScheduledTaskAction -Execute $PythonPath -Argument $Arguments -WorkingDirectory $ProjectPath
$Weekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '18:00'
$Logon = New-ScheduledTaskTrigger -AtLogOn -User $Identity
$Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName 'FCIFamiliar-Semanal' -Action $Action -Trigger @($Weekly,$Logon) -Principal $Principal -Settings $Settings -Description 'Actualiza el fondo familiar y publica reportes cifrados. Domingos 18:00 Argentina.' -Force | Select-Object TaskName,State
