param(
    [Parameter(Mandatory = $true)][string]$NotesFile,
    [string]$Repository = 'shelleyaeel-rgb/excel-voucher',
    [string]$SigningKey = (Join-Path $env:USERPROFILE '.codex/excel-voucher-update/signing-key.pem'),
    [switch]$SkipBuild,
    [long]$ResumeDraft = 0
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$python = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $NotesFile -PathType Leaf)) { throw '请提供本次更新说明文件。' }
if (-not (Test-Path -LiteralPath $SigningKey -PathType Leaf)) { throw '找不到发布签名私钥，请先运行 scripts/update_keys.py 配置。' }
if ($ResumeDraft -ne 0 -and -not $SkipBuild) { throw '继续草稿时请同时加 -SkipBuild，保持已上传的更新包不变。' }
if (-not $SkipBuild) { & (Join-Path $PSScriptRoot 'build.ps1') }
# The publisher always re-verifies the ZIP and runs its isolated self-test.
$publishArguments = @('scripts/publish_update.py', 'github', '--repo', $Repository, '--private-key', $SigningKey, '--notes-file', $NotesFile, '--git-credential')
if ($ResumeDraft -ne 0) { $publishArguments += @('--resume-draft', [string]$ResumeDraft) }
& $python @publishArguments
if ($LASTEXITCODE -ne 0) { throw '远程发布未完成，请检查输出；原有最新版不会被不完整的草稿替换。' }
