param([int]$ApiPort=8000, [int]$UiPort=5173, [switch]$NoBrowser)
& (Join-Path $PSScriptRoot 'start.ps1') -ApiPort $ApiPort -UiPort $UiPort -Dev -NoBrowser:$NoBrowser
