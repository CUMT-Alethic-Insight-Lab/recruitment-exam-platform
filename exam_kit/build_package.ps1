# build_package.ps1 — 维护侧打包脚本
# 下载 Python 3.12 embeddable 到 runtime/，组装考生分发 zip 到 dist/
$ErrorActionPreference = "Stop"
$KitDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $KitDir

$PythonVersion = "3.12.10"
$EmbedUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$RuntimeDir = Join-Path $KitDir "runtime"
$DistDir = Join-Path $KitDir "dist"

Write-Host "===== Alethic Insight 机试包打包 =====" -ForegroundColor Cyan

# 1. Python embeddable 运行时
if (Test-Path (Join-Path $RuntimeDir "python.exe")) {
    Write-Host "[1/2] runtime\ 已存在，跳过下载" -ForegroundColor Green
} else {
    Write-Host "[1/2] 下载 Python $PythonVersion embeddable ..."
    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    $tmp = Join-Path $env:TEMP "python-embed.zip"
    Invoke-WebRequest -Uri $EmbedUrl -OutFile $tmp
    Expand-Archive -Path $tmp -DestinationPath $RuntimeDir -Force
    Remove-Item $tmp
    Write-Host "    已解压到 runtime\" -ForegroundColor Green
}

# 2. 组装分发 zip
Write-Host "[2/2] 组装分发包 ..."
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd"
$zipPath = Join-Path $DistDir "AlethicInsight机试包_$stamp.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath }

$stage = Join-Path $env:TEMP "alethic_exam_pkg"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path "$stage\pending_upload" | Out-Null
$files = @(
    "exam_server.py",
    "config.json",
    "AlethicInsight社团招新机试平台.ps1",
    "README.md"
)
foreach ($f in $files) {
    $src = Join-Path $KitDir $f
    if (-not (Test-Path $src)) { throw "缺少文件: $f" }
    Copy-Item $src $stage
}
Copy-Item $RuntimeDir "$stage\runtime" -Recurse

Compress-Archive -Path "$stage\*" -DestinationPath $zipPath
Remove-Item $stage -Recurse -Force

Write-Host ""
Write-Host "完成: $zipPath" -ForegroundColor Cyan
Write-Host "分发给考生前，请确认 config.json 中 cloud_base / campaign_id 为正式值。"
