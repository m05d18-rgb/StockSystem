# 乾淨重啟:先關掉所有 server.py 與 tick 收集器,再用正式啟動器起一個全新的。
# 用途:套用 server.py 的程式改動(伺服器啟動時載入一次,不重啟不會生效),或
# 收拾雙開/殭屍行程。伺服器視窗是隱藏的、沒辦法 Ctrl+C,所以用這支一鍵重啟。
# **注意:盤中(平日 09:00-13:30)不要重啟**——會打斷盤中報價輪詢與停損守門,
# 請收盤後(13:30 之後)再跑。純重啟,不碰任何交易邏輯/資料。
$ErrorActionPreference = "SilentlyContinue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Repair-ProcessPathEnvironment {
  $keys = @([System.Environment]::GetEnvironmentVariables("Process").Keys |
    Where-Object { $_ -ieq "Path" })
  if ($keys.Count -le 1) { return }

  $pathValue = [System.Environment]::GetEnvironmentVariable("Path", "Process")
  if ([string]::IsNullOrWhiteSpace($pathValue)) {
    foreach ($key in $keys) {
      $candidate = [System.Environment]::GetEnvironmentVariable([string]$key, "Process")
      if (-not [string]::IsNullOrWhiteSpace($candidate)) {
        $pathValue = $candidate
        break
      }
    }
  }

  [System.Environment]::SetEnvironmentVariable("Path", $pathValue, "Process")
  foreach ($key in $keys) {
    if ([string]$key -cne "Path") {
      [System.Environment]::SetEnvironmentVariable([string]$key, $null, "Process")
    }
  }
}

Repair-ProcessPathEnvironment

Write-Host "[1/3] 關閉現有 server.py 與 tick 收集器 ..."
$portOwners = Get-NetTCPConnection -LocalPort 8008 -State Listen -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty OwningProcess -Unique
if (-not $portOwners) {
  $portOwners = netstat -ano |
    Select-String "LISTENING" |
    Select-String ":8008" |
    ForEach-Object {
      $parts = ($_ -split "\s+") | Where-Object { $_ }
      if ($parts.Count -ge 5) { [int]$parts[-1] }
    } |
    Select-Object -Unique
}
if ($portOwners) {
  $portOwners | ForEach-Object {
    Write-Host ("      關閉 8008 port owner PID {0}" -f $_)
    Stop-Process -Id $_ -Force
  }
  Start-Sleep -Seconds 2
}
$targets = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
  Where-Object { $_.CommandLine -match '\b(server|realtime_tick_collector)\.py\b' }
if ($targets) {
  $targets | ForEach-Object {
    Write-Host ("      關閉 PID {0}  {1}" -f $_.ProcessId, $_.CommandLine)
    Stop-Process -Id $_.ProcessId -Force
  }
  Start-Sleep -Seconds 2
} else {
  Write-Host "      (沒有在跑的 server.py,直接啟動新的)"
}

Write-Host "[2/3] 啟動全新 server ..."
& (Join-Path $Root "launch_stock_system.ps1")

Write-Host "[3/3] 完成。稍候幾秒讓伺服器就緒,再 Ctrl+Shift+F5 重新整理網頁。"
