# aod-bridge/start-mock-lan.ps1
# 一键：以 mock 模式在局域网起 aod-relay，供手机（同一 Wi-Fi）先验证链路。
# 用法：powershell -ExecutionPolicy Bypass -File start-mock-lan.ps1 [-Port 8790] [-Token devtoken123]
param(
    [int]$Port = 8790,
    [string]$Token = "devtoken123",
    [string]$Range = "today",
    [string]$Python = ""
)
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$relay = Join-Path $PSScriptRoot "relay.py"
if (-not (Test-Path $relay)) { throw "找不到 relay.py: $relay" }

function Resolve-Python([string]$explicit) {
    if ($explicit) { return $explicit }
    $candidates = @()
    foreach ($name in @("python", "python3")) {
        foreach ($cmd in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if ($cmd.Source) { $candidates += $cmd.Source }
        }
    }
    $candidates += @(
        "C:\Program Files\KiCad\10.0\bin\python.exe",
        "C:\Program Files\PyManager\python.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe")
    )
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not $candidate -or -not (Test-Path $candidate)) { continue }
        try {
            $version = & $candidate --version 2>&1
            if ($LASTEXITCODE -eq 0 -and "$version" -match "Python 3") { return $candidate }
        } catch { }
    }
    return $null
}
$python = Resolve-Python $Python
if (-not $python) {
    throw "未找到可用的 Python 3.8+。安装 Python 3 并勾选 Add to PATH，或用 -Python <python.exe 路径> 指定。"
}
Write-Host ("使用 Python: {0}" -f $python)

# 找真正连到局域网的 IPv4：优先“带默认网关”的网卡（排除 VirtualBox / Hyper-V 的 host-only 虚拟网卡）
$candidates = @()
try {
    $candidates = @(Get-NetIPConfiguration -ErrorAction Stop |
        Where-Object { $_.IPv4DefaultGateway -ne $null -and $_.IPv4Address -ne $null } |
        ForEach-Object { $_.IPv4Address.IPAddress })
} catch {
    $candidates = @()
}
if ($candidates.Count -eq 0) {
    $gated = @()
    $all = @()
    $ip = $null
    $hasGateway = $false
    foreach ($line in (ipconfig)) {
        if ($line -match '^\S.*:\s*$') {
            if ($ip -and $hasGateway) { $gated += $ip }
            $ip = $null
            $hasGateway = $false
            continue
        }
        if ($line -match 'IPv4[^:]*:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)') {
            $ip = $Matches[1]
            $all += $ip
            continue
        }
        if ($ip -and $line -match '(Gateway|网关)[^:]*:\s*[0-9]') { $hasGateway = $true }
    }
    if ($ip -and $hasGateway) { $gated += $ip }
    $candidates = if ($gated.Count -gt 0) { $gated } else { $all }
}
$lan = $candidates | Where-Object { $_ -match '^(192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.)' } | Select-Object -First 1
if (-not $lan) { $lan = $candidates | Select-Object -First 1 }
if (-not $lan) { $lan = "<本机局域网IP>" }

Write-Host ""
Write-Host "AOD Bridge - mock 中转（局域网）"
Write-Host ("  监听      : http://0.0.0.0:{0}" -f $Port)
Write-Host ("  手机地址  : http://{0}:{1}" -f $lan, $Port)
Write-Host ("  手机令牌  : {0}" -f $Token)
Write-Host ("  浏览器自测: http://{0}:{1}/aod/usage?range={2}&format=text&token={3}" -f $lan, $Port, $Range, $Token)
if ($candidates.Count -gt 1) {
    Write-Host ("  检测到的其他地址（若上面那条手机连不上，换这些试）: {0}" -f ($candidates -join ', '))
}
Write-Host ""
Write-Host "  提示：手机连不上时，检查 Windows 防火墙是否放行 python 的专用网络入站，"
Write-Host "        管理员 PowerShell 可一次性放行："
Write-Host ('        netsh advfirewall firewall add rule name="aod-relay {0}" dir=in action=allow protocol=TCP localport={0} profile=private' -f $Port)
Write-Host "  按 Ctrl+C 停止。"
Write-Host ""

& $python $relay --mock --port $Port --relay-token $Token
