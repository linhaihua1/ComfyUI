<#
    ComfyUI 低显存自适应启动器
      - 自动识别显卡显存 / 系统内存，选择 VRAM 预留参数
      - 自动兼容新旧版本：torch >= 2.8 用 --vram-headroom，旧版用 --reserve-vram
      - 自动查找 venv / python_embeded / 系统 Python
    用法:
      启动ComfyUI.bat              以自动档启动
      启动ComfyUI.bat --lowvram    追加任意 ComfyUI 原生参数（透传）
      安装环境.bat                 创建 venv 并安装 PyTorch CUDA + 依赖
#>
param(
    [switch]$Install,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $Root) { $Root = (Get-Location).Path }

# ---------- 定位 ComfyUI 程序目录（当前目录或其子目录中含 main.py） ----------
$AppDir = $null
if (Test-Path (Join-Path $Root 'main.py')) {
    $AppDir = $Root
} else {
    foreach ($d in (Get-ChildItem -Path $Root -Directory -ErrorAction SilentlyContinue)) {
        if (Test-Path (Join-Path $d.FullName 'main.py')) { $AppDir = $d.FullName; break }
    }
}
if (-not $AppDir) {
    Write-Host "[错误] 在 $Root 下未找到 main.py，请把本脚本放在 ComfyUI 仓库根目录。" -ForegroundColor Red
    exit 1
}

# ---------- 定位 Python 解释器 ----------
$PyCandidates = @(
    (Join-Path $Root 'venv\Scripts\python.exe'),
    (Join-Path $AppDir 'venv\Scripts\python.exe'),
    (Join-Path $AppDir 'python_embeded\python.exe')
)
$Python = $PyCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

# ============================ 安装模式 ============================
if ($Install) {
    $VenvDir = Join-Path $Root 'venv'
    if (-not (Test-Path $VenvDir)) {
        $SysPy = $null
        foreach ($c in @('python', 'py')) {
            if (Get-Command $c -ErrorAction SilentlyContinue) { $SysPy = $c; break }
        }
        if (-not $SysPy) {
            Write-Host "[错误] 未检测到 Python，请先安装 Python 3.11 并勾选 Add to PATH。" -ForegroundColor Red
            exit 1
        }
        Write-Host "==> 创建虚拟环境: $VenvDir" -ForegroundColor Cyan
        & $SysPy -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { Write-Host "[错误] 虚拟环境创建失败。" -ForegroundColor Red; exit 1 }
    }
    $Python = Join-Path $VenvDir 'Scripts\python.exe'

    Write-Host "==> 升级 pip / wheel" -ForegroundColor Cyan
    & $Python -m pip install --upgrade pip wheel

    $DriverMajor = 0
    try { $DriverMajor = [int]((& nvidia-smi --query-gpu=driver_version --format=csv,noheader,nounits) -split '\.')[0] } catch {}
    if ($DriverMajor -ge 580) { $CuTag = 'cu130' } else { $CuTag = 'cu128' }
    Write-Host "==> 安装 PyTorch ($CuTag，支持 RTX 20 系 sm75，优先阿里云镜像)" -ForegroundColor Cyan
    & $Python -m pip install torch torchvision torchaudio --no-index --find-links "https://mirrors.aliyun.com/pytorch-wheels/$CuTag/" --no-deps
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    镜像安装失败，回退官方源" -ForegroundColor Yellow
        & $Python -m pip install torch torchvision torchaudio --index-url "https://download.pytorch.org/whl/$CuTag"
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[错误] PyTorch 安装失败，请检查网络后重试。" -ForegroundColor Red
        exit 1
    }
    & $Python -m pip install filelock typing-extensions sympy networkx jinja2 fsspec -i https://mirrors.aliyun.com/pypi/simple/
    $TorchBefore = (@(& $Python -c "import torch;print(torch.__version__)") | Select-Object -Last 1).Trim()
    Write-Host "    已安装 torch $TorchBefore" -ForegroundColor Gray

    # xformers 在 Turing(sm75) 上比 PyTorch SDPA 更快，ComfyUI 会自动优先使用；
    # 若与当前 torch 版本不匹配则回退，不影响运行。
    Write-Host "==> 安装 xformers（可选，加速 attention）" -ForegroundColor Cyan
    & $Python -m pip install xformers --no-deps -i https://mirrors.aliyun.com/pypi/simple/
    if ($LASTEXITCODE -eq 0) {
        $XfOk = $null
        try { $XfOk = (@(& $Python -c "import xformers.ops as x;print('ok' if getattr(x,'memory_efficient_attention',None) else 'bad')" 2>$null) | Select-Object -Last 1).Trim() } catch {}
        if ($XfOk -ne 'ok') {
            # 新版 xformers 依赖 mslk 才能启用 memory_efficient_attention
            Write-Host "    xformers 缺少 mslk，尝试补装" -ForegroundColor Yellow
            & $Python -m pip install mslk --no-deps -i "https://download.pytorch.org/whl/$CuTag"
            try { $XfOk = (@(& $Python -c "import xformers.ops as x;print('ok' if getattr(x,'memory_efficient_attention',None) else 'bad')" 2>$null) | Select-Object -Last 1).Trim() } catch {}
        }
        if ($XfOk -ne 'ok') {
            Write-Host "    xformers 与 torch $TorchBefore 不匹配，已卸载回退（将使用 SDPA）" -ForegroundColor Yellow
            & $Python -m pip uninstall -y xformers mslk | Out-Null
        }
    } else {
        Write-Host "    跳过 xformers（不影响运行，将使用 PyTorch SDPA）" -ForegroundColor Yellow
    }

    Write-Host "==> 安装 ComfyUI 依赖（阿里云镜像）" -ForegroundColor Cyan
    & $Python -m pip install -r (Join-Path $AppDir 'requirements.txt') -i https://mirrors.aliyun.com/pypi/simple/
    if ($LASTEXITCODE -ne 0) {
        & $Python -m pip install -r (Join-Path $AppDir 'requirements.txt') -i https://pypi.org/simple
    }

    Write-Host "==> 环境校验" -ForegroundColor Cyan
    & $Python -c "import torch;print('  torch:', torch.__version__);print('  CUDA 可用:', torch.cuda.is_available());print('  GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else '无')"
    if ($LASTEXITCODE -ne 0 -or -not (& $Python -c "import torch;print(torch.cuda.is_available())").Contains('True')) {
        Write-Host "[警告] CUDA 不可用，请检查 NVIDIA 驱动是否为最新版。" -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "安装完成，双击 启动ComfyUI.bat 即可运行。" -ForegroundColor Green
    exit 0
}

