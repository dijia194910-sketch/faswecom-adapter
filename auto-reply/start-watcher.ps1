$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$watcher = Join-Path $root 'watcher.py'
$config = Join-Path $root 'config.json'
$logDir = Join-Path $root 'logs'

foreach ($path in @($watcher, $config)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "缺少文件：$path" }
}

$settings = Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json
$python = [string]$settings.python_path
if (-not $python -or -not (Test-Path -LiteralPath $python)) {
    throw "未找到 Python 运行时：$python"
}

if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir 'watcher.log'

Write-Host "启动企业微信监听器（$($settings.mode) 模式），日志：$log"
& $python -X utf8 -B $watcher --config $config *>> $log
