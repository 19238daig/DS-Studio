param(
    [int]$ApiPort = 8000,
    [int]$UiPort = 5173,
    [switch]$Dev,
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
if ($ApiPort -lt 1 -or $ApiPort -gt 65535 -or ($Dev -and ($UiPort -lt 1 -or $UiPort -gt 65535 -or $ApiPort -eq $UiPort))) {
    throw '请指定有效端口，开发模式的前后端端口必须不同。'
}
if (-not $Dev -and -not (Test-Path -LiteralPath (Join-Path $projectRoot 'frontend\dist\index.html'))) {
    throw '尚未构建 React 前端。请进入 frontend 目录运行 npm ci 和 npm run build。'
}
$nodePath = $null
if ($Dev) {
    $nodePath = (Get-Command node -ErrorAction Stop).Source
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'frontend\node_modules\vite\bin\vite.js'))) {
        throw '缺少前端开发依赖。请进入 frontend 目录运行 npm ci。'
    }
}
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
if (Test-Path -LiteralPath $processFile) {
    throw '已有本项目的启动记录。请先运行 scripts/stop.ps1，以免重复启动 Worker。'
}
$ports = @($ApiPort)
if ($Dev) { $ports += $UiPort }
foreach ($port in $ports) {
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

function Start-TrackedProcess([string]$Name, [string[]]$Arguments, [string]$Marker, [string]$Executable = $pythonPath, [string]$WorkingDirectory = $projectRoot) {
    $outPath = Join-Path $runtimePath "$Name.stdout.log"
    $errPath = Join-Path $runtimePath "$Name.stderr.log"
    $proc = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -RedirectStandardOutput $outPath -RedirectStandardError $errPath -PassThru
    Start-Sleep -Milliseconds 200
    $identity = Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.Id)"
    if (-not $identity) { throw "$Name 启动后已退出。请检查 .runtime 下对应日志。" }
    $records.Add([pscustomobject]@{
        name = $Name
        pid = $proc.Id
        executable = $Executable
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
    $webUrl = "http://127.0.0.1:$ApiPort"
    if ($Dev) {
        Start-TrackedProcess 'web-dev' @('node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', "$UiPort", '--strictPort') 'vite/bin/vite.js' $nodePath (Join-Path $projectRoot 'frontend')
        $webUrl = "http://127.0.0.1:$UiPort"
    }
    $uiHealthy = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $page = Invoke-WebRequest -UseBasicParsing -Uri "$webUrl/" -TimeoutSec 2
            if ($page.Content -notmatch '<div id="root">') { throw 'React 入口尚未就绪。' }
            $uiHealthy = $true
            break
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $uiHealthy) { throw 'React 界面未通过启动检查。请检查 .runtime 下 API / web-dev 日志。' }
    Write-Host "数据工作台已启动：$webUrl"
    Write-Host "API 文档：http://127.0.0.1:$ApiPort/docs"
    Write-Host '停止本次服务：.\scripts\stop.ps1'
    if (-not $NoBrowser) { Start-Process "$webUrl" }
} catch {
    Write-Warning $_.Exception.Message
    if (Test-Path -LiteralPath $processFile) { & (Join-Path $PSScriptRoot 'stop.ps1') }
    throw
} finally {
    if ($null -eq $oldApiUrl) { Remove-Item Env:DS_API_URL -ErrorAction SilentlyContinue }
    else { $env:DS_API_URL = $oldApiUrl }
}
