# 仅恢复到全新目录，不切换或启动实例。
param(
    [Parameter(Mandatory=$true)][string]$BackupDir,
    [Parameter(Mandatory=$true)][string]$Destination,
    [Parameter(Mandatory=$true)][string]$CodeTag
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonPath = Join-Path $projectRoot '.venv/bin/python'
}
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '缺少项目.venv，请先按docs/environment.md安装环境' }
& $pythonPath (Join-Path $PSScriptRoot 'data_backup.py') restore --source $BackupDir --destination $Destination --code-tag $CodeTag
exit $LASTEXITCODE
