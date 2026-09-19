# Alethic Insight 社团招新机试平台 — 启动器
# 自检（云端连通 / kimi 已装 / kimi 数据目录存在）→ 拉起本地机试服务 → 打开浏览器
$ErrorActionPreference = "Stop"
$KitDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $KitDir

function Fail($msg) {
    Write-Host ""
    Write-Host "[失败] $msg" -ForegroundColor Red
    Write-Host ""
    Read-Host "按回车退出"
    exit 1
}

function Test-Port($port) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect("127.0.0.1", $port)
        $client.Close()
        return $true
    } catch { return $false }
}

Write-Host "===== Alethic Insight 招新机试 =====" -ForegroundColor Cyan

# 读取配置
$CfgPath = Join-Path $KitDir "config.json"
if (-not (Test-Path $CfgPath)) { Fail "缺少 config.json，请确认机试包完整解压。" }
$Cfg = Get-Content $CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
$CloudBase = $Cfg.cloud_base.TrimEnd("/")
$ExamPort = [int]$Cfg.exam_port
$KimiHome = if ($Cfg.kimi_home) { $Cfg.kimi_home } `
    elseif ($env:KIMI_CODE_HOME) { $env:KIMI_CODE_HOME } `
    else { Join-Path $env:USERPROFILE ".kimi-code" }

# 自检 1：云端连通
Write-Host "[自检 1/3] 云端连通性 ($CloudBase) ..."
try {
    $health = Invoke-RestMethod -Uri "$CloudBase/api/health" -TimeoutSec 8
    if (-not $health.ok) { throw "健康检查返回异常" }
    Write-Host "    OK" -ForegroundColor Green
} catch { Fail "无法连接云端 $CloudBase`n请检查网络，或确认 config.json 中 cloud_base 是否正确。`n$_" }

# 自检 2：kimi 已安装
Write-Host "[自检 2/3] Kimi Code CLI ..."
$kimi = Get-Command kimi -ErrorAction SilentlyContinue
if (-not $kimi) { Fail "未找到 kimi 命令。请先按 README 安装 Kimi Code 0.40.1。" }
$ver = (& kimi --version 2>$null | Select-Object -First 1)
Write-Host "    OK（版本 $ver，要求 0.40.1）" -ForegroundColor Green

# 自检 3：kimi 数据目录
Write-Host "[自检 3/3] Kimi 数据目录 ($KimiHome) ..."
if (-not (Test-Path $KimiHome)) {
    Fail "未找到 $KimiHome`n请先启动一次 Kimi Code 并完成 /login 登录。"
}
Write-Host "    OK" -ForegroundColor Green

# 选择 Python
$Python = $null
$Bundled = Join-Path $KitDir "runtime\python.exe"
if (Test-Path $Bundled) { $Python = $Bundled }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $Python = "py -3.12" }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $Python = "python" }
else { Fail "未找到可用 Python（机试包应内含 runtime\python.exe，请勿只解压部分文件）。" }

# 拉起机试服务
Write-Host ""
Write-Host "启动机试服务 ..."
$ServerScript = Join-Path $KitDir "exam_server.py"
if ($Python -eq "py -3.12") {
    Start-Process -FilePath "py" -ArgumentList "-3.12", "`"$ServerScript`"" -WorkingDirectory $KitDir
} else {
    Start-Process -FilePath $Python -ArgumentList "`"$ServerScript`"" -WorkingDirectory $KitDir
}

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-Port $ExamPort) { $ready = $true; break }
}
if (-not $ready) { Fail "机试服务 15 秒内未就绪（端口 $ExamPort）。请查看弹出的服务窗口报错。" }

Write-Host "机试服务已启动: http://127.0.0.1:$ExamPort" -ForegroundColor Green
Start-Process "http://127.0.0.1:$ExamPort"
Write-Host ""
Write-Host "浏览器已打开机试页面。本窗口可以关闭。" -ForegroundColor Cyan