# ============================ 启动模式 ============================
if (-not $Python) { $Python = 'python' }
if ($null -eq $ExtraArgs) { $ExtraArgs = @() }

# ---------- 环境自检：一次调用取回 torch / CUDA / xformers 状态 ----------
$probeCode = @'
import sys
try:
    import torch
except Exception:
    print("TORCH_MISSING")
    sys.exit(0)
print("TORCH=" + torch.__version__)
print("CUDA=" + ("1" if torch.cuda.is_available() else "0"))
if torch.cuda.is_available():
    print("DEV=" + torch.cuda.get_device_name(0))
try:
    import xformers
    import xformers.ops as _xops
    ok = getattr(_xops, "memory_efficient_attention", None) and getattr(xformers, "_has_cpp_library", True)
    print("XF=" + (xformers.__version__ if ok else "broken"))
except Exception:
    print("XF=none")
'@

$TorchVer = $null; $CudaOk = $false; $DevName = ''; $XfVer = 'none'
$probeFile = Join-Path $env:TEMP ("comfyui_probe_{0}.py" -f $PID)
Set-Content -Path $probeFile -Value $probeCode -Encoding ASCII
try {
    foreach ($line in (& $Python $probeFile 2>$null)) {
        if     ($line -like 'TORCH=*') { $TorchVer = (($line.Substring(6)) -split '\+')[0].Trim() }
        elseif ($line -like 'CUDA=*')  { $CudaOk   = ($line.Substring(5) -eq '1') }
        elseif ($line -like 'DEV=*')   { $DevName  = $line.Substring(4).Trim() }
        elseif ($line -like 'XF=*')    { $XfVer    = $line.Substring(3).Trim() }
    }
} catch {} finally {
    Remove-Item $probeFile -Force -ErrorAction SilentlyContinue
}
if (-not $TorchVer) {
    Write-Host "[错误] 当前 Python 环境缺少 PyTorch，请先运行 安装环境.bat。" -ForegroundColor Red
    exit 1
}
if (-not $CudaOk) {
    Write-Host "[警告] PyTorch 检测不到 CUDA，将回退到 CPU 推理（极慢），请检查 NVIDIA 驱动。" -ForegroundColor Yellow
}

