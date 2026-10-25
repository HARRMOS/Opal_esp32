# Installe l'agent de verrouillage Opal au démarrage de session (tâche planifiée).
# À lancer UNE fois dans PowerShell, APRÈS avoir configuré le code :
#     python opal_guard.py --setup
# puis :
#     powershell -ExecutionPolicy Bypass -File installer_windows.ps1

$ErrorActionPreference = "Stop"
$agent = Join-Path $PSScriptRoot "opal_guard.py"
if (-not (Test-Path $agent)) { throw "opal_guard.py introuvable à côté de ce script." }

# Python qui a tkinter (pythonw.exe = sans fenêtre console).
$py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = "C:\Users\$env:USERNAME\AppData\Local\Programs\Python\Python312\pythonw.exe" }
if (-not (Test-Path $py)) { throw "pythonw.exe introuvable ; indique le bon chemin dans le script." }

$taskName = "Opal Gardien"
$action = New-ScheduledTaskAction -Execute $py -Argument "`"$agent`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Redémarre l'agent s'il s'arrête : la garde ne doit pas tomber en silence.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit 0

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Agent de verrouillage Opal Box" -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Tâche « $taskName » installée et démarrée."
Write-Host "Pour l'arrêter plus tard : Unregister-ScheduledTask -TaskName '$taskName'"
