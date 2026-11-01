param([switch]$IfIdle)
$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$processFile = Join-Path $projectRoot '.runtime\processes.json'
if (-not (Test-Path -LiteralPath $processFile)) {
    Write-Host '没有本项目启动脚本登记的进程。'
    exit 0
}
$record = Get-Content -LiteralPath $processFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ([System.IO.Path]::GetFullPath($record.project) -ne $projectRoot) {
    throw '启动记录的项目路径不匹配，未停止任何进程。'
}
$workerEntry = @($record.processes) | Where-Object { $_.name -eq 'worker' } | Select-Object -First 1
if ($workerEntry -and (Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$workerEntry.pid)")) {
    try {
        $health = Invoke-RestMethod -Uri "$($record.api_url)/api/v1/health" -TimeoutSec 3
        $activeTaskIds = @(@($health.worker.task_ids) + @($health.worker.task_id) | Where-Object { $_ } | Select-Object -Unique)
        if ($activeTaskIds.Count -gt 0) {
            if ($IfIdle) { throw "Active tasks exist; services and tasks have been preserved." }
            foreach ($taskId in $activeTaskIds) {
                $null = Invoke-RestMethod -Method Post -Uri "$($record.api_url)/api/v1/tasks/$taskId/cancel" -TimeoutSec 5
            }
            $terminated = $false
            for ($attempt = 0; $attempt -lt 60; $attempt++) {
                $stillActive = $false
                foreach ($taskId in $activeTaskIds) {
                    $task = Invoke-RestMethod -Uri "$($record.api_url)/api/v1/tasks/$taskId" -TimeoutSec 3
                    if ($task.status -in @('queued','running','cancelling','cancel_requested','pending')) { $stillActive = $true }
                }
                if (-not $stillActive) {
                    $terminated = $true
                    break
                }
                Start-Sleep -Milliseconds 500
            }
            if (-not $terminated) { throw 'Worker 尚未确认任务终止。' }
        }
    } catch {
        if ($IfIdle) { throw }
        throw '无法确认 Worker 任务和容器已停止，已保留服务与启动记录。请先恢复 API 或稍后再运行 stop.ps1。'
    }
}
$unmatched = $false
# Windows virtualenv launchers may own a second python.exe process. Stop only
# descendants of the identity-checked process, rather than leaving its server
# listening after the launcher exits. Verify each identity again before killing.
function Stop-OwnedTree($Owner) {
    foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($Owner.ProcessId)")) {
        if ($child.CreationDate -ge $Owner.CreationDate) { Stop-OwnedTree $child }
    }
    $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($Owner.ProcessId)"
    if ($current -and $current.CreationDate -eq $Owner.CreationDate -and $current.CommandLine -eq $Owner.CommandLine) {
        Stop-Process -Id ([int]$current.ProcessId) -ErrorAction SilentlyContinue
    }
}
foreach ($entry in @($record.processes) | Sort-Object @{Expression={ if ($_.name -eq 'worker') { 0 } elseif ($_.name -in @('ui','web-dev')) { 1 } else { 2 } }}) {
    $identity = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$entry.pid)"
    if (-not $identity) { continue }
    $sameTime = $identity.CreationDate.ToUniversalTime().ToString('o') -eq $entry.creationTime
    $sameExe = $identity.ExecutablePath -and ([System.IO.Path]::GetFullPath($identity.ExecutablePath) -eq [System.IO.Path]::GetFullPath($entry.executable))
    $sameCommand = $identity.CommandLine -eq $entry.commandLine
    if ($sameTime -and $sameExe -and $sameCommand -and $identity.CommandLine.Contains($entry.marker)) {
        Stop-OwnedTree $identity
        Write-Host "已停止 $($entry.name)。"
    } else {
        $unmatched = $true
        Write-Warning "PID $($entry.pid) 身份已变化，未停止该进程。"
    }
}
if ($unmatched) {
    throw '部分进程与启动记录不匹配，记录已保留以便核对。'
}
Remove-Item -LiteralPath $processFile
Write-Host '本次登记的服务已停止。'
