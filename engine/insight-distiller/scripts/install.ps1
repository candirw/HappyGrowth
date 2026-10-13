<#
.SYNOPSIS
    Insight Distiller 一键安装（Windows）。

.DESCRIPTION
    在当前项目目录建立虚拟环境并安装依赖，最后跑环境自检。

    执行策略可能阻止脚本运行，那就用：
        powershell -ExecutionPolicy Bypass -File scripts\install.ps1

.PARAMETER Minimal
    只装核心依赖（采集 + 浓缩 + CLI）。不含语义检索、转写、网页版。
    适合先跑通再按需加装。

.PARAMETER Cpu
    强制 CPU 版 PyTorch。没有 NVIDIA 显卡时**务必加这个**——
    默认的 CUDA 版约 2.5GB，而没有独显的话它一点用都没有。

.PARAMETER SkipDoctor
    装完不跑自检。
#>
param(
    [switch]$Minimal,
    [switch]$Cpu,
    [switch]$SkipDoctor
)

$ErrorActionPreference = "Stop"

# Windows 控制台默认不是 UTF-8，中文输出会乱码。
# 这与项目内部强制 UTF-8 的 IO 封装是两回事——这里管的是终端显示。
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

function Info($m)  { Write-Host "  $m" -ForegroundColor Gray }
function Step($m)  { Write-Host "`n$m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "  [!] $m" -ForegroundColor Yellow }
function Die($m)   { Write-Host "`n[X] $m" -ForegroundColor Red; exit 1 }

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "Insight Distiller · Windows 安装" -ForegroundColor White
Info "项目目录 $Root"

# ---------- 1. Python ----------
Step "[1/5] 检查 Python"

$pythonCmd = $null
foreach ($c in @("python", "python3", "py")) {
    try {
        $v = & $c -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) {
            $parts = $v.Trim().Split(".")
            $major = [int]$parts[0]; $minor = [int]$parts[1]
            # 项目要求 >=3.11,<3.15
            if ($major -eq 3 -and $minor -ge 11 -and $minor -lt 15) {
                $pythonCmd = $c
                Ok "Python $v ($c)"
                break
            } else {
                Warn "$c 是 Python $v，需要 3.11 ~ 3.14"
            }
        }
    } catch { }
}

if (-not $pythonCmd) {
    Die @"
未找到合适的 Python（需要 3.11 ~ 3.14）。

安装方式（任选）：
  winget install Python.Python.3.12
  或从 https://www.python.org/downloads/ 下载

安装时务必勾选 "Add python.exe to PATH"，装完重开 PowerShell 再运行本脚本。
"@
}

# ---------- 2. 虚拟环境 ----------
Step "[2/5] 建立虚拟环境"

if (Test-Path ".venv") {
    # 从 macOS 拷过来的包里不该有 .venv，但万一手动 zip 带过来了，
    # 里面全是 macOS 二进制，必须删掉重建，否则后面报错会非常难懂
    $hasWinPython = Test-Path ".venv\Scripts\python.exe"
    if (-not $hasWinPython) {
        Warn ".venv 不是 Windows 环境（可能是从 macOS 拷来的），删除重建"
        Remove-Item -Recurse -Force ".venv"
    } else {
        Ok ".venv 已存在，复用"
    }
}

if (-not (Test-Path ".venv")) {
    & $pythonCmd -m venv .venv
    if ($LASTEXITCODE -ne 0) { Die "创建虚拟环境失败" }
    Ok "已创建 .venv"
}

$Py  = Join-Path $Root ".venv\Scripts\python.exe"
$Pip = Join-Path $Root ".venv\Scripts\pip.exe"
if (-not (Test-Path $Py)) { Die "虚拟环境异常：找不到 $Py" }

& $Py -m pip install --quiet --upgrade pip setuptools wheel
Ok "pip 已更新"

# ---------- 3. PyTorch（决定体积的关键一步）----------
Step "[3/5] 安装依赖"

