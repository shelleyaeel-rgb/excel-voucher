$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$python = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '请先创建 .venv 并安装 requirements-dev.txt。' }
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw '测试未通过。' }
$stageRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot ('outputs/build-staging/' + [Guid]::NewGuid().ToString())))
if (-not $stageRoot.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw '暂存目录超出项目范围。' }
New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
# A one-file helper can run outside the application directory during replacement.
$helperStage = Join-Path $stageRoot 'helper-runtime'
& $python -m PyInstaller --noconfirm --distpath $helperStage updater.spec
if ($LASTEXITCODE -ne 0) { throw '更新程序打包失败。' }
$previousHelperPath = $env:XIAOYU_HELPER_PATH
$env:XIAOYU_HELPER_PATH = Join-Path $helperStage 'update-helper.exe'
# Never let PyInstaller replace the usable dist directory or its user files.
try {
    & $python -m PyInstaller --noconfirm --distpath $stageRoot xiaoyu.spec
    if ($LASTEXITCODE -ne 0) { throw '打包失败。' }
} finally {
    $env:XIAOYU_HELPER_PATH = $previousHelperPath
}
$report = Join-Path $projectRoot 'outputs/qa/package-smoke.json'
$stagedApp = Join-Path $stageRoot '❤'
$executable = Join-Path $stagedApp '❤.exe'
$smoke = Start-Process -FilePath $executable -ArgumentList @('--self-test', ('"' + $report + '"')) -PassThru -WindowStyle Hidden
if (-not $smoke.WaitForExit(60000)) {
    $smoke.Kill()
    throw '免安装程序自检超时，请检查启动日志。'
}
if ($smoke.ExitCode -ne 0) { throw '免安装程序自检未通过。' }
& $python scripts/package_release.py --app-dir $stagedApp
if ($LASTEXITCODE -ne 0) { throw '压缩包生成失败。' }
