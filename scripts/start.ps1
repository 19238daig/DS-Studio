param(
    [int]$ApiPort = 8000,
    [int]$UiPort = 8501,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runtimePath = Join-Path $projectRoot '.runtime'
$processFile = Join-Path $runtimePath 'processes.json'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw '缺少 .venv。请按 README 安装依赖后再启动。'
}
if ($ApiPort -lt 1 -or $ApiPort -gt 65535 -or $UiPort -lt 1 -or $UiPort -gt 65535 -or $ApiPort -eq $UiPort) {
    throw 'API 和界面端口必须为不同的有效端口。'
}
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
if (Test-Path -LiteralPath $processFile) {
    throw '已有本项目的启动记录。请先运行 scripts/stop.ps1，以免重复启动 Worker。'
}
foreach ($port in @($ApiPort, $UiPort)) {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $port)
    try { $listener.Start() }
    catch { throw "端口 $port 已被占用，未启动服务。请检查占用程序或指定其他端口。" }
    finally { $listener.Stop() }
}
$records = [System.Collections.Generic.List[object]]::new()
$runId = [guid]::NewGuid().ToString('N')
# Set only a non-secret API URL in the child environment; credentials stay in local .env.
$oldApiUrl = $env:DS_API_URL
$env:DS_API_URL = "http://127.0.0.1:$ApiPort"

function Start-TrackedProcess([string]$Name, [string[]]$Arguments, [string]$Marker) {
    $outPath = Join-Path $runtimePath "$Name.stdout.log"
    $errPath = Join-Path $runtimePath "$Name.stderr.log"
    $proc = Start-Process -FilePath $pythonPath -ArgumentList $Arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $outPath -RedirectStandardError $errPath -PassThru
    Start-Sleep -Milliseconds 200
    $identity = Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.Id)"
    if (-not $identity) { throw "$Name 启动后已退出。请检查 .runtime 下对应日志。" }
    $records.Add([pscustomobject]@{
        name = $Name
        pid = $proc.Id
        executable = $pythonPath
        creationTime = $identity.CreationDate.ToUniversalTime().ToString('o')
        commandLine = $identity.CommandLine
        marker = $Marker
    })
    @{ run_id = $runId; project = $projectRoot; api_url = "http://127.0.0.1:$ApiPort"; processes = @($records.ToArray()) } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $processFile -Encoding UTF8
}

try {
    Start-TrackedProcess 'api' @('-m', 'uvicorn', 'src.api:app', '--host', '127.0.0.1', '--port', "$ApiPort", '--no-access-log') 'src.api:app'
    $healthy = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $null = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/api/v1/health" -TimeoutSec 2
            $healthy = $true
            break
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $healthy) { throw 'API 未在启动时限内通过健康检查。请检查 .runtime/api.stderr.log。' }
    Start-TrackedProcess 'worker' @('worker.py') 'worker.py'
    $workerHealthy = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/api/v1/health" -TimeoutSec 2
            if ($health.worker.ready) { $workerHealthy = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $workerHealthy) { throw 'Worker 未在启动时限内发送心跳。请检查 .runtime/worker.stderr.log。' }
    Start-TrackedProcess 'ui' @('-m', 'streamlit', 'run', 'app.py', '--server.address', '127.0.0.1', '--server.port', "$UiPort", '--server.headless', 'true', '--browser.gatherUsageStats', 'false') 'streamlit run app.py'
    $uiHealthy = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $null = Invoke-RestMethod -Uri "http://127.0.0.1:$UiPort/_stcore/health" -TimeoutSec 2
            $uiHealthy = $true
            break
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $uiHealthy) { throw '界面未在启动时限内通过健康检查。请检查 .runtime/ui.stderr.log。' }
    Write-Host "数据工作台已启动：http://127.0.0.1:$UiPort"
    Write-Host "API 文档：http://127.0.0.1:$ApiPort/docs"
    Write-Host '停止本次服务：.\scripts\stop.ps1'
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$UiPort" }
} catch {
    Write-Warning $_.Exception.Message
    if (Test-Path -LiteralPath $processFile) { & (Join-Path $PSScriptRoot 'stop.ps1') }
    throw
} finally {
    if ($null -eq $oldApiUrl) { Remove-Item Env:DS_API_URL -ErrorAction SilentlyContinue }
    else { $env:DS_API_URL = $oldApiUrl }
}