# ---------- 硬件探测 ----------
$VramMb = 0; $VramUsed = 0
try {
    $smi = & nvidia-smi --query-gpu=memory.total,memory.used --format=csv,noheader,nounits 2>$null
    if ($LASTEXITCODE -eq 0 -and $smi) {
        $parts = (@($smi)[0]).ToString().Split(',')
        $VramMb = [int]([double]$parts[0].Trim())
        if ($parts.Count -gt 1) { $VramUsed = [int]([double]$parts[1].Trim()) }
    }
} catch {}

$RamGb = 0
try {
    $RamGb = [math]::Round((Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).TotalPhysicalMemory / 1GB, 1)
} catch {}

# ---------- 显存档位：决定预留量 ----------
# DynamicVRAM（torch>=2.8）下 --vram-headroom 表示在默认预留之上额外保留的显存；
# 旧版走 --reserve-vram 表示绝对预留值。
$Dynamic = $false
try { $Dynamic = ([version]$TorchVer -ge [version]'2.8.0') } catch { $Dynamic = $false }

if ($VramMb -ge 7500)      { $NewH, $OldH = 0.5, 0.9 }
elseif ($VramMb -ge 5500)  { $NewH, $OldH = 0.6, 0.7 }
elseif ($VramMb -gt 0)     { $NewH, $OldH = 0.4, 0.5 }
else                       { $NewH, $OldH = 0.6, 0.7 }

$LaunchArgs = @()
if ($Dynamic) {
    $LaunchArgs += @('--vram-headroom', $NewH)
} else {
    $LaunchArgs += @('--reserve-vram', $OldH)
}
# 本地低显存场景不需要云端付费节点，禁用后前端不会再出现“登录以运行”提示
if ($ExtraArgs -notcontains '--disable-partner-nodes') {
    $LaunchArgs += '--disable-partner-nodes'
}
# 启用 ComfyUI-Manager（pip 安装的 comfyui-manager），用于补装缺失节点
if ($ExtraArgs -notcontains '--enable-manager' -and $ExtraArgs -notcontains '--disable-manager-ui') {
    $LaunchArgs += '--enable-manager'
}

# ---------- 文本编码器 fp8：仅 <=6G 档开启，T5-XXL 从 ~9G 降到 ~4.7G ----------
# 设环境变量 COMFY_NO_FP8_TE=1 可关闭；自己传了 text-enc 相关参数时不覆盖。
$Fp8TE = $false
$HasTextEncArg = (@($ExtraArgs -match 'text-enc').Count -gt 0)
if ($VramMb -gt 0 -and $VramMb -lt 7500 -and -not $HasTextEncArg -and -not $env:COMFY_NO_FP8_TE) {
    $LaunchArgs += '--fp8_e4m3fn-text-enc'
    $Fp8TE = $true
}

# ---------- 端口避让：8188 被占用时自动顺延 ----------
function Test-PortOpen([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(300)) { return $client.Connected }
        return $false
    } catch { return $false } finally { $client.Close() }
}
$Port = 8188
$UserPort = (@($ExtraArgs -match '^--port').Count -gt 0)
if ($UserPort) {
    # 用户自己指定了对外端口，仅解析出来用于展示，不再追加参数
    $portIdx = [array]::IndexOf($ExtraArgs, '--port')
    if ($portIdx -ge 0 -and ($portIdx + 1) -lt $ExtraArgs.Count) {
        try { $Port = [int]$ExtraArgs[$portIdx + 1] } catch {}
    } else {
        foreach ($a in $ExtraArgs) { if ($a -match '^--port=(\d+)$') { $Port = [int]$Matches[1] } }
    }
} else {
    while ($Port -lt 8200 -and (Test-PortOpen $Port)) { $Port++ }
}

