# 停止后端/MCP后备份；本脚本不代替所有者停写。
param(
    [Parameter(Mandatory=$true)][string]$DataDir,
    [Parameter(Mandatory=$true)][string]$Destination,
    [Parameter(Mandatory=$true)][string]$CodeTag,
    [switch]$Stopped
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonPath = Join-Path $projectRoot '.venv/bin/python'
}
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '缺少项目.venv，请先按docs/environment.md安装环境' }
$arguments = @((Join-Path $PSScriptRoot 'data_backup.py'), 'backup', '--source', $DataDir, '--destination', $Destination, '--code-tag', $CodeTag)
if ($Stopped) { $arguments += '--stopped' }
& $pythonPath @arguments
exit $LASTEXITCODE
