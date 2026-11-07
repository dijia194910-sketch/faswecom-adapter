$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$config = Join-Path $root 'config.json'
$settings = Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json

$python = [string]$settings.python_path
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
$exe = if (Test-Path -LiteralPath $pythonw) { $pythonw } else { $python }

$taskName = 'WorkBuddy-WeCom-AutoReply'
$argument = "-X utf8 -B `"$root\watcher.py`" --config `"$config`""

$action = New-ScheduledTaskAction -Execute $exe -Argument $argument -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settingsSet = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settingsSet `
    -Description '企业微信新消息监听：IMA 知识库检索 + MCP 自动回复' `
    -RunLevel Limited `
    -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
Write-Host "已注册并启动计划任务：$taskName"
Write-Host "解释器：$exe"
