# DeTSP Windows launcher: create venv on first run, install deps, start.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 找真正的 Python：逐个候选验证能输出版本号（微软商店的占位 python
# 也能被 Get-Command 找到但运行不了，必须实测）
$py = $null
$pyArgs = @()
foreach ($c in @("python", "py")) {
    if (-not (Get-Command $c -ErrorAction SilentlyContinue)) { continue }
    $extra = if ($c -eq "py") { @("-3") } else { @() }
    try {
        $ver = & $c @extra --version 2>&1
        if ($ver -match "Python 3\.(1[1-9]|[2-9][0-9])") { $py = $c; $pyArgs = $extra; break }
        if ($ver -match "Python 3\.") {
            Write-Host "找到的 Python 版本过低：$ver，需要 3.11 以上。"
            Write-Host "请到 https://www.python.org 下载安装 3.11+，安装时务必勾选 Add python.exe to PATH。"
            pause
            exit 1
        }
    } catch { }
}
if (-not $py) {
    Write-Host "没有找到能用的 Python（微软商店的占位程序不算）。"
    Write-Host "请到 https://www.python.org 下载安装 Python 3.11 以上版本，"
    Write-Host "安装时务必勾选 Add python.exe to PATH，然后重新双击一键启动。"
    pause
    exit 1
}
Write-Host "使用 Python：$(& $py @pyArgs --version 2>&1)"

# 已有 .venv 若残缺（Scripts\python.exe 不在），删掉重建
if ((Test-Path ".venv") -and -not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "检测到残缺的 .venv，删除重建..."
    Remove-Item -Recurse -Force ".venv"
}
if (-not (Test-Path ".venv")) {
    Write-Host "First run: creating virtual environment..."
    & $py @pyArgs -m venv .venv
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        Write-Host "虚拟环境创建失败：.venv\Scripts\python.exe 不存在。"
        Write-Host "常见原因：Python 安装不完整。请重新安装 Python 3.11+ 并勾选 pip 与 PATH，再重试。"
        pause
        exit 1
    }
}
$venvPy = ".\.venv\Scripts\python.exe"
Write-Host "Installing dependencies..."
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -e ".[live]"
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
if (-not (Test-Path "config.yaml")) { Copy-Item "config.example.yaml" "config.yaml" }
Write-Host "Starting DeTSP (panel: http://127.0.0.1:8320/) ..."
& $venvPy -m detsp