# ---------- 内部端口：ComfyUI 本体只监听本机内部端口，对外由登录门禁接管 ----------
function Find-FreePort([int]$Start, [int]$End) {
    for ($p = $Start; $p -le $End; $p++) { if (-not (Test-PortOpen $p)) { return $p } }
    return $null
}
$BackendPort = Find-FreePort ($Port + 100) ($Port + 199)
if (-not $BackendPort) {
    Write-Host "[错误] 没有可用的内部端口（$($Port + 100)-$($Port + 199)）。" -ForegroundColor Red
    exit 1
}
$LaunchArgs += @('--port', $BackendPort)

# 端口与自动开浏览器由启动器接管
$CleanArgs = @()
for ($i = 0; $i -lt $ExtraArgs.Count; $i++) {
    $a = $ExtraArgs[$i]
    if ($a -eq '--port') { $i++; continue }
    if ($a -match '^--port=' -or $a -eq '--auto-launch') { continue }
    $CleanArgs += $a
}

# ---------- 启动摘要 ----------
$Attn = if ($XfVer -ne 'none' -and $XfVer -ne 'broken') { "xformers $XfVer" } else { 'pytorch SDPA' }
Write-Host "==================================================" -ForegroundColor DarkGray
Write-Host (" Python     : {0} (torch {1})" -f $Python, $TorchVer)
Write-Host (" GPU        : {0} {1}MB, 当前占用 {2}MB" -f $(if ($DevName) { $DevName } else { '未知' }), $VramMb, $VramUsed)
Write-Host (" 内存       : {0} GB" -f $RamGb)
Write-Host (" Attention  : {0}" -f $Attn)
Write-Host (" 显存策略   : {0} {1} ({2})" -f $(if ($Dynamic) { '--vram-headroom' } else { '--reserve-vram' }), $(if ($Dynamic) { $NewH } else { $OldH }), $(if ($Dynamic) { 'DynamicVRAM' } else { 'legacy 回退' }))
Write-Host (" 文本编码器 : {0}" -f $(if ($Fp8TE) { 'fp8_e4m3fn（低显存优化，COMFY_NO_FP8_TE=1 可关闭）' } else { '默认 fp16' }))
Write-Host (" 访问地址   : http://127.0.0.1:{0}" -f $Port)
Write-Host (" 登录账号   : admin / admin@123（本地内置）")
Write-Host (" 程序目录   : {0}" -f $AppDir)
Write-Host "==================================================" -ForegroundColor DarkGray

if ($VramUsed -gt 1200) {
    Write-Host ("提示: 显存已被其他程序占用 {0}MB，建议关闭浏览器硬件加速/游戏后再启动。" -f $VramUsed) -ForegroundColor Yellow
}
if ($VramMb -gt 0 -and $VramMb -lt 7500) {
    Write-Host "提示: 6G 级别显卡建议使用 SD1.5 / SDXL-Turbo / Flux GGUF-Q4 等轻量模型。" -ForegroundColor Yellow
}
if ($RamGb -gt 0 -and $RamGb -lt 20) {
    Write-Host "提示: 物理内存不足 20G，建议把虚拟内存调到 16G 以上以减少爆内存。" -ForegroundColor Yellow
}

Push-Location $AppDir
$Gate = $null
try {
    # ---------- 启动登录门禁（内置账号 admin / admin@123） ----------
    $GateScript = Join-Path $Root 'launcher_src\login_gate.py'
    if (-not (Test-Path $GateScript)) {
        Write-Host "[错误] 未找到 launcher_src\login_gate.py，登录门禁无法启动。" -ForegroundColor Red
        exit 1
    }
    $Gate = Start-Process -FilePath $Python -ArgumentList @("`"$GateScript`"", "$Port", "$BackendPort", 'admin', 'admin@123') -WindowStyle Hidden -PassThru
    $GateOk = $false
    for ($i = 0; $i -lt 50; $i++) {
        if (Test-PortOpen $Port) { $GateOk = $true; break }
        Start-Sleep -Milliseconds 100
    }
    if (-not $GateOk) {
        Write-Host "[错误] 登录门禁启动失败，端口 $Port 可能被占用。" -ForegroundColor Red
        exit 1
    }
    Start-Sleep -Milliseconds 600
    try { Start-Process "http://127.0.0.1:$Port/" } catch {}

    & $Python 'main.py' @LaunchArgs @CleanArgs
} finally {
    Pop-Location
    if ($Gate -and -not $Gate.HasExited) { Stop-Process -Id $Gate.Id -Force -ErrorAction SilentlyContinue }
}