if (-not $Minimal) {
    $useCpu = $Cpu
    if (-not $useCpu) {
        # 检测 NVIDIA 显卡。检测不到就默认 CPU 版，
        # 否则用户会白下 2.5GB 的 CUDA 包
        $hasNvidia = $false
        try {
            $null = Get-Command nvidia-smi -ErrorAction Stop
            & nvidia-smi | Out-Null
            if ($LASTEXITCODE -eq 0) { $hasNvidia = $true }
        } catch { }

        if ($hasNvidia) {
            Info "检测到 NVIDIA 显卡，使用 CUDA 版 PyTorch（约 2.5GB，转写会快很多）"
        } else {
            Info "未检测到 NVIDIA 显卡，改用 CPU 版 PyTorch（约 200MB）"
            $useCpu = $true
        }
    } else {
        Info "已指定 --Cpu，使用 CPU 版 PyTorch"
    }

    if ($useCpu) {
        # 先装 CPU 版 torch，后续 sentence-transformers 就不会再拉 CUDA 版
        Info "下载 PyTorch (CPU)…"
        & $Pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu
        if ($LASTEXITCODE -ne 0) {
            Warn "CPU 版 PyTorch 安装失败，将由后续步骤自动决定版本"
        } else {
            Ok "PyTorch (CPU) 已安装"
        }
    }
}

$extras = if ($Minimal) { "" } else { "[all]" }
Info "安装项目依赖$extras（首次较慢，请耐心等待）…"
& $Pip install -e ".$extras"
if ($LASTEXITCODE -ne 0) {
    Die @"
依赖安装失败。常见原因：

1. 网络问题 —— 可换国内镜像：
     .venv\Scripts\pip install -e ".$extras" -i https://pypi.tuna.tsinghua.edu.cn/simple
2. 缺少编译环境 —— 极少数包需要 Visual C++ Build Tools：
     https://visualstudio.microsoft.com/visual-cpp-build-tools/
3. 先用最小安装跑通，再按需加装：
     powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Minimal
"@
}
Ok "依赖安装完成"

# ---------- 4. 配置 ----------
Step "[4/5] 准备配置"

if (Test-Path "config.yaml") {
    Ok "config.yaml 已存在，保留不动"
} elseif (Test-Path "config.example.yaml") {
    Copy-Item "config.example.yaml" "config.yaml"
    Ok "已从模板生成 config.yaml"
    Warn "需要填入 API Key 才能使用浓缩与问答功能"
} else {
    Warn "找不到 config.example.yaml"
}

# 模型缓存：如果包里带了 hf-cache，提示放置位置
$hfSrc = Join-Path (Split-Path -Parent $Root) "hf-cache"
if (Test-Path $hfSrc) {
    $hfDst = Join-Path $env:USERPROFILE ".cache\huggingface"
    if (-not (Test-Path $hfDst)) {
        Info "发现随包携带的模型缓存，正在放置…"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $hfDst) | Out-Null
        Copy-Item -Recurse $hfSrc $hfDst
        Ok "模型缓存已就位，首次检索无需联网下载"
    }
}

# ---------- 5. 自检 ----------
if (-not $SkipDoctor) {
    Step "[5/5] 环境自检"
    & (Join-Path $Root ".venv\Scripts\distill.exe") doctor
} else {
    Step "[5/5] 跳过自检"
}

Write-Host "`n安装完成。" -ForegroundColor Green
Write-Host @"

日常使用（每次新开终端先激活环境）：
  .venv\Scripts\activate

  distill add <url>              消化一条内容
  distill ask "问题"             基于知识库提问
  distill serve                  打开网页版
  distill doctor                 环境自检

不想每次激活，也可直接用完整路径：
  .venv\Scripts\distill.exe list

"@ -ForegroundColor Gray

if (-not (Test-Path "config.yaml")) { exit 0 }

$cfg = Get-Content "config.yaml" -Raw -Encoding UTF8
if ($cfg -match '\$\{[A-Z_]+\}' -or $cfg -match 'sk-你的|your-api-key') {
    Warn "config.yaml 里的 API Key 还是占位符，记得填写"
    Info "DeepSeek 申请：https://platform.deepseek.com/"
}
